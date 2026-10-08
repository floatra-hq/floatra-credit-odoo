# -*- coding: utf-8 -*-
"""PII redaction for Floatra API error bodies (O-1 / ERP spec §14).

Mirrors the ERPNext connector's ``services/pii_redaction.py``. The
outbound API client surfaces non-2xx response bodies to the log AND to
the desk UI (via ``FloatraAPIError.body``); the merchant / KYC / bank
endpoints can echo BVN, NIN, bank-account, phone, email and biometric
URLs in those error bodies, which the spec prohibits storing/logging in
plaintext on the ERP side.

We walk the body before it is logged or surfaced and replace the values
of known-sensitive keys with ``[REDACTED]``. Keys are preserved so ops
still sees WHICH field was rejected, just not its value. Pure Python — no
Odoo runtime dependency, so it is unit-testable in isolation.
"""

from typing import Any

# Lowercased key names whose values must never reach a log or the UI.
_REDACT_KEYS: frozenset = frozenset({
    # Identity numbers
    "bvn",
    "nin",
    "bvn_hash",
    "nin_hash",
    "bvnhash",
    "ninhash",
    # Bank account
    "account_number",
    "accountnumber",
    "account_no",
    "bank_account",
    "bank_account_number",
    # Contact PII
    "phone",
    "phone_number",
    "phonenumber",
    "msisdn",
    "email",
    "email_address",
    # Biometrics
    "selfie",
    "selfie_url",
    "selfie_image",
    "photo",
    "photo_url",
    "face_image",
    # Government ID images / document content
    "id_image",
    "id_document",
    "document_image",
    "document_url",
    # Hosted KYC link (a bearer link, OTP-gated) — never in a log.
    "kyc_url",
})

_REDACTED = "[REDACTED]"


def redact_pii(payload: Any) -> Any:
    """Return a copy of ``payload`` with the values of any PII-bearing
    keys replaced by ``[REDACTED]``. Walks dicts recursively and lists
    element-wise; non-container values (incl. plain-string error bodies)
    are returned unchanged. Keys are preserved.
    """
    if isinstance(payload, dict):
        return {
            key: _REDACTED if key.lower() in _REDACT_KEYS
            else redact_pii(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_pii(item) for item in payload]
    return payload
