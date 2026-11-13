"""Monthly owner statements (FIN-04).

``generate(association, month)`` builds one ``Statement`` per unit ownership period that
overlaps the month (a statement never mixes two owners). For each period:

* opening = what that owner owed the day before their part of the month starts;
* charges / payments = their unit-subledger movements (1100 receivable, 2100 prepaid)
  in the month, one row per journal entry (``billing.owner_ledger`` rules);
* closing = opening + charges − payments, which equals the subledger balance at the end
  of their part of the month.

Lines are the owner's when tagged with their ownership period; untagged unit lines count
for whoever owned the unit on the entry date.

PDF: the board-editable "statement-cover" letter template (``letters``) with the
statement table appended. Routing: when every owner has e-delivery consent the
statement goes by email, otherwise by mail through a ``letters.batches`` draft batch.

Generating is preview only: statement notices go through ``notify(..., preview_only=True)``,
so they are stored as ``preview`` for every recipient, board members included, and never
handed to a channel; mail batches stay drafts.

Sending (``send_run`` / ``send_statement``) needs residents on. It re-checks
routing at send time (e-notice consent): email previews are released to the email channel, and
statements that now go by mail are put in a mail batch that is handed to the postal
provider. A sent statement is never sent again or replaced.

Re-running a month is idempotent: unsent statements (with their preview notices and draft
batch) are replaced; sent ones are never touched.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit
from homeoa.letters import batches, merge
from homeoa.letters import services as letter_services
from homeoa.letters.models import LetterTemplate, MailBatch
from homeoa.notify import channels as ch
from homeoa.notify import services as nsvc
from homeoa.notify import types as ntypes
from homeoa.notify.models import Delivery, Notification
from homeoa.providers import get_provider

from .statement_models import Statement, StatementRun

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
NOTIFICATION_TYPE = "statements.statement"
TEMPLATE_KEY = "statement-cover"


class StatementError(Exception):
    pass


class ResidentsOff(StatementError):
    """Nothing is sent to residents while the residents switch is off."""


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=NOTIFICATION_TYPE,
            label=gettext_lazy("Account statement"),
            default_channels=(ch.EMAIL,),
            audience=ntypes.AUDIENCE_RESIDENTS,
            mandatory=True,
            subject_template="{{ subject }}",
            body_template="{{ body }}",
        )
    )


# --- periods and amounts ------------------------------------------------------------------


def month_bounds(month: dt.date) -> tuple[dt.date, dt.date]:
    start = month.replace(day=1)
    nxt = (start + dt.timedelta(days=32)).replace(day=1)
    return start, nxt - dt.timedelta(days=1)


def _periods(unit, start: dt.date, end: dt.date):
    from homeoa.registry.models import OwnershipPeriod

    return (
        OwnershipPeriod.all_associations.filter(unit=unit, start__lte=end)
        .filter(Q(end__isnull=True) | Q(end__gte=start))
        .order_by("start")
    )


def _owner_lines(period):
    """The unit-subledger lines that belong to ``period``'s owners."""
    from homeoa.ledger.models import JournalLine

    untagged = Q(ownership_period__isnull=True, entry__date__gte=period.start)
    if period.end is not None:
        untagged &= Q(entry__date__lte=period.end)
    return JournalLine.all_associations.filter(
        association_id=period.association_id,
        unit_id=period.unit_id,
        account__in_unit_subledger=True,
    ).filter(Q(ownership_period_id=period.pk) | untagged)


def _net(qs) -> Decimal:
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or ZERO) - (agg["c"] or ZERO)


def compute(period, start: dt.date, end: dt.date) -> dict:
    """Opening, rows, charges, payments and closing for ``period`` within [start, end]."""
    lines = _owner_lines(period)
    opening = _net(lines.filter(entry__date__lt=start))
    grouped = (
        lines.filter(entry__date__gte=start, entry__date__lte=end)
        .values("entry_id", "entry__date", "entry__memo")
        .annotate(d=Sum("debit"), c=Sum("credit"))
        .order_by("entry__date", "entry_id")
    )
    rows, charges, payments, balance = [], ZERO, ZERO, opening
    for g in grouped:
        net = (g["d"] or ZERO) - (g["c"] or ZERO)
        if not net:
            continue  # moves between subledger accounts without changing what is owed
        balance += net
        charge, payment = (net, ZERO) if net > 0 else (ZERO, -net)
        charges += charge
        payments += payment
        rows.append(
            {
                "date": g["entry__date"],
                "description": g["entry__memo"],
                "charge": charge,
                "payment": payment,
                "balance": balance,
            }
        )
    return {
        "opening": opening,
        "rows": rows,
        "charges": charges,
        "payments": payments,
        "closing": opening + charges - payments,
    }


def statement_table(amounts: dict, start: dt.date, end: dt.date) -> list[dict]:
    money = merge.money
    rows = [[_("Date"), _("Description"), _("Charges"), _("Payments"), _("Balance")]]
    rows.append(
        [date_format(start, "M j, Y"), _("Opening balance"), "", "", money(amounts["opening"])]
    )
    for r in amounts["rows"]:
        rows.append(
            [
                date_format(r["date"], "M j, Y"),
                r["description"][:60],
                money(r["charge"]) if r["charge"] else "",
                money(r["payment"]) if r["payment"] else "",
                money(r["balance"]),
            ]
        )
    rows.append(
        [
            date_format(end, "M j, Y"),
            _("Closing balance"),
            money(amounts["charges"]),
            money(amounts["payments"]),
            money(amounts["closing"]),
        ]
    )
    heading = _("Statement for %(start)s – %(end)s") % {
        "start": date_format(start, "M j, Y"),
        "end": date_format(end, "M j, Y"),
    }
    return [{"heading": heading, "rows": rows, "num_cols": (2, 3, 4), "bold_last": True}]


def statement_extras(period, as_of: dt.date) -> dict:
    """Fields optional modules add to a period's statement (for example the next autopay
    debit), merged from their ``fn(period, as_of) -> dict`` contributions."""
    from homeoa.plugins import slots

    out: dict = {}
    for fn in slots.values(slots.STATEMENT_EXTRAS, period.association):
        out.update(fn(period, as_of))
    return out


def autopay_text(debit_on, amount, fixed: bool) -> str:
    words = {"amount": merge.money(amount), "date": date_format(debit_on, "F j, Y")}
    if fixed:
        return _("Autopay will debit your fixed amount of up to %(amount)s on %(date)s.") % words
    return (
        _(
            "Autopay will debit up to %(amount)s on %(date)s: your balance plus the charges "
            "due by then. If you pay part of it first, autopay takes only what is still owed."
        )
        % words
    )


def autopay_table(autopay: dict) -> list[dict]:
    if not autopay:
        return []
    text = autopay_text(
        autopay["autopay_debit_on"], autopay["autopay_amount"], autopay["autopay_fixed"]
    )
    return [{"heading": _("Autopay"), "rows": [[text]]}]


def statement_autopay_text(statement) -> str:
    if statement.autopay_debit_on is None:
        return ""
    return autopay_text(
        statement.autopay_debit_on, statement.autopay_amount, statement.autopay_fixed
    )


# --- routing and preview notices ------------------------------------------------------------


def route(owners) -> str:
    """Email when every owner may get this notice by email (e-notice consent), else mail."""
    ntype = ntypes.get(NOTIFICATION_TYPE)
    if owners and all(ch.EMAIL in nsvc.channels_for(ntype, p) for p in owners):
        return Statement.Channel.EMAIL
    return Statement.Channel.MAIL


def _discard_previews(notes) -> None:
    """Delete notices that were only ever previews (never handed to a channel)."""
    for note in notes:
        if not note.deliveries.exclude(status=Delivery.Status.PREVIEW).exists():
            note.delete()


def _replace_unsent(run: StatementRun) -> set[int]:
    """Delete the run's unsent statements; return the ownership periods to keep."""
    keep, old = set(), []
    for s in run.statements.select_related("mail_batch"):
        batch_out = s.mail_batch is not None and s.mail_batch.status != MailBatch.Status.DRAFT
        if s.status == Statement.Status.SENT or batch_out:
            keep.add(s.ownership_period_id)
        else:
            old.append(s)
    drafts = {s.mail_batch for s in old if s.mail_batch is not None}
    letters = [s.letter for s in old]
    stale_files = [b.pdf_key for b in drafts if b.pdf_key] + [
        b.manifest_key for b in drafts if b.manifest_key
    ]
    for batch in drafts:
        _discard_previews(
            Notification.all_associations.filter(
                association=run.association, period=f"batch:{batch.pk}"
            )
        )
        batch.delete()
    keys = [nsvc.object_key_for(s) for s in old]
    for s in old:
        s.delete()
    _discard_previews(
        Notification.all_associations.filter(association=run.association, object_key__in=keys)
    )
    stale_files += _discard_letters(run.association, letters)
    _delete_files_on_commit(run.association, stale_files)
    return keep


def _discard_letters(association, letters) -> list[str]:
    """Delete replaced statement letters nothing else points at; return their file keys."""
    from .models import DelinquencyAction

    keys = []
    for letter in letters:
        if (
            letter.mail_items.exists()
            or DelinquencyAction.all_associations.filter(letter=letter).exists()
        ):
            continue
        audit.record(
            "letter.discard",
            letter,
            changes={"unit": letter.unit.number, "as_of": letter.as_of.isoformat()},
            message="Replaced by a statements re-run",
        )
        keys.append(letter.file_key)
        letter.delete()
    return keys


def _delete_files_on_commit(association, keys) -> None:
    """Remove stored PDFs once the re-run commits (a rollback keeps them)."""
    if not keys:
        return
    from homeoa.core.retention import assert_deletable_files

    assert_deletable_files(association, keys)  # Only draft files are ever removed

    def _delete():
        storage = get_provider("FileStorage", association)
        for key in keys:
            try:
                storage.delete(key)
            except Exception:  # a missing file must not break the re-run
                logger.warning("could not delete stored file %s", key, exc_info=True)

    transaction.on_commit(_delete)


# --- generation ------------------------------------------------------------------------------


@transaction.atomic
def generate(
    association, month: dt.date, *, user=None, as_of: dt.date | None = None
) -> StatementRun:
    from homeoa.registry.models import Unit

    as_of = as_of or timezone.localdate()

    start, end = month_bounds(month)
    run, _created = StatementRun.all_associations.get_or_create(
        association=association, period_start=start, defaults={"period_end": end}
    )
    run = StatementRun.all_associations.select_for_update().get(pk=run.pk)
    letter_services.ensure_default_templates(association)
    template = LetterTemplate.all_associations.get(association=association, key=TEMPLATE_KEY)
    keep = _replace_unsent(run)
    actor = user if user is not None and user.is_authenticated else None

    units = sorted(Unit.all_associations.filter(association=association), key=lambda u: u.sort_key)
    made, to_mail = [], []
    for unit in units:
        for period in _periods(unit, start, end):
            if period.pk in keep:
                continue
            seg_start = max(start, period.start)
            seg_end = min(end, period.end or end)
            amounts = compute(period, seg_start, seg_end)
            autopay = statement_extras(period, max(seg_end, as_of))
            owners = [s.person for s in period.shares.select_related("person")]
            letter = letter_services.generate_letter(
                template,
                unit,
                as_of=seg_end,
                user=user,
                tables=statement_table(amounts, seg_start, seg_end) + autopay_table(autopay),
            )
            statement = Statement.objects.create(
                association=association,
                run=run,
                unit=unit,
                ownership_period=period,
                period_start=seg_start,
                period_end=seg_end,
                owner_names=", ".join(p.full_name for p in owners)[:300],
                opening=amounts["opening"],
                charges=amounts["charges"],
                payments=amounts["payments"],
                closing=amounts["closing"],
                channel=route(owners),
                letter=letter,
                **autopay,
            )
            made.append(statement)
            if statement.channel == Statement.Channel.EMAIL:
                _notify_email(statement, owners)
            else:
                to_mail.append(statement)

    if to_mail:
        batch = batches.create_batch(
            association,
            [s.letter for s in to_mail],
            name=_("Statements %(month)s") % {"month": date_format(start, "F Y")},
            user=user,
            preview_only=True,
        )
        Statement.all_associations.filter(pk__in=[s.pk for s in to_mail]).update(mail_batch=batch)

    run.period_end = end
    run.generated_at = timezone.now()
    run.generated_by = actor
    run.save(update_fields=["period_end", "generated_at", "generated_by"])
    audit.record(
        "statement_run.generate",
        run,
        changes={"month": start.isoformat(), "statements": len(made), "kept": len(keep)},
        actor=user,
    )
    return run


def _notify_email(statement: Statement, owners, *, preview_only: bool = True) -> None:
    subject = _("Your account statement for unit %(unit)s") % {"unit": statement.unit.number}
    body = _("Your statement for %(start)s – %(end)s is ready. Closing balance: %(closing)s.") % {
        "start": date_format(statement.period_start, "M j, Y"),
        "end": date_format(statement.period_end, "M j, Y"),
        "closing": merge.money(statement.closing),
    }
    if statement.autopay_debit_on is not None:
        body = f"{body}\n\n{statement_autopay_text(statement)}"
    # After commit; a failure is logged and audited, never raised.
    nsvc.notify_after_commit(
        NOTIFICATION_TYPE,
        owners,
        {"subject": subject, "body": body},
        statement.period_end,
        obj=statement,
        period=statement.run.period_start.strftime("%Y-%m"),
        association=statement.association,
        unit=statement.unit,
        preview_only=preview_only,
    )


def notices(statement: Statement):
    """The preview (or sent) notices filed for this statement, email or mail."""
    q = Q(object_key=nsvc.object_key_for(statement))
    if statement.mail_batch_id:
        q |= Q(
            period=f"batch:{statement.mail_batch_id}",
            object_key=f"letters.letter:{statement.letter_id}",
        )
    return Notification.all_associations.filter(association=statement.association).filter(q)


# --- sending -------------------------------------------------------------------------


def _owners(statement: Statement):
    return [s.person for s in statement.ownership_period.shares.select_related("person")]


def _email_notes(statement: Statement):
    return Notification.all_associations.filter(
        association=statement.association,
        type_key=NOTIFICATION_TYPE,
        object_key=nsvc.object_key_for(statement),
    )


def _batch_notes(batch: MailBatch, letter=None):
    qs = Notification.all_associations.filter(
        association=batch.association, period=f"batch:{batch.pk}"
    )
    if letter is not None:
        qs = qs.filter(object_key=nsvc.object_key_for(letter))
    return qs


def _detach(statement: Statement) -> None:
    """Take an unsent statement out of its draft mail batch (dropping that batch's preview
    notices for it); an emptied batch is deleted, a smaller one gets new print files."""
    batch = statement.mail_batch
    statement.mail_batch = None
    if batch is None or batch.status != MailBatch.Status.DRAFT:
        return
    notes = list(_batch_notes(batch, statement.letter))
    batch.items.filter(letter=statement.letter).delete()
    _discard_previews(notes)
    if batch.items.exists():
        batches.build_print_files(batch)
    else:
        keys = [k for k in (batch.pdf_key, batch.manifest_key) if k]
        batch.delete()
        _delete_files_on_commit(statement.association, keys)


def _check_gate(association) -> None:
    from homeoa.core import residents_enabled

    if not residents_enabled(association):
        raise ResidentsOff(
            _("Residents are not switched on yet, so statements stay previews. Nothing is sent.")
        )


@transaction.atomic
def _send(association, statements, by=None) -> list[Statement]:
    """Send these statements (unsent ones only). Returns the statements sent now."""
    association = type(association).objects.get(pk=association.pk)  # fresh switch state
    _check_gate(association)
    pks = [s.pk for s in statements]
    locked = list(
        Statement.all_associations.select_for_update(of=("self",))
        .filter(association=association, pk__in=pks, status=Statement.Status.PREVIEW)
        .select_related("unit", "letter", "mail_batch", "run", "ownership_period")
        .order_by("pk")
    )
    if not locked:
        return []
    email, mail = [], []
    for st in locked:
        owners = _owners(st)
        channel = route(owners)
        if channel == Statement.Channel.EMAIL:
            _detach(st)
            leftover = nsvc.release_previews(list(_email_notes(st)))
            _notify_email(st, owners, preview_only=False)
            if leftover and not any(
                n.deliveries.exclude(status=Delivery.Status.PREVIEW).exists()
                for n in _email_notes(st)
            ):
                channel = Statement.Channel.MAIL  # consent gone for everyone: mail it
        if channel == Statement.Channel.MAIL:
            _discard_previews(list(_email_notes(st)))
            mail.append(st)
        else:
            email.append(st)
        st.channel = channel

    if mail:
        _mail(association, mail, by)

    now = timezone.now()
    for st in locked:
        st.status = Statement.Status.SENT
        st.sent_at = now
        st.save(update_fields=["status", "sent_at", "channel", "mail_batch"])
        audit.record(
            "statement.send",
            st,
            changes={"unit": st.unit.number, "channel": st.channel},
            actor=by,
        )
    return locked


def _mail(association, statements, by=None) -> None:
    """Hand the mail statements to the postal provider in one batch. A draft batch holding
    exactly these letters is reused; otherwise they move to a new batch."""
    letters = {st.letter_id for st in statements}
    drafts = {st.mail_batch for st in statements if st.mail_batch is not None}
    batch = None
    if len(drafts) == 1:
        only = next(iter(drafts))
        if only.status == MailBatch.Status.DRAFT and (
            set(only.items.values_list("letter_id", flat=True)) == letters
        ):
            batch = only
    if batch is None:
        for st in statements:
            _detach(st)
        run = statements[0].run
        batch = batches.create_batch(
            association,
            [st.letter for st in statements],
            name=_("Statements %(month)s") % {"month": date_format(run.period_start, "F Y")},
            user=by,
        )
    else:
        nsvc.release_previews(list(_batch_notes(batch)))
    batches.send_batch(batch, user=by)
    for st in statements:
        st.mail_batch = batch


def send_run(run: StatementRun, by=None) -> list[Statement]:
    """Send every unsent statement in the run (resident-access gate, consent re-checked)."""
    return _send(run.association, list(run.statements.all()), by)


def send_statement(statement: Statement, by=None) -> Statement:
    """Send one unit's statement; other units are not touched. Idempotent."""
    _send(statement.association, [statement], by)
    statement.refresh_from_db()
    return statement
