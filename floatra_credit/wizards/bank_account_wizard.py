# -*- coding: utf-8 -*-
"""Update the primary repayment bank account a partner has with
Floatra.

P2-9 button (paired with the `Update Bank Account` action on
res.partner). Wraps POST /v1/partner/merchants/{id}/bank-account.

The Floatra side encrypts the account number at rest (AES-256-GCM)
and returns a masked form for the chatter audit; the wizard never
echoes the full account number back into Odoo.
"""

from odoo import _, fields, models
from odoo.exceptions import UserError


class FloatraBankAccountWizard(models.TransientModel):
    _name = "floatra.bank.account.wizard"
    _description = "Update Floatra primary bank account"

    partner_id = fields.Many2one(
        "res.partner",
        string="Customer",
        required=True,
        domain="[('floatra_merchant_id', '!=', False)]",
    )
    bank_code = fields.Char(
        string="Bank Code",
        required=True,
        help=(
            "CBN-assigned 3-10 digit bank code (e.g. 058 for GTBank, "
            "011 for First Bank)."
        ),
    )
    account_number = fields.Char(
        string="Account Number",
        required=True,
        help="Exactly 10 digits — NUBAN.",
    )
    account_name = fields.Char(
        string="Account Name",
        required=True,
        help=(
            "As registered with the bank. Floatra trusts this value "
            "at MVP — Phase 2E adds server-side resolution."
        ),
    )
    bank_name = fields.Char(
        string="Bank Name (display)",
        help="Human-friendly bank name (`First Bank`, `Zenith`).",
    )
    is_primary = fields.Boolean(
        string="Set as primary",
        default=True,
        help=(
            "Unchecked = add a secondary account without disturbing "
            "the primary."
        ),
    )

    def action_save(self):
        """``POST /v1/partner/merchants/:id/bank-account`` + masked chatter line."""
        self.ensure_one()
        partner = self.partner_id
        if not partner.floatra_merchant_id:
            raise UserError(_(
                "Partner is not onboarded with Floatra yet. Run "
                "Onboard first.",
            ))

        from ..services.floatra_api import (
            ContractError,
            FloatraAPIClient,
            FloatraAPIError,
        )
        from ..services.floatra_contract import bank_account_body

        try:
            body = bank_account_body(
                bank_code=self.bank_code,
                account_number=self.account_number,
                account_name=self.account_name,
                bank_name=self.bank_name,
                is_primary=self.is_primary,
            )
        except ContractError as err:
            raise UserError(str(err)) from err

        try:
            client = FloatraAPIClient.for_env(self.env)
            client.set_bank_account(partner.floatra_merchant_id, body)
        except FloatraAPIError as err:
            raise UserError(
                _("Floatra rejected the bank account update: %s")
                % err.user_message(),
            ) from err

        masked = "*" * 8 + body["account_number"][-2:]
        partner.sudo().message_post(
            body=_(
                "Floatra bank account updated: %(name)s "
                "(%(masked)s) at bank %(code)s. Stored encrypted "
                "at rest.",
                name=body["account_name"],
                masked=masked,
                code=body["bank_code"],
            ),
        )
        return {"type": "ir.actions.act_window_close"}
