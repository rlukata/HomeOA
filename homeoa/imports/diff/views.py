"""Money > Reports > Parallel run (FIN-18): upload an AppFolio snapshot, diff it, explain.

Viewing needs ``finance.read``; uploading, running and explaining need ``finance.manage``.
Nothing here posts to the ledger: the snapshot is stored in comparison mode only.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.core.context import association_today
from homeoa.core.status import is_live
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ui.nav import get_hub

from .. import engine, services
from ..forms import MAX_UPLOAD, MultipleFileField
from ..models import ImportRun, Source
from . import services as diff
from .models import DiffLine, DiffRun


class SnapshotForm(forms.Form):
    source = forms.ChoiceField(label=_l("Exported from"), choices=Source.choices)
    as_of = forms.DateField(
        label=_l("Balances as of"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_l("The date the export was run."),
    )
    files = MultipleFileField(
        label=_l("CSV files"),
        help_text=_l("The same export files as the import: owner balances and trial balance."),
    )

    def clean_files(self):
        files = self.cleaned_data["files"]
        for f in files:
            if f.size > MAX_UPLOAD:
                raise forms.ValidationError(_("%(f)s is larger than 10 MB.") % {"f": f.name})
        return files


class ExplainForm(forms.Form):
    note = forms.CharField(
        label=_l("Explanation"),
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=2000,
        help_text=_l("For example: missing payment, late fee, timing."),
    )


def _ctx(request, title, **extra):
    hub = get_hub("money")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "reports"}
        for i, t in enumerate(hub.subtabs)
    ]
    can_manage = has_perm(request.user, perms.FINANCE_MANAGE, association=request.association)
    return {
        "current_hub": hub,
        "subtabs": tabs,
        "heading": title,
        "can_manage": can_manage,
        **extra,
    }


def _pending(association):
    """Stored snapshots that have not been diffed yet, newest first."""
    return (
        ImportRun.objects.filter(
            status=ImportRun.Status.STORED, mode=ImportRun.Mode.COMPARISON, diff_runs=None
        )
        .order_by("-as_of", "-pk")
        .select_related("created_by")
    )


@require_perm(perms.FINANCE_READ)
def index(request):
    association = request.association
    form = SnapshotForm(
        request.POST or None,
        request.FILES or None,
        initial={"as_of": association_today(association), "source": Source.APPFOLIO},
    )
    if request.method == "POST":
        if not has_perm(request.user, perms.FINANCE_MANAGE, association=association):
            raise PermissionDenied
        if form.is_valid():
            load_chart_of_accounts(association)
            try:
                run = services.create_run(
                    association,
                    source=form.cleaned_data["source"],
                    mode=ImportRun.Mode.COMPARISON,
                    as_of=form.cleaned_data["as_of"],
                    uploads=[(f.name, f.read()) for f in form.cleaned_data["files"]],
                    user=request.user,
                )
            except services.UploadError as e:
                form.add_error("files", str(e))
            else:
                report = engine.commit(run, user=request.user)
                if report.get("problems"):
                    for p in report["problems"][:10]:
                        form.add_error(
                            None, f"{p.get('file', '')} {p.get('row') or ''}: {p.get('message')}"
                        )
                else:
                    messages.success(request, _("Snapshot saved for comparison."))
                    return redirect("imports:parallel_run")
    history = DiffRun.objects.select_related("created_by", "snapshot")[:24]
    ctx = _ctx(
        request,
        _("Parallel run"),
        form=form,
        pending=_pending(association)[:5],
        history=history,
    )
    return render(request, "imports/diff/index.html", ctx)


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def run(request, pk):
    snapshot = get_object_or_404(
        ImportRun, pk=pk, status=ImportRun.Status.STORED, mode=ImportRun.Mode.COMPARISON
    )
    result = diff.run_diff(snapshot, user=request.user)
    return redirect("imports:parallel_run_detail", pk=result.pk)


@require_perm(perms.FINANCE_READ)
def detail(request, pk):
    result = get_object_or_404(DiffRun.objects.select_related("snapshot", "created_by"), pk=pk)
    lines = list(result.lines.select_related("explained_by"))
    units = [ln for ln in lines if ln.kind == DiffLine.Kind.UNIT]
    accounts = [ln for ln in lines if ln.kind == DiffLine.Kind.ACCOUNT]
    ctx = _ctx(
        request,
        _("Parallel run"),
        result=result,
        units=units,
        accounts=accounts,
        explain_form=ExplainForm(),
    )
    return render(request, "imports/diff/detail.html", ctx)


@require_perm(perms.FINANCE_READ)
def line(request, pk):
    ln = get_object_or_404(DiffLine.objects.select_related("run__snapshot", "explained_by"), pk=pk)
    form = ExplainForm(request.POST or None, initial={"note": ln.note})
    if request.method == "POST":
        if not has_perm(request.user, perms.FINANCE_MANAGE, association=request.association):
            raise PermissionDenied
        if form.is_valid():
            diff.explain(ln, form.cleaned_data["note"], request.user)
            messages.success(request, _("Difference marked explained."))
            return redirect("imports:parallel_run_detail", pk=ln.run_id)
    ctx = _ctx(request, _("Parallel run"), line=ln, drill=diff.drilldown(ln), form=form)
    return render(request, "imports/diff/line.html", ctx)


class CleanMonthsForm(forms.Form):
    clean_months_required = forms.IntegerField(
        label=_l("Clean months required before cutover"), min_value=1, max_value=12
    )


@require_perm(perms.FINANCE_READ)
def cutover(request):
    from .. import cutover as co

    association = request.association
    form = CleanMonthsForm(initial={"clean_months_required": co.clean_months_required(association)})
    if request.method == "POST":
        if not has_perm(request.user, perms.FINANCE_MANAGE, association=association):
            raise PermissionDenied
        action = request.POST.get("action")
        if action == "months":
            form = CleanMonthsForm(request.POST)
            if form.is_valid():
                co.set_clean_months_required(
                    association, form.cleaned_data["clean_months_required"], request.user
                )
                messages.success(request, _("Clean months required saved."))
                return redirect("imports:cutover")
        elif action == "records":
            co.set_records_requested(
                association, request.POST.get("requested") == "1", request.user
            )
            messages.success(request, _("Records delivery request saved."))
            return redirect("imports:cutover")
        else:
            return redirect("imports:cutover")
    ctx = _ctx(
        request,
        _("Cutover readiness"),
        r=co.readiness(association),
        months_form=form,
        citation=co.RECORDS_CITATION,
        live=is_live(association),
    )
    return render(request, "imports/diff/cutover.html", ctx)
