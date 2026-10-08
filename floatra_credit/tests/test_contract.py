# -*- coding: utf-8 -*-
"""The connector against Floatra core's real contract, without Odoo.

Every fixture in ``fixtures/contract`` except the ``request-*`` files is
rendered by core (``src/modules/partner-webhooks/partner-contract-fixtures.spec.ts``)
from its real controllers, DTO pipe, exception filter and webhook pipeline.
The ``request-*`` files are this connector's bodies (``contract_support``);
core validates them against its DTOs. So:

* the request bodies built here match what core's DTOs accept;
* the client calls the real ``/v1/partner/...`` route and parses every
  response core can send for it;
* the webhook receiver accepts core's signed deliveries and every handler
  understands the flat payload.

Run from ``floatra_credit/tests``: ``python3 -m unittest test_contract``
(or ``python3 test_contract.py``; pytest works too).
"""

import json
import logging
import os
import sys
import types
import unittest
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contract_support as support  # noqa: E402

# Calls log at INFO/WARNING by design; keep test output readable.
logging.getLogger("floatra_credit").setLevel(logging.CRITICAL)

contract = support.services.floatra_contract
client_mod = support.services.floatra_client
api = support.services.floatra_api
kyc_link = support.services.kyc_link
fixture = support.fixture

LIVE_KEY = "live_pk_" + "a1" * 24
BASE = "https://api.floatra.com"


def _response(fx):
    resp = MagicMock()
    resp.status_code = fx["status"]
    resp.json.return_value = fx["body"]
    return resp


def _client():
    return api.FloatraAPIClient(
        base_url="https://api.floatra.io/v1",  # a pre-0.3 stored value
        api_key=LIVE_KEY,
        platform_id="acme-distributors",
        sleep=lambda _s: None,
    )


class TestUrls(unittest.TestCase):
    def test_base_url_is_host_only(self):
        cases = {
            None: BASE,
            "": BASE,
            "https://api.floatra.io/v1": BASE,
            "https://sandbox.api.floatra.io/v1/": BASE,
            "https://api.floatra.com/v1": BASE,
            "https://api.floatra.com/": BASE,
            "https://staging-api.floatra.com": "https://staging-api.floatra.com",
            " https://staging-api.floatra.com/v1 ": "https://staging-api.floatra.com",
        }
        for raw, expected in cases.items():
            self.assertEqual(contract.normalize_base_url(raw), expected, raw)

    def test_every_path_is_under_v1_partner_and_encoded(self):
        self.assertEqual(
            contract.partner_url(BASE + "/v1", "orders", "loan_01", "cancel"),
            BASE + "/v1/partner/orders/loan_01/cancel",
        )
        self.assertEqual(
            contract.partner_path("orders", "by-external-id", "SO/1?x=1"),
            "/v1/partner/orders/by-external-id/SO%2F1%3Fx%3D1",
        )
        self.assertEqual(
            contract.partner_url(BASE, "orders", query={"limit": 50, "cursor": None}),
            BASE + "/v1/partner/orders?limit=50",
        )


class TestPathSafety(unittest.TestCase):
    def test_dot_or_empty_ids_are_refused(self):
        for bad in ("", ".", ".."):
            with self.assertRaises(contract.ContractError):
                contract.partner_path("orders", bad, "cancel")


class TestMoney(unittest.TestCase):
    def test_decimal_naira_strings(self):
        self.assertEqual(contract.naira(250000.0), "250000.00")
        self.assertEqual(contract.naira("1234.565"), "1234.57")  # half-up
        self.assertEqual(contract.naira(0.1 + 0.2), "0.30")
        self.assertEqual(contract.naira(Decimal("10")), "10.00")
        self.assertEqual(contract.naira_from_kobo(25_000_050), "250000.50")

    def test_refuses_bad_amounts(self):
        for bad in ("abc", "NaN", "Infinity", -1, 0, None):
            with self.assertRaises(contract.ContractError, msg=repr(bad)):
                contract.naira(bad)

    def test_phone_normalization(self):
        for raw in ("08031234567", "+234 803 123 4567", "2348031234567", "8031234567"):
            self.assertEqual(contract.normalize_ng_phone(raw), "+2348031234567", raw)
        for raw in ("12345", "", None, False):
            self.assertIsNone(contract.normalize_ng_phone(raw), raw)


class TestRequestBodies(unittest.TestCase):
    def test_each_body_matches_its_fixture(self):
        for name, body in support.build_requests().items():
            self.assertEqual(body, fixture(name), name)

    def test_no_stale_request_fixture(self):
        names = {
            n for n in support.fixture_names("request-")
            if not n.startswith("request-kyc-retry")
        }
        self.assertEqual(names, set(support.build_requests()))

    def test_no_kobo_or_test_flag_or_identity_numbers(self):
        blob = json.dumps(support.build_requests())
        for stale in ('"amount_kobo"', '"is_test"', '"bvn"', '"nin"', '"idempotency_key"'):
            self.assertNotIn(stale, blob)

    def test_onboard_without_optional_fields(self):
        body = api.onboard_body_for_partner(NS(
            id=5, name="Shop", mobile=False, phone="0803 123 4567",
        ))
        self.assertEqual(body, {
            "external_merchant_id": "5", "name": "Shop", "phone": "+2348031234567",
        })

    def test_onboard_refuses_a_non_nigerian_phone(self):
        with self.assertRaises(contract.ContractError):
            api.onboard_body_for_partner(NS(id=5, name="Shop", mobile="12345"))

    def test_category_falls_back_to_other(self):
        order = NS(**{**support.ORDER.__dict__, "order_line": []})
        self.assertEqual(api.initiate_body_for_order(order)["category"], "OTHER")

    def test_tenure(self):
        self.assertEqual(contract.coerce_tenure_days(None), 14)
        self.assertEqual(contract.coerce_tenure_days("30"), 30)
        with self.assertRaises(contract.ContractError):
            contract.coerce_tenure_days("45")

    def test_partial_delivery_rules(self):
        with self.assertRaises(contract.ContractError):
            contract.confirm_delivery_body(
                delivered_at=support.DELIVERED_AT, installment=1,
                delivered_amount=1000, undelivered_handling="DEFER_DELIVERY",
                defer_delivery_days=9,
            )

    def test_restructure_reason_limit(self):
        with self.assertRaises(contract.ContractError):
            contract.restructure_body(
                new_tenure_days=30, reason="x" * 501,
                lender_approval_reference="LAR",
            )


# (client method, args, HTTP method, path, request fixture, response fixture)
CALLS = [
    ("onboard_merchant", lambda r: (r["request-onboard"],), "POST",
     "/v1/partner/merchants/onboard", "request-onboard", "response-onboard-created"),
    ("initiate_order", lambda r: (r["request-initiate"],), "POST",
     "/v1/partner/orders/initiate", "request-initiate", "response-initiate-approved"),
    ("lookup_order", lambda r: ("S00042",), "GET",
     "/v1/partner/orders/by-external-id/S00042", None, "response-order-lookup"),
    ("confirm_delivery", lambda r: ("loan_01", r["request-confirm-delivery"], "k1"),
     "POST", "/v1/partner/orders/loan_01/confirm-delivery",
     "request-confirm-delivery", "response-confirm-delivery"),
    ("confirm_delivery",
     lambda r: ("loan_01", r["request-confirm-delivery-partial"], "k2"), "POST",
     "/v1/partner/orders/loan_01/confirm-delivery",
     "request-confirm-delivery-partial", "response-confirm-delivery-partial"),
    ("cancel_order", lambda r: ("loan_01", r["request-cancel"], "k3"), "POST",
     "/v1/partner/orders/loan_01/cancel", "request-cancel", "response-cancel"),
    ("record_repayment", lambda r: ("loan_01", r["request-repayment"]), "POST",
     "/v1/partner/orders/loan_01/repayments", "request-repayment",
     "response-repayment"),
    ("restructure_loan", lambda r: ("loan_01", r["request-restructure"], "k4"),
     "POST", "/v1/partner/orders/loan_01/restructure", "request-restructure",
     "response-restructure"),
    ("get_credit_status", lambda r: ("mer_01",), "GET",
     "/v1/partner/merchants/mer_01/credit-status", None,
     "response-credit-status-verified"),
    ("get_lock_status", lambda r: ("mer_01",), "GET",
     "/v1/partner/merchants/mer_01/lock-status", None, "response-lock-status-locked"),
    ("set_bank_account", lambda r: ("mer_01", r["request-bank-account"]), "POST",
     "/v1/partner/merchants/mer_01/bank-account", "request-bank-account",
     "response-bank-account"),
    ("request_kyc_link", lambda r: ("mer_01",), "POST",
     "/v1/partner/merchants/mer_01/kyc/retry", "request-kyc-retry-live",
     "response-kyc-retry-hosted-link"),
    ("get_undelivered_webhooks", lambda r: (50, 0), "GET",
     "/v1/partner/webhooks/undelivered?limit=50&offset=0", None,
     "response-undelivered"),
    ("acknowledge_webhook", lambda r: ("dlv_disbursed",), "POST",
     "/v1/partner/webhooks/dlv_disbursed/acknowledge", None, "response-acknowledge"),
]


class TestClientCalls(unittest.TestCase):
    def _call(self, method, args, response_fixture):
        with patch.object(
            client_mod.requests, "request",
            return_value=_response(fixture(response_fixture)),
        ) as req:
            result = getattr(_client(), method)(*args)
        return result, req

    def test_every_call_hits_the_real_route_with_the_contract_body(self):
        requests_ = support.build_requests()
        requests_["request-kyc-retry-live"] = fixture("request-kyc-retry-live")
        for method, args, verb, path, req_fx, resp_fx in CALLS:
            with self.subTest(method=method, path=path):
                _result, req = self._call(method, args(requests_), resp_fx)
                req.assert_called_once()
                (sent_verb, url), kwargs = req.call_args[0], req.call_args[1]
                self.assertEqual(sent_verb, verb)
                self.assertEqual(url, BASE + path)
                headers = kwargs["headers"]
                self.assertEqual(headers["Authorization"], "Bearer " + LIVE_KEY)
                self.assertEqual(headers["X-Platform-ID"], "acme-distributors")
                self.assertEqual(headers["Accept-Version"], "v1")
                self.assertTrue(headers["X-Floatra-Timestamp"].endswith("+00:00"))
                if verb == "GET":
                    self.assertIsNone(kwargs["data"])
                    self.assertNotIn("Idempotency-Key", headers)
                else:
                    self.assertTrue(headers["Idempotency-Key"])
                    expected = fixture(req_fx) if req_fx else {}
                    self.assertEqual(json.loads(kwargs["data"]), expected)

    def test_natural_idempotency_keys(self):
        requests_ = support.build_requests()
        _r, req = self._call(
            "initiate_order", (requests_["request-initiate"],),
            "response-initiate-approved",
        )
        self.assertEqual(req.call_args[1]["headers"]["Idempotency-Key"], "S00042")
        _r, req = self._call(
            "record_repayment", ("loan_01", requests_["request-repayment"]),
            "response-repayment",
        )
        self.assertEqual(
            req.call_args[1]["headers"]["Idempotency-Key"], "repayment:RCPT-0042",
        )


class TestResponses(unittest.TestCase):
    def test_initiate_decisions(self):
        approved = contract.parse_decision(fixture("response-initiate-approved")["body"])
        self.assertEqual(approved.decision, "APPROVED")
        self.assertEqual(approved.floatra_order_id, "ord_01")
        self.assertEqual(approved.floatra_loan_id, "loan_01")
        updates = approved.order_updates()
        self.assertEqual(updates["floatra_approved_amount"], Decimal("250000.00"))
        self.assertEqual(updates["floatra_interest_amount"], Decimal("12500.00"))
        self.assertEqual(updates["floatra_repayment_amount"], Decimal("262500.00"))
        self.assertEqual(updates["floatra_repayment_due_date"], "2026-10-15")

        declined = contract.parse_decision(fixture("response-initiate-declined")["body"])
        self.assertEqual(declined.order_updates()["floatra_decision"], "declined")
        self.assertIn("CREDIT_002", declined.reason())
        self.assertNotIn("floatra_loan_id", declined.order_updates())

        waitlisted = contract.parse_decision(
            fixture("response-initiate-waitlisted")["body"],
        )
        self.assertEqual(waitlisted.order_updates()["floatra_decision"], "waitlisted")
        self.assertIn("#3", waitlisted.reason())

    def test_onboard(self):
        for name in ("response-onboard-created", "response-onboard-existing"):
            out = contract.parse_onboard(fixture(name)["body"])
            self.assertEqual(out.floatra_merchant_id, "mer_01")
            self.assertEqual(contract.kyc_status_for(out.kyc_status), "not_started")

    def test_credit_status(self):
        expected = {
            "response-credit-status-verified": ("available", "approved", Decimal("400000.00")),
            "response-credit-status-unverified": ("pending_kyc", "not_started", Decimal("0")),
            "response-credit-status-locked": ("locked", "approved", Decimal("400000.00")),
            "response-credit-status-suspended": ("suspended", "approved", Decimal("400000.00")),
        }
        for name, (status, kyc, limit) in expected.items():
            out = contract.parse_credit_status(fixture(name)["body"])
            self.assertEqual(out["floatra_credit_status"], status, name)
            self.assertEqual(out["floatra_kyc_status"], kyc, name)
            self.assertEqual(out["floatra_credit_limit"], limit, name)
        suspended = contract.parse_credit_status(
            fixture("response-credit-status-suspended")["body"],
        )
        self.assertEqual(
            fixture("response-credit-status-suspended")["body"]["data"]["status"],
            "NOT_AVAILABLE",
        )
        self.assertEqual(suspended["floatra_credit_status"], "suspended")

    def test_lock_status(self):
        self.assertTrue(contract.parse_lock_status(fixture("response-lock-status-locked")["body"]))
        self.assertFalse(contract.parse_lock_status(fixture("response-lock-status-unlocked")["body"]))
        self.assertEqual(contract.credit_status_after_lock("available", True), "locked")
        self.assertEqual(contract.credit_status_after_lock("locked", False), "available")
        self.assertEqual(contract.credit_status_after_lock("pending_kyc", False), "pending_kyc")

    def test_delivery(self):
        full = contract.parse_delivery(fixture("response-confirm-delivery")["body"])
        self.assertTrue(full.delivery_confirmed)
        self.assertEqual(full.status, "ACTIVE")
        partial = contract.parse_delivery(fixture("response-confirm-delivery-partial")["body"])
        self.assertFalse(partial.delivery_confirmed)
        self.assertEqual(partial.installment, 1)
        self.assertEqual(partial.remaining_undelivered_amount, Decimal("150000.00"))

    def test_repayment_restructure_lookup_undelivered(self):
        rep = contract.parse_repayment(fixture("response-repayment")["body"])
        self.assertEqual(rep["amount"], Decimal("100000.00"))
        self.assertEqual(rep["outstanding_balance"], Decimal("162500.00"))
        self.assertIsNone(rep["overpayment"])
        rs = contract.parse_restructure(fixture("response-restructure")["body"])
        self.assertEqual(rs["floatra_repayment_due_date"], "2026-10-31")
        found = contract.parse_order_lookup(fixture("response-order-lookup")["body"])
        self.assertEqual(found["floatra_loan_id"], "loan_01")
        page = contract.parse_undelivered(fixture("response-undelivered")["body"])
        self.assertEqual(page["events"][0]["event_id"], "dlv_disbursed")
        self.assertEqual(page["events"][0]["payload"]["event"], "order.disbursed")


class TestErrors(unittest.TestCase):
    CASES = {
        "response-initiate-error-validation": "amount_kobo should not exist",
        "response-initiate-error-merchant-not-found": "Onboard the customer",
        "response-order-lookup-error-not-found": "no order with this reference",
        "response-lock-status-error-unavailable": "Treat the customer as locked",
        "response-kyc-retry-error-realm": "other Floatra environment",
    }

    def test_errors_raise_with_mapped_copy(self):
        for name, expected in self.CASES.items():
            fx = fixture(name)
            with patch.object(client_mod.requests, "request", return_value=_response(fx)):
                with self.assertRaises(contract.FloatraAPIError) as ctx:
                    _client().get_lock_status("mer_01")
            self.assertEqual(ctx.exception.status, fx["status"], name)
            self.assertIn(expected, ctx.exception.user_message(), name)

    def test_forbidden_codes_are_not_reported_as_wrong_platform(self):
        # Staging pass 2026-10-08: confirm-delivery with an Agent ID Floatra
        # does not know answers 403 AGENT_NOT_AUTHORIZED; the generic 403
        # copy blamed the platform settings instead.
        for code, expected in (
            ("AGENT_NOT_AUTHORIZED", "agent ID"),
            ("PLATFORM_SUSPENDED", "suspended your platform"),
        ):
            body = {"success": False, "data": None, "error": "refused",
                    "errorCode": code}
            message = contract.describe_error(403, body)
            self.assertIn(expected, message, code)
            self.assertNotIn("does not belong to the platform", message, code)

    def test_503_is_retried_once(self):
        fx = fixture("response-lock-status-error-unavailable")
        with patch.object(client_mod.requests, "request", return_value=_response(fx)) as req:
            with self.assertRaises(contract.FloatraAPIError):
                _client().get_lock_status("mer_01")
        self.assertEqual(req.call_count, 2)

    def test_network_error(self):
        with patch.object(
            client_mod.requests, "request",
            side_effect=client_mod.requests.ConnectionError("down"),
        ):
            with self.assertRaises(contract.FloatraAPIError) as ctx:
                _client().get_lock_status("mer_01")
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("Could not reach Floatra", ctx.exception.user_message())

    def test_unconfigured_client(self):
        with self.assertRaises(contract.FloatraAPIError):
            api.FloatraAPIClient(base_url=None, api_key="", platform_id="p")


SECRET = "contract-fixture-secret"


class TestWebhookVerification(unittest.TestCase):
    def _delivery(self, event):
        fx = fixture("webhook-" + event)
        return fx["headers"], fx["body"].encode("utf-8")

    def test_every_core_delivery_verifies(self):
        for name in support.fixture_names("webhook-"):
            fx = fixture(name)
            ts = fx["headers"]["X-Floatra-Timestamp"]
            self.assertTrue(ts.isdigit(), name)  # Unix seconds
            contract.verify_webhook(
                fx["secret"], ts, fx["body"].encode("utf-8"),
                fx["headers"]["X-Floatra-Signature"], now=int(ts) + 60,
            )

    def test_rejections(self):
        headers, body = self._delivery("order.disbursed")
        ts, sig = headers["X-Floatra-Timestamp"], headers["X-Floatra-Signature"]
        cases = {
            "webhook_secret_unset": ("", ts, body, sig, int(ts)),
            "invalid_signature": (SECRET, ts, body + b" ", sig, int(ts)),
            "invalid_timestamp": (SECRET, "2026-10-01T12:00:00Z", body, sig, int(ts)),
            "timestamp_outside_window": (SECRET, ts, body, sig, int(ts) + 301),
        }
        for reason, args in cases.items():
            with self.assertRaises(contract.WebhookRejected) as ctx:
                contract.verify_webhook(*args[:4], now=args[4])
            self.assertEqual(ctx.exception.reason, reason)


def _payload(event):
    return json.loads(fixture("webhook-" + event)["body"])


class TestWebhookInterpretation(unittest.TestCase):
    def test_every_core_event_has_a_handler(self):
        for name in support.fixture_names("webhook-"):
            effect = contract.interpret_webhook(_payload(name[len("webhook-"):]))
            self.assertTrue(effect.handled, name)

    def test_unknown_event_is_unhandled(self):
        self.assertFalse(contract.interpret_webhook({"event": "x.y"}).handled)

    def test_merchant_events(self):
        locked = contract.interpret_webhook(_payload("merchant.reorder_locked"))
        self.assertEqual((locked.merchant_id, locked.merchant_external_id), ("mer_01", "77"))
        self.assertIs(locked.lock, True)
        self.assertIs(contract.interpret_webhook(_payload("merchant.reorder_unlocked")).lock, False)

        kyc = contract.interpret_webhook(_payload("merchant.kyc_completed"))
        self.assertTrue(kyc.kyc_completed)
        self.assertEqual(kyc.partner_updates["floatra_kyc_status"], "approved")
        self.assertIsNone(kyc.partner_updates["floatra_kyc_url"])
        self.assertIsNone(kyc.partner_updates["floatra_kyc_url_expires_at"])
        self.assertEqual(contract.credit_status_after_kyc("pending_kyc"), "available")
        self.assertEqual(contract.credit_status_after_kyc("locked"), "locked")

        tier = contract.interpret_webhook(_payload("merchant.credit_tier_changed"))
        self.assertEqual(tier.partner_updates, {
            "floatra_credit_tier": "T1", "floatra_credit_limit": Decimal("400000.00"),
        })

    def test_order_events(self):
        approved = contract.interpret_webhook(_payload("order.credit_approved"))
        self.assertEqual(approved.external_order_id, "S00042")
        self.assertEqual(approved.order_updates["floatra_loan_id"], "loan_01")
        self.assertEqual(approved.order_updates["floatra_repayment_amount"], Decimal("262500.00"))

        declined = contract.interpret_webhook(_payload("order.credit_declined"))
        self.assertEqual(declined.order_updates["floatra_decision"], "declined")
        self.assertIn("CREDIT_LIMIT_EXCEEDED", declined.order_updates["floatra_decision_reason"])

        for event in ("order.funded", "order.disbursed"):
            effect = contract.interpret_webhook(_payload(event))
            self.assertEqual(effect.loan_id, "loan_01", event)
            self.assertIs(effect.order_updates["floatra_disbursed"], True, event)

        overdue = contract.interpret_webhook(_payload("order.overdue"))
        self.assertIn("₦500.00/day", overdue.note)
        restructured = contract.interpret_webhook(_payload("order.restructured"))
        self.assertEqual(
            restructured.order_updates, {"floatra_repayment_due_date": "2026-10-31"},
        )
        bulk = contract.interpret_webhook(_payload("bulk.order_decision"))
        self.assertEqual(bulk.external_order_id, "S00042")
        self.assertEqual(bulk.order_updates["floatra_loan_id"], "loan_01")
        for event in ("order.repayment_due", "order.repayment_received",
                      "order.defaulted", "order.disbursement_failed"):
            self.assertTrue(contract.interpret_webhook(_payload(event)).note, event)
        # ``amount`` is the principal on order.repayment_due; the note quotes
        # what is actually owed.
        due = contract.interpret_webhook(_payload("order.repayment_due")).note
        self.assertIn("₦164,000.00", due)
        self.assertNotIn("₦250,000.00", due)
        legacy = _payload("order.repayment_due")
        del legacy["outstandingAmount"]
        self.assertNotIn("₦", contract.interpret_webhook(legacy).note)

    def test_platform_events(self):
        self.assertTrue(contract.interpret_webhook(_payload("platform.suspended")).platform_suspended)
        self.assertIn("expires", contract.interpret_webhook(_payload("platform.api_key_expiring")).note)


# ----- The Odoo receiver + routing, on a stubbed Odoo -----

class _Empty:
    def __bool__(self):
        return False

    def exists(self):
        return self


class _Record:
    def __init__(self, fields, **values):
        self._fields = set(fields)
        self.messages = []
        self.__dict__.update(values)

    def __bool__(self):
        return True

    def sudo(self):
        return self

    def exists(self):
        return self

    def write(self, values):
        self.__dict__.update(values)
        return True

    def message_post(self, body):
        self.messages.append(body)


class _Model:
    def __init__(self, records, fields=()):
        self.records = records
        self.fields = fields

    def sudo(self):
        return self

    def search(self, domain, limit=None):
        for rec in self.records:
            if all(getattr(rec, f, None) == v for f, _op, v in domain):
                return rec
        return _Empty()

    def browse(self, rec_id):
        for rec in self.records:
            if rec.id == rec_id:
                return rec
        return _Empty()

    def create(self, values):
        rec = _Record(self.fields, id=len(self.records) + 1, processed_ok=False,
                      processing_error=False, partner_id=None, sale_order_id=None,
                      **values)
        self.records.append(rec)
        return rec


class _Params:
    def __init__(self):
        self.values = {}

    def sudo(self):
        return self

    def set_param(self, key, value):
        self.values[key] = value


class _Settings:
    def __init__(self, live):
        self.live = live

    def sudo(self):
        return self

    def get_floatra_key_is_live(self):
        return self.live


class _Cursor:
    """``savepoint()`` that undoes record writes when the block raises."""

    def __init__(self, env):
        self.env = env

    def savepoint(self):
        env = self.env

        class _SP:
            def __enter__(self_inner):
                self_inner.snapshot = [
                    (rec, dict(rec.__dict__))
                    for model in env.models.values() if isinstance(model, _Model)
                    for rec in model.records
                ]

            def __exit__(self_inner, exc_type, exc, tb):
                if exc_type:
                    for rec, state in self_inner.snapshot:
                        rec.__dict__.clear()
                        rec.__dict__.update(state)
                return False

        return _SP()


class _Env:
    def __init__(self, models):
        self.models = models
        self.cr = _Cursor(self)

    def __getitem__(self, name):
        return self.models[name]


def _load_odoo_webhook_controller():
    odoo = sys.modules.setdefault("odoo", types.ModuleType("odoo"))
    http = types.ModuleType("odoo.http")
    http.Controller = object
    http.route = lambda *a, **k: (lambda f: f)
    http.Response = MagicMock()
    http.request = MagicMock()
    sys.modules["odoo.http"] = http
    odoo.http = http
    odoo.fields = NS(Datetime=NS(now=lambda: "NOW"))
    support._ensure_pkg(
        "floatra_credit.controllers",
        os.path.join(os.path.dirname(support.SERVICES), "controllers"),
    )
    import importlib

    return importlib.import_module("floatra_credit.controllers.webhook")


PARTNER_FIELDS = (
    "floatra_credit_status", "floatra_kyc_status", "floatra_liveness_verified",
    "floatra_kyc_url", "floatra_kyc_url_expires_at", "floatra_credit_tier",
    "floatra_credit_limit", "floatra_last_status_check",
)
ORDER_FIELDS = (
    "floatra_order_id", "floatra_loan_id", "floatra_loan_request_id",
    "floatra_decision", "floatra_decision_reason", "floatra_approved_amount",
    "floatra_interest_amount", "floatra_repayment_amount",
    "floatra_repayment_due_date", "floatra_disbursed",
)


class TestOdooWebhookRouting(unittest.TestCase):
    def setUp(self):
        self.webhook = _load_odoo_webhook_controller()
        self.partner = _Record(
            PARTNER_FIELDS, id=77, floatra_merchant_id="mer_01",
            floatra_credit_status="pending_kyc",
            floatra_kyc_url="https://floatra.com/kyc/t",
        )
        self.order = _Record(
            ORDER_FIELDS, id=1, name="S00042", floatra_loan_id=False,
            floatra_loan_request_id="lr_01", floatra_decision="waitlisted",
            floatra_disbursed=False, partner_id=self.partner,
        )
        self.params = _Params()
        self.settings = _Settings(live=True)
        self.logs = _Model([], fields=("processed_ok", "processing_error", "signature"))
        self.env = _Env({
            "res.partner": _Model([self.partner]),
            "sale.order": _Model([self.order]),
            "ir.config_parameter": self.params,
            "res.config.settings": self.settings,
            "floatra.webhook.log": self.logs,
        })

    def _route(self, event, **override):
        payload = {**_payload(event), **override}
        log = NS(event_id="evt", partner_id=None, sale_order_id=None)
        return self.webhook._route_event(payload, log, env=self.env), log

    def test_kyc_completed(self):
        _out, log = self._route("merchant.kyc_completed")
        self.assertEqual(log.partner_id, 77)
        self.assertEqual(self.partner.floatra_kyc_status, "approved")
        self.assertEqual(self.partner.floatra_credit_status, "available")
        self.assertIs(self.partner.floatra_kyc_url, False)  # Odoo "clear"
        self.assertEqual(self.partner.floatra_last_status_check, "NOW")

    def test_lock_then_unlock(self):
        self.partner.floatra_credit_status = "available"
        self._route("merchant.reorder_locked")
        self.assertEqual(self.partner.floatra_credit_status, "locked")
        self._route("merchant.reorder_unlocked")
        self.assertEqual(self.partner.floatra_credit_status, "available")

    def test_credit_approved_by_loan_request_then_funded_by_loan(self):
        self.order.name = "renamed"  # matched on the waitlisted loan request
        self._route("order.credit_approved")
        self.assertEqual(self.order.floatra_decision, "approved")
        self.assertEqual(self.order.floatra_loan_id, "loan_01")
        self.assertEqual(self.order.floatra_approved_amount, Decimal("250000.00"))
        out, log = self._route("order.funded")
        self.assertEqual(out, "ok")
        self.assertIs(self.order.floatra_disbursed, True)
        self.assertEqual(log.sale_order_id, 1)
        self.assertTrue(self.order.messages)

    def test_platform_suspended(self):
        self._route("platform.suspended")
        self.assertEqual(self.params.values["floatra.platform_suspended"], "true")

    def test_unknown_event_changes_nothing(self):
        log = NS(event_id="evt", partner_id=None, sale_order_id=None)
        out = self.webhook._route_event(
            {"event": "lender.paused", "livemode": True}, log, env=self.env,
        )
        self.assertEqual(out, "ok")
        self.assertIsNone(log.partner_id)

    # ----- R1: realm + Floatra-id anchoring -----

    def test_sandbox_event_is_ignored_by_a_live_key(self):
        self.partner.floatra_credit_status = "locked"
        out, _log = self._route("merchant.reorder_unlocked", livemode=False)
        self.assertEqual(out, "ignored")
        self.assertEqual(self.partner.floatra_credit_status, "locked")
        out, _log = self._route("order.disbursed", livemode=False)
        self.assertEqual(out, "ignored")
        self.assertIs(self.order.floatra_disbursed, False)

    def test_missing_livemode_is_refused_live_and_accepted_sandbox(self):
        payload = _payload("order.disbursed")
        del payload["livemode"]
        log = NS(event_id="evt", partner_id=None, sale_order_id=None)
        self.assertEqual(self.webhook._route_event(payload, log, env=self.env), "ignored")
        self.settings.live = False
        payload["livemode"] = False
        self.assertEqual(self.webhook._route_event(payload, log, env=self.env), "ok")
        del payload["livemode"]
        self.order.floatra_disbursed = False
        self.assertEqual(self.webhook._route_event(payload, log, env=self.env), "ok")
        self.assertIs(self.order.floatra_disbursed, True)

    def test_order_name_collision_with_another_loan_is_ignored(self):
        self.order.floatra_loan_id = "loan_other"
        self.order.floatra_loan_request_id = False
        _out, log = self._route("order.disbursed")  # externalOrderId S00042, loan_01
        self.assertIs(self.order.floatra_disbursed, False)
        self.assertIsNone(log.sale_order_id)

    def test_order_name_match_of_another_customer_is_ignored(self):
        self.order.floatra_loan_request_id = False
        self.partner.floatra_merchant_id = "mer_other"
        self._route("order.credit_declined")
        self.assertEqual(self.order.floatra_decision, "waitlisted")

    def test_external_merchant_id_needs_the_floatra_id_to_agree(self):
        self.partner.floatra_merchant_id = "mer_other"  # partner 77, other merchant
        self.partner.floatra_credit_status = "available"
        _out, log = self._route("merchant.reorder_locked")
        self.assertIsNone(log.partner_id)
        self.assertEqual(self.partner.floatra_credit_status, "available")

    # ----- R2: retries + signature replay -----

    def test_a_failed_delivery_is_retried_and_applied(self):
        self.order.floatra_loan_id = "loan_01"
        calls = {"n": 0}
        real = self.webhook._route_event

        def flaky(payload, log_row, env):
            calls["n"] += 1
            if calls["n"] == 1:
                env["sale.order"].records[0].write({"floatra_disbursed": True})
                raise RuntimeError("database hiccup")
            return real(payload, log_row, env=env)

        with patch.object(self.webhook, "_route_event", side_effect=flaky):
            first = self.webhook.process_delivery(
                self.env, "dlv_1", _payload("order.funded"), "sig-1",
            )
            self.assertEqual(first, "failed")
            # The savepoint undid the partial write.
            self.assertIs(self.order.floatra_disbursed, False)
            self.assertFalse(self.logs.records[0].processed_ok)
            second = self.webhook.process_delivery(
                self.env, "dlv_1", _payload("order.funded"), "sig-2",
            )
        self.assertEqual(second, "ok")
        self.assertIs(self.order.floatra_disbursed, True)
        self.assertEqual(len(self.logs.records), 1)
        self.assertTrue(self.logs.records[0].processed_ok)
        self.assertEqual(
            self.webhook.process_delivery(self.env, "dlv_1", _payload("order.funded"), "sig-3"),
            "duplicate",
        )

    def test_same_signature_under_a_new_event_id_is_ignored(self):
        self.order.floatra_loan_id = "loan_01"
        self.assertEqual(
            self.webhook.process_delivery(self.env, "dlv_a", _payload("order.funded"), "sig-x"),
            "ok",
        )
        self.order.floatra_disbursed = False
        self.assertEqual(
            self.webhook.process_delivery(self.env, "dlv_b", _payload("order.funded"), "sig-x"),
            "duplicate",
        )
        self.assertIs(self.order.floatra_disbursed, False)
        self.assertEqual(len(self.logs.records), 1)

    def test_other_realm_delivery_is_logged_done_and_not_applied(self):
        out = self.webhook.process_delivery(
            self.env, "dlv_s", {**_payload("order.funded"), "livemode": False}, "sig-s",
        )
        self.assertEqual(out, "ignored")
        self.assertTrue(self.logs.records[0].processed_ok)
        self.assertIn("other realm", self.logs.records[0].processing_error)
        self.assertIs(self.order.floatra_disbursed, False)


class TestRealm(unittest.TestCase):
    def test_livemode_must_match_the_key(self):
        ok = contract.webhook_realm_matches
        self.assertTrue(ok({"livemode": True}, True))
        self.assertFalse(ok({"livemode": False}, True))
        self.assertTrue(ok({"livemode": False}, False))
        self.assertFalse(ok({"livemode": True}, False))
        self.assertFalse(ok({}, True))   # unmarked: refused by a live key
        self.assertTrue(ok({}, False))   # accepted by a sandbox key
        self.assertFalse(ok({"livemode": "true"}, True))  # not a boolean

    def test_every_core_webhook_is_marked_live(self):
        for name in support.fixture_names("webhook-"):
            self.assertIs(_payload(name[len("webhook-"):])["livemode"], True, name)


class TestOnboardEmail(unittest.TestCase):
    def test_single_email_agrees_with_core_is_email(self):
        cases = fixture("email-cases")
        self.assertGreaterEqual(len(cases), 20)
        for case in cases:
            got = contract.single_email(case["email"]) is not None
            self.assertEqual(got, case["isEmail"], case["email"])

    def test_only_a_single_plain_address_is_sent(self):
        base = dict(id=5, name="Shop", mobile="08031234567")
        for email, expected in (
            ("shop@example.com", "shop@example.com"),
            ("a@x.ng, b@x.ng", None),
            ("a@x.ng;b@x.ng", None),
            ("Shop <shop@example.com>", None),
            ("not-an-email", None),
            (False, None),
        ):
            body = api.onboard_body_for_partner(NS(**base, email=email))
            self.assertEqual(body.get("owner", {}).get("email"), expected, email)


class TestBankIdempotencyKey(unittest.TestCase):
    def test_key_never_carries_the_account_number(self):
        body = contract.bank_account_body(
            bank_code="058", account_number="0123456789", account_name="N O",
        )
        with patch.object(
            client_mod.requests, "request",
            return_value=_response(fixture("response-bank-account")),
        ) as req:
            _client().set_bank_account("mer_01", body)
            _client().set_bank_account("mer_01", body)
        keys = [c[1]["headers"]["Idempotency-Key"] for c in req.call_args_list]
        self.assertEqual(keys[0], keys[1])  # stable: a double-click replays
        self.assertNotIn("0123456789", keys[0])


if __name__ == "__main__":
    unittest.main()
