# -*- coding: utf-8 -*-
"""Hosted KYC link: request body, response parsing, error copy, and the
API client call, against core-rendered contract fixtures.

The fixtures in ``tests/fixtures/contract/`` are written by core's
``src/modules/partner-webhooks/partner-contract-fixtures.spec.ts`` from the
real controller, issuer and exception filter (the two ``request-*`` files are
ours; core validates them against its DTO). A contract change on either side
fails one of the two test suites.

Like ``test_floatra_api_redaction.py``, this loads the pure ``services/``
modules by path (the addon package ``__init__`` imports odoo), so it runs
standalone: ``python3 tests/test_kyc_link.py`` or ``pytest tests/test_kyc_link.py``.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contract_support as support  # noqa: E402

kyc_link = support.services.kyc_link
floatra_api = support.services.floatra_api
floatra_client = support.services.floatra_client
floatra_contract = support.services.floatra_contract
_FIXTURES = support.FIXTURES

# Realistic key shapes, as core issues them.
LIVE_KEY = "live_pk_" + "a1" * 24
SANDBOX_KEY = "sbx_pk_" + "b2" * 16
ROTATED_SANDBOX_KEY = "pk_" + "c3" * 24
BASE_URL = "https://api.floatra.com"


def fixture(name):
    with open(os.path.join(_FIXTURES, f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)


def _response(fx):
    resp = MagicMock()
    resp.status_code = fx["status"]
    resp.ok = 200 <= fx["status"] < 300
    resp.json.return_value = fx["body"]
    return resp


class TestRequestBody(unittest.TestCase):
    def test_live_body_is_empty_and_matches_the_contract_fixture(self):
        body = kyc_link.kyc_retry_body(sandbox=False)
        self.assertEqual(body, {})
        self.assertEqual(body, fixture("request-kyc-retry-live"))

    def test_sandbox_body_matches_the_contract_fixture(self):
        self.assertEqual(
            kyc_link.kyc_retry_body(sandbox=True),
            fixture("request-kyc-retry-sandbox"),
        )

    def test_realm_comes_from_the_key_prefix(self):
        self.assertTrue(kyc_link.is_live_key(LIVE_KEY))
        self.assertFalse(kyc_link.is_sandbox_key(LIVE_KEY))
        for key in (SANDBOX_KEY, ROTATED_SANDBOX_KEY):
            self.assertTrue(kyc_link.is_sandbox_key(key), key)
            self.assertFalse(kyc_link.is_live_key(key), key)
        # The prefixes core never issues are not live either.
        for key in ("pk_live_abc", "pk_sandbox_abc", "", None):
            self.assertFalse(kyc_link.is_live_key(key), key)

    def test_path_is_the_partner_route(self):
        self.assertEqual(
            floatra_contract.partner_path(*kyc_link.kyc_retry_segments("mer_01")),
            "/v1/partner/merchants/mer_01/kyc/retry",
        )


class TestParseResponse(unittest.TestCase):
    def test_hosted_link(self):
        out = kyc_link.parse_kyc_retry_response(
            fixture("response-kyc-retry-hosted-link")["body"],
        )
        self.assertEqual(out.kind, kyc_link.HOSTED_LINK)
        self.assertEqual(out.kyc_url, "https://floatra.com/kyc/contract-fixture-token")
        self.assertEqual(
            out.expires_at, datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(out.sandbox_verified)

    def test_unwrapped_body_also_parses(self):
        data = fixture("response-kyc-retry-hosted-link")["body"]["data"]
        out = kyc_link.parse_kyc_retry_response(data)
        self.assertEqual(out.kind, kyc_link.HOSTED_LINK)

    def test_sandbox_result(self):
        out = kyc_link.parse_kyc_retry_response(
            fixture("response-kyc-retry-sandbox-result")["body"],
        )
        self.assertEqual(out.kind, kyc_link.SANDBOX_RESULT)
        self.assertEqual(out.status, "SUCCESS")
        self.assertEqual(out.verification_tier, "FULLY_VERIFIED")
        self.assertTrue(out.sandbox_verified)

    def test_unrecognised_body_raises(self):
        for body in ({"success": True, "data": {}}, [], "ok", None):
            with self.assertRaises(ValueError):
                kyc_link.parse_kyc_retry_response(body)

    def test_link_without_expiry_raises(self):
        with self.assertRaises(ValueError):
            kyc_link.parse_kyc_retry_response({"kyc_url": "https://x/kyc/t"})


class TestErrorMessages(unittest.TestCase):
    CASES = {
        "response-kyc-retry-error-already-verified": "already verified both",
        "response-kyc-retry-error-not-available": "cannot issue a KYC link",
        "response-kyc-retry-error-rate-limited": "Too many KYC links",
        "response-kyc-retry-error-unavailable": "temporarily unavailable",
        "response-kyc-retry-error-hosted-link-only": "out of date",
        "response-kyc-retry-error-not-found": "does not know this merchant",
        "response-kyc-retry-error-other-platform": "does not belong",
        "response-kyc-retry-error-realm": "other Floatra environment",
    }

    def test_every_contract_error_has_user_copy(self):
        for name, expected in self.CASES.items():
            fx = fixture(name)
            msg = kyc_link.kyc_retry_error_message(fx["status"], fx["body"])
            self.assertIn(expected, msg, name)
            self.assertNotIn("errorCode", msg, name)

    def test_status_fallbacks(self):
        self.assertIn("Too many requests", kyc_link.kyc_retry_error_message(429, "x"))
        self.assertIn("Could not reach", kyc_link.kyc_retry_error_message(0, None))
        self.assertIn("HTTP 418", kyc_link.kyc_retry_error_message(418, {}))


class TestClientRequestKycLink(unittest.TestCase):
    def _client(self, key):
        return floatra_api.FloatraAPIClient(
            base_url=BASE_URL, api_key=key, platform_id="plat-1",
        )

    def _call(self, key, fx_name):
        with patch.object(
            floatra_client.requests,
            "request",
            return_value=_response(fixture(fx_name)),
        ) as req:
            try:
                return self._client(key).request_kyc_link("mer_01"), req
            except floatra_api.FloatraAPIError as err:
                return err, req

    def test_live_key_posts_an_empty_body_and_returns_the_link(self):
        out, req = self._call(LIVE_KEY, "response-kyc-retry-hosted-link")
        method, url = req.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{BASE_URL}/v1/partner/merchants/mer_01/kyc/retry")
        self.assertEqual(json.loads(req.call_args.kwargs["data"]), {})
        self.assertEqual(out.kind, kyc_link.HOSTED_LINK)
        self.assertTrue(out.kyc_url.startswith("https://"))

    def test_rotated_pk_key_is_sandbox_and_posts_the_test_body(self):
        _out, req = self._call(ROTATED_SANDBOX_KEY, "response-kyc-retry-sandbox-result")
        self.assertEqual(
            json.loads(req.call_args.kwargs["data"]),
            fixture("request-kyc-retry-sandbox"),
        )

    def test_sandbox_key_posts_the_test_body_and_returns_the_result(self):
        out, req = self._call(SANDBOX_KEY, "response-kyc-retry-sandbox-result")
        sent = json.loads(req.call_args.kwargs["data"])
        self.assertEqual(sent, fixture("request-kyc-retry-sandbox"))
        self.assertEqual(sent["identifier"], kyc_link.SANDBOX_TEST_IDENTIFIER)
        self.assertEqual(out.kind, kyc_link.SANDBOX_RESULT)

    def test_errors_surface_status_and_code(self):
        err, _req = self._call(LIVE_KEY, "response-kyc-retry-error-already-verified")
        self.assertIsInstance(err, floatra_api.FloatraAPIError)
        self.assertEqual(err.status, 409)
        self.assertEqual(kyc_link.error_code_of(err.body), "KYC_ALREADY_VERIFIED")

    def test_the_old_identity_body_method_is_gone(self):
        self.assertFalse(hasattr(floatra_api.FloatraAPIClient, "retry_kyc"))


if __name__ == "__main__":
    unittest.main()
