"""``{% unit_statements unit as info %}``: the unit page's latest statements."""

from django import template

from homeoa.core import residents_enabled

register = template.Library()

SHOWN = 3


@register.simple_tag
def unit_statements(unit) -> dict:
    from homeoa.statements.statement_models import Statement

    items = list(
        Statement.all_associations.filter(association_id=unit.association_id, unit=unit)
        .select_related("run")
        .order_by("-period_start", "-pk")[:SHOWN]
    )
    return {"items": items, "residents_on": residents_enabled(unit.association)}
