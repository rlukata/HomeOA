from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def money(value) -> str:
    """``Decimal("-1234.5")`` -> ``-$1,234.50``. Blank for None."""
    if value is None or value == "":
        return ""
    value = Decimal(value)
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


@register.filter
def money_or_blank(value) -> str:
    """Like ``money`` but blank for zero (debit/credit columns)."""
    return money(value) if value else ""
