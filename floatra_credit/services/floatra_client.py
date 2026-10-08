# -*- coding: utf-8 -*-
"""HTTP client for Floatra's partner API (``/v1/partner/*``).

One method per partner call. Every request goes through ``_request``:

* the URL comes from ``floatra_contract.partner_url`` (host-only base URL);
* headers: ``Authorization: Bearer <key>``, ``X-Platform-ID``,
  ``X-Floatra-Timestamp`` (ISO-8601 UTC), ``Accept-Version: v1`` and an
  ``Idempotency-Key`` on every POST (a natural key when the caller has one,
  else a UUID);
* 30 s timeout; one retry after 5 s on HTTP 503;
* a 2xx body is unwrapped from ``{success, data, ...}`` and parsed by the
  matching ``floatra_contract.parse_*``; a non-2xx raises ``FloatraAPIError``
  with the error envelope, PII-redacted, so ``errorCode`` drives the copy.

No Odoo/Frappe import: the connector builds this with its own settings
(``FloatraAPIClient.for_env`` / ``for_site``). The Odoo addon and the ERPNext
app carry byte-identical copies of this file.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

from . import floatra_contract as contract
from .floatra_contract import FloatraAPIError
from .kyc_link import (
    KycRetryOutcome,
    is_sandbox_key,
    kyc_retry_body,
    kyc_retry_segments,
    parse_kyc_retry_response,
)
from .pii_redaction import redact_pii

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
RETRY_AFTER_503_SECONDS = 5


class FloatraClient:
    """Stateless; build one per call so settings changes apply at once."""

    def __init__(
        self,
        base_url: Optional[str],
        api_key: str,
        platform_id: str,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key or not platform_id:
            raise FloatraAPIError(
                0,
                None,
                "Floatra is not configured: set the API key and Platform ID "
                "in the Floatra settings.",
            )
        self.base_url = contract.normalize_base_url(base_url)
        self.api_key = api_key
        self.platform_id = platform_id
        self._sleep = sleep

    # ----- Merchants -----

    def onboard_merchant(self, body: dict) -> contract.Onboarded:
        """POST /v1/partner/merchants/onboard."""
        data = self._request(
            "POST", ("merchants", "onboard"), body,
            idempotency_key=f"onboard:{body['external_merchant_id']}",
        )
        return contract.parse_onboard(data)

    def get_credit_status(self, floatra_merchant_id: str) -> dict:
        """GET /v1/partner/merchants/:id/credit-status → customer updates."""
        data = self._request(
            "GET", ("merchants", floatra_merchant_id, "credit-status"),
        )
        return contract.parse_credit_status(data)

    def get_lock_status(self, floatra_merchant_id: str) -> bool:
        """GET /v1/partner/merchants/:id/lock-status → locked?"""
        data = self._request(
            "GET", ("merchants", floatra_merchant_id, "lock-status"),
        )
        return contract.parse_lock_status(data)

    def set_bank_account(self, floatra_merchant_id: str, body: dict) -> dict:
        """POST /v1/partner/merchants/:id/bank-account."""
        return self._request(
            "POST", ("merchants", floatra_merchant_id, "bank-account"), body,
            # Stable per (merchant, account) so a double-click replays, but
            # never the account number itself (headers end up in logs). An
            # HMAC under the API key: a plain hash of a 10-digit number is
            # trivially reversible.
            idempotency_key=(
                f"bank-account:{floatra_merchant_id}:"
                + hmac.new(
                    self.api_key.encode("utf-8"),
                    body["account_number"].encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()[:24]
            ),
        )

    def request_kyc_link(self, floatra_merchant_id: str) -> KycRetryOutcome:
        """POST /v1/partner/merchants/:id/kyc/retry (hosted KYC link)."""
        data = self._request(
            "POST", kyc_retry_segments(floatra_merchant_id),
            kyc_retry_body(is_sandbox_key(self.api_key)),
        )
        return parse_kyc_retry_response(data)

    # ----- Orders -----

    def initiate_order(self, body: dict) -> contract.Decision:
        """POST /v1/partner/orders/initiate (idempotent on the order reference)."""
        data = self._request(
            "POST", ("orders", "initiate"), body,
            idempotency_key=body["external_order_id"],
        )
        return contract.parse_decision(data)

    def lookup_order(self, external_order_id: str) -> dict:
        """GET /v1/partner/orders/by-external-id/:ref (Order + Loan ids)."""
        data = self._request(
            "GET", ("orders", "by-external-id", external_order_id),
        )
        return contract.parse_order_lookup(data)

    def confirm_delivery(
        self, floatra_loan_id: str, body: dict, idempotency_key: str,
    ) -> contract.Delivery:
        """POST /v1/partner/orders/:loan_id/confirm-delivery."""
        data = self._request(
            "POST", ("orders", floatra_loan_id, "confirm-delivery"), body,
            idempotency_key=idempotency_key,
        )
        return contract.parse_delivery(data)

    def cancel_order(
        self, floatra_loan_id: str, body: dict, idempotency_key: str,
    ) -> dict:
        """POST /v1/partner/orders/:loan_id/cancel."""
        return self._request(
            "POST", ("orders", floatra_loan_id, "cancel"), body,
            idempotency_key=idempotency_key,
        )

    def record_repayment(self, floatra_loan_id: str, body: dict) -> dict:
        """POST /v1/partner/orders/:loan_id/repayments (idempotent on reference)."""
        data = self._request(
            "POST", ("orders", floatra_loan_id, "repayments"), body,
            idempotency_key=f"repayment:{body['reference']}",
        )
        return contract.parse_repayment(data)

    def restructure_loan(
        self, floatra_loan_id: str, body: dict, idempotency_key: str,
    ) -> dict:
        """POST /v1/partner/orders/:loan_id/restructure."""
        data = self._request(
            "POST", ("orders", floatra_loan_id, "restructure"), body,
            idempotency_key=idempotency_key,
        )
        return contract.parse_restructure(data)

    def initiate_return(
        self, floatra_loan_id: str, body: dict, idempotency_key: str,
    ) -> dict:
        """POST /v1/partner/orders/:loan_id/return."""
        return self._request(
            "POST", ("orders", floatra_loan_id, "return"), body,
            idempotency_key=idempotency_key,
        )

    def list_orders(
        self,
        status: Optional[str] = None,
        external_merchant_id: Optional[str] = None,
        from_iso: Optional[str] = None,
        to_iso: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """GET /v1/partner/orders (cursor-paginated reconciliation listing)."""
        data = self._request(
            "GET", ("orders",),
            query={
                "status": status,
                "external_merchant_id": external_merchant_id,
                "from": from_iso,
                "to": to_iso,
                "cursor": cursor,
                "limit": limit,
            },
        )
        return contract.parse_orders_page(data)

    def bulk_initiate_orders(self, body: dict) -> dict:
        """POST /v1/partner/orders/bulk (decisions arrive as webhooks)."""
        return self._request(
            "POST", ("orders", "bulk"), body,
            idempotency_key=body["batch_idempotency_key"],
        )

    # ----- Platform + webhooks -----

    def get_platform_health(self) -> dict:
        """GET /v1/partner/platform/health."""
        return self._request("GET", ("platform", "health"))

    def post_key_rotation_complete(self) -> dict:
        """POST /v1/partner/platform/key-rotation-complete (with the NEW key)."""
        return self._request(
            "POST", ("platform", "key-rotation-complete"), {},
        )

    def get_undelivered_webhooks(
        self, limit: Optional[int] = None, offset: Optional[int] = None,
    ) -> dict:
        """GET /v1/partner/webhooks/undelivered → {events, has_more}."""
        data = self._request(
            "GET", ("webhooks", "undelivered"),
            query={"limit": limit, "offset": offset},
        )
        return contract.parse_undelivered(data)

    def acknowledge_webhook(self, event_id: str) -> dict:
        """POST /v1/partner/webhooks/:event_id/acknowledge (idempotent)."""
        return self._request(
            "POST", ("webhooks", event_id, "acknowledge"), {},
        )

    # ----- Internals -----

    def _request(
        self,
        method: str,
        segments: tuple,
        body: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        query: Optional[dict] = None,
    ) -> Any:
        url = contract.partner_url(self.base_url, *segments, query=query)
        path = contract.partner_path(*segments)
        data = json.dumps(body) if body is not None else None
        retries_remaining = 1
        while True:
            headers = self._headers(method, idempotency_key)
            try:
                response = requests.request(
                    method, url, data=data, headers=headers,
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                _logger.error("Floatra %s %s failed: %s", method, path, exc)
                raise FloatraAPIError(0, None, str(exc)) from exc
            if response.status_code == 503 and retries_remaining > 0:
                retries_remaining -= 1
                _logger.warning(
                    "Floatra %s %s returned 503; retrying in %ds",
                    method, path, RETRY_AFTER_503_SECONDS,
                )
                self._sleep(RETRY_AFTER_503_SECONDS)
                continue
            return self._handle_response(response, method, path)

    def _headers(self, method: str, idempotency_key: Optional[str]) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-Platform-ID": self.platform_id,
            "X-Floatra-Timestamp": datetime.now(timezone.utc).isoformat(),
            "Accept-Version": "v1",
            "Content-Type": "application/json",
        }
        if method != "GET":
            headers["Idempotency-Key"] = idempotency_key or uuid.uuid4().hex
        return headers

    def _handle_response(self, response, method: str, path: str) -> Any:
        try:
            body = response.json()
        except ValueError:
            body = response.text
        if 200 <= response.status_code < 300:
            _logger.info("Floatra %s %s -> %d", method, path, response.status_code)
            return contract.unwrap_envelope(body)
        # Error bodies from merchant / KYC / bank calls can echo PII: redact
        # before the log AND before the body is carried into the error.
        safe_body = redact_pii(body)
        _logger.warning(
            "Floatra %s %s -> %d: %r", method, path, response.status_code, safe_body,
        )
        raise FloatraAPIError(response.status_code, safe_body)
