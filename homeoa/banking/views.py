"""Money hub > Bank & deposits: accounts, upload, transactions, matching, reconciliation."""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.changes.replay import money_change
from homeoa.ledger.services import LedgerError, unit_balance
from homeoa.payments.models import Receipt
from homeoa.providers import get_provider
from homeoa.ui.nav import get_hub

from . import matching, reconcile, reserve, services
from .forms import (
    BankAccountForm,
    CategorizeForm,
    CategorizeRuleForm,
    CsvMappingForm,
    ImportForm,
    ReconcileStartForm,
    ReserveApprovalForm,
    TxnFilterForm,
)
from .models import (
    BankAccount,
    BankTransaction,
    CategorizeRule,
    CsvColumnMapping,
    ImportBatch,
    Reconciliation,
    ReserveApproval,
    StatementBalance,
    TxnStatus,
)


def _shell(title, **extra):
    hub = get_hub("money")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "bank"}
        for i, t in enumerate(hub.subtabs)
    ]
    current = next((t for t in hub.subtabs if t.slug == "bank"), None)
    ctx = {"current_hub": hub, "subtabs": tabs, "current_tab": current, "heading": title}
    ctx.update(extra)
    return ctx


def _overlap_message(a, b):
    return _(
        "%(a)s and %(b)s on %(account)s are from different sources and cover overlapping "
        "dates, so some transactions may be in twice. Undo one of them."
    ) % {"a": _batch_label(a), "b": _batch_label(b), "account": a.bank_account}


def _batch_label(b):
    return _("import #%(n)s (%(source)s %(file)s)") % {
        "n": b.pk,
        "source": b.get_source_display(),
        "file": b.filename,
    }


def _warn_overlaps(request, account):
    for a, b in services.overlapping_batches(account):
        messages.warning(request, _overlap_message(a, b))


@require_perm(perms.FINANCE_READ)
def index(request):
    latest = StatementBalance.objects.filter(bank_account=OuterRef("pk")).order_by("-as_of")
    accounts = BankAccount.objects.select_related("gl_account", "fund").annotate(
        stmt_balance=Subquery(latest.values("amount")[:1]),
        stmt_as_of=Subquery(latest.values("as_of")[:1]),
    )
    filters = TxnFilterForm(request.GET or None)
    txns = BankTransaction.objects.select_related("bank_account")
    if filters.is_valid():
        if filters.cleaned_data["account"]:
            txns = txns.filter(bank_account=filters.cleaned_data["account"])
        if filters.cleaned_data["status"]:
            txns = txns.filter(status=filters.cleaned_data["status"])
    txns = list(txns[:500])
    for t in txns:
        if t.status == TxnStatus.UNMATCHED:
            t.suggestion = next(iter(matching.suggestions(t)), None)
    ctx = _shell(
        _("Bank & deposits"),
        accounts=accounts,
        txns=txns,
        reserve_owed=matching.reserve_transfer_owed(request.association),
        insurance_owed=matching.insurance_transfer_owed(request.association),
        filters=filters,
        overlaps=[p for a in accounts for p in services.overlapping_batches(a)],
        batches=ImportBatch.objects.select_related("bank_account")[:20],
        feed=_feed_status(request),
        can_manage=has_perm(request.user, perms.FINANCE_MANAGE, association=request.association),
    )
    return render(request, "banking/index.html", ctx)


def _sync_feed(request):
    feed = get_provider("BankFeedProvider", request.association)
    return feed if getattr(feed, "syncs", False) else None


def _feed_status(request):
    feed = _sync_feed(request)
    return feed.feed_status(request.association) if feed else None


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def sync(request):
    """Sync now: pull the bank feed (when a syncing feed plugin is on)."""
    feed = _sync_feed(request)
    if feed is None:
        messages.error(request, _("No bank feed is turned on. Upload a statement instead."))
        return redirect("banking:index")
    result = feed.sync(request.association, user=request.user)
    if result.ok:
        messages.success(
            request,
            _("Bank sync imported %(n)s new transactions; %(d)s already imported were skipped.")
            % {"n": result.imported, "d": result.duplicates},
        )
        for batch in result.batches:
            _warn_overlaps(request, batch.bank_account)
    else:
        messages.error(request, _("Bank sync failed. The bank feed page says what to do."))
    return redirect("banking:index")


def _describe_import(request):
    account = BankAccount.objects.filter(pk=request.POST.get("bank_account") or 0).first()
    upload = request.FILES.get("file")
    return _("Import a bank statement into %(a)s") % {"a": account or "?"}, [
        (_("Bank account"), str(account or "")),
        (_("File"), upload.name if upload else ""),
    ]


def _describe_txn(verb):
    def describe(request, pk):
        txn = BankTransaction.objects.filter(pk=pk).first()
        label = f"{txn.date} {txn.description} ${txn.amount:,.2f}" if txn else f"#{pk}"
        details = [(_("Bank transaction"), label)]
        for key, values in request.POST.lists():
            if key in ("csrfmiddlewaretoken", "next"):
                continue
            shown = ", ".join(v for v in values if v)
            if shown:
                details.append((key.replace("_", " ").capitalize(), shown))
        return str(verb) % {"txn": label}, details

    return describe


@money_change("money.bank_import", _l("Import a bank statement"), describe=_describe_import)
@require_perm(perms.FINANCE_MANAGE)
def import_view(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        account = form.cleaned_data["bank_account"]
        try:
            batch = services.import_file(account, upload.name, upload.read(), user=request.user)
        except services.BankFileError as e:
            form.add_error("file", str(e))
        else:
            messages.success(
                request,
                _("Imported %(n)s new transactions; %(d)s already imported were skipped.")
                % {"n": batch.imported_count, "d": batch.duplicate_count},
            )
            _warn_overlaps(request, account)
            return redirect("banking:index")
    no_accounts = not BankAccount.objects.filter(active=True).exists()
    ctx = _shell(_("Import bank statement"), form=form, no_accounts=no_accounts)
    return render(request, "banking/import.html", ctx)


@require_perm(perms.FINANCE_MANAGE)
def account_edit(request, pk=None):
    account = get_object_or_404(BankAccount, pk=pk) if pk else None
    form = BankAccountForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        if account is None:
            form.instance.association = request.association
        form.save()
        messages.success(request, _("Bank account saved."))
        return redirect("banking:index")
    title = _("Edit bank account") if account else _("Add bank account")
    overlaps = services.overlapping_batches(account) if account else []
    ctx = _shell(title, form=form, account=account, overlaps=overlaps)
    return render(request, "banking/account_form.html", ctx)


@require_perm(perms.FINANCE_MANAGE)
def mapping_edit(request, pk):
    account = get_object_or_404(BankAccount, pk=pk)
    mapping = CsvColumnMapping.objects.filter(bank_account=account).first()
    form = CsvMappingForm(request.POST or None, instance=mapping)
    if request.method == "POST" and form.is_valid():
        form.instance.bank_account = account
        form.instance.association = account.association
        form.save()
        messages.success(request, _("CSV column mapping saved."))
        return redirect("banking:index")
    ctx = _shell(_("CSV column mapping"), form=form, account=account)
    return render(request, "banking/mapping_form.html", ctx)


@require_perm(perms.FINANCE_READ)
def batch_detail(request, pk):
    batch = get_object_or_404(ImportBatch.objects.select_related("bank_account"), pk=pk)
    overlaps = [p for p in services.overlapping_batches(batch.bank_account) if batch in p]
    ctx = _shell(
        _("Bank import"),
        batch=batch,
        txns=batch.transactions.all(),
        overlaps=overlaps,
        can_undo=not batch.is_undone
        and not batch.transactions.exclude(status="unmatched").exists(),
        error=None,
    )
    if request.method == "POST":
        if not has_perm(request.user, perms.FINANCE_MANAGE, association=request.association):
            raise PermissionDenied
        try:
            services.undo_batch(batch, request.user, request.POST.get("reason", ""))
        except services.UndoRefused as e:
            ctx["error"] = str(e)
        else:
            messages.success(request, _("Import undone."))
            return redirect("banking:batch", pk=batch.pk)
    return render(request, "banking/batch.html", ctx)


# ------------------------------------------------------------- matching


def _can_manage(request):
    return has_perm(request.user, perms.FINANCE_MANAGE, association=request.association)


@require_perm(perms.FINANCE_READ)
def txn_detail(request, pk):
    from homeoa.registry.models import Unit

    txn = get_object_or_404(BankTransaction.objects.select_related("bank_account"), pk=pk)
    active = list(matching.active_matches(txn))
    units = sorted(Unit.objects.all(), key=lambda u: u.sort_key)
    balances = {
        m.unit_id: unit_balance(m.unit_id, association=request.association)
        for m in active
        if m.unit_id
    }
    ctx = _shell(
        _("Bank transaction"),
        txn=txn,
        matches=active,
        unit_balances=[(m.unit, balances[m.unit_id]) for m in active if m.unit_id],
        suggestions=matching.suggestions(txn) if txn.status == TxnStatus.UNMATCHED else [],
        categorize_form=CategorizeForm(),
        units=units,
        split_rows=range(3),
        can_manage=_can_manage(request),
    )
    return render(request, "banking/txn.html", ctx)


def _refused(request, txn, err):
    messages.error(request, str(err))
    return redirect("banking:txn", pk=txn.pk)


@money_change(
    "money.bank_match", _l("Match a bank transaction"), describe=_describe_txn(_l("Match %(txn)s"))
)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def confirm(request, pk):
    """Confirm a suggestion: ``unit`` (owner receipt), ``entry`` (link), ``bill`` (an
    approved bill this debit paid), ``partner`` or
    ``clearing`` (a plugin suggestion's ``extra["key"]``, e.g. a Stripe payout)."""
    from homeoa.registry.models import Unit

    txn = get_object_or_404(BankTransaction, pk=pk)
    try:
        if request.POST.get("receipt"):
            receipt = get_object_or_404(Receipt, pk=request.POST["receipt"])
            matching.link_receipt(txn, receipt, request.user)
            messages.success(request, _("Linked to the payment already recorded."))
        elif request.POST.get("bill"):
            from homeoa.ap.models import Bill

            bill = get_object_or_404(Bill, pk=request.POST["bill"])
            if bill.status == Bill.Status.PAID:
                matching.link_bill_payment(txn, bill, request.user)
                messages.success(
                    request, _("Linked to the recorded payment of bill %(bill)s.") % {"bill": bill}
                )
            else:
                matching.confirm_bill(txn, bill, request.user)
                messages.success(request, _("Matched: bill %(bill)s is paid.") % {"bill": bill})
        elif request.POST.get("clearing"):
            key = request.POST["clearing"]
            best = next(
                (
                    s
                    for s in matching.suggestions(txn)
                    if s.kind == "clearing" and s.extra.get("key") == key
                ),
                None,
            )
            if best is None:
                raise matching.MatchRefused(_("That suggestion no longer applies."))
            matching.confirm_clearing(txn, best, request.user)
            messages.success(request, _("Matched: %(reason)s.") % {"reason": best.reason})
        elif request.POST.get("partner"):
            partner = get_object_or_404(BankTransaction, pk=request.POST["partner"])
            matching.confirm_interfund(txn, partner, request.user)
            messages.success(request, _("Matched as a reserve transfer."))
        else:
            unit = get_object_or_404(Unit, pk=request.POST.get("unit") or 0)
            reason = request.POST.get("reason", "")
            matching.confirm_units(txn, [(unit, txn.amount)], request.user, reason=reason)
            messages.success(
                request,
                _("Matched to %(unit)s. Balance is now %(bal)s.")
                % {
                    "unit": unit,
                    "bal": f"${unit_balance(unit.pk, association=request.association):,.2f}",
                },
            )
    except (matching.MatchRefused, LedgerError) as e:
        return _refused(request, txn, e)
    if request.POST.get("next") == "index":
        return redirect("banking:index")
    return redirect("banking:txn", pk=txn.pk)


@money_change(
    "money.bank_split",
    _l("Split a bank deposit"),
    describe=_describe_txn(_l("Split %(txn)s across units")),
)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def split(request, pk):
    from homeoa.registry.models import Unit

    txn = get_object_or_404(BankTransaction, pk=pk)
    allocations = []
    try:
        for uid, amt in zip(
            request.POST.getlist("unit"), request.POST.getlist("amount"), strict=False
        ):
            if not uid and not amt.strip():
                continue
            try:
                amount = Decimal(amt.strip().replace(",", "").replace("$", ""))
            except InvalidOperation:
                raise matching.MatchRefused(_("Enter amounts like 120.00.")) from None
            if amount != amount.quantize(Decimal("0.01")):
                raise matching.MatchRefused(_("Enter amounts like 120.00."))
            allocations.append((get_object_or_404(Unit, pk=uid or 0), amount))
        matching.confirm_units(txn, allocations, request.user, reason=_("split"))
    except (matching.MatchRefused, LedgerError) as e:
        return _refused(request, txn, e)
    messages.success(request, _("Deposit split across %(n)s units.") % {"n": len(allocations)})
    return redirect("banking:txn", pk=txn.pk)


@money_change(
    "money.bank_categorize",
    _l("Categorize a bank transaction"),
    describe=_describe_txn(_l("Categorize %(txn)s")),
)
@require_perm(perms.FINANCE_MANAGE)
def categorize(request, pk):
    txn = get_object_or_404(BankTransaction, pk=pk)
    initial = {}
    best = next((s for s in matching.suggestions(txn) if s.kind == "categorize"), None)
    if best is not None:
        initial["account"] = best.account.pk
    warning = best.extra.get("warning") if best is not None else None
    form = CategorizeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        account = form.cleaned_data["account"]
        try:
            matching.categorize(
                txn, account, request.user, save_rule=form.cleaned_data["save_rule"]
            )
        except (matching.MatchRefused, LedgerError) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, _("Categorized as %(a)s.") % {"a": account.name})
            return redirect("banking:txn", pk=txn.pk)
    ctx = _shell(_("Categorize transaction"), txn=txn, form=form, warning=warning)
    return render(request, "banking/categorize.html", ctx)


@money_change("money.bank_undo", _l("Undo a bank import"))
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def undo(request, pk):
    txn = get_object_or_404(BankTransaction, pk=pk)
    try:
        matching.undo(txn, request.user)
    except (matching.MatchRefused, LedgerError) as e:
        return _refused(request, txn, e)
    messages.success(request, _("Undone. The transaction is unmatched again."))
    return redirect("banking:txn", pk=txn.pk)


def _reserve_txn(request):
    """The unapproved reserve debit named by ``txn`` (POST or GET), if any."""
    pk = request.POST.get("txn") or request.GET.get("txn") or ""
    if not str(pk).isdigit():
        return None
    return next((t for t in reserve.unapproved(request.association) if t.pk == int(pk)), None)


def _describe_reserve(request):
    txn = _reserve_txn(request)
    purpose = request.POST.get("purpose", "")
    if txn is not None:
        amount = f"${-txn.amount:,.2f}"
        summary = _("Approve a reserve withdrawal already made: %(amount)s") % {"amount": amount}
        details = [
            (_("Bank transaction"), f"{txn.date} {txn.description} {amount}"),
            (_("Bank account"), str(txn.bank_account)),
        ]
    else:
        amount = f"${request.POST.get('amount', '')}"
        summary = _("Approve a reserve withdrawal: %(amount)s") % {"amount": amount}
        details = [(_("Amount"), amount)]
    return summary, [*details, (_("Purpose"), purpose)]


# Money leaving a reserve account always needs a second board member (RCW 64.34.372(2) in
# WA, through the reserves.spend_min_approvals rule).
@money_change(
    "money.reserve_withdrawal",
    _l("Reserve withdrawal"),
    always=True,
    describe=_describe_reserve,
)
@require_perm(perms.FINANCE_READ)
def reserve_withdrawals(request):
    association = request.association
    txn = _reserve_txn(request)
    can_manage = has_perm(request.user, perms.FINANCE_MANAGE, association=association)
    form = ReserveApprovalForm(request.POST or None, txn=txn) if can_manage else None
    if request.method == "POST":
        if not can_manage:
            raise PermissionDenied
        if form.is_valid():
            d = form.cleaned_data
            reserve.approve(
                association, amount=d["amount"], purpose=d["purpose"], user=request.user, txn=txn
            )
            messages.success(request, _("Reserve withdrawal approved."))
            return redirect("banking:reserve")
    ctx = _shell(
        _("Reserve withdrawals"),
        form=form,
        txn=txn,
        required=reserve.required(association),
        unapproved=reserve.unapproved(association),
        approvals=ReserveApproval.objects.select_related("txn", "created_by")[:50],
    )
    return render(
        request,
        "banking/reserve.html",
        ctx,
        status=400 if request.method == "POST" else 200,
    )


@require_perm(perms.FINANCE_MANAGE)
def rules(request):
    form = CategorizeRuleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.instance.association = request.association
        form.save()
        messages.success(request, _("Rule saved."))
        return redirect("banking:rules")
    ctx = _shell(
        _("Categorize rules"),
        form=form,
        rules=CategorizeRule.objects.select_related("account"),
    )
    return render(request, "banking/rules.html", ctx)


# ------------------------------------------------------------- reconciliation


def _describe_reconcile_start(request):
    account = BankAccount.objects.filter(pk=request.POST.get("bank_account") or 0).first()
    return _("Start reconciling %(a)s through %(d)s") % {
        "a": account or "?",
        "d": request.POST.get("statement_date", ""),
    }, [
        (_("Bank account"), str(account or "")),
        (_("Statement date"), request.POST.get("statement_date", "")),
        (_("Ending balance"), request.POST.get("ending_balance", "")),
    ]


def _describe_reconcile_finish(request, pk):
    rec = Reconciliation.objects.select_related("bank_account").filter(pk=pk).first()
    label = f"{rec.bank_account.name} {rec.statement_date}" if rec else f"#{pk}"
    return _("Finish reconciling %(r)s") % {"r": label}, [
        (_("Reconciliation"), label),
        (_("Note"), request.POST.get("note", "")),
    ]


@money_change(
    "money.reconcile_start", _l("Start a bank reconciliation"), describe=_describe_reconcile_start
)
@require_perm(perms.FINANCE_READ)
def reconcile_index(request):
    form = ReconcileStartForm(request.POST or None)
    if request.method == "POST":
        if not _can_manage(request):
            raise PermissionDenied
        if form.is_valid():
            d = form.cleaned_data
            try:
                rec = reconcile.start(
                    d["bank_account"],
                    d["statement_date"],
                    d["ending_balance"],
                    beginning_balance=d["beginning_balance"],
                    user=request.user,
                )
            except reconcile.ReconcileRefused as e:
                form.add_error("statement_date", str(e))
            else:
                return redirect("banking:reconciliation", pk=rec.pk)
    ctx = _shell(
        _("Reconcile"),
        form=form,
        recs=Reconciliation.objects.select_related("bank_account", "completed_by")[:24],
        can_manage=_can_manage(request),
    )
    return render(request, "banking/reconcile.html", ctx)


@money_change(
    "money.reconcile_finish",
    _l("Finish a bank reconciliation"),
    describe=_describe_reconcile_finish,
)
@require_perm(perms.FINANCE_READ)
def reconciliation(request, pk):
    rec = get_object_or_404(Reconciliation.objects.select_related("bank_account"), pk=pk)
    error = None
    if request.method == "POST":
        if not _can_manage(request):
            raise PermissionDenied
        try:
            reconcile.complete(rec, request.user, note=request.POST.get("note", ""))
        except reconcile.ReconcileRefused as e:
            error = str(e)
        else:
            messages.success(
                request,
                _("%(acct)s reconciled through %(d)s.")
                % {"acct": rec.bank_account.name, "d": rec.statement_date.strftime("%b %-d, %Y")},
            )
            return redirect("banking:reconciliation", pk=rec.pk)
    reconcile.refresh(rec)
    ctx = _shell(
        _("Reconciliation"),
        rec=rec,
        cleared=reconcile.cleared_items(rec),
        unmatched=reconcile.unmatched_items(rec),
        books=None if rec.is_complete else reconcile.book_check(rec),
        note=request.POST.get("note", ""),
        error=error,
        can_manage=_can_manage(request),
    )
    return render(request, "banking/reconciliation.html", ctx)
