"""Render the module parts contributed to one place of an owner portal page."""

from django import template
from django.utils.html import format_html_join
from django.utils.safestring import mark_safe

from homeoa.portal import contributions

register = template.Library()


@register.simple_tag(takes_context=True)
def portal_parts(context, place):
    """Each part of ``place`` rendered with the page context, one per line, in slot order."""
    association = getattr(context.get("request"), "association", None)
    engine = context.template.engine
    rendered = []
    for part in contributions.parts(place, association):
        extra = part.context(context) if part.context else {}
        if extra is None:
            continue
        with context.push(**extra):
            html = engine.get_template(part.template).render(context)
        # Drop the template file's final newline so the part sits on its own line. The
        # html is a rendered template, already escaped; trimming it loses the safe mark.
        rendered.append(mark_safe(html.removesuffix("\n")))  # noqa: S308
    return format_html_join("\n", "{}", ((html,) for html in rendered))
