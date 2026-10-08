# -*- coding: utf-8 -*-
"""O-1 regression: the Floatra API client must redact PII from non-2xx
response bodies before they reach the log OR the UI-surfaced error.

The Odoo module package `__init__` imports odoo, so this test loads the
pure `services/` modules by path (``contract_support``) and runs standalone
(`python3 tests/test_floatra_api_redaction.py`).
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contract_support as support  # noqa: E402

pii_redaction = support.services.pii_redaction
floatra_api = support.services.floatra_api
floatra_client = support.services.floatra_client
redact_pii = pii_redaction.redact_pii
FloatraAPIClient = floatra_api.FloatraAPIClient
FloatraAPIError = floatra_api.FloatraAPIError


class TestRedactPii(unittest.TestCase):
    def test_redacts_known_keys_recursively(self):
        out = redact_pii({
            "external_merchant_id": "M1",
            "owner": {"bvn": "22212345678", "nin": "1234", "first_name": "Ada"},
            "accounts": [{"account_number": "0123456789", "bank_code": "058"}],
        })
        self.assertEqual(out["external_merchant_id"], "M1")  # non-PII preserved
        self.assertEqual(out["owner"]["bvn"], "[REDACTED]")
        self.assertEqual(out["owner"]["nin"], "[REDACTED]")
        self.assertEqual(out["owner"]["first_name"], "Ada")
        self.assertEqual(out["accounts"][0]["account_number"], "[REDACTED]")
        self.assertEqual(out["accounts"][0]["bank_code"], "058")

    def test_redacts_the_hosted_kyc_link(self):
        out = redact_pii({"data": {"kyc_url": "https://floatra.com/kyc/t", "expires_at": "x"}})
        self.assertEqual(out["data"]["kyc_url"], "[REDACTED]")
        self.assertEqual(out["data"]["expires_at"], "x")

    def test_non_container_passthrough(self):
        self.assertEqual(redact_pii("plain error string"), "plain error string")


class TestHandleResponseRedaction(unittest.TestCase):
    def _client(self):
        return FloatraAPIClient(
            base_url="https://gateway.example",
            api_key="k",
            platform_id="p",
        )

    def test_error_body_redacted_in_log_and_in_raised_error(self):
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 422
        resp.json.return_value = {
            "error": "validation_failed",
            "bvn": "22212345678",
            "account_number": "0123456789",
        }

        with self.assertLogs(floatra_client._logger.name, level="WARNING") as cm:
            with self.assertRaises(FloatraAPIError) as ctx:
                self._client()._handle_response(resp, "POST", "/v1/partner/merchants/onboard")

        output = "\n".join(cm.output)
        self.assertIn("[REDACTED]", output)
        self.assertNotIn("22212345678", output)
        self.assertNotIn("0123456789", output)

        # The body carried into the error (surfaced to the desk UI) is redacted.
        body_repr = repr(ctx.exception.body)
        self.assertNotIn("22212345678", body_repr)
        self.assertNotIn("0123456789", body_repr)
        self.assertIn("[REDACTED]", body_repr)


if __name__ == "__main__":
    unittest.main()
