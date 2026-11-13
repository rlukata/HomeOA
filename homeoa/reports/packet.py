"""Monthly board packet (RPT-03): one PDF for a month.

Built when the Treasurer closes the month (and on demand from Money > Reports > Board
packet): a summary opening with the month in plain words, the income statement
for the month and the year to date, the balance sheet, budget vs actual, receivables
aging, bills paid, open work orders and violations, and upcoming deadlines. Each table
uses the same query and builder as its own screen. The PDF is filed under Documents
(board only) and emailed to the board.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

from . import export, queries, statements, story, tables
from .export import DATE, MONEY, Column, TableSpec
from .queries import ACCRUAL
from .sections import PacketContext, PacketPart

logger = logging.getLogger(__name__)
ZERO = Decimal("0.00")
PACKET_TYPE = "reports.board_packet"
DEADLINE_DAYS = 60


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            module="board_packet",
            key=PACKET_TYPE,
            label=gettext_lazy("Monthly board packet"),
            default_channels=(ch.EMAIL, ch.INAPP),
            audience=ntypes.AUDIENCE_BOARD,
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with m=month a=association_name %}"
                "Board packet for {{ m }}, {{ a }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with m=month %}"
                "The board packet for {{ m }} is attached.{% endblocktranslate %}\n\n{{ url }}"
            ),
        )
    )


def _month_label(start: dt.date) -> str:
    return f"{start:%B} {start.year}"


def _bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    from homeoa.closing.services import month_bounds

    return month_bounds(year, month)


def _summary(association, end: dt.date, extra_rows: list[list], sentences: list[str]):
    from homeoa.dashboard.cards import cash_by_fund

    rows = [
        [_("Cash: %(fund)s") % {"fund": fund.name}, bal]
        for fund, bal in cash_by_fund(association, as_of=end)
    ]
    ar = queries.ar_aging(as_of=end, association=association)
    owed = sum((r.total for r in ar.rows if r.total > 0), ZERO)
    rows += [[_("Owed by owners"), owed], *extra_rows]
    return TableSpec(
        title=_("Summary"),
        columns=[Column(_("Item")), Column(_("Amount or count"))],
        rows=rows,
        subtitle=_("As of %(d)s") % {"d": end.isoformat()},
        intro=sentences,
    )


def _budget(association, start: dt.date, end: dt.date) -> TableSpec | None:
    from homeoa.budget.services import adopted_for, budget_vs_actual

    budget = adopted_for(association, statements.fiscal_year_start(association, end).year)
    if budget is None:
        return None
    report = budget_vs_actual(budget, end)
    rows = [
        [r.label, r.budget, r.actual, r.variance]
        for _group, group_rows in report.groups
        for r in group_rows
    ]
    return TableSpec(
        title=_("Budget vs actual, year to date"),
        columns=[
            Column(_("Account")),
            Column(_("Budget"), MONEY),
            Column(_("Actual"), MONEY),
            Column(_("Difference"), MONEY),
        ],
        rows=rows,
        subtitle=_("%(name)s · through %(d)s") % {"name": budget.name, "d": end.isoformat()},
    )


def _income_statements(ctx: PacketContext) -> PacketPart:
    out = []
    for title, s in (
        (_("Income statement, month"), ctx.start),
        (_("Income statement, year to date"), ctx.fy_start),
    ):
        st = statements.income_statement(
            ctx.association,
            start=s,
            end=ctx.end,
            basis=ACCRUAL,
            fund_filter=statements.ALL_TOTAL,
            compare=False,
        )
        period = f"{s.isoformat()} – {ctx.end.isoformat()}"
        out.append(
            tables.statement(
                st,
                title=title,
                filename="income-statement",
                period=period,
                scope=_("All funds"),
                basis=ACCRUAL,
            )
        )
    return PacketPart(out)


def _balance_sheet(ctx: PacketContext) -> PacketPart:
    bs = statements.balance_sheet(ctx.association, as_of=ctx.end, basis=ACCRUAL)
    return PacketPart(
        [
            tables.statement(
                bs,
                title=_("Balance sheet"),
                filename="balance-sheet",
                period=_("As of %(d)s") % {"d": ctx.end.isoformat()},
                scope=_("All funds, by fund"),
                basis=ACCRUAL,
            )
        ]
    )


def _budget_part(ctx: PacketContext) -> PacketPart:
    budget = _budget(ctx.association, ctx.fy_start, ctx.end)
    return PacketPart([budget] if budget is not None else [])


def _aging(ctx: PacketContext) -> PacketPart:
    ar = queries.ar_aging(as_of=ctx.end, association=ctx.association)
    return PacketPart(
        [
            tables.aging(
                ar,
                title=_("Receivables aging"),
                who=_("Unit"),
                filename="receivables-aging",
                show_credit=True,
            )
        ]
    )


def _bills_paid(ctx: PacketContext) -> PacketPart:
    from homeoa.ap.models import Bill

    bills = list(
        Bill.all_associations.filter(
            association=ctx.association,
            status=Bill.Status.PAID,
            paid_on__gte=ctx.start,
            paid_on__lte=ctx.end,
        ).select_related("vendor")
    )
    table = TableSpec(
        title=_("Bills paid"),
        columns=[
            Column(_("Vendor")),
            Column(_("Invoice")),
            Column(_("Paid on"), DATE),
            Column(_("Amount"), MONEY),
        ],
        rows=[[b.vendor.name, b.invoice_number, b.paid_on, b.total] for b in bills],
    )
    total = sum((b.total for b in bills), ZERO)
    return PacketPart([table], [[_("Bills paid this month"), total]])


def _deadlines(ctx: PacketContext) -> PacketPart:
    from homeoa.compliance.calendar import entries

    upcoming = [
        e
        for e in entries(ctx.association, ctx.end, ctx.end + dt.timedelta(days=DEADLINE_DAYS))
        if not e.done
    ]
    sentences = []
    if upcoming:
        sentences.append(
            _("Next deadline: %(what)s, due %(date)s.")
            % {"what": upcoming[0].title, "date": date_format(upcoming[0].due, "M j")}
        )
    return PacketPart(
        [
            TableSpec(
                title=_("Deadlines in the next %(n)s days") % {"n": DEADLINE_DAYS},
                columns=[Column(_("Due"), DATE), Column(_("What"))],
                rows=[[e.due, e.title] for e in upcoming],
            )
        ],
        sentences=sentences,
    )


# Sections this module adds to the packet, with their place in it. Optional modules add
# theirs (open work orders at 70, open violations at 80) from their own ready hooks.
SECTIONS = (
    (_income_statements, 20),
    (_balance_sheet, 30),
    (_budget_part, 40),
    (_aging, 50),
    (_bills_paid, 60),
    (_deadlines, 90),
)


def specs(association, year: int, month: int) -> list[TableSpec]:
    """The packet's tables in order: the summary, then every contributed section."""
    from homeoa.plugins import slots

    start, end = _bounds(year, month)
    ctx = PacketContext(association, start, end, statements.fiscal_year_start(association, end))
    parts = [build(ctx) for build in slots.values(slots.BOARD_PACKET_SECTIONS, association)]
    summary_rows = [row for part in parts for row in part.summary_rows]
    sentences = story.money_sentences(association, start, end) + [
        s for part in parts for s in part.sentences
    ]
    summary = _summary(association, end, summary_rows, sentences)
    return [summary] + [t for part in parts for t in part.tables]


def build(association, year: int, month: int) -> bytes:
    from homeoa.letters.services import append_pdfs

    pdfs = [export.to_pdf(s, association=association) for s in specs(association, year, month)]
    return append_pdfs(pdfs[0], pdfs[1:])


@transaction.atomic
def file_and_send(association, year: int, month: int, *, user=None):
    """Store the packet under Documents (board only, one document per month, new version
    when rebuilt) and email it to the board after commit. Returns the version."""
    from homeoa.documents import services as documents
    from homeoa.documents.models import Category, Document, Visibility
    from homeoa.notify.services import board_recipients, notify_after_commit

    start, _end = _bounds(year, month)
    title = _("Board packet · %(m)s") % {"m": _month_label(start)}
    existing = Document.all_associations.filter(association=association, title=title).first()
    version = documents.attach(
        association,
        filename=f"board-packet-{start:%Y-%m}.pdf",
        content=build(association, year, month),
        title=title,
        category=Category.FINANCIAL,
        visibility=Visibility.BOARD,
        user=user,
        document=existing,
    )
    notify_after_commit(
        PACKET_TYPE,
        board_recipients(association),
        {
            "month": _month_label(start),
            "url": getattr(settings, "HOMEOA_BASE_URL", "")
            + reverse("documents:detail", args=[version.document.public_id]),
            "association_name": association.name,
        },
        obj=version,
        association=association,
        force_channels=(ch.EMAIL, ch.INAPP),
        attachments=[
            {
                "name": f"board-packet-{start:%Y-%m}.pdf",
                "content_type": "application/pdf",
                "file_key": version.file_key,
            }
        ],
    )
    return version


def after_close(association, year: int, month: int, *, user=None) -> None:
    """Called once a month close commits. A failure is logged; it never undoes the close."""
    try:
        file_and_send(association, year, month, user=user)
    except Exception:  # noqa: BLE001 - the close already happened; the board can rebuild
        logger.exception("board packet for %s-%02d failed", year, month)
