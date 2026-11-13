"""Units hub > Import: upload → map → preview (dry run) → commit (REG-05).

Imports write the registry and post opening balances and history to the ledger, so they need
``units.manage`` and ``finance.manage``.
"""

from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.core.context import association_today
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund
from homeoa.ui.nav import get_hub

from . import engine, retention, services
from .forms import UploadForm
from .models import ImportRun, MappingProfile, Source
from .parsing import DATE_FORMATS, KINDS, Problem, parse_file, read_csv
from .profiles import NEW_ACCOUNT


def require_import_perms(view):
    @require_perm(perms.UNITS_MANAGE)
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not has_perm(request.user, perms.FINANCE_MANAGE, association=request.association):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


def _render(request, template, context):
    hub = get_hub("units")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": False} for i, t in enumerate(hub.subtabs)
    ]
    return render(request, template, {"current_hub": hub, "subtabs": tabs, **context})


@require_import_perms
def index(request):
    association = request.association
    form = UploadForm(
        request.POST or None,
        request.FILES or None,
        initial={"as_of": association_today(association), "source": Source.APPFOLIO},
    )
    if request.method == "POST" and form.is_valid():
        load_chart_of_accounts(association)
        try:
            run = services.create_run(
                association,
                source=form.cleaned_data["source"],
                mode=form.cleaned_data["mode"],
                as_of=form.cleaned_data["as_of"],
                uploads=[(f.name, f.read()) for f in form.cleaned_data["files"]],
                user=request.user,
            )
        except services.UploadError as e:
            form.add_error("files", str(e))
        else:
            return redirect("imports:mapping", pk=run.pk)
    runs = ImportRun.objects.select_related("created_by")[:20]
    return _render(request, "imports/index.html", {"form": form, "runs": runs})


# ---------------------------------------------------------------- mapping


def _account_rows(run, files):
    """AppFolio accounts from the chart-of-accounts file, with the current choices."""
    kind_files = [f for f in files if run.config["files"].get(f.name) == "accounts"]
    if not kind_files:
        return []
    f = kind_files[0]
    parsed = parse_file(
        "accounts",
        f.name,
        f.content,
        run.config["columns"].get("accounts", {}),
        run.config.get("date_format") or "%m/%d/%Y",
        [],
    )
    codes = set(Account.objects.values_list("code", flat=True))
    funds = {fd.name.lower(): fd.code for fd in Fund.objects.all()}
    rows = []
    for r in parsed.rows:
        code = r.get("code")
        if not code:
            continue
        choice = run.config.get("accounts", {}).get(code, {})
        account = choice.get("account") or (code if code in codes else NEW_ACCOUNT)
        fund = choice.get("fund") or funds.get((r.get("fund") or "").lower(), "operating")
        rows.append({"code": code, "name": r.get("name"), "account": account, "fund": fund})
    return rows


def _history_types(run, files):
    kind_files = [f for f in files if run.config["files"].get(f.name) == "history"]
    types = dict(run.config.get("history_types", {}))
    if kind_files:
        col = run.config["columns"].get("history", {}).get("type")
        _h, rows = read_csv(kind_files[0].content)
        for r in rows:
            t = (r.get(col) or "").strip() if col else ""
            if t and t not in types:
                types[t] = ""
        return [{"name": t, "account": types[t]} for t in sorted(types)]
    return []


def _mapping_context(run):
    files = list(run.files.all())
    file_rows = []
    for f in files:
        headers, rows = read_csv(f.content)
        kind = run.config["files"].get(f.name, "")
        cols = run.config["columns"].get(kind, {}) if kind else {}
        fields = (
            [
                {
                    "name": fld.name,
                    "label": fld.label,
                    "required": fld.required,
                    "value": cols.get(fld.name, ""),
                }
                for fld in KINDS[kind].fields
            ]
            if kind
            else []
        )
        file_rows.append(
            {"file": f, "headers": headers, "row_count": len(rows), "kind": kind, "fields": fields}
        )
    return {
        "run": run,
        "files": file_rows,
        "kinds": [(k.name, k.label) for k in KINDS.values()],
        "profiles": MappingProfile.objects.filter(source=run.source),
        "date_formats": DATE_FORMATS,
        "accounts": Account.objects.filter(active=True),
        "funds": Fund.objects.filter(active=True),
        "account_rows": _account_rows(run, files),
        "history_types": _history_types(run, files),
        "new_account": NEW_ACCOUNT,
        "profile_name": run.profile.name
        if run.profile
        else _("AppFolio-style default")
        if run.source == Source.APPFOLIO
        else _("CSV mapping"),
    }


def _config_from_post(run, post) -> dict:
    config = dict(run.config)
    files = list(run.files.all())
    account_codes = set(Account.objects.values_list("code", flat=True))
    fund_codes = set(Fund.objects.values_list("code", flat=True))
    kinds_by_file, columns = {}, dict(config.get("columns", {}))
    for f in files:
        kind = post.get(f"kind-{f.pk}", "")
        kind = kind if kind in KINDS else ""
        kinds_by_file[f.name] = kind
        if not kind:
            continue
        headers, _rows = read_csv(f.content)
        submitted = {
            fld.name: post.get(f"col-{f.pk}-{fld.name}")
            for fld in KINDS[kind].fields
            if f"col-{f.pk}-{fld.name}" in post
        }
        if submitted:
            columns[kind] = {k: (v if v in headers else "") for k, v in submitted.items()}
    config["files"] = kinds_by_file
    config["columns"] = columns
    fmt = post.get("date_format")
    if fmt in dict(DATE_FORMATS):
        config["date_format"] = fmt
    accounts = dict(config.get("accounts", {}))
    for key in post:
        if key.startswith("acct-"):
            code = key[5:]
            acct = post[key]
            fund = post.get(f"fund-{code}", "")
            entry = {}
            if acct == NEW_ACCOUNT or acct in account_codes:
                entry["account"] = acct
            if fund in fund_codes:
                entry["fund"] = fund
            accounts[code] = entry
    config["accounts"] = accounts
    history = dict(config.get("history_types", {}))
    for key in post:
        if key.startswith("histname-"):
            name = post[key]
            acct = post.get(f"hist-{key[9:]}", "")
            history[name] = acct if acct in account_codes else ""
    config["history_types"] = history
    if post.get("opening_equity") in account_codes:
        config["opening_equity"] = post["opening_equity"]
    return config


@require_import_perms
def mapping(request, pk):
    run = get_object_or_404(ImportRun, pk=pk)
    if run.is_done:
        return redirect("imports:run", pk=run.pk)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "load":
            profile = MappingProfile.objects.filter(
                pk=request.POST.get("profile") or 0, source=run.source
            ).first()
            if profile:
                run.config = services.config_for_files(
                    profile.config, list(run.files.all()), run.source
                )
                run.config["accounts"] = services.account_choices_from_saved(
                    run.association, run.source, run.config
                )
                run.profile = profile
                run.save(update_fields=["config", "profile"])
                messages.success(request, _("Loaded the mapping “%(n)s”.") % {"n": profile.name})
            return redirect("imports:mapping", pk=run.pk)
        run.config = _config_from_post(run, request.POST)
        run.save(update_fields=["config"])
        if action == "save":
            name = (request.POST.get("profile_name") or "").strip()[:100] or _("Saved mapping")
            services.save_profile(run, name)
            messages.success(request, _("Mapping saved as “%(n)s”.") % {"n": name})
        elif action == "preview":
            engine.preview(run)
            return redirect("imports:run", pk=run.pk)
        return redirect("imports:mapping", pk=run.pk)
    return _render(request, "imports/mapping.html", _mapping_context(run))


# ---------------------------------------------------------------- preview / result

SUMMARY = {
    "units": lambda n: ngettext("%(n)s unit", "%(n)s units", n),
    "owners": lambda n: ngettext("%(n)s owner row", "%(n)s owner rows", n),
    "tenants": lambda n: ngettext("%(n)s tenant", "%(n)s tenants", n),
    "history": lambda n: ngettext("%(n)s ledger history row", "%(n)s ledger history rows", n),
    "balances": lambda n: ngettext("%(n)s owner balance", "%(n)s owner balances", n),
    "accounts": lambda n: ngettext("%(n)s account", "%(n)s accounts", n),
    "trial_balance": lambda n: ngettext("%(n)s trial balance line", "%(n)s trial balance lines", n),
}

SECTIONS = {
    "units": _l("Units"),
    "people": _l("People"),
    "ownership": _l("Ownership"),
    "tenants": _l("Tenants"),
    "notices": _l("Notice preferences"),
    "accounts": _l("Accounts"),
    "account mappings": _l("Account mappings"),
    "history": _l("Ledger history"),
    "opening": _l("Opening balances"),
}

ACTIONS = {"created": _l("New"), "updated": _l("Changed"), "unchanged": _l("Unchanged")}


@require_import_perms
def run_detail(request, pk):
    run = get_object_or_404(ImportRun, pk=pk)
    report = run.report or {}
    summary = [
        SUMMARY[k](n) % {"n": n} for k, n in report.get("totals", {}).items() if k in SUMMARY
    ]
    counts = [{"section": SECTIONS.get(k, k), **v} for k, v in report.get("counts", {}).items()]
    changes = [
        {
            **c,
            "section": SECTIONS.get(c["section"], c["section"]),
            "action": ACTIONS.get(c["action"], c["action"]),
        }
        for c in report.get("changes", [])
    ]
    problems = [Problem(**p) for p in report.get("problems", [])]
    ctx = {
        "run": run,
        "summary": summary,
        "counts": counts,
        "changes": changes,
        "change_count": report.get("change_count", 0),
        "problems": problems,
        "warnings": report.get("warnings", []),
        "can_commit": run.status == ImportRun.Status.PREVIEWED and not problems,
        "comparison": run.mode == ImportRun.Mode.COMPARISON,
        "files": list(run.files.all()),
        "can_purge": retention.can_purge(run)
        and has_perm(request.user, perms.FINANCE_MANAGE, association=request.association),
        "retention_days": retention.retention_days(request.association),
    }
    return _render(request, "imports/run.html", ctx)


@require_POST
@require_import_perms
def commit(request, pk):
    run = get_object_or_404(ImportRun, pk=pk)
    if run.status != ImportRun.Status.PREVIEWED:
        messages.error(request, _("Preview the import before committing it."))
        return redirect("imports:run", pk=run.pk)
    engine.commit(run, user=request.user)
    return redirect("imports:run", pk=run.pk)


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def purge(request, pk):
    """Purge now: empty a committed run's raw file contents (REG-05 privacy)."""
    run = get_object_or_404(ImportRun, pk=pk)
    if retention.purge_run(run, reason="manual", actor=request.user):
        messages.success(request, _("The uploaded files were purged. The import record stays."))
    else:
        messages.error(request, _("Only a committed import's files can be purged, once."))
    return redirect("imports:run", pk=run.pk)
