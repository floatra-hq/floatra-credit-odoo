# -*- coding: utf-8 -*-
"""Inbound Floatra webhook receiver: ``POST /floatra/webhook``.

Public route with no Odoo auth: authenticity is the HMAC signature.

Per request:
  1. Verify (``floatra_contract.verify_webhook``): ``X-Floatra-Timestamp`` is
     Unix SECONDS; ``X-Floatra-Signature`` is hex HMAC-SHA256 over
     ``"<timestamp>.<raw body>"`` with the webhook secret; the timestamp must
     be within 5 minutes. Any failure → 401.
  2. ``process_delivery``: skip an event id already applied, or a signature
     already applied under another event id; re-run a failed one.
  3. Drop (200, logged) an event whose ``livemode`` is not this key's realm.
  4. Apply the flat camelCase payload (``{event, merchantId, loanId, ...}``,
     money as decimal-Naira strings) in a savepoint, matching records by
     Floatra ids first.
  5. Log every delivery in ``floatra.webhook.log``.

Status codes: 200 processed / replay; 400 invalid JSON; 401 rejected;
500 handler error (so Floatra's retry budget applies).
"""

import json
import logging

from odoo import http
from odoo.http import Response, request

from ..services.floatra_api import odoo_values
from ..services.floatra_contract import (
    WebhookRejected,
    credit_status_after_kyc,
    credit_status_after_lock,
    interpret_webhook,
    order_candidate_ok,
    partner_candidate_ok,
    verify_webhook,
    webhook_realm_matches,
)

_logger = logging.getLogger(__name__)

_LOG_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


def _json_response(body: dict, status: int) -> Response:
    return Response(json.dumps(body), status=status, content_type="application/json")


class FloatraWebhookController(http.Controller):
    @http.route(
        "/floatra/webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def receive(self, **_kwargs):
        body_bytes = request.httprequest.get_data() or b""
        headers = request.httprequest.headers
        event_id = headers.get("X-Floatra-Event-ID", "")
        signature = headers.get("X-Floatra-Signature", "")

        # O-2: read only the webhook secret, never the outbound api_key.
        secret = request.env["res.config.settings"].sudo().get_webhook_secret()
        try:
            verify_webhook(
                secret,
                headers.get("X-Floatra-Timestamp", ""),
                body_bytes,
                signature,
            )
        except WebhookRejected as rejected:
            _logger.warning(
                "Floatra webhook %s rejected: %s", event_id, rejected.reason,
            )
            return _json_response(
                {"status": "error", "reason": rejected.reason}, 401,
            )

        if not event_id:
            return _json_response(
                {"status": "error", "reason": "missing_event_id"}, 400,
            )
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except ValueError:
            _logger.error("Floatra webhook %s has invalid JSON", event_id)
            return _json_response(
                {"status": "error", "reason": "invalid_json"}, 400,
            )
        outcome = process_delivery(request.env, event_id, payload, signature)
        status = 500 if outcome == "failed" else 200
        return _json_response({"status": outcome}, status)


def process_delivery(env, event_id, payload, signature=""):
    """Log + apply one delivery; returns duplicate / ok / ignored / failed.

    Only a delivery that was applied successfully counts as done: a failed
    row is re-run when Floatra retries (or the replay cron lists it). A
    delivery whose signature was already applied is a replay under a new
    event id and is not applied again. The routing runs in a savepoint, so a
    failure leaves no partial writes.
    """
    logs = env["floatra.webhook.log"].sudo()
    existing = logs.search([("event_id", "=", event_id)], limit=1)
    if existing and existing.processed_ok:
        return "duplicate"
    if signature and logs.search(
        [("signature", "=", signature), ("processed_ok", "=", True)], limit=1,
    ):
        _logger.warning(
            "Floatra webhook %s replays an already-applied signature; ignored",
            event_id,
        )
        return "duplicate"
    log_row = existing or logs.create(
        {
            "event_id": event_id,
            "event_type": str(payload.get("event") or ""),
            "payload": json.dumps(payload),
            "signature": signature or "",
        }
    )
    if signature and existing:
        log_row.signature = signature
    try:
        with env.cr.savepoint():
            outcome = _route_event(payload, log_row, env=env)
    except Exception as err:  # noqa: BLE001 — answered 500 so Floatra retries
        _logger.exception("Floatra webhook %s processing failed", event_id)
        log_row.write({"processed_ok": False, "processing_error": str(err)})
        return "failed"
    log_row.write({
        "processed_ok": True,
        "processing_error": (
            "Ignored: event from the other realm (livemode mismatch)"
            if outcome == "ignored" else False
        ),
    })
    return outcome


def _route_event(payload: dict, log_row, env):
    """Apply one Floatra webhook payload to this Odoo.

    Used by the live receiver and by the undelivered-webhook cron, which
    passes its own ``env`` (``request`` is unbound in a cron).
    """
    live_key = env["res.config.settings"].sudo().get_floatra_key_is_live()
    if not webhook_realm_matches(payload, live_key):
        _logger.info(
            "Floatra webhook %s (%s) is from the other realm (livemode=%r, "
            "live key=%s); ignored",
            log_row.event_id, payload.get("event"), payload.get("livemode"),
            live_key,
        )
        return "ignored"
    effect = interpret_webhook(payload)
    if not effect.handled:
        _logger.info(
            "Floatra webhook %s: no handler for %r", log_row.event_id, effect.event,
        )
        return "ok"

    if effect.platform_suspended:
        params = env["ir.config_parameter"].sudo()
        params.set_param("floatra.platform_suspended", "true")
        params.set_param(
            "floatra.platform_suspended_at", payload.get("suspendedAt") or "",
        )

    partner = _find_partner(env, effect)
    order = _find_order(env, effect)
    if partner:
        _apply_partner(partner, effect)
        log_row.partner_id = partner.id
    if order:
        values = odoo_values(effect.order_updates, order._fields)
        if values:
            order.sudo().write(values)
        log_row.sale_order_id = order.id

    if effect.note:
        _logger.log(
            _LOG_LEVELS.get(effect.severity, logging.INFO),
            "Floatra %s: %s", effect.event, effect.note,
        )
        target = order or partner
        if target:
            target.sudo().message_post(body=effect.note)
    return "ok"


def _apply_partner(partner, effect):
    from odoo import fields

    values = odoo_values(effect.partner_updates, partner._fields)
    status_event = effect.lock is not None or effect.kyc_completed
    if effect.lock is not None:
        values["floatra_credit_status"] = credit_status_after_lock(
            partner.floatra_credit_status, effect.lock,
        )
    if effect.kyc_completed:
        values["floatra_credit_status"] = credit_status_after_kyc(
            partner.floatra_credit_status,
        )
    if status_event:
        values["floatra_last_status_check"] = fields.Datetime.now()
    if values:
        partner.sudo().write(values)


def _find_partner(env, effect):
    """By Floatra merchant id; our own id only if its Floatra id agrees."""
    partners = env["res.partner"].sudo()
    if effect.merchant_id:
        found = partners.search(
            [("floatra_merchant_id", "=", effect.merchant_id)], limit=1,
        )
        if found:
            return found
    external = str(effect.merchant_external_id or "")
    if external.isdigit():
        found = partners.browse(int(external)).exists()
        if found and partner_candidate_ok(found.floatra_merchant_id, effect):
            return found
    return None


def _find_order(env, effect):
    """By Floatra loan id, then loan-request id; by our order reference only
    when the order's stored Floatra ids agree with the payload's."""
    orders = env["sale.order"].sudo()
    if effect.loan_id:
        found = orders.search([("floatra_loan_id", "=", effect.loan_id)], limit=1)
        if found:
            return found
    if effect.loan_request_id:
        found = orders.search(
            [("floatra_loan_request_id", "=", effect.loan_request_id)], limit=1,
        )
        if found:
            return found
    if effect.external_order_id:
        found = orders.search([("name", "=", effect.external_order_id)], limit=1)
        if found and order_candidate_ok(
            found.floatra_loan_id,
            found.floatra_loan_request_id,
            found.partner_id.floatra_merchant_id,
            effect,
        ):
            return found
        if found:
            _logger.warning(
                "Floatra %s names order %s but its Floatra ids disagree; ignored",
                effect.event, effect.external_order_id,
            )
    return None
