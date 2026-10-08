# -*- coding: utf-8 -*-
"""Floatra-credit fields on Odoo's customer model.

Adds an "Floatra" tab to the customer form (defined in
views/res_partner_views.xml) and a set of read-only fields the
webhook receiver + scheduled cron jobs write into. Nothing in this
file initiates a Floatra API call — the file is purely the field
definitions that Odoo's ORM needs.

The matching XML view (`view_partner_form_floatra`) puts these
fields under a notebook page. The `floatra_credit_status` field is
the canonical "can this merchant transact?" signal — every sale
order template + dispatch action keys off it.
"""

from odoo import fields, models

# The connector's user group (security/floatra_security.xml).
FLOATRA_USER_GROUP = "floatra_credit.group_floatra_user"


class ResPartner(models.Model):
    _inherit = "res.partner"

    # ----- Identity (set by webhook after onboarding) -----

    floatra_merchant_id = fields.Char(
        string="Floatra Merchant ID",
        readonly=True,
        copy=False,
        help="Set automatically after Floatra onboarding. Empty until "
        "the partner is registered with Floatra via the Onboard action.",
    )
    floatra_enrolled_at = fields.Datetime(
        string="Enrolled on Floatra",
        readonly=True,
        help="When Floatra accepted the onboarding request. Empty for "
        "partners that have never been enrolled.",
    )

    # ----- Credit state (refreshed by cron + webhooks) -----

    floatra_credit_status = fields.Selection(
        [
            ("not_enrolled", "Not Enrolled"),
            ("pending_kyc", "Pending KYC"),
            ("available", "Credit Available"),
            ("locked", "Reorder Locked"),
            ("suspended", "Suspended"),
        ],
        string="Credit Status",
        default="not_enrolled",
        readonly=True,
        help="High-level state that gates new credit requests. "
        "'locked' blocks reorders until outstanding balances clear; "
        "'suspended' is a permanent halt requiring Floatra ops action.",
    )
    floatra_credit_tier = fields.Char(
        string="Credit Tier",
        readonly=True,
        help="Floatra's tier classification (T0, T1, T2, T3, T4, "
        "RESTRICTED, SUSPENDED). Drives the credit-limit multiplier.",
    )
    floatra_credit_limit = fields.Monetary(
        string="Credit Limit",
        readonly=True,
        currency_field="currency_id",
        help="Maximum outstanding credit the merchant can carry from "
        "Floatra. Refreshed by the credit-status sync cron.",
    )

    # ----- KYC state -----

    floatra_kyc_status = fields.Selection(
        [
            ("not_started", "Not Started"),
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        string="Floatra KYC",
        default="not_started",
        readonly=True,
        help="Identity verification (NIN/BVN + biometric) status. "
        "Must be 'approved' before Floatra will fund any credit.",
    )
    floatra_liveness_verified = fields.Boolean(
        string="Liveness Verified",
        readonly=True,
        help="True when Floatra's biometric face-match has passed.",
    )

    # ----- Hosted KYC link (POST /v1/partner/merchants/:id/kyc/retry) -----
    # Production KYC runs on the merchant's own phone: Floatra returns a
    # single-use link valid for 24h, which staff copy or email to the
    # merchant. Requesting a new link revokes the previous one; the
    # merchant.kyc_completed webhook clears it.

    floatra_kyc_url = fields.Char(
        string="KYC Link",
        readonly=True,
        copy=False,
        # A bearer link (OTP-gated): Floatra users only, not every
        # partner reader.
        groups=FLOATRA_USER_GROUP,
        help="Floatra's hosted KYC link for this merchant. Send it to the "
        "merchant: they open it on their phone, confirm a code sent to "
        "their phone number on Floatra, enter their NIN or BVN and take a "
        "selfie. Single use; requesting a new link revokes this one.",
    )
    floatra_kyc_url_expires_at = fields.Datetime(
        string="KYC Link Expires",
        readonly=True,
        copy=False,
        groups=FLOATRA_USER_GROUP,
        help="After this time the KYC link no longer opens (24h after it "
        "was issued).",
    )
    floatra_kyc_url_active = fields.Boolean(
        string="KYC Link Active",
        compute="_compute_floatra_kyc_url_active",
        groups=FLOATRA_USER_GROUP,
        help="A KYC link is stored and has not expired.",
    )

    def _compute_floatra_kyc_url_active(self):
        now = fields.Datetime.now()
        for partner in self:
            partner.floatra_kyc_url_active = bool(
                partner.floatra_kyc_url
                and partner.floatra_kyc_url_expires_at
                and partner.floatra_kyc_url_expires_at > now
            )

    # ----- Cache freshness (Spec v3 §2.2 tertiary enforcement layer) -----

    floatra_last_status_check = fields.Datetime(
        string="Last Status Check",
        readonly=True,
        help="When the credit-status cron last successfully refreshed "
        "this partner's Floatra fields. The credit-lock fail-safe "
        "treats partners with stale data (older than 15 min) as "
        "locked — better to reject an order than ship goods against "
        "unverified credit state.",
    )

    # ----- Onboarding profile (POST /v1/partner/merchants/onboard) -----
    # Optional extras the connector sends at onboarding. No BVN/NIN: Floatra
    # ignores identity numbers at onboarding; the merchant enters them on
    # the hosted KYC link.

    floatra_business_type = fields.Selection(
        [
            ("RETAILER", "Retailer"),
            ("WHOLESALER", "Wholesaler"),
            ("KIOSK", "Kiosk"),
        ],
        string="Floatra Business Type",
        help="Spec §3.2 business_type. Defaults the merchant's trade "
        "segment for Floatra's underwriting model.",
    )
    floatra_primary_category = fields.Selection(
        [
            ("FMCG", "FMCG"),
            ("ELECTRONICS", "Electronics"),
            ("FASHION", "Fashion"),
            ("AGRICULTURE", "Agriculture"),
            ("PHARMACY", "Pharmacy"),
            ("OTHER", "Other"),
        ],
        string="Floatra Primary Category",
        help="Spec §3.2 category. Default Floatra OrderCategory for "
        "subsequent /v1/orders/initiate calls when the per-order "
        "category isn't supplied.",
    )
    floatra_assigned_agent_id = fields.Char(
        string="Floatra Assigned Agent ID",
        help="Spec §3.2 assigned_agent_id. The field agent the "
        "distributor has assigned to this merchant; Floatra uses it "
        "for fraud aggregation and delivery routing.",
    )
    floatra_owner_first_name = fields.Char(
        string="Owner First Name",
        help="Spec §3.2 owner.first_name. Used verbatim in Floatra's "
        "KYC + bureau reporting payloads.",
    )
    floatra_owner_last_name = fields.Char(
        string="Owner Last Name",
        help="Spec §3.2 owner.last_name.",
    )

    # ----- Cron handlers (data/floatra_cron.xml references these) -----

    def _floatra_cron_sync_credit_status(self):
        """Refresh credit status, tier, limit and KYC for enrolled partners.

        Hourly ir.cron ``cron_credit_status_sync``. Calls
        ``GET /v1/partner/merchants/:id/credit-status`` per partner in
        batches of 50 with a 1 s pause (rate-limit politeness). Webhooks are
        the primary signal; this catches missed ones.
        """
        import logging
        import time

        from ..services.floatra_api import (
            FloatraAPIClient,
            FloatraAPIError,
            odoo_values,
        )

        _logger = logging.getLogger(__name__)

        try:
            client = FloatraAPIClient.for_env(self.env)
        except FloatraAPIError as err:
            _logger.warning("Credit-status sync skipped (config error): %s", err)
            return True

        partners = self.search([("floatra_merchant_id", "!=", False)])
        batch_size = 50
        total_synced = 0
        for batch_start in range(0, len(partners), batch_size):
            for partner in partners[batch_start: batch_start + batch_size]:
                try:
                    updates = client.get_credit_status(partner.floatra_merchant_id)
                except (FloatraAPIError, ValueError) as err:
                    _logger.warning(
                        "Floatra status sync failed for %s: %s",
                        partner.floatra_merchant_id,
                        err,
                    )
                    continue
                updates["floatra_last_status_check"] = fields.Datetime.now()
                partner.sudo().write(odoo_values(updates, partner._fields))
                total_synced += 1
            time.sleep(1)

        _logger.info("Credit-status sync: refreshed %d partner(s)", total_synced)
        return True

    # ----- P2-9: form-action buttons -----
    # The view's <header> exposes these as buttons so distributors
    # can trigger onboard + a hosted KYC link from the partner form without
    # waiting for the next cron tick (or the live webhook). Both
    # methods read the existing res.partner fields the
    # `FloatraAPIClient` already understands — the form is the input
    # surface, not these methods.

    def action_floatra_onboard(self):
        """Onboard this partner with Floatra and store the Floatra merchant id.

        ``POST /v1/partner/merchants/onboard`` with ``external_merchant_id`` =
        this partner's id (idempotent on Floatra's side: a repeat returns the
        existing mapping). The partner then needs KYC (Get KYC Link).
        """
        from odoo import _
        from odoo.exceptions import UserError

        from ..services.floatra_api import (
            ContractError,
            FloatraAPIClient,
            FloatraAPIError,
            onboard_body_for_partner,
        )
        from ..services.floatra_contract import kyc_status_for, parse_iso_utc_or_none

        self.ensure_one()
        self._floatra_check_user_access(_("Only Floatra users can onboard customers with Floatra."))
        if self.floatra_merchant_id:
            raise UserError(_(
                "Partner already onboarded with Floatra "
                "(merchant id %s). Use the credit-status sync to "
                "refresh state instead.",
            ) % self.floatra_merchant_id)
        try:
            body = onboard_body_for_partner(self)
        except ContractError as err:
            raise UserError(_("Cannot onboard with Floatra: %s") % err) from err

        try:
            client = FloatraAPIClient.for_env(self.env)
            onboarded = client.onboard_merchant(body)
        except FloatraAPIError as err:
            raise UserError(
                _("Floatra onboarding failed: %s") % err.user_message(),
            ) from err
        except ValueError as err:
            raise UserError(
                _("Floatra returned an unexpected response: %s") % err,
            ) from err

        enrolled_at = parse_iso_utc_or_none(onboarded.created_at)
        self.sudo().write({
            "floatra_merchant_id": onboarded.floatra_merchant_id,
            "floatra_enrolled_at": (
                enrolled_at.replace(tzinfo=None) if enrolled_at
                else fields.Datetime.now()
            ),
            "floatra_credit_status": "pending_kyc",
            "floatra_kyc_status": kyc_status_for(onboarded.kyc_status),
        })
        return True

    def _floatra_check_user_access(self, message):
        """Floatra actions write with sudo(): Floatra users only. The header
        buttons are group-gated in the view; this holds for RPC callers."""
        from odoo.exceptions import AccessError

        if not self.env.user.has_group(FLOATRA_USER_GROUP):
            raise AccessError(message)

    def _floatra_check_kyc_link_access(self):
        """The KYC-link actions write and reveal a bearer link: Floatra
        users only."""
        from odoo import _

        self._floatra_check_user_access(
            _("Only Floatra users can manage Floatra KYC links."),
        )

    def action_floatra_get_kyc_link(self):
        """Get a hosted KYC link for this merchant from Floatra.

        Calls `POST /v1/partner/merchants/:id/kyc/retry` with NO identity
        data (Floatra refuses BVN/NIN/selfie from a partner in production).
        Live key: stores the returned `kyc_url` + `expires_at` on the
        partner so staff can copy or email it to the merchant; the KYC
        outcome arrives later via the `merchant.kyc_completed` webhook.
        Sandbox key: Floatra returns a simulated result instead, which is
        mirrored onto `floatra_kyc_status`.

        Floatra decides who can verify (409 when both IDs are already
        verified), so a partner whose status reads "approved" with only
        one ID can still get a link to add the other.
        """
        self._floatra_check_kyc_link_access()
        from odoo import _
        from odoo.exceptions import UserError

        from ..services.floatra_api import (
            FloatraAPIClient,
            FloatraAPIError,
        )
        from ..services.kyc_link import (
            HOSTED_LINK,
            kyc_retry_error_message,
        )

        self.ensure_one()
        if not self.floatra_merchant_id:
            raise UserError(_(
                "Partner is not onboarded with Floatra yet. Run "
                "Onboard first.",
            ))

        try:
            client = FloatraAPIClient.for_env(self.env)
        except FloatraAPIError as err:
            raise UserError(_(
                "Floatra is not configured. (%s)",
            ) % err) from err

        try:
            outcome = client.request_kyc_link(self.floatra_merchant_id)
        except FloatraAPIError as err:
            raise UserError(
                kyc_retry_error_message(err.status, err.body),
            ) from err
        except ValueError as err:
            raise UserError(_(
                "Floatra returned an unexpected response: %s",
            ) % err) from err

        if outcome.kind == HOSTED_LINK:
            self.sudo().write({
                "floatra_kyc_url": outcome.kyc_url,
                # Odoo stores naive UTC.
                "floatra_kyc_url_expires_at": outcome.expires_at.replace(
                    tzinfo=None,
                ),
            })
            title = _("KYC link ready")
            message = _(
                "Copy the link from the Floatra tab (or use Email KYC "
                "Link) and send it to the merchant. It works once and "
                "expires in 24 hours.",
            )
            notice_type = "success"
        else:
            self.sudo().write({
                "floatra_kyc_url": False,
                "floatra_kyc_url_expires_at": False,
                "floatra_kyc_status": (
                    "approved" if outcome.sandbox_verified else "pending"
                ),
            })
            title = _("Sandbox KYC")
            message = _("Simulated result: %s") % (
                outcome.status or "",
            )
            notice_type = "info"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": notice_type,
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_floatra_email_kyc_link(self):
        """Open Odoo's email composer with the KYC link for the merchant.

        Staff review and send it themselves; nothing is sent automatically.
        """
        self._floatra_check_kyc_link_access()
        from odoo import _
        from odoo.exceptions import UserError
        from odoo.tools import html_escape

        self.ensure_one()
        if not self.floatra_kyc_url_active:
            raise UserError(_(
                "There is no unexpired KYC link. Use Get KYC Link first.",
            ))
        if not self.email:
            raise UserError(_(
                "This partner has no email address. Copy the KYC link "
                "from the Floatra tab and send it another way.",
            ))
        expires = fields.Datetime.to_string(
            fields.Datetime.context_timestamp(
                self, self.floatra_kyc_url_expires_at,
            ),
        )
        body = _(
            "<p>Hello,</p>"
            "<p>To use credit on your orders, please verify your identity "
            "with Floatra. Open this link on your phone:</p>"
            "<p><a href=\"%(url)s\">%(url)s</a></p>"
            "<p>You will get a code by SMS on the phone number we have for "
            "you, then enter your NIN or BVN and take a selfie. The link "
            "works once and expires on %(expires)s.</p>",
        ) % {
            "url": html_escape(self.floatra_kyc_url),
            "expires": html_escape(expires),
        }
        return {
            "type": "ir.actions.act_window",
            "name": _("Email KYC Link"),
            "res_model": "mail.compose.message",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_model": "res.partner",
                "default_res_ids": [self.id],
                "default_composition_mode": "comment",
                "default_partner_ids": [self.id],
                "default_subject": _("Verify your identity with Floatra"),
                "default_body": body,
            },
        }

    def action_floatra_set_bank_account(self):
        """Open the bank-account wizard pre-filled with this partner.

        P2-9 button. The actual call to Floatra happens in
        `FloatraBankAccountWizard.action_save` so we benefit from
        Odoo's standard form validation for the bank_code +
        account_number fields without having to render a custom
        dialog here.
        """
        from odoo import _
        from odoo.exceptions import UserError

        self.ensure_one()
        if not self.floatra_merchant_id:
            raise UserError(_(
                "Partner is not onboarded with Floatra yet. Run "
                "Onboard first.",
            ))
        return {
            "type": "ir.actions.act_window",
            "name": _("Update Floatra Bank Account"),
            "res_model": "floatra.bank.account.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_partner_id": self.id},
        }
