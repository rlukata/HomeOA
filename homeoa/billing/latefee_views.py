"""Money > Dues & ledger: late fee policy, assessed fees and waivers, special assessments."""

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy
from django.views.decorators.http import require_POST

from homeoa import rules
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_any
from homeoa.changes.forms import add_resolution_field
from homeoa.changes.replay import change_view, date_value, money_change, money_value
from homeoa.core import status
from homeoa.core.context import association_today
from homeoa.ledger.models import Account, Fund

from .latefee_models import LateFee, LateFeePolicy, SpecialAssessment, SpecialInstallment
from .services import BillingError, latefees, special
from .views import _shell


class PolicyForm(forms.ModelForm):
    class Meta:
        model = LateFeePolicy
        fields = [
            "name",
            "active",
            "grace_days",
            "flat_amount",
            "percent",
            "interest_apr",
            "interest_adopted_on",
            "interest_resolution",
            "minimum_balance",
            "applies_to",
            "effective_start",
            "receivable_account",
            "fee_income_account",
            "interest_income_account",
            "fund",
        ]
        widgets = {
            "effective_start": forms.DateInput(attrs={"type": "date"}),
            "interest_adopted_on": forms.DateInput(attrs={"type": "date"}),
            "applies_to": forms.Textarea(attrs={"rows": 1}),
        }

    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.association = association or self.instance.association
        accounts = Account.all_associations.filter(association=association, active=True)
        self.fields["receivable_account"].queryset = accounts.filter(in_unit_subledger=True)
        self.fields["fee_income_account"].queryset = accounts.filter(type="income")
        self.fields["interest_income_account"].queryset = accounts.filter(type="income")
        self.fields["fund"].queryset = Fund.all_associations.filter(
            association=association, active=True
        )


class WaiveForm(forms.Form):
    reason = forms.CharField(label=_lazy("Reason"), max_length=300)


class SpecialForm(forms.Form):
    name = forms.CharField(label=_lazy("Name"), max_length=100)
    description = forms.CharField(
        label=_lazy("Description"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    total = forms.DecimalField(
        label=_lazy("Total"), max_digits=12, decimal_places=2, min_value=0.01
    )
    method = forms.ChoiceField(
        label=_lazy("Split"), choices=SpecialAssessment._meta.get_field("method").choices
    )
    installments = forms.IntegerField(
        label=_lazy("Installments"), min_value=1, max_value=60, initial=1
    )
    first_due = forms.DateField(
        label=_lazy("First due date"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_lazy("Later installments fall due on the same day of each following month."),
    )
    operating_fund = forms.ModelChoiceField(
        label=_lazy("Operating fund"), queryset=Fund.objects.none()
    )
    for_reserves = forms.BooleanField(
        label=_lazy("Designated for reserves"),
        required=False,
        help_text=_lazy(
            "Owners are billed in operating; each posted installment then moves to the "
            "reserve fund."
        ),
    )
    receivable_account = forms.ModelChoiceField(
        label=_lazy("Receivable account"), queryset=Account.objects.none()
    )
    income_account = forms.ModelChoiceField(
        label=_lazy("Income account"), queryset=Account.objects.none()
    )

    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, **kwargs)
        # A special assessment is approved by a board resolution.
        add_resolution_field(self, association)
        accounts = Account.all_associations.filter(association=association, active=True)
        self.fields["operating_fund"].queryset = Fund.all_associations.filter(
            association=association, active=True, is_reserve=False
        )
        self.fields["receivable_account"].queryset = accounts.filter(in_unit_subledger=True)
        self.fields["income_account"].queryset = accounts.filter(type="income")


@require_perm(perms.FINANCE_READ)
def late_fees(request):
    association = request.association
    ctx = _shell(_("Late fees"))
    ctx.update(
        {
            "policies": LateFeePolicy.objects.all(),
            "cap": rules.resolve(association, "late_fees.cap"),
            "fees": LateFee.objects.select_related("charge", "charge__unit", "source_charge")[:100],
            "waive_form": WaiveForm(),
        }
    )
    return render(request, "billing/late_fees.html", ctx)


@change_view(
    "policy.late_fees",
    _lazy("Change the late fee policy"),
    manage=perms.POLICY_PROPOSE,
    propose=perms.POLICY_PROPOSE,
    codes={perms.POLICY_PROPOSE},
    always=True,
)
@require_perm(perms.POLICY_PROPOSE)
def policy_edit(request, pk=None):
    association = request.association
    instance = get_object_or_404(LateFeePolicy, pk=pk) if pk else None
    if instance is None:
        instance = LateFeePolicy(
            association=association,
            effective_start=status.setup_start(association) or association_today(association),
            **latefees.default_policy_kwargs(association),
        )
    form = PolicyForm(request.POST or None, instance=instance, association=association)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Late fee policy saved."))
        return redirect("billing:late_fees")
    ctx = _shell(_("Late fee policy"))
    ctx.update(
        {
            "form": form,
            "title": _("Late fee policy"),
            "cap": rules.resolve(association, "late_fees.cap"),
            "rate": rules.resolve(association, "interest.annual_rate"),
        }
    )
    return render(request, "billing/policy_form.html", ctx)


def _describe_waiver(request, pk):
    fee = LateFee.objects.select_related("charge__unit").filter(pk=pk).first()
    label = f"${fee.charge.amount:,.2f}, Unit {fee.charge.unit.number}" if fee else f"#{pk}"
    return (
        _("Waive the late fee of %(fee)s") % {"fee": label},
        [(_("Late fee"), label), (_("Reason"), request.POST.get("reason", ""))],
    )


# Waivers always need a second board member.
@money_change(
    "money.late_fee_waiver", _lazy("Waive a late fee"), always=True, describe=_describe_waiver
)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def waive(request, pk):
    fee = get_object_or_404(LateFee, pk=pk)
    form = WaiveForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Give a reason for the waiver."))
        return redirect("billing:late_fees")
    try:
        latefees.waive(
            fee,
            reason=form.cleaned_data["reason"],
            user=request.user,
            on=association_today(request.association),
        )
    except BillingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Fee waived and reversed in the ledger."))
    return redirect("billing:late_fees")


@require_perm(perms.FINANCE_READ)
def specials(request):
    ctx = _shell(_("Special assessments"))
    ctx["specials"] = SpecialAssessment.objects.prefetch_related("installments")
    return render(request, "billing/specials.html", ctx)


def _describe_special(request):
    p = request.POST
    summary = _("Special assessment %(name)s: %(total)s") % {
        "name": p.get("name", ""),
        "total": money_value(p.get("total")),
    }
    details = [
        (_("Name"), p.get("name", "")),
        (_("Total"), money_value(p.get("total"))),
        (_("Installments"), p.get("installments", "")),
        (_("First due"), date_value(p.get("first_due"))),
        (_("Description"), p.get("description", "")),
    ]
    return summary, [(k, v) for k, v in details if v]


@money_change(
    "money.special_assessment",
    _lazy("New special assessment"),
    by_resolution=True,
    describe=_describe_special,
)
@require_perm(perms.FINANCE_MANAGE)
def special_new(request):
    association = request.association
    form = SpecialForm(
        request.POST or None,
        association=association,
        initial=special.default_special_kwargs(association),
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            sa = special.create_special(
                association,
                name=d["name"],
                description=d["description"],
                total=d["total"],
                method=d["method"],
                installments=special.monthly_dates(d["first_due"], d["installments"]),
                operating_fund=d["operating_fund"],
                for_reserves=d["for_reserves"],
                receivable_account=d["receivable_account"],
                income_account=d["income_account"],
                created_by=request.user,
            )
        except BillingError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Special assessment saved. Review it, then post."))
            return redirect("billing:special_detail", pk=sa.pk)
    ctx = _shell(_("New special assessment"))
    ctx.update({"form": form, "title": _("New special assessment")})
    return render(request, "billing/special_form.html", ctx)


@require_perm(perms.FINANCE_READ)
def special_detail(request, pk):
    sa = get_object_or_404(SpecialAssessment, pk=pk)
    installments = list(sa.installments.all())
    problem = ""
    try:
        rows = sorted(
            special.preview(sa).values(),
            key=lambda r: (r["unit"].building, _nat(r["unit"].number)),
        )
    except BillingError as exc:
        rows, problem = [], str(exc)
    ctx = _shell(sa.name)
    ctx.update(
        {
            "special": sa,
            "installments": installments,
            "rows": rows,
            "problem": problem,
            "ratification_required": special.ratification_required(request.association),
            "may_post": special.may_post(sa),
            "window": special.ratification_window(sa),
            "ratify_form": RatifyForm(
                initial={
                    "summary_sent_on": sa.summary_sent_on,
                    "meeting_on": sa.ratification_meeting_on,
                    "outcome": sa.ratification or "ratified",
                }
            ),
            "emergency_rule": special.emergency_rule(request.association),
            "emergency_vote": BOARD_VOTE_LABELS.get(
                special.emergency_rule(request.association).value, ""
            ),
            "emergency_form": EmergencyForm(
                initial={"decided_on": association_today(request.association)}
            ),
            "can_ratify": has_any(
                request.user,
                (perms.FINANCE_MANAGE, perms.GOVERNANCE_MANAGE),
                association=request.association,
            ),
        }
    )
    return render(request, "billing/special_detail.html", ctx)


class RatifyForm(forms.Form):
    summary_sent_on = forms.DateField(
        label=_lazy("Summary sent to owners on"), widget=forms.DateInput(attrs={"type": "date"})
    )
    meeting_on = forms.DateField(
        label=_lazy("Ratification meeting on"), widget=forms.DateInput(attrs={"type": "date"})
    )
    outcome = forms.ChoiceField(
        label=_lazy("Result"),
        choices=[
            ("ratified", _lazy("Ratified by owners")),
            ("rejected", _lazy("Rejected by owners")),
        ],
    )


@require_POST
@require_perm(perms.FINANCE_MANAGE, perms.GOVERNANCE_MANAGE)
def special_ratify(request, pk):
    """Record the owners' ratification decision (Treasurer or Secretary)."""
    sa = get_object_or_404(SpecialAssessment, pk=pk)
    form = RatifyForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Enter both dates and the result."))
        return redirect("billing:special_detail", pk=sa.pk)
    d = form.cleaned_data
    try:
        special.record_ratification(
            sa,
            summary_sent_on=d["summary_sent_on"],
            meeting_on=d["meeting_on"],
            outcome=d["outcome"],
        )
    except BillingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Ratification recorded."))
    return redirect("billing:special_detail", pk=sa.pk)


BOARD_VOTE_LABELS = {
    "majority": _lazy("a majority"),
    "two_thirds": _lazy("a two-thirds"),
    "unanimous": _lazy("a unanimous"),
}


class EmergencyForm(forms.Form):
    kind = forms.ChoiceField(
        label=_lazy("Kind of emergency"),
        choices=SpecialAssessment._meta.get_field("emergency_kind").choices,
    )
    reason = forms.CharField(
        label=_lazy("What happened"),
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_lazy("Sent to the owners with the notice of the assessment."),
    )
    decided_on = forms.DateField(
        label=_lazy("Board vote on"), widget=forms.DateInput(attrs={"type": "date"})
    )
    votes_for = forms.IntegerField(label=_lazy("Board members voting for"), min_value=0)
    seats = forms.IntegerField(label=_lazy("Board seats"), min_value=1)


@require_POST
@require_perm(perms.FINANCE_MANAGE, perms.GOVERNANCE_MANAGE)
def special_emergency(request, pk):
    """Record the board's emergency vote: effective without ratification, owners told."""
    sa = get_object_or_404(SpecialAssessment, pk=pk)
    form = EmergencyForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Fill in every field of the emergency vote."))
        return redirect("billing:special_detail", pk=sa.pk)
    d = form.cleaned_data
    try:
        special.record_emergency(
            sa,
            kind=d["kind"],
            reason=d["reason"],
            decided_on=d["decided_on"],
            votes_for=d["votes_for"],
            seats=d["seats"],
            user=request.user,
        )
    except BillingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Emergency vote recorded; the owners are being notified."))
    return redirect("billing:special_detail", pk=sa.pk)


def _nat(number):
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


def _describe_installment(request, pk):
    inst = SpecialInstallment.objects.select_related("special").filter(pk=pk).first()
    label = f"{inst.special} #{inst.number}" if inst else f"#{pk}"
    return _("Post special assessment installment %(i)s") % {"i": label}, [
        (_("Installment"), label)
    ]


@money_change(
    "money.installment",
    _lazy("Post a special assessment installment"),
    describe=_describe_installment,
)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def installment_post(request, pk):
    inst = get_object_or_404(SpecialInstallment, pk=pk)
    try:
        created = special.post_installment(inst, posted_by=request.user)
    except BillingError as exc:
        messages.error(request, str(exc))
    else:
        if created:
            messages.success(request, _("Installment %(n)s posted.") % {"n": inst.number})
        else:
            messages.info(request, _("Installment %(n)s was already posted.") % {"n": inst.number})
    return redirect("billing:special_detail", pk=inst.special_id)
