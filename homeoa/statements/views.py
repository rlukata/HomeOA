"""Money > Delinquency (board page) and Settings > Delinquency (step offsets)."""

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from homeoa import rules
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import is_read_only
from homeoa.changes.replay import change_view
from homeoa.core import residents_enabled
from homeoa.core.context import association_today
from homeoa.letters import statutory
from homeoa.registry.models import language_label
from homeoa.ui.nav import get_hub

from . import delinquency as ladder
from .models import DelinquencyAction, DelinquencyCase, Step


def _shell(**extra):
    hub = get_hub("money")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "delinquency"}
        for i, t in enumerate(hub.subtabs)
    ]
    return {"current_hub": hub, "subtabs": tabs, **extra}


class ReasonForm(forms.Form):
    reason = forms.CharField(label=gettext_lazy("Reason"), max_length=300)


class OffsetsForm(forms.Form):
    reason = forms.CharField(label=gettext_lazy("Reason for the change"), max_length=500)

    def __init__(self, *args, resolved=None, **kwargs):
        super().__init__(*args, **kwargs)
        for step, r in (resolved or {}).items():
            self.fields[step] = forms.IntegerField(
                label=r.label,
                min_value=0,
                max_value=3650,
                required=False,
                initial=r.value,
                help_text=gettext_lazy(
                    "Days after the due date. Blank: the rule pack value (off if it has none)."
                ),
            )
        self.order_fields([*(resolved or {}), "reason"])


class ContactsForm(forms.Form):
    """Foreclosure-assistance contacts printed on the statutory delinquency notice."""

    reason = forms.CharField(label=gettext_lazy("Reason for the change"), max_length=500)

    def __init__(self, *args, current=None, **kwargs):
        super().__init__(*args, **kwargs)
        current = current or {}
        for slot, label in statutory.CONTACT_SLOTS:
            self.fields[f"{slot}_phone"] = forms.CharField(
                label=gettext_lazy("%(name)s: telephone") % {"name": label},
                max_length=40,
                required=False,
                initial=current.get(slot, {}).get("phone", ""),
            )
            self.fields[f"{slot}_url"] = forms.URLField(
                label=gettext_lazy("%(name)s: website") % {"name": label},
                max_length=200,
                required=False,
                initial=current.get(slot, {}).get("url", ""),
            )
        self.order_fields([*(f for f in self.fields if f != "reason"), "reason"])

    def contacts(self) -> dict:
        return {
            slot: {
                "phone": self.cleaned_data[f"{slot}_phone"].strip(),
                "url": self.cleaned_data[f"{slot}_url"].strip(),
            }
            for slot, _label in statutory.CONTACT_SLOTS
        }


def _rows(association, as_of):
    configured = ladder.offsets(association)
    cfg = ladder.statutory_config(association, as_of)
    overdue = ladder.overdue_by_unit(association, as_of)
    cases = (
        DelinquencyCase.objects.filter(status=DelinquencyCase.Status.OPEN)
        .select_related("unit")
        .prefetch_related("actions__letter", "actions__batch", "actions__records__letter")
        .order_by("unit__number")
    )
    rows = []
    for case in cases:
        nxt = ladder.upcoming(case, as_of, configured)
        info = overdue.get(case.unit_id, {})
        row = {
            "case": case,
            "balance": info.get("balance"),
            "oldest": min(case.anchor_due_date, info.get("oldest") or case.anchor_due_date),
            "days_late": (
                as_of - min(case.anchor_due_date, info.get("oldest") or case.anchor_due_date)
            ).days,
            "actions": [_with_email(a) for a in case.actions.all()],
            "next_label": Step(nxt[0]).label if nxt else None,
            "next_on": nxt[1] if nxt else None,
        }
        if cfg:
            until = ladder.standstill_until(case, cfg)
            row.update(
                first_deadline=ladder.first_notice_deadline(case, cfg),
                standstill_until=until if until and until >= as_of else None,
                foreclosure_ready=ladder.foreclosure_ready(case, as_of, cfg),
                waiting_translation=[
                    language_label(x)
                    for x in ladder.waiting_translation(case, as_of, configured, cfg)
                ],
            )
        rows.append(row)
    return rows


def _with_email(action):
    action.email_copy = ladder.email_copy_state(action) if action.statutory_text else None
    return action


@require_perm(perms.FINANCE_READ)
def delinquency(request):
    association = request.association
    if is_read_only(request.user, association):
        raise PermissionDenied  # collections work with owner contact data, not the CPA's
    as_of = association_today(association)
    closed = (
        DelinquencyCase.objects.filter(status=DelinquencyCase.Status.CLOSED)
        .select_related("unit")
        .order_by("-closed_on", "-pk")[:20]
    )
    return render(
        request,
        "statements/delinquency.html",
        _shell(
            rows=_rows(association, as_of),
            closed=closed,
            offsets=rules.delinquency_offsets(association),
            statutory=ladder.statutory_config(association, as_of) is not None,
            contacts_missing=statutory.contacts_missing(association),
            as_of=as_of,
            residents_on=residents_enabled(association),
            reason_form=ReasonForm(),
        ),
    )


@require_perm(perms.FINANCE_MANAGE)
@require_POST
def run_now(request):
    association = request.association
    try:
        result = ladder.run(association, association_today(association), user=request.user)
    except ladder.DelinquencyError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            _("Ladder checked: %(steps)d step(s) taken, %(closed)d case(s) closed.")
            % {"steps": len(result.actions), "closed": len(result.closed)},
        )
        if result.held:
            messages.warning(
                request,
                _("%(n)d notice(s) held, waiting for a translation.") % {"n": len(result.held)},
            )
    return redirect("statements:delinquency")


@require_perm(perms.FINANCE_MANAGE)
@require_POST
def hold(request, pk):
    case = get_object_or_404(DelinquencyCase, pk=pk, status=DelinquencyCase.Status.OPEN)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("A reason is required."))
    else:
        on = request.POST.get("on_hold") == "1"
        ladder.set_hold(case, on, reason=form.cleaned_data["reason"], user=request.user)
        messages.success(
            request,
            (_("Unit %(unit)s is on hold.") if on else _("Unit %(unit)s is off hold."))
            % {"unit": case.unit.number},
        )
    return redirect("statements:delinquency")


@require_perm(perms.FINANCE_MANAGE)
@require_POST
def skip(request, pk):
    case = get_object_or_404(DelinquencyCase, pk=pk, status=DelinquencyCase.Status.OPEN)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("A reason is required."))
        return redirect("statements:delinquency")
    try:
        action = ladder.skip_next(
            case,
            association_today(request.association),
            reason=form.cleaned_data["reason"],
            user=request.user,
        )
    except ladder.DelinquencyError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            _("Skipped the %(step)s for unit %(unit)s.")
            % {"step": action.get_step_display().lower(), "unit": case.unit.number},
        )
    return redirect("statements:delinquency")


@require_perm(perms.FINANCE_MANAGE)
@require_POST
def demand_reviewed(request, pk):
    action = get_object_or_404(DelinquencyAction, pk=pk, step=Step.DEMAND)
    ladder.mark_reviewed(action, user=request.user)
    if request.htmx:
        from django.http import HttpResponse

        return HttpResponse(status=204)
    messages.success(request, _("Demand letter marked reviewed."))
    return redirect("statements:delinquency")


@require_perm(perms.FINANCE_MANAGE)
@require_POST
def send_email_copy(request, pk):
    action = get_object_or_404(DelinquencyAction, pk=pk)
    result = ladder.release_email_copy(action, user=request.user)
    for person in result.no_email:
        messages.error(request, _("No email on file for %(person)s.") % {"person": person})
    if result.gated:
        messages.warning(
            request,
            _("Not sent: residents are off, so owners get no email yet."),
        )
    if result.sent:
        messages.success(
            request,
            _("Email copy sent to %(people)s.")
            % {"people": ", ".join(str(p) for p in result.sent)},
        )
    return redirect("statements:delinquency")


@change_view(
    "policy.collections",
    gettext_lazy("Change the collection steps or contacts"),
    manage=perms.POLICY_PROPOSE,
    propose=perms.POLICY_PROPOSE,
    codes={perms.POLICY_PROPOSE},
    always=True,
)
@require_perm(perms.BOARD_SHELL)
def settings_page(request):
    association = request.association
    resolved = rules.delinquency_offsets(association)
    is_contacts = request.method == "POST" and "contacts" in request.POST
    form = OffsetsForm(
        request.POST if request.method == "POST" and not is_contacts else None, resolved=resolved
    )
    contacts_form = ContactsForm(
        request.POST if is_contacts else None, current=statutory.contacts(association)
    )
    status = 200
    if is_contacts:
        if not _can_manage(request):
            messages.error(request, _("You can't change these settings."))
            return redirect("statements:settings")
        if contacts_form.is_valid():
            ladder.set_contacts(
                association, contacts_form.contacts(), reason=contacts_form.cleaned_data["reason"]
            )
            messages.success(request, _("Assistance contacts saved."))
            return redirect("statements:settings")
        status = 400
    elif request.method == "POST":
        if not _can_manage(request):
            messages.error(request, _("You can't change these settings."))
            return redirect("statements:settings")
        if form.is_valid():
            reason = form.cleaned_data["reason"]
            changed = 0
            for step, r in resolved.items():
                new = form.cleaned_data.get(step)
                if new == r.value or (new is None and r.source != "override"):
                    continue
                ladder.set_offset(association, step, new, reason=reason)
                changed += 1
            messages.success(
                request, _("Delinquency steps saved (%(n)d changed).") % {"n": changed}
            )
            return redirect("statements:settings")
        status = 400
    return render(
        request,
        "statements/settings.html",
        {
            "current_hub": None,
            "subtabs": [],
            "offsets": resolved,
            "form": form,
            "contacts_form": contacts_form,
            "can_manage": _can_manage(request),
            **_statutory_context(association),
        },
        status=status,
    )


def _statutory_context(association) -> dict:
    as_of = association_today(association)
    text = rules.resolve(association, ladder.STATUTORY_KEYS["text"], as_of)
    rendered = statutory.render(text.value, association)[0] if text.value else ""
    return {
        "statutory_rule": text,
        "statutory_source": statutory.get(text.value) if text.value else None,
        "statutory_text": rendered,
        "contacts_missing": statutory.contacts_missing(association),
    }


def _can_manage(request) -> bool:
    from homeoa.access.services import has_perm

    return has_perm(request.user, perms.POLICY_PROPOSE, association=request.association)
