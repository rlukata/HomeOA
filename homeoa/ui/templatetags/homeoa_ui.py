from django import template
from django.templatetags.static import static
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def icon(name: str, css: str = "i"):
    """Inline reference to a symbol in the icon sprite (decorative, hidden from AT)."""
    return format_html(
        '<svg class="{}" aria-hidden="true" focusable="false"><use href="{}#i-{}"></use></svg>',
        css,
        static("homeoa/icons.svg"),
        name,
    )
