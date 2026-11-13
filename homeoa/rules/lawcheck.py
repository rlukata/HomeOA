"""The yearly law check: ask the active pack's change source which of the sections the
pack relies on changed since its law was checked through, and keep the review list.

Nothing in the app changes by itself: a person updates the pack and releases a new
version. A check that finds changes or cannot run puts a to-do on the System admin's
Home page and emails the System admins.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext

from homeoa import rules
from homeoa.notify import types as ntypes

from . import citations, lawwatch
from .models import LawCheck

logger = logging.getLogger(__name__)

NOTIFY_TYPE = "rules.law_check"


def pack_version(association) -> str:
    """The active pack's version: its plugin's version, empty for the built-in pack."""
    from homeoa import providers
    from homeoa.plugins import registry

    plugin_id = providers.plugin_for("RulePack", association)
    manifest = registry.get(plugin_id) if plugin_id else None
    return manifest.version if manifest else ""


def check(
    association,
    as_of: dt.date,
    source: lawwatch.ChangeSource | None = None,
    since: dt.date | None = None,
) -> LawCheck:
    """Run the check for the association's active pack; the result is not saved.

    ``source`` replaces the pack's own change source (tests, ``law_check --fake``);
    ``since`` replaces the pack's checked-through date."""
    pack = rules.active_pack(association)
    index = citations.index(pack)
    result = LawCheck(
        association=association,
        run_on=as_of,
        pack_id=pack.id,
        pack_version=pack_version(association),
        checked_through=pack.law_checked_through,
        not_checked=[
            {"citation": c, "places": [p.to_json() for p in places]}
            for c, places in index.other.items()
        ],
    )
    source = source or lawwatch.source_for(pack)
    if source is None or not index.sections:
        result.status = LawCheck.Status.NO_SOURCE
        return result
    since = since or pack.law_checked_through or as_of
    try:
        changes = source.affected(list(index.sections), since)
    except lawwatch.LawSourceError as exc:
        result.status = LawCheck.Status.FAILED
        result.reason = str(exc)
        return result
    by_section: dict[str, list[dict]] = {}
    for change in changes:
        by_section.setdefault(change.section, []).append(change.to_json())
    result.findings = [
        {
            "section": section,
            "changes": by_section[section],
            "places": [p.to_json() for p in index.sections.get(section, [])],
        }
        for section in index.sections
        if section in by_section
    ]
    result.status = LawCheck.Status.CHANGES if result.findings else LawCheck.Status.NO_CHANGES
    return result


def run(
    association,
    as_of: dt.date,
    source: lawwatch.ChangeSource | None = None,
    since: dt.date | None = None,
) -> LawCheck:
    """Check, store the result and tell the System admins when they need to look."""
    result = check(association, as_of, source, since)
    result.save()
    if result.needs_review:
        _notify(result)
    return result


def latest(association) -> LawCheck | None:
    return LawCheck.all_associations.filter(association=association).first()


def mark_reviewed(law_check: LawCheck, user) -> LawCheck:
    from homeoa import audit

    law_check.reviewed_at = timezone.now()
    law_check.reviewed_by = user
    law_check.save(update_fields=["reviewed_at", "reviewed_by"])
    audit.record(
        "rules.law_check_reviewed",
        law_check,
        changes={"status": [law_check.status, "reviewed"]},
    )
    return law_check


def admin_recipients(association) -> list:
    """One ``registry.Person`` per active System admin (created for a login with none)."""
    from homeoa.access import permissions as perms
    from homeoa.access.services import role_holders
    from homeoa.registry.models import Person

    out = []
    for user in role_holders(association, perms.SYS_ADMIN):
        person, _created = Person.all_associations.get_or_create(
            association=association,
            user=user,
            defaults={"full_name": user.full_name or user.email, "email": user.email},
        )
        out.append(person)
    return out


def summary(law_check: LawCheck) -> str:
    if law_check.status == LawCheck.Status.FAILED:
        return _("The law check could not run: %(reason)s") % {"reason": law_check.reason}
    count = len(law_check.findings)
    return ngettext(
        "The law changed: %(count)s section the state pack relies on. Review the list.",
        "The law changed: %(count)s sections the state pack relies on. Review the list.",
        count,
    ) % {"count": count}


def _notify(law_check: LawCheck) -> None:
    from homeoa.notify.services import notify

    try:
        notify(
            NOTIFY_TYPE,
            admin_recipients(law_check.association),
            {
                "summary": summary(law_check),
                "sections": [f["section"] for f in law_check.findings],
                "path": reverse("rules:law_changes"),
            },
            as_of=law_check.run_on,
            obj=law_check,
            association=law_check.association,
        )
    except Exception:
        logger.exception("law check email failed for association %s", law_check.association_id)


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=NOTIFY_TYPE,
            label=gettext_lazy("Law changes the rule pack relies on"),
            default_channels=("email", "inapp"),
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% translate 'The law behind the rule pack needs a review' %}"
            ),
            body_template=(
                "{% load i18n %}{{ summary }}\n\n"
                "{% if sections %}{% translate 'Sections:' %} {{ sections|join:', ' }}\n\n"
                "{% endif %}"
                "{% blocktranslate %}Nothing in HomeOA changes by itself. Open Settings > "
                "Law changes to see which pack values cite each section.{% endblocktranslate %}"
            ),
        )
    )


def todo_items(association) -> list[dict]:
    """While the latest check found changes or could not run and nobody marked it
    reviewed: one to-do for whoever can manage settings."""
    from homeoa.access import permissions as perms

    last = latest(association)
    if last is None or not last.needs_review:
        return []
    return [
        {
            "id": f"law-check-{last.pk}",
            "kind": "gov",
            "icon": "alert",
            "title": summary(last),
            "sub": _("Checked on %(date)s") % {"date": last.run_on.isoformat()},
            "url": reverse("rules:law_changes"),
            "action": _("Review"),
            "perm": perms.SETTINGS_MANAGE,
        }
    ]
