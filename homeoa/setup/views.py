"""Settings > Association setup: the policy sections and their questions."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa import rules
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm

from . import services


@require_perm(perms.BOARD_SHELL)
def index(request):
    association = request.association
    st = services.states(association)
    return render(
        request,
        "setup/index.html",
        {
            "pack": rules.active_pack(association),
            "states": st,
            "done": sum(1 for s in st if s.done),
            "can_propose": has_perm(request.user, perms.POLICY_PROPOSE, association=association),
        },
    )


def _range(q, value):
    """The slider and number box of a number answer with legal bounds."""
    law = q.law
    if not services.is_ranged(q):
        return None
    step = "1" if q.kind == "days" else "0.01"
    return {
        "min": law.minimum,
        "max": law.maximum,
        "slider_min": law.minimum if law.minimum is not None else 0,
        "min_text": services.shown_amount(q, law.minimum) if law.minimum is not None else "",
        "max_text": services.shown_amount(q, law.maximum) if law.maximum is not None else "",
        "suggested_text": (
            services.shown_amount(q, q.suggested) if q.suggested is not None else ""
        ),
        "step": step,
        "value": "" if value is None else value,
    }


def _ruled_out(q, value):
    law = q.law
    return (
        law is not None
        and law.allowed is not None
        and not any(str(a) == str(value) for a in law.allowed)
    )


def _rows(s, values, errors):
    rows = []
    for i, q in enumerate(s.questions):
        name = services.field_name(i)
        value = values.get(q.target)
        listed = any(str(c.value) == str(value) for c in q.choices)
        rows.append(
            {
                "q": q,
                "name": name,
                "error": errors.get(name, ""),
                "choices": [
                    {
                        "value": str(c.value),
                        "label": c.label,
                        "explain": c.explain,
                        "suggested": str(c.value) == str(q.suggested),
                        "checked": str(c.value) == str(value),
                        "ruled_out": _ruled_out(q, c.value),
                    }
                    for c in q.choices
                ],
                "other_checked": q.other and not listed and value not in (None, ""),
                "value": "" if value is None else value,
                "feature": services.is_feature(q),
                "fixed": services.shown(q, value) if services.is_fixed(q) else None,
                "range": _range(q, value),
                "law": q.law,
            }
        )
    return rows


def _always_on(association):
    """The core parts the law requires, with their citations. Modules the pack requires
    are Features questions set by law instead."""
    pack = rules.active_pack(association)
    return [
        {"label": f.label, "citation": f.citation, "verified": f.verified}
        for f in pack.required_features
    ]


@require_perm(perms.BOARD_SHELL)
def section(request, key):
    association = request.association
    s = services.section(association, key)
    if s is None:
        raise Http404
    can_propose = has_perm(request.user, perms.POLICY_PROPOSE, association=association)
    st = services.state(association, s)
    values, errors = services.initial(association, s), {}
    refusals = []
    if request.method == "POST":
        if not can_propose:
            raise PermissionDenied
        answers, errors = services.parse(s, request.POST)
        values = {**values, **answers}
        if not errors:
            try:
                change = services.propose(association, s, answers, user=request.user)
            except services.SetupError as exc:
                if s.key == services.FEATURES and exc.reasons:
                    refusals = exc.reasons
                else:
                    messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    _(
                        "Draft resolution %(number)s is ready. The settings apply when the "
                        "board adopts it."
                    )
                    % {"number": change.resolution.number},
                )
                return redirect(reverse("governance:resolution", args=[change.resolution_id]))
        else:
            messages.error(request, _("Please fix the highlighted answers."))
    return render(
        request,
        "setup/section.html",
        {
            "section": s,
            "state": st,
            "rows": _rows(s, values, errors),
            "can_propose": can_propose and st.pending is None,
            "refusals": refusals,
            "always_on": _always_on(association) if s.key == services.FEATURES else None,
        },
        status=400 if errors or refusals else 200,
    )
