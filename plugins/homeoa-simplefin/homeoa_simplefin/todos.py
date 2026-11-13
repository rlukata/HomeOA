"""Board to-do items for the bank feed. The Home to-do registry consumes these.

``todo_items(association)`` returns dicts shaped like the prototype's to-dos
(``id``, ``kind``, ``title``, ``sub``, ``url``, ``action``).
"""

from django.urls import reverse
from django.utils.translation import gettext as _

from .models import Connection, ErrorKind, SimpleFinAccount


def todo_items(association) -> list[dict]:
    from homeoa.plugins import services

    if not services.is_enabled(association.pk, "simplefin"):
        return []
    url = reverse("homeoa_simplefin:index")
    items = []
    conn = Connection.all_associations.filter(association=association).first()
    if conn and conn.error_kind in (ErrorKind.AUTH, ErrorKind.PAYMENT):
        if conn.error_kind == ErrorKind.AUTH:
            sub = _("SimpleFIN refused our access. Get a new setup token and reconnect.")
        else:
            sub = _("SimpleFIN says the bridge subscription needs payment.")
        items.append(
            {
                "id": f"simplefin-{conn.error_kind}",
                "kind": "money",
                "title": _("Reconnect the bank feed"),
                "sub": sub,
                "url": url,
                "action": _("Reconnect"),
            }
        )
    if conn and conn.bank_errors:
        items.append(
            {
                "id": "simplefin-bank-errors",
                "kind": "money",
                "title": _("The bank feed reported a problem"),
                "sub": "; ".join(str(e) for e in conn.bank_errors)[:200],
                "url": url,
                "action": _("Review"),
            }
        )
    for link in SimpleFinAccount.all_associations.filter(
        association=association, bank_account__isnull=True, ignored=False
    ):
        items.append(
            {
                "id": f"simplefin-map-{link.pk}",
                "kind": "money",
                "title": _("Choose where %(name)s goes") % {"name": link},
                "sub": _("The bank feed found an account that isn't linked to a bank account."),
                "url": url,
                "action": _("Link account"),
            }
        )
    return items
