"""Autopay on the owner's Pay page: the status line and the link to the autopay page."""

from homeoa.portal.contributions import PortalPart


def pay_context(context):
    """The owner's active autopay enrollment for the unit on the Pay page."""
    from homeoa.payments import autopay

    pay = context.get("pay")
    return {"enrollment": autopay.active_enrollment(pay.period if pay else None)}


PARTS = (
    PortalPart("pay_balance", "payments/_portal_autopay_line.html", pay_context),
    PortalPart("pay_methods", "payments/_portal_autopay_method.html", pay_context),
)
