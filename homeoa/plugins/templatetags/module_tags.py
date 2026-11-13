"""``{% if "work_orders"|module_on:request.association %}``: hide links into a module that
is off."""

from django import template

from homeoa.plugins import slots

register = template.Library()


@register.filter
def module_on(module_id, association):
    return slots.module_on(module_id, association)
