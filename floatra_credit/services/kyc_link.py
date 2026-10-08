# -*- coding: utf-8 -*-
"""Floatra hosted KYC link: request body, response parsing, error copy.

``POST /v1/partner/merchants/{floatra_merchant_id}/kyc/retry`` behaves by
API-key realm:

* **Live key** (``live_pk_…``): send NO identity fields. Floatra returns
  ``{kyc_url, expires_at}``, a single-use link valid for 24 hours. The
  merchant opens it on their own phone, confirms a code sent to the phone
  number on their Floatra record, enters their NIN or BVN, accepts the
  consents and takes the selfie. The outcome arrives as the
  ``merchant.kyc_completed`` webhook. Requesting a new link revokes the
  previous one. A live call that carries ``kyc_type`` / ``identifier`` /
  ``selfie_base64`` is refused ``400 KYC_HOSTED_LINK_ONLY``.
* **Sandbox key** (``sbx_pk_…``, or a rotated ``pk_<hex>``): returns a
  simulated verification
  result. Floatra requires a submission body there, so the connector sends
  a fixed TEST body (a dummy identifier and a 1x1 placeholder image), never
  the partner's real BVN/NIN or a photo.

No Odoo/Frappe imports, so this file is unit-testable with plain Python.
The Odoo addon and the ERPNext app carry byte-identical copies of this file.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .floatra_contract import (  # noqa: F401  (re-exported)
    LIVE_KEY_PREFIX,
    error_code_of,
    is_live_key,
    unwrap_envelope,
)

# Sandbox-only submission. Floatra never sends it to an identity provider
# (the sandbox short-circuits to a simulated result); the identifier is a
# dummy and the image is a 1x1 PNG. The same body is checked by core's
# contract test (tests/fixtures/contract/request-kyc-retry-sandbox.json).
SANDBOX_TEST_IDENTIFIER = "00000000000"
SANDBOX_TEST_SELFIE_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYA"
    "AjCB0C8AAAAASUVORK5CYII="
)

HOSTED_LINK = "hosted_link"
SANDBOX_RESULT = "sandbox_result"

# Sandbox statuses that mean "verified".
SANDBOX_VERIFIED_STATUSES = frozenset({"SUCCESS", "VERIFIED"})


def is_sandbox_key(api_key: Optional[str]) -> bool:
    """Every non-``live_pk_`` key is a sandbox key (``sbx_pk_``, ``pk_<hex>``)."""
    return not is_live_key(api_key)


def kyc_retry_segments(floatra_merchant_id: str) -> tuple:
    """Path segments under ``/v1/partner`` (see ``floatra_contract.partner_path``)."""
    return ("merchants", floatra_merchant_id, "kyc", "retry")


def kyc_retry_body(sandbox: bool) -> dict:
    """The request body: empty for a live key, the fixed test body for sandbox."""
    if not sandbox:
        return {}
    return {
        "kyc_type": "NIN",
        "identifier": SANDBOX_TEST_IDENTIFIER,
        "selfie_base64": SANDBOX_TEST_SELFIE_PNG_BASE64,
        "merchant_consent_acknowledged": True,
    }


@dataclass(frozen=True)
class KycRetryOutcome:
    kind: str  # HOSTED_LINK | SANDBOX_RESULT
    kyc_url: Optional[str] = None
    expires_at: Optional[datetime] = None  # timezone-aware, UTC
    status: Optional[str] = None
    verification_tier: Optional[str] = None
    message: Optional[str] = None

    @property
    def sandbox_verified(self) -> bool:
        return (
            self.kind == SANDBOX_RESULT
            and (self.status or "").upper() in SANDBOX_VERIFIED_STATUSES
        )


def parse_iso_utc(value: str) -> datetime:
    """``2026-10-08T10:30:00.000Z`` → aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_kyc_retry_response(body: Any) -> KycRetryOutcome:
    """Read a 2xx kyc/retry body (enveloped or not) into an outcome.

    Raises ValueError when the body is neither shape, so a contract change
    surfaces as an error instead of a silently empty link.
    """
    data = unwrap_envelope(body)
    if not isinstance(data, dict):
        raise ValueError("Unexpected kyc/retry response from Floatra")
    if data.get("kyc_url"):
        expires_raw = data.get("expires_at")
        if not expires_raw:
            raise ValueError("Floatra returned a KYC link without expires_at")
        return KycRetryOutcome(
            kind=HOSTED_LINK,
            kyc_url=str(data["kyc_url"]),
            expires_at=parse_iso_utc(str(expires_raw)),
        )
    if data.get("status"):
        return KycRetryOutcome(
            kind=SANDBOX_RESULT,
            status=str(data["status"]),
            verification_tier=data.get("verification_tier"),
            message=data.get("message"),
        )
    raise ValueError("Unexpected kyc/retry response from Floatra")


_MESSAGES_BY_CODE = {
    "KYC_ALREADY_VERIFIED": (
        "This merchant has already verified both NIN and BVN with Floatra. "
        "There is nothing left to verify."
    ),
    "NOT_AVAILABLE": (
        "Floatra cannot issue a KYC link for this merchant right now. "
        "The merchant should contact Floatra."
    ),
    "KYC_UNAVAILABLE": (
        "Identity verification is temporarily unavailable at Floatra. "
        "Please try again later."
    ),
    "KYC_LINK_RATE_LIMITED": (
        "Too many KYC links were requested in the last hour. Reuse the link "
        "you already sent, or try again later."
    ),
    "KYC_HOSTED_LINK_ONLY": (
        "Floatra refused the request because it carried identity data. "
        "This connector version is out of date; upgrade it."
    ),
    "KYC_RETRY_CAP_REACHED": (
        "The sandbox KYC attempt limit for this merchant is reached. "
        "Contact Floatra."
    ),
    "MERCHANT_004": (
        "This merchant is in the other Floatra environment. Use the API key "
        "for the merchant's own environment (live vs sandbox)."
    ),
}

_MESSAGES_BY_STATUS = {
    0: "Could not reach Floatra. Check the connection and try again.",
    401: "Floatra rejected the API key. Check the Floatra settings.",
    403: (
        "This merchant does not belong to the platform configured in the "
        "Floatra settings."
    ),
    404: (
        "Floatra does not know this merchant. Check the Floatra Merchant ID "
        "on this record."
    ),
    409: "Floatra cannot issue a KYC link for this merchant right now.",
    422: "Floatra could not accept the request for this merchant.",
    429: (
        "Too many requests to Floatra. Wait a few minutes and try again; "
        "reuse the link you already sent if it has not expired."
    ),
    503: "Floatra is temporarily unavailable. Please try again later.",
}


def kyc_retry_error_message(status: int, body: Any) -> str:
    """User-facing copy for a failed kyc/retry call (never the raw body)."""
    code = error_code_of(body)
    if code in _MESSAGES_BY_CODE:
        return _MESSAGES_BY_CODE[code]
    if status in _MESSAGES_BY_STATUS:
        return _MESSAGES_BY_STATUS[status]
    return f"Floatra could not create a KYC link (HTTP {status}). Try again or contact Floatra."
