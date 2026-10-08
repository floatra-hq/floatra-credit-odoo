# -*- coding: utf-8 -*-
"""Audit log for every inbound Floatra webhook.

The /floatra/webhook controller writes one row per validated event,
keyed by the X-Floatra-Event-ID header so that retries from the
gateway can short-circuit (idempotency at the inbound side). Ops
gets a chronological view of what Floatra has told us under
Floatra → Logs → Webhooks.

Rows are write-once; we never edit. Retention is left to Odoo's
standard model lifecycle (no auto-pruning here — distributors
have their own retention policies).
"""

from odoo import fields, models


class FloatraWebhookLog(models.Model):
    _name = "floatra.webhook.log"
    _description = "Floatra inbound webhook audit log"
    _order = "received_at desc"
    _rec_name = "event_type"

    event_id = fields.Char(
        string="Event ID",
        required=True,
        index=True,
        copy=False,
        help="X-Floatra-Event-ID header. Unique per webhook; the "
        "controller looks this up before processing to skip "
        "already-handled events on retry.",
    )
    _sql_constraints = [
        (
            "event_id_uniq",
            "UNIQUE(event_id)",
            "A Floatra webhook with this event_id was already received.",
        ),
    ]

    event_type = fields.Char(string="Event Type", required=True, index=True)
    received_at = fields.Datetime(
        string="Received At",
        required=True,
        default=fields.Datetime.now,
        readonly=True,
    )
    payload = fields.Text(
        string="Raw Payload",
        required=True,
        readonly=True,
        help="JSON body as received. Stored verbatim for ops debugging.",
    )
    signature = fields.Char(
        string="X-Floatra-Signature",
        readonly=True,
        index=True,
        help="HMAC the controller validated before processing. A delivery "
        "whose signature already processed successfully is not applied "
        "again, even under a different event id.",
    )
    processed_ok = fields.Boolean(
        string="Processed Successfully",
        default=False,
        help="True if the controller routed the event to a model "
        "handler without raising. False on routing errors (e.g. "
        "unknown event_type, or a target sale.order that doesn't "
        "exist in this Odoo instance).",
    )
    processing_error = fields.Text(
        string="Processing Error",
        readonly=True,
    )

    # Optional cross-references — set when the event resolved to
    # specific records in this Odoo. Both nullable.
    partner_id = fields.Many2one(
        "res.partner",
        string="Affected Customer",
        ondelete="set null",
    )
    sale_order_id = fields.Many2one(
        "sale.order",
        string="Affected Sale Order",
        ondelete="set null",
    )

    def _floatra_cron_poll_undelivered_webhooks(self):
        """Replay events Floatra could not deliver, then acknowledge them.

        Daily cron ``cron_undelivered_webhook_poll``: pages
        ``GET /v1/partner/webhooks/undelivered``, routes each payload through
        the live receiver's ``process_delivery`` (deduplicated on event id;
        failed rows re-run; savepoint per event), and
        ``POST /v1/partner/webhooks/:event_id/acknowledge`` once handled so
        Floatra stops listing it. A failed replay is not acknowledged; it is
        listed again next run. Returns the number of events replayed.
        """
        import logging

        from ..controllers.webhook import process_delivery
        from ..services.floatra_api import FloatraAPIClient, FloatraAPIError

        _logger = logging.getLogger(__name__)
        page_size = 50
        max_per_run = 200

        try:
            client = FloatraAPIClient.for_env(self.env)
        except FloatraAPIError as err:
            _logger.warning("Undelivered-webhook cron skipped (config error): %s", err)
            return 0

        replayed = 0
        scanned = 0
        offset = 0
        while scanned < max_per_run:
            try:
                page = client.get_undelivered_webhooks(limit=page_size, offset=offset)
            except (FloatraAPIError, ValueError) as err:
                _logger.warning("Undelivered-webhook cron stopped: %s", err)
                break
            events = page["events"]
            if not events:
                break
            # Acknowledged events leave Floatra's list, so the next page
            # starts after the ones still listed (the failures).
            still_listed = 0
            for event in events:
                scanned += 1
                outcome = process_delivery(
                    self.env, event["event_id"], event["payload"],
                )
                if outcome == "failed":
                    still_listed += 1
                    continue
                if outcome in ("ok", "ignored"):
                    replayed += 1
                self._floatra_ack(client, event["event_id"])
            if not page["has_more"]:
                break
            offset += still_listed

        _logger.info(
            "Undelivered-webhook cron: %d event(s) scanned, %d replayed",
            scanned,
            replayed,
        )
        return replayed

    @staticmethod
    def _floatra_ack(client, event_id):
        import logging

        from ..services.floatra_api import FloatraAPIError

        try:
            client.acknowledge_webhook(event_id)
        except FloatraAPIError as err:
            logging.getLogger(__name__).warning(
                "Floatra acknowledge failed for %s: %s", event_id, err,
            )
