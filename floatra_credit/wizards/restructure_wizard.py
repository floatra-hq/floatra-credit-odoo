# -*- coding: utf-8 -*-
"""Restructure an active or overdue Floatra loan.

P2-9 button (paired with the `Restructure Loan` action on
sale.order). Wraps POST /v1/partner/orders/{loan_id}/restructure.

The gateway DTO requires a `lender_approval_reference` so the
wizard does too — Floatra's recovery flow is the source of truth
for those references. The spec allows 7-30 days; we expose 14/30
only because those are the merchant-facing spec set (matches the
existing per-order `floatra_tenure_days` Selection).
"""

from odoo import _, fields, models
from odoo.exceptions import UserError


class FloatraRestructureWizard(models.TransientModel):
    _name = "floatra.restructure.wizard"
    _description = "Restructure a Floatra loan"

    sale_order_id = fields.Many2one(
        "sale.order",
        string="Sale Order",
        required=True,
        domain="[('floatra_disbursed', '=', True)]",
    )
    new_tenure_days = fields.Selection(
        [("14", "14 days"), ("30", "30 days")],
        string="New Tenure",
        required=True,
        default="30",
        help=(
            "Extended tenure. Spec §7.2 caps at 30 days; merchant-"
            "facing set is {14, 30}."
        ),
    )
    reason = fields.Text(
        string="Reason",
        required=True,
        help=(
            "Free-form reason recorded in the loan audit log "
            "(max 500 chars)."
        ),
    )
    lender_approval_reference = fields.Char(
        string="Lender Approval Reference",
        required=True,
        help=(
            "Spec §7.1: mandatory. The reference from the lender's "
            "recovery workflow. Floatra resolves this against a "
            "LenderApprovalRequest row in production; test envs "
            "accept any non-empty string."
        ),
    )
    approved_by_label = fields.Char(
        string="Approved By (label)",
        help=(
            "Optional: human-readable identifier of who in the ERP "
            "triggered the restructure. Stored verbatim in the audit row."
        ),
    )

    def action_restructure(self):
        """``POST /v1/partner/orders/:loan_id/restructure`` + chatter line."""
        self.ensure_one()
        order = self.sale_order_id
        if not order.floatra_disbursed:
            raise UserError(_(
                "Cannot restructure a loan Floatra has not funded yet.",
            ))

        from ..services.floatra_api import (
            ContractError,
            FloatraAPIClient,
            FloatraAPIError,
        )
        from ..services.floatra_contract import restructure_body

        try:
            body = restructure_body(
                new_tenure_days=self.new_tenure_days,
                reason=self.reason,
                lender_approval_reference=self.lender_approval_reference,
                approved_by_label=self.approved_by_label,
            )
        except ContractError as err:
            raise UserError(str(err)) from err

        try:
            client = FloatraAPIClient.for_env(self.env)
            loan_id = order._floatra_loan_id_or_raise(client)
            # Order + new tenure: a double-click replays; a second restructure
            # to another tenure is a new request (Floatra refuses a second
            # restructure anyway).
            result = client.restructure_loan(
                loan_id,
                body,
                idempotency_key=(
                    f"odoo-restructure:{order.name}:{body['new_tenure_days']}"
                ),
            )
        except FloatraAPIError as err:
            raise UserError(
                _("Floatra rejected the restructure: %s") % err.user_message(),
            ) from err

        if result["floatra_repayment_due_date"]:
            order.sudo().write(
                {"floatra_repayment_due_date": result["floatra_repayment_due_date"]},
            )
        order.sudo().message_post(
            body=_(
                "Loan restructured to %(tenure)s days (due %(due)s). "
                "Reason: %(reason)s. Lender approval reference: %(ref)s.",
                tenure=body["new_tenure_days"],
                due=result["floatra_repayment_due_date"],
                reason=body["reason"],
                ref=body["lender_approval_reference"],
            ),
        )
        return {"type": "ir.actions.act_window_close"}
