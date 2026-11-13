"""Shared form, message and status components. ``{% load ui %}``; see ``homeoa/ui/README.md``."""

from __future__ import annotations

from django import forms, template
from django.template.loader import render_to_string
from django.utils.html import format_html

register = template.Library()

BADGE_KINDS = frozenset({"", "ok", "warn", "danger", "info", "violet", "accent"})

# Django message level tags -> callout colour classes.
MESSAGE_KINDS = {
    "success": "ok",
    "error": "danger",
    "warning": "warn",
    "info": "info",
    "debug": "info",
}

GROUP_WIDGETS = (forms.RadioSelect, forms.CheckboxSelectMultiple)


def field_ids(bf) -> dict[str, str]:
    """The ids used for a field's help text and error list."""
    base = bf.auto_id or bf.html_name
    return {"help": f"{base}_helptext", "error": f"{base}_error"}


@register.simple_tag
def field(bf, full: bool = False, label: str | None = None, hide_label: bool = False):
    """Render one bound field: label, control, help text and errors, linked for AT.

    The control gets ``aria-describedby`` (help + error ids) and ``aria-invalid`` when
    it has errors. Radio and multi-checkbox widgets render as a ``fieldset``/``legend``.
    Textareas and groups span the full form row; pass ``full=True`` for others.
    """
    if bf.is_hidden:
        return bf.as_widget()
    widget = bf.field.widget
    ids = field_ids(bf)
    described = []
    if bf.help_text:
        described.append(ids["help"])
    if bf.errors:
        described.append(ids["error"])
    attrs = {}
    if described:
        attrs["aria-describedby"] = " ".join(described)
    if bf.errors:
        attrs["aria-invalid"] = "true"
    if isinstance(widget, GROUP_WIDGETS):
        kind = "group"
    elif isinstance(widget, forms.CheckboxInput):
        kind = "check"
    else:
        kind = "control"
    return render_to_string(
        "ui/components/field.html",
        {
            "f": bf,
            "widget_html": bf.as_widget(attrs=attrs),
            "label": label if label is not None else bf.label,
            "hide_label": hide_label,
            "kind": kind,
            "required": bf.field.required and kind != "check",
            "full": full or kind == "group" or isinstance(widget, forms.Textarea),
            "help_id": ids["help"],
            "error_id": ids["error"],
        },
    )


@register.simple_tag
def form_errors(form):
    """Non-field (and hidden-field) errors as an alert callout; empty when there are none."""
    errors = list(form.non_field_errors())
    for bf in form.hidden_fields():
        errors.extend(f"{bf.label}: {e}" for e in bf.errors)
    if not errors:
        return ""
    return render_to_string("ui/components/form_errors.html", {"errors": errors})


@register.simple_tag
def badge(kind: str, label):
    """``{% badge "ok" _("Paid") %}``: a status pill. ``kind`` is one of BADGE_KINDS."""
    kind = kind or ""
    if kind not in BADGE_KINDS:
        raise template.TemplateSyntaxError(f"Unknown badge kind {kind!r}")
    return format_html('<span class="badge{}">{}</span>', f" {kind}" if kind else "", label)


@register.filter
def message_kind(message) -> str:
    """Map a Django message's level tag to a callout colour class."""
    return MESSAGE_KINDS.get(getattr(message, "level_tag", ""), "info")


@register.simple_tag(takes_context=True)
def export_links(context, size="btn-sm btn-ghost"):
    """CSV / XLSX / PDF download links for the current page, keeping its filters (RPT-01)."""
    from django.utils.html import format_html_join
    from django.utils.translation import gettext as _

    request = context.get("request")
    params = request.GET.copy() if request is not None else None
    links = []
    for fmt, label, title in (
        ("csv", "CSV", _("Export as CSV")),
        ("xlsx", "XLSX", _("Export as Excel spreadsheet")),
        ("pdf", "PDF", _("Export as PDF")),
    ):
        if params is not None:
            params["format"] = fmt
            query = params.urlencode()
        else:
            query = f"format={fmt}"
        links.append((size, query, title, fmt, label))
    inner = format_html_join(
        "",
        '<a class="btn {}" href="?{}" aria-label="{}" data-export="{}" download>{}</a>',
        links,
    )
    return format_html(
        '<span class="chips" role="group" aria-label="{}">{}</span>', _("Export"), inner
    )
