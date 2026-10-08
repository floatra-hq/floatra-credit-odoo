# -*- coding: utf-8 -*-
"""Floatra credentials + behaviour toggles on Settings → Floatra.

Stores the API key + webhook secret as `ir.config_parameter` rows. These
are PLAINTEXT in the database and readable by anyone in Settings /
`base.group_system` (and by direct database access); Odoo does not encrypt
them. Restrict who holds the Settings group and who can read the database.
The fields here are transient (`TransientModel` via the
`res.config.settings` parent) — they read/write to ir.config_parameter
on save.

Never log the api_key value. The API client retrieves it via
env['ir.config_parameter'].sudo().get_param('floatra_credit.api_key')
at call time, not at module-load time.
"""

from odoo import api, fields, models

from ..services.floatra_contract import DEFAULT_BASE_URL, normalize_base_url


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    floatra_api_base_url = fields.Char(
        string="Floatra API Base URL",
        config_parameter="floatra_credit.api_base_url",
        default=DEFAULT_BASE_URL,
        help="Host only, e.g. https://api.floatra.com (the connector adds "
        "/v1/partner/...). The same host serves live and sandbox: the API "
        "key decides which.",
    )
    floatra_api_key = fields.Char(
        string="API Key",
        config_parameter="floatra_credit.api_key",
        help="Bearer token from your Floatra account manager. Treat "
        "as a secret — anyone with this token can act as your platform.",
    )
    floatra_platform_id = fields.Char(
        string="Platform ID",
        config_parameter="floatra_credit.platform_id",
        help="Your Floatra platform slug (e.g. acme-distributors). "
        "Used in the X-Platform-ID request header.",
    )
    floatra_webhook_secret = fields.Char(
        string="Webhook Secret",
        config_parameter="floatra_credit.webhook_secret",
        help="HMAC-SHA256 secret Floatra uses to sign outbound "
        "webhooks. The receiver at /floatra/webhook validates this; "
        "without it, requests are rejected.",
    )
    floatra_enforcement_mode = fields.Selection(
        [
            ("reorder_lock", "Reorder Lock"),
            ("settlement_deduction", "Settlement Deduction"),
            ("hybrid", "Hybrid (Lock + Deduction)"),
        ],
        string="Default Enforcement",
        config_parameter="floatra_credit.enforcement_mode",
        default="reorder_lock",
    )

    @api.model
    def get_floatra_config(self):
        """Return the outbound API-client config as a dict.

        Centralises the ir.config_parameter reads for the API client.

        O-2: the inbound `webhook_secret` is intentionally NOT included —
        it's a distinct secret only the webhook controller needs. Bundling
        both secrets in one dict widened the blast radius if this method's
        result were ever logged or leaked. The webhook controller reads its
        secret via `get_webhook_secret()` instead, so no single call
        materialises both the outbound api_key and the inbound secret.
        """
        params = self.env["ir.config_parameter"].sudo()
        return {
            "base_url": normalize_base_url(
                params.get_param("floatra_credit.api_base_url", DEFAULT_BASE_URL),
            ),
            "api_key": params.get_param("floatra_credit.api_key", ""),
            "platform_id": params.get_param(
                "floatra_credit.platform_id", ""
            ),
            "enforcement_mode": params.get_param(
                "floatra_credit.enforcement_mode", "reorder_lock"
            ),
        }

    @api.model
    def get_floatra_key_is_live(self):
        """Whether the configured API key is a live key (``live_pk_``).

        The webhook receiver needs only the realm (to drop the other realm's
        events), never the key itself.
        """
        from ..services.floatra_contract import is_live_key

        return is_live_key(
            self.env["ir.config_parameter"].sudo().get_param("floatra_credit.api_key", ""),
        )

    @api.model
    def get_webhook_secret(self):
        """Return only the inbound webhook HMAC secret (O-2).

        Separate from get_floatra_config so the webhook controller never
        pulls the outbound api_key it doesn't need, and vice versa.
        """
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("floatra_credit.webhook_secret", "")
        )
