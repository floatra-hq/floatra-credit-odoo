# -*- coding: utf-8 -*-
"""Credit fields + workflow hooks on Odoo's sale.order.

Two things happen here:

  1. Fields track Floatra's decision + lifecycle (decision, amounts, the
     Floatra Order id AND Loan id, funding + delivery flags).

  2. ``action_confirm`` BLOCKS dispatch while credit is approved but the
     platform has not been funded yet, and for a reorder-locked or suspended
     customer. Without this a sales user could ship goods Floatra has not
     paid for.

Floatra ids: ``floatra_order_id`` is the Floatra Order; every lifecycle call
(confirm-delivery, cancel, repayments, restructure) takes the Floatra LOAN id
(``floatra_loan_id``), set from the initiate response or the
``order.credit_approved`` webhook. Orders created before 17.0.0.3.0 only have
the Order id; ``_floatra_loan_id_or_raise`` looks the loan up by the order
reference (``GET /v1/partner/orders/by-external-id/:ref``) the first time.

The HTTP + wire contract lives in services/floatra_client.py and
services/floatra_contract.py; this model only writes fields.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Decisions under which Floatra may still fund the order: dispatch waits.
_AWAITING_FUNDING = ("pending", "approved", "waitlisted")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # ----- Floatra identity for this order -----

    floatra_order_id = fields.Char(
        string="Floatra Order ID",
        readonly=True,
        copy=False,
        help="Floatra's id for the order, set when the credit request "
        "returned APPROVED, DECLINED or WAITLISTED.",
    )
    floatra_loan_id = fields.Char(
        string="Floatra Loan ID",
        readonly=True,
        copy=False,
        index=True,
        help="Floatra's id for the loan funding this order. Delivery, "
        "repayment, cancel and restructure calls use it; loan webhooks "
        "identify the order by it.",
    )
    floatra_loan_request_id = fields.Char(
        string="Floatra Loan Request ID",
        readonly=True,
        copy=False,
        index=True,
        help="Set while the order is waitlisted: Floatra's later approval or "
        "decline names this request.",
    )
    floatra_tenure_days = fields.Selection(
        [("14", "14 days"), ("30", "30 days")],
        string="Floatra Tenure",
        default="14",
        copy=False,
        help="Repayment tenure requested from Floatra (14 or 30 days).",
    )

    # ----- Decision + credit envelope -----

    floatra_decision = fields.Selection(
        [
            ("not_requested", "Not Requested"),
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("declined", "Declined"),
            ("manual_review", "Under Review"),
            ("waitlisted", "Waitlisted"),
        ],
        string="Floatra Decision",
        default="not_requested",
        readonly=True,
        copy=False,
    )
    floatra_decision_reason = fields.Text(
        string="Decision Detail",
        readonly=True,
        copy=False,
        help="For DECLINED: Floatra's reason. For WAITLISTED: queue position.",
    )
    floatra_approved_amount = fields.Monetary(
        string="Approved Amount",
        readonly=True,
        currency_field="currency_id",
        copy=False,
    )
    floatra_interest_amount = fields.Monetary(
        string="Interest",
        readonly=True,
        currency_field="currency_id",
        copy=False,
    )
    floatra_repayment_amount = fields.Monetary(
        string="Total Repayment",
        readonly=True,
        currency_field="currency_id",
        copy=False,
    )
    floatra_repayment_due_date = fields.Date(
        string="Repayment Due",
        readonly=True,
        copy=False,
    )

    # ----- Lifecycle markers -----

    floatra_disbursed = fields.Boolean(
        string="Floatra Funded",
        readonly=True,
        default=False,
        copy=False,
        help="True once Floatra has paid the platform (order.funded / "
        "order.disbursed webhook). Until then the order cannot be confirmed "
        "for dispatch.",
    )
    floatra_delivery_confirmed = fields.Boolean(
        string="Delivery Confirmed (Floatra)",
        readonly=True,
        default=False,
        copy=False,
        help="True once Floatra recorded the delivery (the repayment clock "
        "runs from it).",
    )
    floatra_delivery_installments = fields.Integer(
        string="Partial Deliveries Recorded",
        readonly=True,
        default=0,
        copy=False,
        help="Partial-delivery installments Floatra has accepted (max 3).",
    )

    # ----- Workflow override -----

    def action_confirm(self):
        """Refuse dispatch for a locked/suspended customer, or while credit
        is approved (or pending/waitlisted) but not yet funded."""
        for order in self:
            status = order.partner_id.floatra_credit_status
            if status == "locked":
                raise UserError(
                    _(
                        "Cannot confirm sale order %(name)s — customer "
                        "%(partner)s is currently reorder-locked by Floatra. "
                        "Outstanding balance must be cleared before any new "
                        "order can be dispatched."
                    )
                    % {"name": order.name, "partner": order.partner_id.name},
                )
            if status == "suspended":
                raise UserError(
                    _(
                        "Cannot confirm sale order %(name)s — customer "
                        "%(partner)s is suspended by Floatra. Contact "
                        "Floatra ops to reinstate before dispatching new "
                        "orders."
                    )
                    % {"name": order.name, "partner": order.partner_id.name},
                )
            if (
                order.floatra_decision in _AWAITING_FUNDING
                and not order.floatra_disbursed
            ):
                raise UserError(
                    _(
                        "Cannot confirm sale order %(name)s — Floatra credit "
                        "was requested but the funding has not landed yet. "
                        "Wait for Floatra's funding webhook, or cancel the "
                        "order to dispatch without Floatra credit."
                    )
                    % {"name": order.name},
                )
        return super().action_confirm()

    def _action_cancel(self):
        """Release Floatra's reservation for an approved, unfunded order.

        Approved + funded: refused (the merchant has a loan; use repayments,
        or Floatra ops for a return). Approved + unfunded: ``POST
        /v1/partner/orders/:loan_id/cancel``; a failure is logged on the
        order and does not block the local cancel (Floatra's reconciliation
        catches the orphan).
        """
        for order in self:
            if order.floatra_decision != "approved":
                continue
            if order.floatra_disbursed:
                raise UserError(
                    _(
                        "Cannot cancel sale order %(name)s — Floatra has "
                        "already funded it, so the customer has a loan."
                    )
                    % {"name": order.name},
                )
            order._floatra_cancel_credit()
        return super()._action_cancel()

    def _floatra_cancel_credit(self):
        from ..services.floatra_api import FloatraAPIClient, FloatraAPIError
        from ..services.floatra_contract import cancel_body

        self.ensure_one()
        try:
            client = FloatraAPIClient.for_env(self.env)
            loan_id = self._floatra_loan_id_or_raise(client)
            client.cancel_order(
                loan_id,
                cancel_body(
                    reason_code="OTHER",
                    reason_notes=f"Sale order {self.name} cancelled in Odoo.",
                ),
                idempotency_key=f"odoo-cancel:{self.name}",
            )
        except (FloatraAPIError, UserError) as err:
            message = (
                err.user_message() if isinstance(err, FloatraAPIError)
                else str(err)
            )
            _logger.warning("Floatra cancel failed for %s: %s", self.name, message)
            self.message_post(
                body=_("Floatra cancel failed (reconcile with Floatra): %s")
                % message,
            )
            return
        self.write({"floatra_decision": "not_requested"})
        self.message_post(body=_("Floatra credit cancelled; lender capacity released."))

    # ----- Buttons -----

    def action_request_floatra_credit(self):
        """``POST /v1/partner/orders/initiate`` for this order."""
        self.ensure_one()
        self._floatra_check_user_group()
        partner = self.partner_id
        if not partner.floatra_merchant_id:
            raise UserError(
                _(
                    "Customer %(name)s is not enrolled with Floatra. Use "
                    "Onboard with Floatra on the customer first."
                )
                % {"name": partner.name}
            )
        if partner.floatra_credit_status in ("locked", "suspended"):
            raise UserError(
                _(
                    "Customer %(name)s is %(status)s by Floatra; new credit "
                    "cannot be requested."
                )
                % {"name": partner.name, "status": partner.floatra_credit_status}
            )
        if (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("floatra.platform_suspended", "false")
            .lower()
            == "true"
        ):
            raise UserError(
                _(
                    "Floatra platform is currently suspended. No new credit "
                    "requests will succeed until Floatra ops reinstate it "
                    "(then clear the system parameter "
                    "floatra.platform_suspended)."
                ),
            )

        from ..services.credit_lock import is_locked_failsafe
        from ..services.floatra_api import (
            ContractError,
            FloatraAPIClient,
            FloatraAPIError,
            initiate_body_for_order,
            odoo_values,
        )

        # Tertiary lock layer: a stale or unverified "available" counts as
        # locked until one live lock-status refresh clears it.
        if is_locked_failsafe(partner):
            refreshed = self._floatra_refresh_lock_status()
            if is_locked_failsafe(partner):
                if refreshed:
                    # Floatra confirmed the lock. Refuse with a notification,
                    # not an exception: an exception rolls back the stored
                    # status and check time, so every retry would call
                    # Floatra again. initiate is still never called.
                    return self._floatra_refused_notification(
                        _(
                            "Customer %(name)s is locked by Floatra; new "
                            "credit cannot be requested."
                        )
                        % {"name": partner.name},
                    )
                raise UserError(
                    _(
                        "Floatra reorder-lock cache is stale or locked for "
                        "%(name)s and the live refresh did not clear it. "
                        "Retry in a moment; if the problem persists, contact "
                        "Floatra ops."
                    )
                    % {"name": partner.name},
                )

        try:
            body = initiate_body_for_order(self)
        except ContractError as err:
            raise UserError(_("Cannot request Floatra credit: %s") % err) from err
        try:
            decision = FloatraAPIClient.for_env(self.env).initiate_order(body)
        except FloatraAPIError as err:
            raise UserError(
                _("Credit request failed: %s") % err.user_message(),
            ) from err
        except ValueError as err:
            raise UserError(
                _("Floatra returned an unexpected response: %s") % err,
            ) from err

        self.write(odoo_values(decision.order_updates(), self._fields))
        if decision.decision == "APPROVED":
            self.message_post(
                body=_(
                    "Floatra approved %(amount)s. Waiting for funding before "
                    "dispatch."
                )
                % {"amount": decision.approved_amount},
            )
        else:
            self.message_post(
                body=_("Floatra %(decision)s: %(reason)s")
                % {"decision": decision.decision, "reason": decision.reason()},
            )
        return True

    def action_floatra_restructure(self):
        """Open the restructure wizard (funded loans only)."""
        self.ensure_one()
        if not self.floatra_disbursed:
            raise UserError(_(
                "Floatra hasn't funded this order yet — nothing to restructure.",
            ))
        return {
            "type": "ir.actions.act_window",
            "name": _("Restructure Loan"),
            "res_model": "floatra.restructure.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_sale_order_id": self.id},
        }

    def action_confirm_floatra_delivery(self):
        """Open the delivery-confirmation wizard for this order."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Confirm Floatra Delivery"),
            "res_model": "floatra.delivery.confirm.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_sale_order_id": self.id},
        }

    # ----- Helpers -----

    def _floatra_check_user_group(self):
        """Requesting credit is a Floatra-user action; the header button is
        group-gated, and this holds for RPC callers too."""
        from odoo.exceptions import AccessError

        from .res_partner import FLOATRA_USER_GROUP

        if not self.env.user.has_group(FLOATRA_USER_GROUP):
            raise AccessError(_("Only Floatra users can request Floatra credit."))

    def _floatra_loan_id_or_raise(self, client=None) -> str:
        """The Floatra LOAN id, looked up by order reference when missing."""
        self.ensure_one()
        if self.floatra_loan_id:
            return self.floatra_loan_id
        from ..services.floatra_api import FloatraAPIClient, FloatraAPIError

        try:
            client = client or FloatraAPIClient.for_env(self.env)
            found = client.lookup_order(self.name)
        except FloatraAPIError as err:
            raise UserError(
                _("Could not find this order's Floatra loan: %s")
                % err.user_message(),
            ) from err
        if not found.get("floatra_loan_id"):
            raise UserError(_(
                "Floatra has no loan for this order yet (it may still be "
                "waitlisted).",
            ))
        self.sudo().write({
            "floatra_loan_id": found["floatra_loan_id"],
            "floatra_order_id": found.get("floatra_order_id")
            or self.floatra_order_id,
        })
        return found["floatra_loan_id"]

    def _floatra_refresh_lock_status(self):
        """One live ``GET /v1/partner/merchants/:id/lock-status`` refresh.

        If Floatra is unreachable the cache is left alone and the fail-safe
        declines the request. Returns True when Floatra answered.
        """
        from ..services.floatra_api import FloatraAPIClient, FloatraAPIError
        from ..services.floatra_contract import credit_status_after_lock

        partner = self.partner_id
        if not partner.floatra_merchant_id:
            return False
        try:
            locked = FloatraAPIClient.for_env(self.env).get_lock_status(
                partner.floatra_merchant_id,
            )
        except (FloatraAPIError, ValueError) as err:
            _logger.warning(
                "Floatra lock-status refresh failed for %s: %s",
                partner.floatra_merchant_id,
                err,
            )
            return False
        partner.sudo().write(
            {
                "floatra_credit_status": credit_status_after_lock(
                    partner.floatra_credit_status, locked,
                ),
                "floatra_last_status_check": fields.Datetime.now(),
            },
        )
        return True

    def _floatra_refused_notification(self, message):
        """A sticky danger notification for a refused credit request."""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Floatra: credit not requested"),
                "message": message,
                "type": "danger",
                "sticky": True,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    @api.onchange("partner_id")
    def _onchange_partner_floatra_status(self):
        """Warn on the form when the customer is locked or suspended."""
        if not self.partner_id:
            return None
        status = self.partner_id.floatra_credit_status
        if status == "locked":
            return {
                "warning": {
                    "title": _("Reorder Locked"),
                    "message": _(
                        "%(name)s is currently locked by Floatra. New "
                        "credit cannot be extended until the outstanding "
                        "balance clears."
                    )
                    % {"name": self.partner_id.name},
                },
            }
        if status == "suspended":
            return {
                "warning": {
                    "title": _("Suspended"),
                    "message": _(
                        "%(name)s is suspended by Floatra. Contact ops to "
                        "reinstate before placing this order."
                    )
                    % {"name": self.partner_id.name},
                },
            }
        return None
