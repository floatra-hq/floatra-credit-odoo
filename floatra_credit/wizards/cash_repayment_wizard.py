# -*- coding: utf-8 -*-
"""Record an off-Paystack repayment with Floatra.

For distributors who collect repayment via cash at the door,
settlement deduction, or bank transfer — channels Floatra doesn't
see directly. The wizard wraps POST /v1/partner/orders/{loan_id}/repayments.

Floatra deduplicates on `reference`, so even if a sales user
double-submits the same wizard, the DB-level uniqueness on
Repayment.reference catches the dupe.
"""

from odoo import _, fields, models
from odoo.exceptions import UserError


class FloatraCashRepaymentWizard(models.TransientModel):
    _name = "floatra.cash.repayment.wizard"
    _description = "Record manual Floatra repayment"

    partner_id = fields.Many2one(
        "res.partner",
        string="Customer",
        required=True,
        domain="[('floatra_merchant_id', '!=', False)]",
    )
    sale_order_id = fields.Many2one(
        "sale.order",
        string="Sale Order",
        domain="[('partner_id', '=', partner_id), "
        "('floatra_disbursed', '=', True)]",
        required=True,
    )
    amount = fields.Monetary(
        string="Amount Paid",
        required=True,
        currency_field="currency_id",
    )
    reference = fields.Char(
        string="Reference",
        required=True,
        help="Your own deduplication key (settlement batch ID, "
        "receipt number, bank transfer reference). Floatra uses "
        "this to detect double-credits.",
    )
    source = fields.Selection(
        [
            ("SETTLEMENT_DEDUCTION", "Settlement Deduction"),
            ("MANUAL_CASH", "Manual Cash"),
            ("BANK_TRANSFER", "Bank Transfer"),
            ("OTHER", "Other"),
        ],
        string="Source",
        default="MANUAL_CASH",
        required=True,
    )
    collected_at = fields.Datetime(
        string="Collected At",
        required=True,
        default=fields.Datetime.now,
    )
    notes = fields.Text(string="Notes")

    currency_id = fields.Many2one(
        related="sale_order_id.currency_id", readonly=True
    )

    def action_record(self):
        """``POST /v1/partner/orders/:loan_id/repayments`` + chatter line."""
        self.ensure_one()
        from ..services.floatra_api import (
            ContractError,
            FloatraAPIClient,
            FloatraAPIError,
        )
        from ..services.floatra_contract import repayment_body

        try:
            body = repayment_body(
                amount=self.amount,
                reference=self.reference,
                source=self.source,
                collected_at=self.collected_at,
                notes=self.notes,
            )
        except ContractError as err:
            raise UserError(str(err)) from err

        order = self.sale_order_id
        try:
            client = FloatraAPIClient.for_env(self.env)
            loan_id = order._floatra_loan_id_or_raise(client)
            result = client.record_repayment(loan_id, body)
        except FloatraAPIError as err:
            raise UserError(
                _("Floatra repayment recording failed: %s") % err.user_message(),
            ) from err

        message = _(
            "Floatra repayment recorded: %(amount)s via %(source)s "
            "(ref: %(ref)s). Outstanding: %(left)s."
        ) % {
            "amount": result["amount"],
            "source": self.source,
            "ref": body["reference"],
            "left": result["outstanding_balance"],
        }
        if result["overpayment"]:
            message += _(" Overpayment %s kept as credit balance.") % result["overpayment"]
        order.message_post(body=message)
        return {"type": "ir.actions.act_window_close"}
