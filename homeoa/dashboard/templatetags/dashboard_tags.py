"""Home page cards: core and module cards contributed to the ``HOME_CARDS`` slot."""

from django import template
from django.utils.html import format_html_join

from homeoa.plugins import slots

register = template.Library()

# Each card is rendered on its own line of the Home side column.
CARD_SEPARATOR = "\n    "


@register.simple_tag(takes_context=True)
def home_cards(context):
    """Render each ``HOME_CARDS`` template with the Home page context, in slot order."""
    association = getattr(context.get("request"), "association", None)
    engine = context.template.engine
    rendered = []
    for name in slots.values(slots.HOME_CARDS, association):
        with context.push():
            rendered.append(engine.get_template(name).render(context))
    # Rendered templates are already safe, so format_html_join leaves them as they are.
    return format_html_join(CARD_SEPARATOR, "{}", ((html,) for html in rendered))
