# -*- coding: utf-8 -*-
"""Odoo-runtime tests for the Floatra connector.

Run inside Odoo's test runner:

    odoo-bin -i floatra_credit -d <test_db> --test-enable --stop-after-init

The wire contract (request bodies, response parsing, webhook verification
and interpretation) is covered without Odoo by ``test_contract.py`` against
fixtures Floatra core renders. These tests cover what needs the ORM: field
writes, the dispatch gate, the webhook receiver and routing, crons and
wizards. Floatra responses here come from the same core-rendered fixtures.
"""

import json
import os
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..services import floatra_contract as contract
from ..services.floatra_api import FloatraAPIClient, FloatraAPIError

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "contract")


def fixture(name):
    with open(os.path.join(_FIXTURES, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def webhook_payload(event):
    return json.loads(fixture("webhook-" + event)["body"])


def patch_client(**returns):
    """Patch FloatraAPIClient.for_env with a mock client."""
    fake = MagicMock(spec=FloatraAPIClient)
    for method, value in returns.items():
        getattr(fake, method).return_value = value
    return patch.object(FloatraAPIClient, "for_env", return_value=fake)


def as_floatra_admin(case):
    """Floatra actions require the Floatra user group (admin is a manager);
    a live key, so the core-rendered webhooks (livemode: true) apply."""
    case.env = case.env(user=case.env.ref("base.user_admin"))
    case.env["ir.config_parameter"].sudo().set_param(
        "floatra_credit.api_key", "live_pk_test",
    )


class _OrderCase(TransactionCase):
    def setUp(self):
        super().setUp()
        as_floatra_admin(self)
        self.partner = self.env["res.partner"].create({
            "name": "Pilot Distributor",
            "mobile": "08031234567",
            "floatra_merchant_id": "mer_01",
            "floatra_credit_status": "available",
            "floatra_last_status_check": fields.Datetime.now(),
        })
        self.product = self.env["product.product"].create({
            "name": "Test SKU", "list_price": 250000, "type": "consu",
        })
        self.order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_uom_qty": 1,
                "price_unit": 250000,
                # No sales tax: the chart of accounts gives products a
                # default tax, and Floatra is asked for amount_total.
                "tax_id": [(5, 0, 0)],
            })],
        })


@tagged("floatra_credit", "-at_install", "post_install")
class TestFloatraCreditScaffold(TransactionCase):
    def test_floatra_fields_on_res_partner(self):
        partner = self.env["res.partner"].create({"name": "Test Merchant"})
        self.assertEqual(partner.floatra_credit_status, "not_enrolled")
        self.assertEqual(partner.floatra_kyc_status, "not_started")
        self.assertFalse(partner.floatra_merchant_id)
        self.assertNotIn("floatra_bvn", partner._fields)
        self.assertNotIn("floatra_nin", partner._fields)

    def test_floatra_fields_on_sale_order(self):
        partner = self.env["res.partner"].create({"name": "Test Merchant"})
        order = self.env["sale.order"].create({"partner_id": partner.id})
        self.assertEqual(order.floatra_decision, "not_requested")
        self.assertFalse(order.floatra_order_id)
        self.assertFalse(order.floatra_loan_id)
        self.assertFalse(order.floatra_disbursed)
        self.assertNotIn("floatra_is_test", order._fields)

    def test_product_category_floatra_field(self):
        cat = self.env["product.category"].create({"name": "Test Beverages"})
        for value in contract.ORDER_CATEGORIES:
            cat.write({"floatra_category": value})
            self.assertEqual(cat.floatra_category, value)
        with self.assertRaises(ValueError):
            cat.write({"floatra_category": "FMCG_BEVERAGES"})

    def test_base_url_setting_is_normalized(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("floatra_credit.api_base_url", "https://api.floatra.io/v1")
        config = self.env["res.config.settings"].get_floatra_config()
        self.assertEqual(config["base_url"], "https://api.floatra.com")


@tagged("floatra_credit", "-at_install", "post_install")
class TestDispatchGating(_OrderCase):
    def test_confirm_blocked_while_approved_but_not_funded(self):
        self.order.write({"floatra_decision": "approved", "floatra_disbursed": False})
        with self.assertRaises(UserError):
            self.order.action_confirm()

    def test_confirm_blocked_while_waitlisted(self):
        self.order.write({"floatra_decision": "waitlisted"})
        with self.assertRaises(UserError):
            self.order.action_confirm()

    def test_confirm_allowed_once_funded(self):
        self.order.write({"floatra_decision": "approved", "floatra_disbursed": True})
        self.order.action_confirm()
        self.assertIn(self.order.state, ("sale", "done"))

    def test_confirm_allowed_after_a_decline(self):
        # A declined order has a Floatra order id but no credit: ship it
        # on the distributor's own terms.
        self.order.write({"floatra_decision": "declined", "floatra_order_id": "ord_01"})
        self.order.action_confirm()
        self.assertIn(self.order.state, ("sale", "done"))

    def test_lock_blocks_even_without_a_credit_request(self):
        self.partner.write({"floatra_credit_status": "locked"})
        with self.assertRaises(UserError):
            self.order.action_confirm()

    def test_suspended_blocks(self):
        self.partner.write({"floatra_credit_status": "suspended"})
        with self.assertRaises(UserError):
            self.order.action_confirm()


@tagged("floatra_credit", "-at_install", "post_install")
class TestRequestCredit(_OrderCase):
    def test_unenrolled_partner_raises(self):
        self.partner.write({"floatra_merchant_id": False})
        with self.assertRaises(UserError):
            self.order.action_request_floatra_credit()

    def test_locked_partner_raises(self):
        self.partner.write({"floatra_credit_status": "locked"})
        with self.assertRaises(UserError):
            self.order.action_request_floatra_credit()

    def test_platform_suspended_blocks(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "floatra.platform_suspended", "true",
        )
        with self.assertRaises(UserError):
            self.order.action_request_floatra_credit()

    def _request(self, response_fixture):
        decision = contract.parse_decision(fixture(response_fixture)["body"])
        with patch_client(initiate_order=decision) as patched:
            self.order.action_request_floatra_credit()
            body = patched.return_value.initiate_order.call_args[0][0]
        return body

    def test_sends_the_contract_body(self):
        body = self._request("response-initiate-approved")
        self.assertEqual(body["external_merchant_id"], str(self.partner.id))
        self.assertEqual(body["external_order_id"], self.order.name)
        self.assertEqual(body["amount"], "250000.00")
        self.assertNotIn("amount_kobo", body)
        self.assertNotIn("is_test", body)

    def test_approved_stores_loan_id_and_amounts(self):
        self._request("response-initiate-approved")
        self.assertEqual(self.order.floatra_decision, "approved")
        self.assertEqual(self.order.floatra_order_id, "ord_01")
        self.assertEqual(self.order.floatra_loan_id, "loan_01")
        self.assertEqual(self.order.floatra_approved_amount, 250000)
        self.assertEqual(self.order.floatra_interest_amount, 12500)
        self.assertEqual(self.order.floatra_repayment_amount, 262500)
        self.assertEqual(str(self.order.floatra_repayment_due_date), "2026-10-15")

    def test_declined_records_reason(self):
        self._request("response-initiate-declined")
        self.assertEqual(self.order.floatra_decision, "declined")
        self.assertIn("CREDIT_002", self.order.floatra_decision_reason)

    def test_waitlisted_records_position(self):
        self._request("response-initiate-waitlisted")
        self.assertEqual(self.order.floatra_decision, "waitlisted")
        self.assertIn("#3", self.order.floatra_decision_reason)
        self.assertFalse(self.order.floatra_loan_id)

    def test_error_surfaces_mapped_copy(self):
        fx = fixture("response-initiate-error-merchant-not-found")
        with patch_client() as patched:
            patched.return_value.initiate_order.side_effect = FloatraAPIError(
                fx["status"], fx["body"],
            )
            with self.assertRaises(UserError) as ctx:
                self.order.action_request_floatra_credit()
        self.assertIn("Onboard the customer", str(ctx.exception))

    def test_stale_cache_refreshes_lock_status_first(self):
        self.partner.write({"floatra_last_status_check": False})
        decision = contract.parse_decision(
            fixture("response-initiate-approved")["body"],
        )
        with patch_client(get_lock_status=False, initiate_order=decision) as p:
            self.order.action_request_floatra_credit()
            p.return_value.get_lock_status.assert_called_once_with("mer_01")
        self.assertEqual(self.order.floatra_decision, "approved")

    def test_live_lock_refuses_and_keeps_the_status(self):
        # No exception: the refusal is a notification, so the transaction
        # commits the confirmed lock and its check time.
        self.partner.write({"floatra_last_status_check": False})
        with patch_client(get_lock_status=True) as p:
            result = self.order.action_request_floatra_credit()
            p.return_value.initiate_order.assert_not_called()
        self.assertEqual(result["tag"], "display_notification")
        self.assertEqual(result["params"]["type"], "danger")
        self.assertEqual(self.partner.floatra_credit_status, "locked")
        self.assertTrue(self.partner.floatra_last_status_check)
        self.assertEqual(self.order.floatra_decision, "not_requested")

    def test_retry_after_a_live_lock_makes_no_api_call(self):
        self.partner.write({"floatra_last_status_check": False})
        with patch_client(get_lock_status=True):
            self.order.action_request_floatra_credit()
        with patch_client() as p:
            with self.assertRaises(UserError):
                self.order.action_request_floatra_credit()
            p.return_value.get_lock_status.assert_not_called()
            p.return_value.initiate_order.assert_not_called()

    def test_unreachable_floatra_still_blocks(self):
        # Fail-closed: an unverified "available" without a live answer
        # refuses, and the cache is left unverified.
        self.partner.write({"floatra_last_status_check": False})
        with patch_client() as p:
            p.return_value.get_lock_status.side_effect = FloatraAPIError(503, {})
            with self.assertRaises(UserError):
                self.order.action_request_floatra_credit()
            p.return_value.initiate_order.assert_not_called()
        self.assertEqual(self.partner.floatra_credit_status, "available")
        self.assertFalse(self.partner.floatra_last_status_check)


@tagged("floatra_credit", "-at_install", "post_install")
class TestLoanIdLookup(_OrderCase):
    def test_pre_upgrade_order_looks_up_its_loan_id(self):
        self.order.write({"floatra_decision": "approved", "floatra_order_id": "ord_01"})
        found = contract.parse_order_lookup(fixture("response-order-lookup")["body"])
        with patch_client(lookup_order=found) as p:
            loan_id = self.order._floatra_loan_id_or_raise()
            p.return_value.lookup_order.assert_called_once_with(self.order.name)
        self.assertEqual(loan_id, "loan_01")
        self.assertEqual(self.order.floatra_loan_id, "loan_01")


@tagged("floatra_credit", "-at_install", "post_install")
class TestWebhookRouting(_OrderCase):
    def setUp(self):
        super().setUp()
        self.order.write({"floatra_decision": "approved", "floatra_loan_id": "loan_01"})
        from ..controllers.webhook import _route_event

        self._route_event = _route_event

    def _route(self, event):
        log = self.env["floatra.webhook.log"].create({
            "event_id": "evt-" + event, "event_type": event, "payload": "{}",
        })
        self._route_event(webhook_payload(event), log, env=self.env)
        return log

    def test_reorder_locked_and_unlocked(self):
        self._route("merchant.reorder_locked")
        self.assertEqual(self.partner.floatra_credit_status, "locked")
        self._route("merchant.reorder_unlocked")
        self.assertEqual(self.partner.floatra_credit_status, "available")

    def test_kyc_completed_marks_approved_and_clears_the_link(self):
        self.partner.write({
            "floatra_credit_status": "pending_kyc",
            "floatra_kyc_url": "https://floatra.com/kyc/tok",
            "floatra_kyc_url_expires_at": datetime(2099, 1, 1),
        })
        self._route("merchant.kyc_completed")
        self.assertEqual(self.partner.floatra_kyc_status, "approved")
        self.assertTrue(self.partner.floatra_liveness_verified)
        self.assertEqual(self.partner.floatra_credit_status, "available")
        self.assertFalse(self.partner.floatra_kyc_url)
        self.assertFalse(self.partner.floatra_kyc_url_expires_at)

    def test_tier_changed_updates_tier_and_limit(self):
        self._route("merchant.credit_tier_changed")
        self.assertEqual(self.partner.floatra_credit_tier, "T1")
        self.assertEqual(self.partner.floatra_credit_limit, 400000)

    def test_funded_and_disbursed_lift_the_dispatch_gate(self):
        for event in ("order.funded", "order.disbursed"):
            self.order.write({"floatra_disbursed": False})
            log = self._route(event)
            self.assertTrue(self.order.floatra_disbursed, event)
            self.assertEqual(log.sale_order_id, self.order)

    def test_sandbox_event_is_ignored_by_a_live_key(self):
        payload = {**webhook_payload("order.funded"), "livemode": False}
        log = self.env["floatra.webhook.log"].create({
            "event_id": "evt-sbx", "event_type": "order.funded", "payload": "{}",
        })
        self.order.write({"floatra_disbursed": False})
        self.assertEqual(self._route_event(payload, log, env=self.env), "ignored")
        self.assertFalse(self.order.floatra_disbursed)

    def test_name_collision_with_another_loan_is_ignored(self):
        self.order.write({"name": "S00042", "floatra_loan_id": "loan_other",
                          "floatra_disbursed": False})
        self._route("order.disbursed")
        self.assertFalse(self.order.floatra_disbursed)

    def test_credit_approved_resolves_by_order_reference(self):
        self.order.write({"name": "S00042", "floatra_loan_id": False,
                          "floatra_decision": "waitlisted"})
        self._route("order.credit_approved")
        self.assertEqual(self.order.floatra_decision, "approved")
        self.assertEqual(self.order.floatra_loan_id, "loan_01")
        self.assertEqual(self.order.floatra_repayment_amount, 262500)

    def test_credit_declined(self):
        # A decline carries no loan id: only an order with none matches.
        self.order.write({"name": "S00042", "floatra_loan_id": False})
        self._route("order.credit_declined")
        self.assertEqual(self.order.floatra_decision, "declined")
        self.assertIn("CREDIT_LIMIT_EXCEEDED", self.order.floatra_decision_reason)

    def test_restructured_moves_the_due_date(self):
        self._route("order.restructured")
        self.assertEqual(str(self.order.floatra_repayment_due_date), "2026-10-31")

    def test_note_events_post_to_the_order(self):
        for event in ("order.overdue", "order.repayment_due",
                      "order.repayment_received", "order.defaulted",
                      "order.disbursement_failed"):
            before = len(self.order.message_ids)
            self._route(event)
            self.assertGreater(len(self.order.message_ids), before, event)

    def test_platform_suspended_sets_the_flag(self):
        self._route("platform.suspended")
        self.assertEqual(
            self.env["ir.config_parameter"].sudo().get_param(
                "floatra.platform_suspended",
            ),
            "true",
        )


@tagged("floatra_credit", "-at_install", "post_install")
class TestWebhookHttp(HttpCase):
    SECRET = "test-webhook-secret"

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "floatra_credit.webhook_secret", self.SECRET,
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "floatra_credit.api_key", "live_pk_test",
        )

    def _post(self, body, event_id="evt-http-1", ts=None, signature=None):
        ts = ts or str(int(time.time()))
        raw = body.encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Floatra-Timestamp": ts,
            "X-Floatra-Signature": signature
            or contract.sign_webhook(self.SECRET, ts, raw),
            "X-Floatra-Event-ID": event_id,
        }
        return self.url_open("/floatra/webhook", data=raw, headers=headers)

    def test_core_delivery_is_accepted(self):
        body = fixture("webhook-order.repayment_due")["body"]
        self.assertEqual(self._post(body).status_code, 200)

    def test_replay_is_idempotent(self):
        body = fixture("webhook-order.repayment_due")["body"]
        self._post(body, event_id="evt-dup")
        resp = self._post(body, event_id="evt-dup")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("status"), "duplicate")

    def test_same_signature_under_a_new_event_id_is_ignored(self):
        body = fixture("webhook-order.repayment_due")["body"]
        ts = str(int(time.time()))
        sig = contract.sign_webhook(self.SECRET, ts, body.encode("utf-8"))
        self._post(body, event_id="evt-sig-1", ts=ts, signature=sig)
        resp = self._post(body, event_id="evt-sig-2", ts=ts, signature=sig)
        self.assertEqual(resp.json().get("status"), "duplicate")
        self.assertFalse(self.env["floatra.webhook.log"].search(
            [("event_id", "=", "evt-sig-2")],
        ))

    def test_bad_signature_is_401(self):
        body = fixture("webhook-order.repayment_due")["body"]
        self.assertEqual(self._post(body, signature="0" * 64).status_code, 401)

    def test_stale_timestamp_is_401(self):
        body = fixture("webhook-order.repayment_due")["body"]
        old = str(int(time.time()) - 3600)
        self.assertEqual(self._post(body, ts=old).status_code, 401)

    def test_iso_timestamp_is_401(self):
        body = fixture("webhook-order.repayment_due")["body"]
        iso = datetime.now(timezone.utc).isoformat()
        self.assertEqual(self._post(body, ts=iso).status_code, 401)

    def test_missing_secret_is_401(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "floatra_credit.webhook_secret", "",
        )
        body = fixture("webhook-order.repayment_due")["body"]
        self.assertEqual(self._post(body).status_code, 401)


@tagged("floatra_credit", "-at_install", "post_install")
class TestUndeliveredCron(_OrderCase):
    def test_replays_and_acknowledges(self):
        self.order.write({"floatra_loan_id": "loan_01", "floatra_decision": "approved"})
        page = contract.parse_undelivered(fixture("response-undelivered")["body"])
        with patch_client(get_undelivered_webhooks=page) as p:
            replayed = self.env["floatra.webhook.log"]._floatra_cron_poll_undelivered_webhooks()
            p.return_value.acknowledge_webhook.assert_called_once_with("dlv_disbursed")
        self.assertEqual(replayed, 1)
        self.assertTrue(self.order.floatra_disbursed)


@tagged("floatra_credit", "-at_install", "post_install")
class TestCreditStatusSync(_OrderCase):
    def test_sync_writes_status_tier_limit_and_kyc(self):
        updates = contract.parse_credit_status(
            fixture("response-credit-status-locked")["body"],
        )
        with patch_client(get_credit_status=updates):
            self.env["res.partner"]._floatra_cron_sync_credit_status()
        self.assertEqual(self.partner.floatra_credit_status, "locked")
        self.assertEqual(self.partner.floatra_credit_tier, "T1")
        self.assertEqual(self.partner.floatra_credit_limit, 400000)
        self.assertEqual(self.partner.floatra_kyc_status, "approved")


@tagged("floatra_credit", "-at_install", "post_install")
class TestPartnerActions(TransactionCase):
    def setUp(self):
        super().setUp()
        as_floatra_admin(self)
        self.partner = self.env["res.partner"].create({
            "name": "Mama Nkechi Stores", "mobile": "0803 123 4567",
        })

    def test_onboard_stores_the_floatra_merchant_id(self):
        onboarded = contract.parse_onboard(fixture("response-onboard-created")["body"])
        with patch_client(onboard_merchant=onboarded) as p:
            self.partner.action_floatra_onboard()
            body = p.return_value.onboard_merchant.call_args[0][0]
        self.assertEqual(body["external_merchant_id"], str(self.partner.id))
        self.assertEqual(body["phone"], "+2348031234567")
        self.assertNotIn("owner", body)  # no names, no (valid) email
        self.assertNotIn("bvn", json.dumps(body))
        self.assertEqual(self.partner.floatra_merchant_id, "mer_01")
        self.assertEqual(self.partner.floatra_credit_status, "pending_kyc")
        self.assertEqual(self.partner.floatra_kyc_status, "not_started")

    def test_onboard_requires_a_nigerian_mobile(self):
        self.partner.write({"mobile": False, "phone": "12345"})
        with self.assertRaises(UserError) as ctx:
            self.partner.action_floatra_onboard()
        self.assertIn("Nigerian mobile", str(ctx.exception))

    def test_onboard_refuses_an_enrolled_partner(self):
        self.partner.write({"floatra_merchant_id": "mer_01"})
        with self.assertRaises(UserError):
            self.partner.action_floatra_onboard()

    def test_get_kyc_link_stores_the_link(self):
        from ..services.kyc_link import parse_kyc_retry_response

        self.partner.write({"floatra_merchant_id": "mer_01"})
        outcome = parse_kyc_retry_response(
            fixture("response-kyc-retry-hosted-link")["body"],
        )
        with patch_client(request_kyc_link=outcome) as p:
            self.partner.action_floatra_get_kyc_link()
            p.return_value.request_kyc_link.assert_called_once_with("mer_01")
        self.assertEqual(
            self.partner.floatra_kyc_url,
            "https://floatra.com/kyc/contract-fixture-token",
        )

    def test_get_kyc_link_maps_refusals(self):
        self.partner.write({"floatra_merchant_id": "mer_01"})
        fx = fixture("response-kyc-retry-error-already-verified")
        with patch_client() as p:
            p.return_value.request_kyc_link.side_effect = FloatraAPIError(
                fx["status"], fx["body"],
            )
            with self.assertRaises(UserError) as ctx:
                self.partner.action_floatra_get_kyc_link()
        self.assertIn("already verified", str(ctx.exception).lower())

    def test_onboard_and_request_credit_require_the_floatra_user_group(self):
        from odoo.exceptions import AccessError

        outsider = self.env["res.users"].create({
            "name": "No Floatra Group 2",
            "login": "no-floatra-group-2@example.com",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        with patch_client() as p:
            with self.assertRaises(AccessError):
                self.partner.with_user(outsider).action_floatra_onboard()
            p.return_value.onboard_merchant.assert_not_called()
        order = self.env["sale.order"].create({"partner_id": self.partner.id})
        with self.assertRaises(AccessError):
            order.with_user(outsider).action_request_floatra_credit()

    def test_kyc_link_actions_require_the_floatra_user_group(self):
        from odoo.exceptions import AccessError

        outsider = self.env["res.users"].create({
            "name": "No Floatra Group",
            "login": "no-floatra-group@example.com",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        self.partner.write({"floatra_merchant_id": "mer_01"})
        with self.assertRaises(AccessError):
            self.partner.with_user(outsider).action_floatra_get_kyc_link()


@tagged("floatra_credit", "-at_install", "post_install")
class TestWizards(_OrderCase):
    def setUp(self):
        super().setUp()
        self.order.write({
            "floatra_decision": "approved",
            "floatra_loan_id": "loan_01",
            "floatra_disbursed": True,
        })

    def test_delivery_wizard_posts_to_the_loan(self):
        delivery = contract.parse_delivery(fixture("response-confirm-delivery")["body"])
        wiz = self.env["floatra.delivery.confirm.wizard"].create({
            "sale_order_id": self.order.id, "agent_id": "agt_01",
        })
        with patch_client(confirm_delivery=delivery) as p:
            wiz.action_confirm()
            loan_id, body = p.return_value.confirm_delivery.call_args[0][:2]
        self.assertEqual(loan_id, "loan_01")
        self.assertEqual(body["confirmed_by"]["agent_id"], "agt_01")
        self.assertTrue(self.order.floatra_delivery_confirmed)

    def test_partial_delivery_sends_the_installment(self):
        delivery = contract.parse_delivery(
            fixture("response-confirm-delivery-partial")["body"],
        )
        wiz = self.env["floatra.delivery.confirm.wizard"].create({
            "sale_order_id": self.order.id,
            "delivery_type": "PARTIAL",
            "delivered_amount": 100000,
            "undelivered_handling": "DEFER_DELIVERY",
            "defer_delivery_days": 3,
        })
        with patch_client(confirm_delivery=delivery) as p:
            wiz.action_confirm()
            body = p.return_value.confirm_delivery.call_args[0][1]
        self.assertEqual(body["delivery_installment"], 1)
        self.assertEqual(body["delivered_amount"], "100000.00")
        self.assertFalse(self.order.floatra_delivery_confirmed)
        self.assertEqual(self.order.floatra_delivery_installments, 1)

    def test_repayment_wizard_sends_a_decimal_string(self):
        result = contract.parse_repayment(fixture("response-repayment")["body"])
        wiz = self.env["floatra.cash.repayment.wizard"].create({
            "partner_id": self.partner.id,
            "sale_order_id": self.order.id,
            "amount": 100000,
            "reference": "RCPT-0042",
            "source": "MANUAL_CASH",
        })
        with patch_client(record_repayment=result) as p:
            wiz.action_record()
            loan_id, body = p.return_value.record_repayment.call_args[0]
        self.assertEqual(loan_id, "loan_01")
        self.assertEqual(body["amount"], "100000.00")
        self.assertNotIn("amount_kobo", body)

    def test_restructure_wizard(self):
        result = contract.parse_restructure(fixture("response-restructure")["body"])
        wiz = self.env["floatra.restructure.wizard"].create({
            "sale_order_id": self.order.id,
            "new_tenure_days": "30",
            "reason": "Market closure",
            "lender_approval_reference": "LAR-1",
        })
        with patch_client(restructure_loan=result) as p:
            wiz.action_restructure()
            loan_id, body = p.return_value.restructure_loan.call_args[0]
        self.assertEqual(loan_id, "loan_01")
        self.assertNotIn("idempotency_key", body)
        self.assertEqual(str(self.order.floatra_repayment_due_date), "2026-10-31")

    def test_bank_account_wizard_masks_the_number(self):
        wiz = self.env["floatra.bank.account.wizard"].create({
            "partner_id": self.partner.id,
            "bank_code": "058",
            "account_number": "0123456789",
            "account_name": "Nkechi Okafor",
        })
        with patch_client(set_bank_account={}) as p:
            wiz.action_save()
            merchant_id, body = p.return_value.set_bank_account.call_args[0]
        self.assertEqual(merchant_id, "mer_01")
        self.assertNotIn("bank_name", body)
        joined = " ".join(self.partner.message_ids.mapped("body"))
        self.assertNotIn("0123456789", joined)

    def test_cancel_of_an_unfunded_approval_calls_floatra(self):
        self.order.write({"floatra_disbursed": False})
        with patch_client(cancel_order={}) as p:
            self.order._action_cancel()
            loan_id, body = p.return_value.cancel_order.call_args[0][:2]
        self.assertEqual(loan_id, "loan_01")
        self.assertEqual(body["cancelled_by"], "DISTRIBUTOR")

    def test_cancel_of_a_funded_order_is_refused(self):
        with self.assertRaises(UserError):
            self.order._action_cancel()
