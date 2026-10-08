# -*- coding: utf-8 -*-
"""The Floatra partner API contract, in one place.

Everything the connector knows about Floatra's wire format lives here:

* the URL rule (a host-only base URL; every path is ``/v1/partner/...``);
* the response envelope (``{success, data, error, errorCode, timestamp}``)
  and error copy keyed on ``errorCode``;
* money (decimal-Naira strings, built with ``Decimal``, never float);
* every request body, shaped exactly as core's DTOs declare them (the global
  validation pipe rejects unknown keys and JS numbers for money);
* parsing of every response the connector reads;
* webhook verification (``X-Floatra-Timestamp`` in Unix seconds, HMAC-SHA256
  over ``"<ts>.<raw body>"``, a replay window) and interpretation of the flat
  camelCase payloads core sends.

Pure Python: no Odoo, Frappe or ``requests`` import, so the whole contract is
tested with plain ``python3 -m unittest`` against fixtures that Floatra core
renders from its real controllers and webhook builders
(``tests/fixtures/contract``; core's
``src/modules/partner-webhooks/partner-contract-fixtures.spec.ts``).

The Odoo addon (``connectors/odoo/floatra_credit/services/``) and the ERPNext
app (``connectors/erpnext/floatra_credit/floatra_credit/services/``) carry
byte-identical copies of this file; core's contract spec fails if they drift.
"""

import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode, urlsplit

# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.floatra.com"
API_PREFIX = "/v1/partner"
# Connector versions before 17.0.0.3.0 / 0.3.0 defaulted to
# ``https://api.floatra.io/v1`` (and a ``sandbox.`` variant). Those hosts never
# resolved; Floatra has one host and picks the realm from the API key.
_RETIRED_HOST = "floatra.io"


def normalize_base_url(raw: Optional[str]) -> str:
    """Host-only base URL: no trailing ``/`` and no trailing ``/v1``.

    Older settings stored ``https://api.floatra.io/v1``; that value (and any
    other ``*.floatra.io`` host) maps to the real host, so an upgraded install
    keeps working without anyone editing the setting.
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        return DEFAULT_BASE_URL
    if text.lower().endswith("/v1"):
        text = text[: -len("/v1")].rstrip("/")
    host = (urlsplit(text).hostname or "").lower()
    if host == _RETIRED_HOST or host.endswith("." + _RETIRED_HOST):
        return DEFAULT_BASE_URL
    return text


def partner_path(*segments: Any) -> str:
    """``("orders", "loan_1", "cancel")`` → ``/v1/partner/orders/loan_1/cancel``.

    Every segment is percent-encoded, so an id can never add a path segment
    or a query string; an empty or dot segment is refused.
    """
    parts = [str(s) for s in segments]
    if any(p in ("", ".", "..") for p in parts):
        raise ContractError(f"Invalid id in Floatra path: {parts!r}")
    return API_PREFIX + "".join("/" + quote(p, safe="") for p in parts)


def partner_url(
    base_url: Optional[str], *segments: Any, query: Optional[dict] = None,
) -> str:
    """The one place a Floatra URL is built."""
    url = normalize_base_url(base_url) + partner_path(*segments)
    params = {k: v for k, v in (query or {}).items() if v not in (None, "")}
    if params:
        url += "?" + urlencode(params)
    return url


# --------------------------------------------------------------------------
# Realm (live vs sandbox)
# --------------------------------------------------------------------------

# Core mints live platform keys ONLY as ``live_pk_<hex>``; every other key a
# platform holds (``sbx_pk_<hex>``, a rotated ``pk_<hex>``) authenticates in
# the sandbox realm. Core resolves the realm by which stored hash matched, so
# "live iff ``live_pk_``" mirrors it without a round trip.
LIVE_KEY_PREFIX = "live_pk_"


def is_live_key(api_key: Optional[str]) -> bool:
    """Live iff the key is a ``live_pk_`` key."""
    return bool(api_key) and str(api_key).startswith(LIVE_KEY_PREFIX)


def webhook_realm_matches(payload: Any, live_key: bool) -> bool:
    """Whether a webhook belongs to the realm of this connector's API key.

    A platform's sandbox and live keys share webhook destinations and the
    signing secret, so a valid signature does not say which realm an event is
    about; core's top-level ``livemode`` does. A missing ``livemode`` (only
    deliveries queued before core added it) is refused for a LIVE key, since
    it could be a sandbox event about records named like real ones, and
    accepted for a sandbox key, where the worst case is a sandbox record
    updated from an old event and refusing would drop pending sandbox replays.
    """
    livemode = payload.get("livemode") if isinstance(payload, dict) else None
    if livemode is None:
        return not live_key
    return livemode is live_key


# --------------------------------------------------------------------------
# Envelope + errors
# --------------------------------------------------------------------------


def unwrap_envelope(body: Any) -> Any:
    """Core wraps every response as ``{success, data, error, errorCode, timestamp}``."""
    if isinstance(body, dict) and "success" in body and "data" in body:
        return body["data"]
    return body


def error_code_of(body: Any) -> Optional[str]:
    """The machine code of a Floatra error envelope (``errorCode``)."""
    if isinstance(body, dict):
        code = body.get("errorCode")
        if isinstance(code, str) and code:
            return code
    return None


def error_text_of(body: Any) -> Optional[str]:
    """The human text core put in the error envelope (``error``)."""
    if isinstance(body, dict):
        text = body.get("error")
        if isinstance(text, str) and text:
            return text
    return None


class FloatraAPIError(Exception):
    """A failed Floatra call: HTTP status (0 = never reached Floatra) + body."""

    def __init__(self, status: int, body: Any, message: str = ""):
        super().__init__(message or f"Floatra API returned {status}: {body!r}")
        self.status = status
        self.body = body

    @property
    def code(self) -> Optional[str]:
        return error_code_of(self.body)

    def user_message(self) -> str:
        return describe_error(self.status, self.body, fallback=str(self))


class ContractError(ValueError):
    """Input the connector cannot turn into a valid Floatra request."""


_MESSAGES_BY_CODE = {
    "MERCHANT_NOT_FOUND": (
        "Floatra does not know this customer. Onboard the customer with "
        "Floatra first."
    ),
    "MERCHANT_LINK_INACTIVE": (
        "This customer's Floatra link was deactivated. Contact Floatra."
    ),
    "ORDER_NOT_FOUND": "Floatra has no order with this reference.",
    "GENERAL_002": "Floatra has no loan for this order on your platform.",
    "IS_TEST_NOT_SUPPORTED": (
        "Floatra does not take test orders on a live key. Use your sandbox "
        "API key to test."
    ),
    "ROUTING_FAILED": (
        "Floatra could not route this order to a lender. Try again in 15 "
        "minutes."
    ),
    "CANCELLATION_NOT_ALLOWED": (
        "Floatra cannot cancel this loan any more (it was already delivered "
        "or repaid)."
    ),
    "REPAYMENT_ALREADY_RECORDED": (
        "Floatra already has a repayment with this reference."
    ),
    "REPAYMENT_REFERENCE_IN_USE": (
        "This payment reference was already used for another payment, so "
        "nothing was recorded. Use a new reference."
    ),
    "RETURN_015": (
        "Floatra only takes a return against a loan with an open delivery "
        "dispute. Raise the dispute with Floatra first."
    ),
    "MERCHANT_004": (
        "This record is in the other Floatra environment. Use the API key "
        "for its own environment (live vs sandbox)."
    ),
    "LOAN_029": (
        "This loan is in the other Floatra environment. Use the API key for "
        "its own environment (live vs sandbox)."
    ),
    "AGENT_NOT_AUTHORIZED": (
        "Floatra does not recognise this agent ID on your platform. Leave "
        "Agent ID empty, or enter the agent's Floatra agent ID."
    ),
    "PLATFORM_SUSPENDED": (
        "Floatra has suspended your platform. Contact Floatra before "
        "retrying."
    ),
    "PLATFORM_INACTIVE": (
        "Floatra has deactivated your platform. Contact Floatra; retrying "
        "will not help."
    ),
    "INVALID_API_KEY": "Floatra rejected the API key. Check the Floatra settings.",
    "PLATFORM_ID_MISMATCH": (
        "The Platform ID in the Floatra settings does not match the API key."
    ),
    "MISSING_PLATFORM_ID": "Set the Platform ID in the Floatra settings.",
    "INVALID_TIMESTAMP": (
        "Floatra rejected the request time. Check this server's clock."
    ),
    "SERVICE_TEMPORARILY_UNAVAILABLE": (
        "Floatra cannot confirm the lock status right now. Treat the "
        "customer as locked and try again shortly."
    ),
}

_MESSAGES_BY_STATUS = {
    0: "Could not reach Floatra. Check the connection and try again.",
    401: "Floatra rejected the API key. Check the Floatra settings.",
    403: "This record does not belong to the platform in the Floatra settings.",
    404: "Floatra does not know this record.",
    429: "Too many requests to Floatra. Wait a minute and try again.",
    503: "Floatra is temporarily unavailable. Try again later.",
}


def describe_error(status: int, body: Any, fallback: str = "") -> str:
    """User-facing copy for a failed call, keyed on ``errorCode`` first."""
    code = error_code_of(body)
    if code in _MESSAGES_BY_CODE:
        return _MESSAGES_BY_CODE[code]
    text = error_text_of(body)
    if status in (400, 409, 422) and text:
        # Validation and business refusals: core's text names the problem.
        return f"Floatra refused the request: {text}"
    if status in _MESSAGES_BY_STATUS:
        return _MESSAGES_BY_STATUS[status]
    if text:
        return f"Floatra error (HTTP {status}): {text}"
    return fallback or f"Floatra error (HTTP {status})."


# --------------------------------------------------------------------------
# Money, dates, phones
# --------------------------------------------------------------------------

_KOBO = Decimal("0.01")


def to_decimal(value: Any) -> Decimal:
    """Exact Decimal from a Decimal, int, str or float (via ``str``)."""
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value).strip())
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ContractError(f"Not an amount: {value!r}") from exc
    if not result.is_finite():
        raise ContractError(f"Not a finite amount: {value!r}")
    return result


def naira(value: Any, *, minimum: str = "0.01") -> str:
    """Decimal-Naira wire string: ``Decimal("450000")`` → ``"450000.00"``.

    Rounds half-up to the kobo and refuses anything below ``minimum``.
    """
    amount = to_decimal(value).quantize(_KOBO, rounding=ROUND_HALF_UP)
    if amount < Decimal(minimum):
        raise ContractError(f"Amount must be at least {minimum} (got {amount}).")
    return format(amount, "f")


def naira_from_kobo(kobo: Any) -> str:
    """Integer kobo (older form inputs) → decimal-Naira string."""
    try:
        whole = int(kobo)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"Not a kobo amount: {kobo!r}") from exc
    return naira(Decimal(whole) / 100)


def parse_naira(value: Any) -> Optional[Decimal]:
    """A wire decimal string → Decimal; None when absent."""
    if value is None or value == "":
        return None
    return to_decimal(value)


def iso_utc(value: Any) -> str:
    """A datetime (naive = UTC) or ISO string → ISO-8601 with ``+00:00``."""
    if isinstance(value, str):
        text = value.strip()
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                value = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError as exc:
                raise ContractError(f"Not a date-time: {text!r}") from exc
    if not isinstance(value, datetime):
        raise ContractError(f"Not a date-time: {value!r}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso_utc_or_none(value: Any) -> Optional[datetime]:
    """``2026-10-01T12:00:00.000Z`` → aware UTC datetime; None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def date_part(value: Any) -> Optional[str]:
    """``2026-10-15T00:00:00.000Z`` → ``2026-10-15``."""
    if not value or not isinstance(value, str):
        return None
    return value[:10]


# What class-validator's ``isEmail`` (validator.js defaults) accepts, for
# plain addresses: dot-separated atoms in the local part (no leading,
# trailing or doubled dot, at most 64 chars); hostname labels of letters,
# digits and inner hyphens; an alphabetic TLD of 2+ letters; no IP literal.
# Core's ``tests/fixtures/contract/email-cases.json`` holds isEmail's own
# verdicts and the contract tests check this agrees.
_EMAIL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_EMAIL_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL = re.compile(
    rf"^(?=[^@]{{1,64}}@){_EMAIL_ATOM}(?:\.{_EMAIL_ATOM})*"
    rf"@(?:{_EMAIL_LABEL}\.)+[A-Za-z]{{2,63}}$"
)


def single_email(raw: Any) -> Optional[str]:
    """One plain address that core's ``IsEmail`` accepts, else None: a list
    like ``a@x.ng, b@x.ng``, a display name or a malformed address is left
    out rather than failing the whole onboarding."""
    text = _text(raw, 200)
    return text if text and _EMAIL.match(text) else None


def normalize_ng_phone(raw: Any) -> Optional[str]:
    """Any Nigerian mobile shape → ``+234XXXXXXXXXX`` (core's onboard format)."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 13 and digits.startswith("234"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+234" + digits[1:]
    if len(digits) == 10 and digits[0] in "789":
        return "+234" + digits
    return None


# --------------------------------------------------------------------------
# Request bodies (exactly the keys core's DTOs declare)
# --------------------------------------------------------------------------

ORDER_CATEGORIES = (
    "FMCG", "ELECTRONICS", "FASHION", "AGRICULTURE", "PHARMACY", "OTHER",
)
TENURES = (14, 30)
BUSINESS_TYPES = ("RETAILER", "WHOLESALER", "KIOSK")
CONFIRMATION_METHODS = ("AGENT_APP", "ERP_CONSOLE", "GPS_VERIFIED")
UNDELIVERED_HANDLING = ("CANCEL_BALANCE", "DEFER_DELIVERY")
MAX_DEFER_DELIVERY_DAYS = 7
CANCEL_REASONS = (
    "MERCHANT_DECLINED_GOODS", "STOCK_UNAVAILABLE", "PRICE_DISPUTE",
    "DUPLICATE_ORDER", "AGENT_ERROR", "OTHER",
)
REPAYMENT_SOURCES = (
    "SETTLEMENT_DEDUCTION", "MANUAL_CASH", "BANK_TRANSFER", "OTHER",
)
RETURN_REASONS = (
    "DAMAGED", "WRONG_ITEM", "MERCHANT_REQUESTED", "EXPIRED", "OTHER",
)
MIN_RESTRUCTURE_TENURE_DAYS = 7
MAX_RESTRUCTURE_TENURE_DAYS = 30
MAX_BULK_ORDERS = 50


def _text(value: Any, limit: int) -> Optional[str]:
    """Trimmed text capped at ``limit`` (the DTO's MaxLength); None if empty."""
    if value is None or value is False:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _required(value: Any, limit: int, name: str) -> str:
    """Trimmed required text; refused (never truncated) above ``limit``."""
    text = _text(value, 10 * limit)
    if not text:
        raise ContractError(f"{name} is required.")
    if len(text) > limit:
        raise ContractError(f"{name} must be at most {limit} characters.")
    return text


def _put(body: dict, key: str, value: Any) -> None:
    if value is not None:
        body[key] = value


def coerce_tenure_days(raw: Any) -> int:
    """The per-order tenure selection → 14 or 30 (unset → 14)."""
    if raw in (None, "", 0, False):
        return 14
    try:
        days = int(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"Invalid tenure: {raw!r}") from exc
    if days not in TENURES:
        raise ContractError(f"Tenure must be 14 or 30 days (got {days}).")
    return days


def onboard_body(
    *,
    external_merchant_id: Any,
    name: Any,
    phone: Any,
    business_type: Any = None,
    category: Any = None,
    assigned_agent_id: Any = None,
    owner_first_name: Any = None,
    owner_last_name: Any = None,
    owner_email: Any = None,
    street: Any = None,
    lga: Any = None,
    state: Any = None,
) -> dict:
    """``POST /v1/partner/merchants/onboard`` (``OnboardMerchantDto``).

    No BVN/NIN: core does not use identity numbers sent at onboarding, and
    KYC happens on Floatra's hosted link where the merchant enters them.
    """
    normalized = normalize_ng_phone(phone)
    if not normalized:
        raise ContractError(
            "Phone must be a Nigerian mobile number (e.g. 0803 123 4567).",
        )
    body = {
        "external_merchant_id": _required(external_merchant_id, 64, "Customer id"),
        "name": _required(name, 200, "Name"),
        "phone": normalized,
    }
    if business_type in BUSINESS_TYPES:
        body["business_type"] = business_type
    if category in ORDER_CATEGORIES:
        body["category"] = category
    _put(body, "assigned_agent_id", _text(assigned_agent_id, 64))
    owner: dict = {}
    _put(owner, "first_name", _text(owner_first_name, 120))
    _put(owner, "last_name", _text(owner_last_name, 120))
    _put(owner, "email", single_email(owner_email))
    if owner:
        body["owner"] = owner
    address: dict = {}
    _put(address, "street", _text(street, 200))
    _put(address, "lga", _text(lga, 100))
    _put(address, "state", _text(state, 100))
    if address:
        body["address"] = address
    return body


def initiate_body(
    *,
    external_merchant_id: Any,
    external_order_id: Any,
    amount: Any,
    category: Any,
    tenure_days: Any,
    erp_type: str,
) -> dict:
    """``POST /v1/partner/orders/initiate`` (``InitiateOrderDto``)."""
    return {
        "external_merchant_id": _required(external_merchant_id, 64, "Customer id"),
        "external_order_id": _required(external_order_id, 64, "Order reference"),
        "amount": naira(amount),
        "category": category if category in ORDER_CATEGORIES else "OTHER",
        "tenure_days": coerce_tenure_days(tenure_days),
        "metadata": {"erp_type": erp_type, "source": "erp_console"},
    }


def confirm_delivery_body(
    *,
    delivered_at: Any,
    notes: Any = None,
    agent_id: Any = None,
    agent_name: Any = None,
    confirmation_method: Any = None,
    lat: Any = None,
    lng: Any = None,
    accuracy_meters: Any = None,
    location_recorded_at: Any = None,
    proof_of_delivery_url: Any = None,
    installment: Optional[int] = None,
    delivered_amount: Any = None,
    undelivered_handling: Any = None,
    defer_delivery_days: Any = None,
) -> dict:
    """``POST /v1/partner/orders/:loan_id/confirm-delivery`` (``ConfirmDeliveryDto``).

    ``installment`` set → a partial delivery (1-based installment number,
    ``delivered_amount`` required); otherwise the whole order was delivered.
    """
    body: dict = {"delivered_at": iso_utc(delivered_at)}
    _put(body, "notes", _text(notes, 500))
    confirmed_by: dict = {}
    _put(confirmed_by, "agent_id", _text(agent_id, 64))
    _put(confirmed_by, "agent_name", _text(agent_name, 120))
    if confirmation_method in CONFIRMATION_METHODS:
        confirmed_by["confirmation_method"] = confirmation_method
    if confirmed_by:
        body["confirmed_by"] = confirmed_by
    location: dict = {}
    if lat not in (None, False, ""):
        location["lat"] = float(lat)
    if lng not in (None, False, ""):
        location["lng"] = float(lng)
    if accuracy_meters not in (None, False, ""):
        location["accuracy_meters"] = int(accuracy_meters)
    if location_recorded_at:
        location["recorded_at"] = iso_utc(location_recorded_at)
    if location:
        body["location"] = location
    _put(body, "proof_of_delivery_url", _text(proof_of_delivery_url, 1024))
    if installment is not None:
        body["delivery_installment"] = int(installment)
        body["delivered_amount"] = naira(delivered_amount)
        if undelivered_handling:
            if undelivered_handling not in UNDELIVERED_HANDLING:
                raise ContractError(
                    f"Unknown undelivered handling: {undelivered_handling!r}",
                )
            body["undelivered_handling"] = undelivered_handling
            if undelivered_handling == "DEFER_DELIVERY":
                days = int(defer_delivery_days or 0)
                if not 1 <= days <= MAX_DEFER_DELIVERY_DAYS:
                    raise ContractError("Defer days must be between 1 and 7.")
                body["defer_delivery_days"] = days
    return body


def cancel_body(
    *, reason_code: Any = "OTHER", reason_notes: Any = None, agent_id: Any = None,
) -> dict:
    """``POST /v1/partner/orders/:loan_id/cancel`` (``CancelOrderDto``).

    The connector acts for the distributor, so ``cancelled_by`` is always
    ``DISTRIBUTOR``.
    """
    if reason_code not in CANCEL_REASONS:
        raise ContractError(f"Unknown cancel reason: {reason_code!r}")
    body = {"cancelled_by": "DISTRIBUTOR", "reason_code": reason_code}
    _put(body, "reason_notes", _text(reason_notes, 500))
    _put(body, "cancelled_by_agent_id", _text(agent_id, 64))
    return body


def repayment_body(
    *,
    amount: Any,
    reference: Any,
    source: Any,
    collected_at: Any,
    notes: Any = None,
    agent_id: Any = None,
) -> dict:
    """``POST /v1/partner/orders/:loan_id/repayments`` (``RecordRepaymentDto``)."""
    if source not in REPAYMENT_SOURCES:
        raise ContractError(
            "Source must be one of: " + ", ".join(REPAYMENT_SOURCES),
        )
    body = {
        "amount": naira(amount),
        "reference": _required(reference, 120, "Reference"),
        "source": source,
        "collected_at": iso_utc(collected_at),
    }
    _put(body, "notes", _text(notes, 500))
    _put(body, "agent_id", _text(agent_id, 64))
    return body


def restructure_body(
    *,
    new_tenure_days: Any,
    reason: Any,
    lender_approval_reference: Any,
    approved_by_label: Any = None,
) -> dict:
    """``POST /v1/partner/orders/:loan_id/restructure`` (``RestructureOrderDto``)."""
    try:
        days = int(new_tenure_days)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"Invalid tenure: {new_tenure_days!r}") from exc
    if not MIN_RESTRUCTURE_TENURE_DAYS <= days <= MAX_RESTRUCTURE_TENURE_DAYS:
        raise ContractError("New tenure must be between 7 and 30 days.")
    reason_text = _required(reason, 500, "Reason")
    body = {
        "new_tenure_days": days,
        "reason": reason_text,
        "lender_approval_reference": _required(
            lender_approval_reference, 200, "Lender approval reference",
        ),
    }
    _put(body, "approved_by_label", _text(approved_by_label, 120))
    return body


def return_body(
    *, return_amount: Any, reason_code: Any, reason_notes: Any = None,
    agent_id: Any = None,
) -> dict:
    """``POST /v1/partner/orders/:loan_id/return`` (``ReturnGoodsDto``)."""
    if reason_code not in RETURN_REASONS:
        raise ContractError(
            "Reason must be one of: " + ", ".join(RETURN_REASONS),
        )
    body = {"return_amount": naira(return_amount), "reason_code": reason_code}
    _put(body, "reason_notes", _text(reason_notes, 500))
    _put(body, "initiated_by_agent_id", _text(agent_id, 64))
    return body


def bank_account_body(
    *,
    bank_code: Any,
    account_number: Any,
    account_name: Any,
    bank_name: Any = None,
    is_primary: bool = True,
) -> dict:
    """``POST /v1/partner/merchants/:id/bank-account`` (``UpdateBankAccountDto``)."""
    code = (str(bank_code or "")).strip()
    number = (str(account_number or "")).strip()
    if not (code.isdigit() and 3 <= len(code) <= 10):
        raise ContractError(f"Bank code must be 3-10 digits (got {code}).")
    if not (number.isdigit() and len(number) == 10):
        raise ContractError("Account number must be exactly 10 digits.")
    body = {
        "bank_code": code,
        "account_number": number,
        "account_name": _required(account_name, 120, "Account name"),
        "is_primary": bool(is_primary),
    }
    _put(body, "bank_name", _text(bank_name, 120))
    return body


def bulk_body(orders: List[dict], batch_idempotency_key: Any) -> dict:
    """``POST /v1/partner/orders/bulk`` (``BulkOrdersDto``): 1-50 initiate bodies."""
    if not orders:
        raise ContractError("A bulk submission needs at least one order.")
    if len(orders) > MAX_BULK_ORDERS:
        raise ContractError(
            f"A bulk submission takes at most {MAX_BULK_ORDERS} orders "
            f"({len(orders)} given).",
        )
    return {
        "orders": list(orders),
        "batch_idempotency_key": _required(batch_idempotency_key, 120, "Batch key"),
    }


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
# Field names below are the connector's own record fields (identical on the
# Odoo and ERPNext sides). Money values are Decimal; None clears a field.

VERIFIED_KYC_TIERS = frozenset({"NIN_ONLY", "BVN_VERIFIED", "FULLY_VERIFIED"})


def _data(body: Any) -> dict:
    data = unwrap_envelope(body)
    if not isinstance(data, dict):
        raise ValueError("Unexpected response from Floatra")
    return data


@dataclass(frozen=True)
class Decision:
    """An initiate decision (sync response or ``bulk.order_decision`` envelope)."""

    decision: str
    floatra_order_id: Optional[str] = None
    floatra_loan_id: Optional[str] = None
    external_order_id: Optional[str] = None
    approved_amount: Optional[Decimal] = None
    interest_amount: Optional[Decimal] = None
    repayment_amount: Optional[Decimal] = None
    due_date: Optional[str] = None
    reason_code: Optional[str] = None
    reason_text: Optional[str] = None
    retry_eligible_after: Optional[str] = None
    waitlist_position: Optional[int] = None
    waitlist_message: Optional[str] = None
    loan_request_id: Optional[str] = None

    def order_updates(self) -> dict:
        if self.decision == "APPROVED":
            return {
                "floatra_order_id": self.floatra_order_id,
                "floatra_loan_id": self.floatra_loan_id,
                "floatra_decision": "approved",
                "floatra_decision_reason": None,
                "floatra_approved_amount": self.approved_amount,
                "floatra_interest_amount": self.interest_amount,
                "floatra_repayment_amount": self.repayment_amount,
                "floatra_repayment_due_date": self.due_date,
            }
        if self.decision == "DECLINED":
            return {
                "floatra_order_id": self.floatra_order_id,
                "floatra_decision": "declined",
                "floatra_decision_reason": self.reason(),
            }
        if self.decision == "WAITLISTED":
            return {
                "floatra_order_id": self.floatra_order_id,
                # The later order.credit_approved / _declined webhook names
                # this loan request: it is how that event finds the order.
                "floatra_loan_request_id": self.loan_request_id,
                "floatra_decision": "waitlisted",
                "floatra_decision_reason": self.reason(),
            }
        # A bulk row that failed before a decision: nothing was reserved,
        # so the order can be submitted again.
        return {
            "floatra_decision": "not_requested",
            "floatra_decision_reason": self.reason(),
        }

    def reason(self) -> str:
        if self.decision == "WAITLISTED":
            position = self.waitlist_position if self.waitlist_position else "?"
            text = f"Waitlist position #{position}"
            return f"{text}: {self.waitlist_message}" if self.waitlist_message else text
        text = self.reason_text or self.reason_code or "No reason given"
        if self.reason_code and self.reason_text:
            text = f"{self.reason_text} ({self.reason_code})"
        if self.retry_eligible_after:
            text += f" Retry after {self.retry_eligible_after}."
        return text


def parse_decision(body: Any) -> Decision:
    """``POST /v1/partner/orders/initiate`` 2xx body (enveloped or not)."""
    data = _data(body)
    decision = str(data.get("decision") or "").upper()
    if decision == "APPROVED":
        credit = data.get("credit") or {}
        return Decision(
            decision=decision,
            floatra_order_id=data.get("floatra_order_id"),
            floatra_loan_id=data.get("floatra_loan_id"),
            external_order_id=data.get("external_order_id"),
            approved_amount=parse_naira(credit.get("approved_amount")),
            interest_amount=parse_naira(credit.get("interest")),
            repayment_amount=parse_naira(credit.get("repayment_amount")),
            due_date=date_part(credit.get("due_date")),
        )
    if decision == "DECLINED":
        decline = data.get("decline") or {}
        return Decision(
            decision=decision,
            floatra_order_id=data.get("floatra_order_id"),
            external_order_id=data.get("external_order_id"),
            reason_code=decline.get("reason_code"),
            reason_text=decline.get("reason_text"),
            retry_eligible_after=decline.get("retry_eligible_after"),
        )
    if decision == "WAITLISTED":
        waitlist = data.get("waitlist") or {}
        return Decision(
            decision=decision,
            floatra_order_id=data.get("floatra_order_id"),
            external_order_id=data.get("external_order_id"),
            waitlist_position=waitlist.get("position"),
            waitlist_message=waitlist.get("message"),
            loan_request_id=waitlist.get("loan_request_id"),
        )
    if decision == "ERROR":
        error = data.get("error") or {}
        return Decision(
            decision=decision,
            external_order_id=data.get("external_order_id"),
            reason_code=error.get("reason_code"),
            reason_text=error.get("reason_text"),
        )
    raise ValueError(f"Unexpected Floatra decision: {decision or '(none)'}")


@dataclass(frozen=True)
class Onboarded:
    floatra_merchant_id: str
    kyc_status: Optional[str]
    created_at: Optional[str]


def parse_onboard(body: Any) -> Onboarded:
    """``POST /v1/partner/merchants/onboard`` 2xx body."""
    data = _data(body)
    merchant_id = data.get("floatra_merchant_id")
    if not merchant_id:
        raise ValueError("Floatra onboarded the customer but returned no merchant id")
    return Onboarded(
        floatra_merchant_id=str(merchant_id),
        kyc_status=data.get("kyc_status"),
        created_at=data.get("created_at"),
    )


def kyc_status_for(verification_tier: Optional[str]) -> str:
    """Core's verification tier → the connector's KYC selection."""
    if verification_tier in VERIFIED_KYC_TIERS:
        return "approved"
    if verification_tier == "PENDING":
        return "pending"
    return "not_started"


def parse_credit_status(body: Any) -> dict:
    """``GET /v1/partner/merchants/:id/credit-status`` → customer updates.

    ``status`` is ``ACTIVE`` or ``NOT_AVAILABLE`` (a suspended merchant,
    founder ruling 2026-10-04); a lock wins over KYC; an unverified merchant
    is ``pending_kyc`` (its limit is ₦0 until KYC completes).
    """
    data = _data(body)
    lock = data.get("lock_status") or {}
    kyc_tier = data.get("kyc_status")
    if data.get("status") != "ACTIVE":
        credit_status = "suspended"
    elif lock.get("locked"):
        credit_status = "locked"
    elif kyc_tier in VERIFIED_KYC_TIERS:
        credit_status = "available"
    else:
        credit_status = "pending_kyc"
    return {
        "floatra_credit_status": credit_status,
        "floatra_credit_tier": data.get("credit_tier") or None,
        "floatra_credit_limit": parse_naira(data.get("max_credit")) or Decimal("0"),
        "floatra_kyc_status": kyc_status_for(kyc_tier),
        "floatra_liveness_verified": bool(data.get("liveness_verified")),
    }


def parse_lock_status(body: Any) -> bool:
    """``GET /v1/partner/merchants/:id/lock-status`` → locked?"""
    data = _data(body)
    if "locked" not in data:
        raise ValueError("Floatra lock-status response has no 'locked' field")
    return bool(data["locked"])


def credit_status_after_lock(current: Optional[str], locked: bool) -> str:
    """A lock-state change applied to the stored credit status.

    Unlocking returns a ``locked`` customer to ``available`` (a lock only
    exists on a KYC'd merchant with a loan) and leaves every other status
    alone: lock-status says nothing about KYC or suspension.
    """
    if locked:
        return "locked"
    if current == "locked":
        return "available"
    return current or "not_enrolled"


def credit_status_after_kyc(current: Optional[str]) -> str:
    """``merchant.kyc_completed`` moves an un-verified customer to available."""
    if current in (None, "", "not_enrolled", "pending_kyc"):
        return "available"
    return current


@dataclass(frozen=True)
class Delivery:
    delivery_confirmed: bool
    status: Optional[str]
    repayment_due_date: Optional[str]
    repayment_amount: Optional[Decimal]
    installment: Optional[int]
    remaining_undelivered_amount: Optional[Decimal]


def parse_delivery(body: Any) -> Delivery:
    """``POST /v1/partner/orders/:loan_id/confirm-delivery`` 2xx body."""
    data = _data(body)
    return Delivery(
        delivery_confirmed=bool(data.get("delivery_confirmed")),
        status=data.get("status"),
        repayment_due_date=date_part(data.get("repayment_due_date")),
        repayment_amount=parse_naira(data.get("repayment_amount")),
        installment=data.get("delivery_installment"),
        remaining_undelivered_amount=parse_naira(
            data.get("remaining_undelivered_amount"),
        ),
    )


def parse_repayment(body: Any) -> dict:
    """``POST /v1/partner/orders/:loan_id/repayments`` 2xx body."""
    data = _data(body)
    overpayment = data.get("overpayment") or {}
    return {
        "amount": parse_naira(data.get("amount")),
        "outstanding_balance": parse_naira(data.get("outstanding_balance")),
        "overpayment": parse_naira(overpayment.get("excess_amount")),
        "reference": data.get("reference"),
    }


def parse_restructure(body: Any) -> dict:
    """``POST /v1/partner/orders/:loan_id/restructure`` 2xx body."""
    data = _data(body)
    return {
        "floatra_repayment_due_date": date_part(data.get("new_due_date")),
        "new_tenure_days": data.get("new_tenure_days"),
    }


def parse_order_lookup(body: Any) -> dict:
    """``GET /v1/partner/orders/by-external-id/:ref`` 2xx body."""
    data = _data(body)
    return {
        "floatra_order_id": data.get("floatra_order_id"),
        "floatra_loan_id": data.get("floatra_loan_id"),
        "loan_status": data.get("loan_status"),
    }


def parse_orders_page(body: Any) -> dict:
    """``GET /v1/partner/orders`` 2xx body.

    Each row's ``floatra_order_id`` is the LOAN id (core names it that way on
    this list), so it is returned as ``floatra_loan_id``.
    """
    data = _data(body)
    rows = [
        {
            "floatra_loan_id": row.get("floatra_order_id"),
            "floatra_merchant_id": row.get("floatra_merchant_id"),
            "status": row.get("status"),
            "principal": parse_naira(row.get("principal")),
            "due_date": date_part(row.get("due_date")),
            "created_at": row.get("created_at"),
        }
        for row in data.get("data") or []
    ]
    return {
        "rows": rows,
        "next_cursor": data.get("next_cursor"),
        "has_more": bool(data.get("has_more")),
    }


def parse_undelivered(body: Any) -> dict:
    """``GET /v1/partner/webhooks/undelivered`` 2xx body."""
    data = _data(body)
    events = [
        {
            "event_id": row.get("event_id"),
            "event_type": row.get("event_type") or "",
            "payload": row.get("payload") or {},
        }
        for row in data.get("data") or []
        if row.get("event_id")
    ]
    return {"events": events, "has_more": bool(data.get("has_more"))}


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------

# Core retries a failed delivery for hours; each attempt is signed afresh, so
# only a captured request older than this is refused.
WEBHOOK_REPLAY_WINDOW_SECONDS = 300


class WebhookRejected(Exception):
    """A webhook that must be answered 401 (``reason`` says why)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def sign_webhook(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Hex HMAC-SHA256 over ``"<timestamp>.<raw body>"`` (core's signature)."""
    message = timestamp.encode("utf-8") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_webhook(
    secret: Optional[str],
    timestamp: Optional[str],
    raw_body: bytes,
    signature: Optional[str],
    now: Optional[float] = None,
) -> None:
    """Raise ``WebhookRejected`` unless the delivery is authentic and fresh.

    ``X-Floatra-Timestamp`` is Unix SECONDS (core:
    ``Math.floor(Date.now() / 1000)``); the signature covers the timestamp,
    so a captured body cannot be replayed under a new one.
    """
    if not secret:
        raise WebhookRejected("webhook_secret_unset")
    ts = (timestamp or "").strip()
    if not ts.isdigit():
        raise WebhookRejected("invalid_timestamp")
    expected = sign_webhook(secret, ts, raw_body or b"")
    if not hmac.compare_digest(expected, (signature or "").strip()):
        raise WebhookRejected("invalid_signature")
    current = time.time() if now is None else now
    if abs(current - int(ts)) > WEBHOOK_REPLAY_WINDOW_SECONDS:
        raise WebhookRejected("timestamp_outside_window")


@dataclass
class WebhookEffect:
    """What one webhook means for this ERP, independent of Odoo/ERPNext.

    Records are matched on Floatra's ids first (see ``partner_candidate_ok``
    and ``order_candidate_ok``): ERP-local names (``merchant_external_id``,
    ``external_order_id``) are only accepted when the record's stored
    Floatra ids agree with the payload's.
    """

    event: str
    handled: bool = True
    merchant_id: Optional[str] = None
    merchant_external_id: Optional[str] = None
    loan_id: Optional[str] = None
    loan_request_id: Optional[str] = None
    external_order_id: Optional[str] = None
    partner_updates: Dict[str, Any] = field(default_factory=dict)
    order_updates: Dict[str, Any] = field(default_factory=dict)
    lock: Optional[bool] = None  # True locked / False unlocked / None n/a
    kyc_completed: bool = False
    platform_suspended: bool = False
    note: Optional[str] = None
    severity: str = "info"  # info | warning | error


def partner_candidate_ok(stored_merchant_id: Any, effect: WebhookEffect) -> bool:
    """A customer found by ``merchantExternalId`` is the one only if its
    stored Floatra merchant id equals the payload's ``merchantId``."""
    return bool(effect.merchant_id) and stored_merchant_id == effect.merchant_id


def order_candidate_ok(
    stored_loan_id: Any,
    stored_loan_request_id: Any,
    stored_merchant_id: Any,
    effect: WebhookEffect,
) -> bool:
    """Accept an order found by ``externalOrderId`` (an ERP-local name) only
    when nothing Floatra told us about it contradicts the payload: its stored
    loan id is empty or equals ``loanId``, its stored loan-request id is empty
    or equals ``loanRequestId``, and its customer's Floatra merchant id equals
    ``merchantId`` when the payload names one."""
    if stored_loan_id and stored_loan_id != effect.loan_id:
        return False
    if (
        stored_loan_request_id
        and effect.loan_request_id
        and stored_loan_request_id != effect.loan_request_id
    ):
        return False
    if effect.merchant_id and stored_merchant_id != effect.merchant_id:
        return False
    return True


def _money_text(value: Any) -> str:
    amount = parse_naira(value)
    return f"₦{amount:,.2f}" if amount is not None else "₦?"


def interpret_webhook(payload: dict) -> WebhookEffect:
    """Map one flat camelCase Floatra webhook payload to its effect."""
    event = str(payload.get("event") or "")
    effect = WebhookEffect(
        event=event,
        merchant_id=payload.get("merchantId"),
        merchant_external_id=payload.get("merchantExternalId")
        or payload.get("externalMerchantId"),
        loan_id=payload.get("loanId"),
        loan_request_id=payload.get("loanRequestId"),
        external_order_id=payload.get("externalOrderId"),
    )
    handler = _WEBHOOK_HANDLERS.get(event)
    if handler is None:
        effect.handled = False
        return effect
    handler(payload, effect)
    return effect


def _on_reorder_locked(p: dict, e: WebhookEffect) -> None:
    e.lock = True
    e.note = f"Floatra reorder-locked this customer ({p.get('reason') or 'no reason'})."
    e.severity = "warning"


def _on_reorder_unlocked(p: dict, e: WebhookEffect) -> None:
    e.lock = False
    e.note = "Floatra lifted the reorder lock on this customer."


def _on_kyc_completed(p: dict, e: WebhookEffect) -> None:
    # Fired only for a successful verification, with the merchant's OWN tier.
    e.kyc_completed = True
    e.partner_updates = {
        "floatra_kyc_status": kyc_status_for(p.get("verificationTier")),
        "floatra_liveness_verified": True,
        # The hosted link was single use and is spent.
        "floatra_kyc_url": None,
        "floatra_kyc_url_expires_at": None,
    }
    e.note = f"Floatra KYC completed ({p.get('verificationTier')})."


def _on_tier_changed(p: dict, e: WebhookEffect) -> None:
    e.partner_updates = {"floatra_credit_tier": p.get("newTier") or None}
    new_limit = parse_naira(p.get("newLimit"))
    if new_limit is not None:
        e.partner_updates["floatra_credit_limit"] = new_limit
    e.note = (
        f"Floatra credit tier {p.get('previousTier')} → {p.get('newTier')} "
        f"(limit {_money_text(p.get('newLimit'))})."
    )


def _on_credit_approved(p: dict, e: WebhookEffect) -> None:
    e.order_updates = {
        "floatra_loan_id": p.get("loanId"),
        "floatra_loan_request_id": p.get("loanRequestId"),
        "floatra_decision": "approved",
        "floatra_decision_reason": None,
        "floatra_approved_amount": parse_naira(p.get("principalAmount")),
        "floatra_interest_amount": parse_naira(p.get("interestAmount")),
        "floatra_repayment_amount": parse_naira(p.get("totalRepayment")),
        "floatra_repayment_due_date": date_part(p.get("dueDate")),
    }
    e.note = (
        f"Floatra approved {_money_text(p.get('principalAmount'))}. Waiting "
        "for funding before dispatch."
    )


def _on_credit_declined(p: dict, e: WebhookEffect) -> None:
    reason = p.get("reason") or "No reason given"
    if p.get("reasonCode"):
        reason = f"{reason} ({p['reasonCode']})"
    e.order_updates = {
        "floatra_decision": "declined",
        "floatra_decision_reason": reason,
    }
    e.note = f"Floatra declined the credit request: {reason}"


def _on_funded(p: dict, e: WebhookEffect) -> None:
    # order.funded (lender-paid track) and order.disbursed (Paystack
    # transfer) both mean the platform has been paid: dispatch may proceed.
    e.order_updates = {"floatra_disbursed": True}
    if p.get("loanId"):
        e.order_updates["floatra_loan_id"] = p["loanId"]
    e.note = (
        f"Floatra funding landed ({_money_text(p.get('amount'))}). Dispatch "
        "is now unblocked."
    )


def _on_disbursement_failed(p: dict, e: WebhookEffect) -> None:
    e.note = (
        "Floatra could not pay out this loan yet; Floatra retries the "
        "disbursement. Do not dispatch."
    )
    e.severity = "warning"


def _on_repayment_due(p: dict, e: WebhookEffect) -> None:
    # ``amount`` on this event is the loan PRINCIPAL; what is due is
    # ``outstandingAmount`` (principal + interest + late fees, less what was
    # paid). A delivery queued before core sent it names no amount.
    due = p.get("outstandingAmount")
    what = (
        f"Floatra repayment of {_money_text(due)}"
        if due is not None else "Floatra repayment"
    )
    e.note = (
        f"{what} due in {p.get('daysUntilDue', '?')} day(s) "
        f"({date_part(p.get('dueDate'))})."
    )


def _on_repaid(p: dict, e: WebhookEffect) -> None:
    e.note = "Floatra: the loan on this order is repaid in full."


def _on_overdue(p: dict, e: WebhookEffect) -> None:
    text = (
        f"Floatra loan OVERDUE: {p.get('daysOverdue', '?')} day(s) past due."
    )
    if p.get("dailyLateFee") is not None:
        text += (
            f" Late fee accruing at {_money_text(p.get('dailyLateFee'))}/day "
            f"(accrued {_money_text(p.get('accruedLateFee'))})."
        )
    e.note = text
    e.severity = "warning"


def _on_defaulted(p: dict, e: WebhookEffect) -> None:
    e.note = "Floatra marked the loan on this order as DEFAULTED."
    e.severity = "error"


def _on_restructured(p: dict, e: WebhookEffect) -> None:
    e.order_updates = {
        "floatra_repayment_due_date": date_part(p.get("newDueDate")),
    }
    e.note = (
        f"Floatra restructured the loan: new due date "
        f"{date_part(p.get('newDueDate'))}, capitalised late fee "
        f"{_money_text(p.get('capitalisedLateFee'))}."
    )


def _on_bulk_decision(p: dict, e: WebhookEffect) -> None:
    decision = parse_decision(p.get("envelope") or {})
    e.external_order_id = p.get("externalOrderId") or decision.external_order_id
    e.order_updates = decision.order_updates()
    e.note = f"Floatra bulk decision: {decision.decision} ({decision.reason()})."


def _on_platform_suspended(p: dict, e: WebhookEffect) -> None:
    e.platform_suspended = True
    e.note = (
        f"FLOATRA PLATFORM SUSPENDED at {p.get('suspendedAt')}: new credit "
        "requests are blocked until Floatra reinstates the platform."
    )
    e.severity = "error"


def _on_api_key_expiring(p: dict, e: WebhookEffect) -> None:
    e.note = (
        f"Floatra {p.get('credentialMode')} API key expires at "
        f"{p.get('expiresAt')} ({p.get('daysRemaining')} day(s)). Rotate it "
        "before then."
    )
    e.severity = "warning"


def _on_suspension_requested(p: dict, e: WebhookEffect) -> None:
    e.note = (
        "Floatra asks you to suspend trade credit for this customer: a loan "
        f"is 7+ days overdue ({p.get('reason') or 'no reason'})."
    )
    e.severity = "warning"


_WEBHOOK_HANDLERS = {
    "merchant.reorder_locked": _on_reorder_locked,
    "merchant.reorder_unlocked": _on_reorder_unlocked,
    "merchant.kyc_completed": _on_kyc_completed,
    "merchant.credit_tier_changed": _on_tier_changed,
    "vendor.suspension_requested": _on_suspension_requested,
    "order.credit_approved": _on_credit_approved,
    "order.credit_declined": _on_credit_declined,
    "order.funded": _on_funded,
    "order.disbursed": _on_funded,
    "order.disbursement_failed": _on_disbursement_failed,
    "order.repayment_due": _on_repayment_due,
    "order.repayment_received": _on_repaid,
    "order.overdue": _on_overdue,
    "order.defaulted": _on_defaulted,
    "order.restructured": _on_restructured,
    "bulk.order_decision": _on_bulk_decision,
    "platform.suspended": _on_platform_suspended,
    "platform.api_key_expiring": _on_api_key_expiring,
}

HANDLED_WEBHOOK_EVENTS = frozenset(_WEBHOOK_HANDLERS)
