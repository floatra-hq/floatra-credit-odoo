# -*- coding: utf-8 -*-
"""Modal form that captures delivery details before calling
POST /v1/partner/orders/{loan_id}/confirm-delivery on Floatra.

Triggered from sale.order via `action_confirm_floatra_delivery`. The
sales user fills in the actual delivered amount + agent + optional
location, and the wizard relays the data to Floatra. The repayment
clock starts on Floatra's side as soon as the call returns.

Fields mirror core's ConfirmDeliveryDto. A PARTIAL delivery sends the next
installment number (max 3), the delivered amount and, for the last
installment, what happens to the rest (cancel it, or defer up to 7 days).
"""

from odoo import _, fields, models
from odoo.exceptions import UserError


class FloatraDeliveryConfirmWizard(models.TransientModel):
    _name = "floatra.delivery.confirm.wizard"
    _description = "Floatra delivery confirmation"

    sale_order_id = fields.Many2one(
        "sale.order",
        string="Sale Order",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    delivery_type = fields.Selection(
        [("FULL", "Full Delivery"), ("PARTIAL", "Partial Delivery")],
        string="Delivery Type",
        required=True,
        default="FULL",
    )
    delivered_amount = fields.Monetary(
        string="Delivered Amount",
        currency_field="currency_id",
        help="Only for PARTIAL deliveries: the value delivered in this "
        "installment (at least ₦1,000).",
    )
    undelivered_handling = fields.Selection(
        [
            ("CANCEL_BALANCE", "Cancel undelivered balance"),
            ("DEFER_DELIVERY", "Defer remaining delivery"),
        ],
        string="Undelivered Handling",
        help="Only relevant for PARTIAL. CANCEL_BALANCE reduces the "
        "loan to the delivered amount; DEFER_DELIVERY keeps the "
        "original loan and gives up to 7 days to deliver the rest.",
    )
    defer_delivery_days = fields.Integer(
        string="Defer Days",
        default=3,
        help="Max 7 (Floatra hard cap). Only used when "
        "undelivered_handling = DEFER_DELIVERY.",
    )
    agent_id = fields.Char(
        string="Delivering Agent",
        help="External ID of the agent confirming the delivery — used "
        "for fraud-signal aggregation on Floatra's side.",
    )
    notes = fields.Text(string="Notes")

    # ----- P1-11 phase 2 / ERP spec §3.3 expanded fields -----

    agent_name = fields.Char(
        string="Agent Name",
        help="Spec §3.3 confirmed_by.agent_name. Captured at delivery "
        "time so Floatra's audit logs show the human name without "
        "a downstream join to the Agent table.",
    )
    confirmation_method = fields.Selection(
        [
            ("AGENT_APP", "Agent App"),
            ("ERP_CONSOLE", "ERP Console"),
            ("GPS_VERIFIED", "GPS Verified"),
        ],
        string="Confirmation Method",
        default="ERP_CONSOLE",
        help="Spec §3.3 confirmed_by.confirmation_method. Defaults to "
        "ERP_CONSOLE because the wizard fires from the desk; agents "
        "using a mobile app or scanning a QR with GPS should override.",
    )
    delivery_lat = fields.Float(
        string="Delivery Latitude",
        digits=(9, 6),
        help="Spec §3.3 location.lat. GPS sample taken at the moment "
        "of delivery — used by Floatra's offline-delivery proximity "
        "check (must be within ~50m of the merchant's registered "
        "location).",
    )
    delivery_lng = fields.Float(
        string="Delivery Longitude",
        digits=(9, 6),
    )
    delivery_accuracy_meters = fields.Integer(
        string="GPS Accuracy (m)",
        help="Spec §3.3 location.accuracy_meters. Reported by the GPS "
        "stack; samples worse than ~25m are flagged by Floatra's "
        "delivery dispute review.",
    )
    delivery_location_at = fields.Datetime(
        string="Location Recorded At",
        help="Spec §3.3 location.recorded_at. When the GPS sample was "
        "taken — usually a few seconds before the delivery confirm "
        "tap.",
    )
    proof_of_delivery_url = fields.Char(
        string="Proof of Delivery URL",
        help="Spec §3.3 proof_of_delivery_url. Hosted image / PDF the "
        "agent captured at delivery — typically a signed waybill or "
        "merchant selfie holding goods.",
    )

    currency_id = fields.Many2one(
        related="sale_order_id.currency_id", readonly=True
    )

    def action_confirm(self):
        """``POST /v1/partner/orders/:loan_id/confirm-delivery``, then mirror it."""
        self.ensure_one()
        order = self.sale_order_id
        partial = self.delivery_type == "PARTIAL"
        if partial and not self.delivered_amount:
            raise UserError(_("Enter the delivered amount for a partial delivery."))
        if order.floatra_delivery_confirmed:
            raise UserError(_("Floatra delivery has already been confirmed."))
        if not order.floatra_disbursed:
            raise UserError(_("Floatra has not funded this order yet."))

        from ..services.floatra_api import (
            ContractError,
            FloatraAPIClient,
            FloatraAPIError,
        )
        from ..services.floatra_contract import confirm_delivery_body

        installment = order.floatra_delivery_installments + 1 if partial else None
        try:
            body = confirm_delivery_body(
                delivered_at=fields.Datetime.now(),
                notes=self.notes,
                agent_id=self.agent_id,
                agent_name=self.agent_name,
                confirmation_method=self.confirmation_method,
                lat=self.delivery_lat or None,
                lng=self.delivery_lng or None,
                accuracy_meters=self.delivery_accuracy_meters or None,
                location_recorded_at=self.delivery_location_at or None,
                proof_of_delivery_url=self.proof_of_delivery_url,
                installment=installment,
                delivered_amount=self.delivered_amount if partial else None,
                undelivered_handling=self.undelivered_handling if partial else None,
                defer_delivery_days=self.defer_delivery_days if partial else None,
            )
        except ContractError as err:
            raise UserError(str(err)) from err

        try:
            client = FloatraAPIClient.for_env(self.env)
            loan_id = order._floatra_loan_id_or_raise(client)
            delivery = client.confirm_delivery(
                loan_id,
                body,
                idempotency_key=f"odoo-delivery:{order.name}:{installment or 'full'}",
            )
        except FloatraAPIError as err:
            raise UserError(
                _("Floatra delivery confirmation failed: %s") % err.user_message(),
            ) from err

        values = {"floatra_delivery_confirmed": delivery.delivery_confirmed}
        if installment:
            values["floatra_delivery_installments"] = installment
        if delivery.repayment_due_date:
            values["floatra_repayment_due_date"] = delivery.repayment_due_date
        if delivery.repayment_amount is not None:
            values["floatra_repayment_amount"] = delivery.repayment_amount
        order.write(values)
        if delivery.delivery_confirmed:
            message = _("Floatra delivery confirmed. Repayment clock started.")
        else:
            message = _(
                "Floatra recorded partial delivery #%(n)s; %(left)s still to "
                "deliver."
            ) % {"n": installment, "left": delivery.remaining_undelivered_amount}
        order.message_post(body=message)
        return {"type": "ir.actions.act_window_close"}
