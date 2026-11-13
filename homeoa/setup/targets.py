"""What each setup answer sets, once the board adopts the section's resolution.

A question's ``target`` is either ``rule:<key>`` (an association override of that rule)
or one of :data:`TARGETS`. Answers that belong together are applied together: the
``late_fees.*`` answers make one new late fee policy, the ``fines.*`` answers one new
fine schedule version. Every write cites the resolution in its audit reason.

Targets about an optional module are applied by that module: it registers
``(target or prefix ending in ".", applier)`` in the wizard sections slot, and the
applier is called as ``applier(association, answers, resolution=, on=, reason=)`` with
the answers it matches.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.utils.translation import gettext as _

from homeoa import audit, rules
from homeoa.plugins import slots
from homeoa.rules.keys import KEYS

RULE_PREFIX = "rule:"
# ``module:<id>`` answers switch optional modules, all together.
MODULE_PREFIX = "module:"

# target -> answer kind it accepts
TARGETS = {
    "late_fees.flat_amount": "money",
    "late_fees.grace_days": "days",
    "late_fees.interest_apr": "percent",
    "payments.allocation": "choice",
    "fines.first_amount": "money",
    "fines.repeat_amount": "money",
    "fines.continuing_amount": "money",
    "fines.continuing_days": "days",
    "bills.two_approvals_from": "money",
    "maintenance.oneoff_limit": "money",
    "resale.fee": "money",
    "consent.adopt": "bool",
}

# ``payments.allocation`` answers
ALLOCATIONS = {
    "assessments_first": (("assessment", "late_fee", "interest", "other"), "by_type"),
    "fees_first": (("late_fee", "interest", "assessment", "other"), "by_type"),
    "oldest_first": (("assessment", "late_fee", "interest", "other"), "oldest_first"),
}

RESALE_FEE_SETTING = "resale_fee"
CONSENT_SETTING = "enotice_consent_adopted"
BILL_RULE_PREFIX = "Setup: "
# The category a first fine schedule from setup applies to when there are none yet.
GENERAL_CATEGORY = "General rules violation"


def known(target: str) -> bool:
    if target.startswith(RULE_PREFIX):
        return target[len(RULE_PREFIX) :] in KEYS
    if target.startswith(MODULE_PREFIX):
        from homeoa.plugins import registry

        return target[len(MODULE_PREFIX) :] in {m.id for m in registry.all_modules()}
    return target in TARGETS


def apply(association, answers: dict, *, resolution, title: str, change_request=None) -> None:
    """Apply ``answers`` ({target: JSON value}) for an adopted ``resolution``. Module
    switches are applied as one set, linked to ``change_request``, or not at all."""
    on = resolution.adopted_on
    reason = _("Board resolution %(number)s: %(title)s") % {
        "number": resolution.number,
        "title": title,
    }
    for target, raw in answers.items():
        if target.startswith(RULE_PREFIX):
            rules.set_override(association, target[len(RULE_PREFIX) :], raw, reason=reason)
    wanted = {
        k[len(MODULE_PREFIX) :]: bool(v) for k, v in answers.items() if k.startswith(MODULE_PREFIX)
    }
    if wanted:
        from homeoa.audit.context import get_audit_actor
        from homeoa.plugins import modules

        # The switches record who adopted the resolution, as the audit log does.
        adopter = get_audit_actor().current_user
        if not getattr(adopter, "is_authenticated", False):
            adopter = None
        modules.set_modules(association, wanted, user=adopter, change_request=change_request)
    late = {k.split(".", 1)[1]: v for k, v in answers.items() if k.startswith("late_fees.")}
    if late:
        _late_fee_policy(association, late, resolution=resolution, on=on)
    if "payments.allocation" in answers:
        order, mode = ALLOCATIONS[answers["payments.allocation"]]
        rules.set_override(association, "payments.allocation_order", list(order), reason=reason)
        rules.set_override(association, "payments.allocation_mode", mode, reason=reason)
    if "bills.two_approvals_from" in answers:
        _bill_rules(association, Decimal(answers["bills.two_approvals_from"]))
    if "resale.fee" in answers:
        _setting(association, RESALE_FEE_SETTING, str(answers["resale.fee"]), reason)
    if answers.get("consent.adopt"):
        from homeoa.registry import consent

        _setting(
            association,
            CONSENT_SETTING,
            {"version": consent.VERSION, "resolution": resolution.number, "on": on.isoformat()},
            reason,
        )
    for match, applier in slots.values(slots.WIZARD_SECTIONS, association):
        mine = {
            k: v
            for k, v in answers.items()
            if (k.startswith(match) if match.endswith(".") else k == match)
        }
        if mine:
            applier(association, mine, resolution=resolution, on=on, reason=reason)


def _setting(association, key, value, reason) -> None:
    data = dict(association.settings or {})
    old = data.get(key)
    data[key] = value
    association.settings = data
    association.save(update_fields=["settings"])
    audit.record("association.settings", association, changes={key: [old, value]}, message=reason)


def resale_fee(association) -> Decimal | None:
    raw = (association.settings or {}).get(RESALE_FEE_SETTING)
    return Decimal(raw) if raw not in (None, "") else None


def _late_fee_policy(association, late: dict, *, resolution, on: dt.date) -> None:
    """A new active late fee policy from the answers; earlier policies stop."""
    from homeoa.billing.latefee_models import LateFeePolicy
    from homeoa.billing.services.latefees import default_policy_kwargs

    kwargs = default_policy_kwargs(association)
    kwargs["name"] = _("Late fees (resolution %(number)s)") % {"number": resolution.number}
    if "flat_amount" in late:
        kwargs["flat_amount"] = Decimal(late["flat_amount"])
    if "grace_days" in late:
        kwargs["grace_days"] = int(late["grace_days"])
    if "interest_apr" in late:
        kwargs["interest_apr"] = Decimal(late["interest_apr"])
        kwargs["interest_adopted_on"] = on
        kwargs["interest_resolution"] = _("Board resolution %(number)s") % {
            "number": resolution.number
        }
    for old in LateFeePolicy.all_associations.filter(association=association, active=True):
        old.active = False
        old.save()
    from homeoa.core import status

    start = status.setup_start(association) or on
    LateFeePolicy.all_associations.create(association=association, effective_start=start, **kwargs)


def _bill_rules(association, threshold: Decimal) -> None:
    """Two approvals for bills of ``threshold`` or more, one below it (0: every bill needs
    two). Replaces the rules an earlier setup made; other rules stay, after these."""
    from homeoa.ap.approvals.models import ApprovalRule

    for old in ApprovalRule.all_associations.filter(
        association=association, name__startswith=BILL_RULE_PREFIX, active=True
    ):
        old.active = False
        old.save()
    if threshold > 0:
        ApprovalRule.all_associations.create(
            association=association,
            name=BILL_RULE_PREFIX + _("bills under $%(amount)s") % {"amount": f"{threshold:,.2f}"},
            priority=10,
            max_amount=threshold,
            required_approvals=1,
        )
    ApprovalRule.all_associations.create(
        association=association,
        name=BILL_RULE_PREFIX + _("bills of $%(amount)s or more") % {"amount": f"{threshold:,.2f}"},
        priority=11,
        min_amount=threshold,
        required_approvals=2,
    )
