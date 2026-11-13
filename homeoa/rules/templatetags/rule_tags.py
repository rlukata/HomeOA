from decimal import Decimal

from django import template
from django.utils.html import format_html
from django.utils.translation import gettext as _

from ..keys import KEYS

register = template.Library()


@register.simple_tag
def rule_badge(resolved):
    """Render a "Verified"/"Unverified" badge for a ``Resolved`` or ``RuleValue``, plus a
    "Confirm with attorney" badge when the value is flagged for attorney review."""
    if getattr(resolved, "verified", False):
        out = format_html('<span class="badge ok">{}</span>', _("Verified"))
    else:
        out = format_html(
            '<span class="badge warn" title="{}">{}</span>',
            _("Not checked against current law. Confirm before relying on it."),
            _("Unverified"),
        )
    if getattr(resolved, "attorney_review", False):
        out += format_html(
            ' <span class="badge info" title="{}">{}</span>',
            _("Depends on the declaration, unclear drafting or case law."),
            _("Confirm with attorney"),
        )
    if getattr(resolved, "clamped", False):
        out += format_html(' <span class="badge danger">{}</span>', _("Capped by statute"))
    return out  # SafeString: every part is built with format_html


def format_value(key, v):
    if v is None:
        return _("Not set")
    if isinstance(v, bool):
        return _("Yes") if v else _("No")
    if isinstance(v, tuple):
        return ", ".join(x.replace("_", " ") for x in v)
    if isinstance(v, Decimal) and KEYS[key].kind == "money":
        return f"${v:,.2f}"
    if isinstance(v, Decimal):
        return f"{v.normalize():f}%"
    return str(v)


@register.filter
def rule_display(resolved):
    return format_value(resolved.key, resolved.value)


@register.filter
def rule_next_display(resolved):
    """The next scheduled pack value, formatted (``Not set`` when it unsets the rule)."""
    return format_value(resolved.key, resolved.next_value)


@register.filter
def rule_requested_display(resolved):
    return format_value(resolved.key, resolved.requested)
