"""The import engine: parse a run's files with its mapping, then dry-run or apply them.

Dry runs use the exact commit code path inside a transaction that is always rolled back, so
the preview report shows precisely what a commit would do and writes nothing.

Ledger rules (money via ``post_entry`` only):

* **History** rows (owner ledger) post one entry each, dated in the past:
  a charge is Dr receivable / Cr the type's counter account (assessment income, late fee
  income, ...); a payment or credit is Dr counter account (cash for payments) / Cr receivable,
  with any excess over what the unit owed going to prepaid. Charges use up prepaid credit.
* **Opening balances** post one entry against the opening-equity account, dated the day
  before the earliest history row (the cutover start), or ``as_of`` with no history. It holds
  whatever is needed so that, at ``as_of``, every account and fund matches the source trial
  balance and every unit's balance matches the source owner balances. Per-fund differences
  are plugged to the opening-equity account in that fund so each fund balances.

Re-imports are keyed by :class:`ExternalRef`: unchanged records are left alone, changed
registry fields are updated, and a changed ledger row is reversed and reposted.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, AccountType, Fund, JournalEntry, JournalLine
from homeoa.ledger.services import LedgerError, Line, post_entry, reverse_entry
from homeoa.registry.models import (
    NoticePreference,
    Occupancy,
    OwnershipPeriod,
    OwnershipShare,
    Person,
    Unit,
)
from homeoa.registry.services import current_notice_preference, record_notice_preference

from .models import AccountMapping, ExternalRef, ImportRun
from .parsing import KINDS, Parsed, Problem, parse_file, split_address
from .profiles import NEW_ACCOUNT, account_choice

ZERO = Decimal("0.00")
HUNDRED = Decimal("100")
OPENING = "imports.opening"
HISTORY = "imports.history"

ACCOUNT_TYPES = {
    "asset": AccountType.ASSET,
    "liability": AccountType.LIABILITY,
    "equity": AccountType.EQUITY,
    "income": AccountType.INCOME,
    "revenue": AccountType.INCOME,
    "expense": AccountType.EXPENSE,
}


class _Rollback(Exception):
    pass


# ---------------------------------------------------------------- report


class Report:
    def __init__(self):
        self.counts: dict[str, dict[str, int]] = defaultdict(
            lambda: {"created": 0, "updated": 0, "unchanged": 0}
        )
        self.changes: list[dict] = []
        self.problems: list[Problem] = []
        self.warnings: list[str] = []
        self.totals: dict[str, int] = {}

    def add(self, section: str, action: str, label: str, fields: dict | None = None):
        self.counts[section][action] += 1
        if action != "unchanged":
            self.changes.append(
                {"section": section, "action": action, "label": label, "fields": fields or {}}
            )

    def problem(self, file, row, column, message):
        self.problems.append(Problem(file, row, column, message))

    def as_dict(self) -> dict:
        return {
            "counts": dict(self.counts),
            "changes": self.changes,
            "change_count": len(self.changes),
            "problems": [p.as_dict() for p in self.problems],
            "warnings": self.warnings,
            "totals": self.totals,
        }


def _s(value) -> str:
    if value is None:
        return ""
    return str(value)


# ---------------------------------------------------------------- parsing a run


def parse_run(run: ImportRun, report: Report) -> dict[str, Parsed]:
    config = run.config or {}
    files_cfg = config.get("files", {})
    columns_cfg = config.get("columns", {})
    date_format = config.get("date_format") or "%m/%d/%Y"
    parsed: dict[str, Parsed] = {}
    for f in run.files.all():
        kind = files_cfg.get(f.name, "")
        if not kind:
            report.warnings.append(_("%(f)s was not used.") % {"f": f.name})
            continue
        if kind not in KINDS:
            report.problem(f.name, None, "", _("Unknown data type."))
            continue
        if kind in parsed:
            report.problem(
                f.name,
                None,
                "",
                _("%(a)s and %(b)s both hold %(k)s; use one file per type.")
                % {"a": parsed[kind].file, "b": f.name, "k": str(KINDS[kind].label).lower()},
            )
            continue
        parsed[kind] = parse_file(
            kind, f.name, f.content, columns_cfg.get(kind, {}), date_format, report.problems
        )
    for kind, p in parsed.items():
        report.totals[kind] = len(p.rows)
    return parsed


def snapshot_json(parsed: dict[str, Parsed]) -> dict:
    """JSON-safe copy of parsed rows (Decimals and dates as strings) for comparison runs."""
    return {
        kind: [{k: _s(v) if k != "_row" else v for k, v in row.items()} for row in p.rows]
        for kind, p in parsed.items()
    }


# ---------------------------------------------------------------- applier


class Applier:
    def __init__(self, run: ImportRun, parsed: dict[str, Parsed], report: Report, user=None):
        self.run = run
        self.a = run.association
        self.src = run.source
        self.parsed = parsed
        self.report = report
        self.user = user if getattr(user, "pk", None) else None
        self.mapped = {
            k: {f for f, c in v.items() if c} for k, v in run.config.get("columns", {}).items()
        }
        self.units: dict[str, Unit] = {}
        self.seen_people: set[str] = set()

    # -- helpers

    def _ref(self, kind, ext):
        return ExternalRef.all_associations.filter(
            association=self.a, source=self.src, kind=kind, external_id=ext[:255]
        ).first()

    def _set_ref(self, kind, ext, object_id, fingerprint=""):
        ExternalRef.all_associations.update_or_create(
            association=self.a,
            source=self.src,
            kind=kind,
            external_id=ext[:255],
            defaults={"object_id": object_id, "fingerprint": fingerprint},
        )

    def _unit(self, number, file, row, column):
        number = _s(number).strip()
        if number not in self.units:
            unit = Unit.all_associations.filter(association=self.a, number=number).first()
            if unit is None:
                self.report.problem(
                    file,
                    row,
                    column,
                    _("Unit %(n)s is not in HomeOA or the unit file.") % {"n": number},
                )
                return None
            self.units[number] = unit
        return self.units[number]

    def _update(self, obj, values: dict) -> dict:
        diffs = {}
        for k, v in values.items():
            old = getattr(obj, k)
            if old != v:
                diffs[k] = [_s(old), _s(v)]
                setattr(obj, k, v)
        return diffs

    def _save(self, obj, file, row):
        try:
            obj.full_clean()
        except ValidationError as e:
            for msg in e.messages:
                self.report.problem(file, row, "", msg)
            return False
        obj.save()
        return True

    # -- registry

    def units_step(self):
        p = self.parsed.get("units")
        if not p:
            return
        mapped = self.mapped.get("units", set())
        for row in p.rows:
            number = row.get("number")
            if not number:
                continue
            values = {}
            if "building" in mapped:
                values["building"] = row["building"]
            if "address" in mapped:
                values.update(split_address(row["address"]))
            if "parcel_id" in mapped:
                values["parcel_id"] = row["parcel_id"]
            if "allocated_interest" in mapped and row.get("allocated_interest") is not None:
                values["allocated_interest"] = row["allocated_interest"].quantize(Decimal("0.0001"))
            unit = Unit.all_associations.filter(association=self.a, number=number).first()
            label = _("Unit %(n)s") % {"n": number}
            if unit is None:
                unit = Unit(association=self.a, number=number, **values)
                if self._save(unit, p.file, row["_row"]):
                    self.report.add("units", "created", label)
            else:
                diffs = self._update(unit, values)
                if diffs and self._save(unit, p.file, row["_row"]):
                    self.report.add("units", "updated", label, diffs)
                elif not diffs:
                    self.report.add("units", "unchanged", label)
            self.units[number] = unit

    def _person(self, kind, row, file, unit_number) -> Person | None:
        name = row.get("name") or ""
        email = (row.get("email") or "").strip()
        key = email.lower() or f"{unit_number}|{name.lower()}"
        values = {"full_name": name}
        if "email" in self.mapped.get(kind, set()):
            values["email"] = email
        if "phone" in self.mapped.get(kind, set()):
            values["phone"] = row.get("phone") or ""
        if row.get("mailing_address"):
            values.update(split_address(row["mailing_address"]))
        ref = self._ref("person", key)
        person = Person.all_associations.filter(pk=ref.object_id).first() if ref else None
        if person is None and email:
            person = (
                Person.all_associations.filter(association=self.a, email__iexact=email)
                .order_by("pk")
                .first()
            )
        first = key not in self.seen_people
        self.seen_people.add(key)
        if person is None:
            person = Person(association=self.a, **values)
            if not self._save(person, file, row["_row"]):
                return None
            self.report.add("people", "created", name)
        else:
            diffs = self._update(person, values)
            if diffs:
                if not self._save(person, file, row["_row"]):
                    return None
                self.report.add("people", "updated", name, diffs)
            elif first:
                self.report.add("people", "unchanged", name)
        if ref is None or ref.object_id != person.pk:
            self._set_ref("person", key, person.pk)
        return person

    def owners_step(self):
        p = self.parsed.get("owners")
        if not p:
            return
        groups: dict[str, dict[tuple, list]] = defaultdict(lambda: defaultdict(list))
        for row in p.rows:
            if row.get("unit") and row.get("start"):
                groups[row["unit"]][(row["start"], row.get("end"))].append(row)
        for number, periods in groups.items():
            first_row = next(iter(periods.values()))[0]
            unit = self._unit(number, p.file, first_row["_row"], "unit")
            if unit is None:
                continue
            for (start, end), rows in sorted(periods.items(), key=lambda kv: kv[0][0]):
                people = []
                for row in rows:
                    person = self._person("owners", row, p.file, number)
                    if person is not None and person not in people:
                        people.append(person)
                        if end is None or end >= self.run.as_of:
                            self._notice(person, row)
                if people:
                    self._period(unit, start, end, people, p.file, rows[0]["_row"])

    def _period(self, unit, start, end, people, file, row):
        label = _("Ownership of unit %(n)s from %(d)s") % {"n": unit.number, "d": start.isoformat()}
        period = OwnershipPeriod.all_associations.filter(unit=unit, start=start).first()
        if period is None:
            period = OwnershipPeriod(association=self.a, unit=unit, start=start, end=end)
            if not self._save(period, file, row):
                return
            self._shares(period, people)
            self.report.add("ownership", "created", label)
            return
        diffs = self._update(period, {"end": end})
        current = [s.person_id for s in period.shares.all()]
        if sorted(current) != sorted(p.pk for p in people):
            diffs["owners"] = [
                ", ".join(s.person.full_name for s in period.shares.all()),
                ", ".join(p.full_name for p in people),
            ]
            period.shares.all().delete()
            self._shares(period, people)
        if diffs:
            if self._save(period, file, row):
                self.report.add("ownership", "updated", label, diffs)
        else:
            self.report.add("ownership", "unchanged", label)

    def _shares(self, period, people):
        n = len(people)
        each = (HUNDRED / n).quantize(Decimal("0.0001"))
        for i, person in enumerate(people):
            OwnershipShare.all_associations.create(
                association=self.a,
                period=period,
                person=person,
                percent=HUNDRED - each * (n - 1) if i == 0 else each,
                is_primary=i == 0,
            )

    def _notice(self, person, row):
        """The AppFolio "E-Delivery" flag is never consent. It is kept only as a note on
        a postal record (and the email stays the person's designated address); owners give
        consent in HomeOA or on a signed paper form."""
        wants = row.get("e_delivery")
        if wants is None:
            return
        note = "Imported e-delivery: yes" if wants else "Imported e-delivery: no"
        current = current_notice_preference(person)
        if current is not None and (
            current.source != NoticePreference.Source.IMPORT or current.note == note
        ):
            return
        record_notice_preference(
            person,
            NoticePreference.Delivery.POSTAL,
            e_delivery_consent=False,
            source=NoticePreference.Source.IMPORT,
            recorded_by=self.user,
            note=note,
        )
        self.report.add(
            "notices",
            "updated" if current else "created",
            person.full_name,
            {"note": [current.note if current else "", note]},
        )

    def tenants_step(self):
        p = self.parsed.get("tenants")
        if not p:
            return
        for row in p.rows:
            if not (row.get("unit") and row.get("start") and row.get("name")):
                continue
            unit = self._unit(row["unit"], p.file, row["_row"], "unit")
            if unit is None:
                continue
            person = self._person("tenants", row, p.file, row["unit"])
            if person is None:
                continue
            key = f"{row['unit']}|{row['name'].lower()}|{row['start'].isoformat()}"
            label = _("%(p)s in unit %(n)s") % {"p": person.full_name, "n": unit.number}
            ref = self._ref("tenancy", key)
            occ = Occupancy.all_associations.filter(pk=ref.object_id).first() if ref else None
            if occ is None:
                occ = Occupancy(
                    association=self.a,
                    unit=unit,
                    person=person,
                    kind=Occupancy.Kind.TENANT,
                    start=row["start"],
                    end=row.get("end"),
                )
                if self._save(occ, p.file, row["_row"]):
                    self._set_ref("tenancy", key, occ.pk)
                    self.report.add("tenants", "created", label)
                continue
            diffs = self._update(occ, {"end": row.get("end"), "person_id": person.pk})
            if diffs:
                if self._save(occ, p.file, row["_row"]):
                    self.report.add("tenants", "updated", label, diffs)
            else:
                self.report.add("tenants", "unchanged", label)

    # -- ledger

    def ledger_setup(self) -> bool:
        if not Fund.all_associations.filter(association=self.a).exists():
            load_chart_of_accounts(self.a)
        self.funds = {f.code: f for f in Fund.all_associations.filter(association=self.a)}
        self.accounts = {a.code: a for a in Account.all_associations.filter(association=self.a)}
        sub = Account.all_associations.filter(association=self.a, in_unit_subledger=True)
        self.ar = sub.filter(type=AccountType.ASSET).order_by("code").first()
        self.prepaid = sub.filter(type=AccountType.LIABILITY).order_by("code").first()
        self.op_fund = (
            Fund.all_associations.filter(association=self.a, is_reserve=False)
            .order_by("sort_order", "code")
            .first()
        )
        self.ext_accounts: dict[str, tuple[Account, Fund]] = {}
        return True

    def _fund_for(self, name: str) -> Fund | None:
        name = (name or "").strip().lower()
        for f in self.funds.values():
            if name and name in {f.code.lower(), f.name.lower()}:
                return f
        return self.op_fund

    def accounts_step(self):
        p = self.parsed.get("accounts")
        if not p:
            return
        for row in p.rows:
            code = row.get("code")
            if not code:
                continue
            choice = account_choice(self.run.config, code, row.get("name"), self.src)
            target = choice.get("account") or (code if code in self.accounts else NEW_ACCOUNT)
            if target == NEW_ACCOUNT:
                acct_type = ACCOUNT_TYPES.get((row.get("type") or "").strip().lower())
                if acct_type is None:
                    self.report.problem(
                        p.file,
                        row["_row"],
                        "type",
                        _("“%(t)s” is not Asset, Liability, Equity, Income or Expense.")
                        % {"t": row.get("type")},
                    )
                    continue
                new_code = code if code not in self.accounts else f"{code}-{self.src}"
                account = Account.all_associations.filter(association=self.a, code=new_code).first()
                if account is None:
                    account = Account.all_associations.create(
                        association=self.a, code=new_code, name=row["name"], type=acct_type
                    )
                    self.report.add("accounts", "created", str(account))
                self.accounts[new_code] = account
            else:
                account = self.accounts.get(target)
                if account is None:
                    self.report.problem(
                        p.file,
                        row["_row"],
                        "code",
                        _("HomeOA has no account %(c)s to map %(e)s to.")
                        % {"c": target, "e": code},
                    )
                    continue
            fund = self.funds.get(choice.get("fund", "")) or self._fund_for(row.get("fund"))
            self.ext_accounts[code] = (account, fund)
            label = f"{code} {row['name']}"
            m = AccountMapping.all_associations.filter(
                association=self.a, source=self.src, external_code=code
            ).first()
            if m is None:
                AccountMapping.all_associations.create(
                    association=self.a,
                    source=self.src,
                    external_code=code,
                    external_name=row["name"],
                    account=account,
                    fund=fund,
                )
                self.report.add("account mappings", "created", label)
            else:
                diffs = self._update(
                    m, {"account_id": account.pk, "fund_id": fund.pk, "external_name": row["name"]}
                )
                if diffs:
                    m.save()
                    self.report.add("account mappings", "updated", label, diffs)
                else:
                    self.report.add("account mappings", "unchanged", label)

    def _ext(self, code) -> tuple[Account, Fund] | None:
        if code in self.ext_accounts:
            return self.ext_accounts[code]
        m = AccountMapping.all_associations.filter(
            association=self.a, source=self.src, external_code=code
        ).first()
        if m:
            return m.account, m.fund
        if code in self.accounts:
            return self.accounts[code], self.op_fund
        return None

    def _period_on(self, unit, on):
        for period in self._periods(unit):
            if period.covers(on):
                return period.pk
        return None

    def _periods(self, unit):
        if not hasattr(self, "_period_cache"):
            self._period_cache = {}
        if unit.pk not in self._period_cache:
            self._period_cache[unit.pk] = list(OwnershipPeriod.all_associations.filter(unit=unit))
        return self._period_cache[unit.pk]

    @staticmethod
    def _fingerprint(date, memo, lines) -> str:
        text = repr(
            (
                date.isoformat(),
                memo,
                sorted(
                    (
                        ln.account.code,
                        ln.fund.code,
                        str(ln.debit),
                        str(ln.credit),
                        ln.unit_id,
                        ln.ownership_period_id,
                    )
                    for ln in lines
                ),
            )
        )
        return hashlib.sha256(text.encode()).hexdigest()

    def _post_or_update(self, kind, key, date, memo, lines, label, source_type):
        """Post ``lines`` for ``key`` unless an identical entry is already there."""
        fp = self._fingerprint(date, memo, lines) if lines else ""
        ref = self._ref(kind, key)
        old = JournalEntry.all_associations.filter(pk=ref.object_id).first() if ref else None
        if old is not None and ref.fingerprint == fp:
            self.report.add(kind, "unchanged", label)
            return
        try:
            if old is not None and not hasattr(old, "reversed_by"):
                reverse_entry(
                    old, date=old.date, posted_by=self.user, memo=_("Replaced by re-import")
                )
            if not lines:
                ExternalRef.all_associations.filter(pk=ref.pk).delete()
                self.report.add(kind, "updated", label, {"amount": [_("posted"), _("removed")]})
                return
            entry = post_entry(
                lines,
                date=date,
                memo=memo[:255],
                source_type=source_type,
                source_id=hashlib.sha256(key.encode()).hexdigest()[:40],
                posted_by=self.user,
                association=self.a,
            )
        except LedgerError as e:
            self.report.problem("", None, "", f"{label}: {e}")
            return
        self._set_ref(kind, key, entry.pk, fp)
        total = sum((ln.debit for ln in lines), ZERO)
        if old is None:
            self.report.add(kind, "created", label, {"amount": ["", f"{total:,.2f}"]})
        else:
            old_total = sum((ln.debit for ln in old.lines.all()), ZERO)
            self.report.add(
                kind, "updated", label, {"amount": [f"{old_total:,.2f}", f"{total:,.2f}"]}
            )

    def history_step(self):
        p = self.parsed.get("history")
        if not p:
            return
        if self.ar is None or self.prepaid is None or self.op_fund is None:
            self.report.problem(
                p.file, None, "", _("HomeOA needs receivable and prepaid accounts.")
            )
            return
        types = self.run.config.get("history_types", {})
        by_unit = defaultdict(list)
        for row in p.rows:
            if row.get("unit") and row.get("date"):
                by_unit[row["unit"]].append(row)
        seen = set()
        for number, rows in by_unit.items():
            unit = self._unit(number, p.file, rows[0]["_row"], "unit")
            if unit is None:
                continue
            rows.sort(key=lambda r: (r["date"], r["_row"]))
            first = rows[0]
            amt0 = (first.get("charge") or ZERO) - (first.get("payment") or ZERO)
            prior = first["balance"] - amt0 if first.get("balance") is not None else ZERO
            ar, prepaid = max(prior, ZERO), max(-prior, ZERO)
            occurrences = defaultdict(int)
            for row in rows:
                typ = row.get("type") or ""
                counter_code = types.get(typ)
                counter = self.accounts.get(counter_code or "")
                if counter is None:
                    self.report.problem(
                        p.file,
                        row["_row"],
                        "type",
                        _("No account is chosen for history type “%(t)s”.") % {"t": typ},
                    )
                    continue
                amount = (row.get("charge") or ZERO) - (row.get("payment") or ZERO)
                base = "|".join(
                    (
                        number,
                        row["date"].isoformat(),
                        typ,
                        row.get("description") or "",
                        row.get("reference") or "",
                    )
                )
                occurrences[base] += 1
                key = f"{base}#{occurrences[base]}"
                seen.add(key)
                period = self._period_on(unit, row["date"])
                tag = {"unit_id": unit.pk, "ownership_period_id": period}
                f = self.op_fund
                lines = []
                if amount > 0:
                    lines += [
                        Line(account=self.ar, fund=f, debit=amount, **tag),
                        Line(account=counter, fund=f, credit=amount),
                    ]
                    apply = min(prepaid, amount)
                    if apply > 0:
                        lines += [
                            Line(account=self.prepaid, fund=f, debit=apply, **tag),
                            Line(account=self.ar, fund=f, credit=apply, **tag),
                        ]
                    ar, prepaid = ar + amount - apply, prepaid - apply
                elif amount < 0:
                    x = -amount
                    to_ar = min(ar, x)
                    lines.append(Line(account=counter, fund=f, debit=x))
                    if to_ar > 0:
                        lines.append(Line(account=self.ar, fund=f, credit=to_ar, **tag))
                    if x - to_ar > 0:
                        lines.append(Line(account=self.prepaid, fund=f, credit=x - to_ar, **tag))
                    ar, prepaid = ar - to_ar, prepaid + x - to_ar
                memo = " · ".join(
                    s
                    for s in (
                        _("Unit %(n)s") % {"n": number},
                        row.get("description") or typ,
                        row.get("reference") or "",
                    )
                    if s
                )
                label = f"{memo} ({row['date']:%m/%d/%Y})"
                self._post_or_update("history", key, row["date"], memo, lines, label, HISTORY)
        for ref in ExternalRef.all_associations.filter(
            association=self.a, source=self.src, kind="history"
        ).exclude(external_id__in=seen):
            self.report.warnings.append(
                _("History row %(k)s is in HomeOA but not in this file; it was left as is.")
                % {"k": ref.external_id.split("#")[0].replace("|", " ")}
            )
        self.history_start = min((r["date"] for r in p.rows if r.get("date")), default=None)

    def opening_step(self):
        tb = self.parsed.get("trial_balance")
        bal = self.parsed.get("balances")
        if not tb and not bal:
            return
        equity = self.accounts.get(self.run.config.get("opening_equity") or "")
        if equity is None:
            self.report.problem("", None, "", _("Choose the opening balance equity account."))
            return
        as_of = self.run.as_of
        start = getattr(self, "history_start", None)
        date = start - dt.timedelta(days=1) if start else as_of
        ref = self._ref("opening", "opening")
        old = JournalEntry.all_associations.filter(pk=ref.object_id).first() if ref else None
        if old is not None and old.date < date:
            date = old.date
        if old is not None and hasattr(old, "reversed_by"):
            old = None

        target: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
        scope_all = bool(tb)
        if tb:
            for row in tb.rows:
                code = row.get("code")
                resolved = self._ext(code)
                if resolved is None:
                    self.report.problem(
                        tb.file,
                        row["_row"],
                        "code",
                        _("Account %(c)s is not in the chart of accounts.") % {"c": code},
                    )
                    continue
                acct, fund = resolved
                target[(acct.pk, fund.pk, None)] += (row.get("debit") or ZERO) - (
                    row.get("credit") or ZERO
                )
        unit_keys = set()
        if bal:
            if self.ar is None or self.prepaid is None:
                self.report.problem(
                    bal.file, None, "", _("HomeOA needs receivable and prepaid accounts.")
                )
                return
            f = self.op_fund.pk
            for row in bal.rows:
                if row.get("balance") is None or not row.get("unit"):
                    continue
                unit = self._unit(row["unit"], bal.file, row["_row"], "unit")
                if unit is None:
                    continue
                b = row["balance"]
                acct = self.ar if b >= 0 else self.prepaid
                target[(acct.pk, f, unit.pk)] += b
                unit_keys |= {(self.ar.pk, f, unit.pk), (self.prepaid.pk, f, unit.pk)}
                if scope_all:
                    target[(acct.pk, f, None)] -= b

        current: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
        qs = (
            JournalLine.all_associations.filter(association=self.a, entry__date__lte=as_of)
            .exclude(entry__source_type=OPENING)
            .values("account_id", "fund_id", "unit_id")
            .annotate(d=Sum("debit"), c=Sum("credit"))
        )
        for r in qs:
            current[(r["account_id"], r["fund_id"], r["unit_id"])] += r["d"] - r["c"]
        keys = (set(target) | set(current)) if scope_all else unit_keys
        delta = {k: target.get(k, ZERO) - current.get(k, ZERO) for k in keys}
        per_fund = defaultdict(lambda: ZERO)
        for (_a, f, _u), v in delta.items():
            per_fund[f] += v
        for f, v in per_fund.items():
            if v:
                k = (equity.pk, f, None)
                delta[k] = delta.get(k, ZERO) - v
        by_pk_acct = {a.pk: a for a in self.accounts.values()}
        by_pk_fund = {f.pk: f for f in self.funds.values()}
        units_by_pk = {u.pk: u for u in Unit.all_associations.filter(association=self.a)}
        lines = []
        for (a, f, u), v in sorted(
            delta.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or 0)
        ):
            if not v:
                continue
            tag = {}
            if u is not None:
                tag = {
                    "unit_id": u,
                    "ownership_period_id": self._period_on(units_by_pk[u], date)
                    or self._period_on(units_by_pk[u], as_of),
                }
            lines.append(
                Line(
                    account=by_pk_acct[a],
                    fund=by_pk_fund[f],
                    debit=v if v > 0 else ZERO,
                    credit=-v if v < 0 else ZERO,
                    **tag,
                )
            )
        if old is None and not lines:
            return
        memo = _("Opening balances from %(s)s as of %(d)s") % {
            "s": self.run.get_source_display(),
            "d": as_of.strftime("%m/%d/%Y"),
        }
        # Fingerprint ignores the memo so a later as_of with the same balances is "no change".
        label = _("Opening balances")
        fp = self._fingerprint(date, "", lines) if lines else ""
        if ref is not None and old is not None and ref.fingerprint == fp:
            self.report.add("opening", "unchanged", label)
            return
        if ref is not None and old is None:
            ref = None
        self._post_or_update_opening(ref, old, date, memo, lines, fp, label)

    def _post_or_update_opening(self, ref, old, date, memo, lines, fp, label):
        try:
            if old is not None:
                reverse_entry(
                    old, date=old.date, posted_by=self.user, memo=_("Replaced by re-import")
                )
            entry = None
            if lines:
                entry = post_entry(
                    lines,
                    date=date,
                    memo=memo,
                    source_type=OPENING,
                    source_id=self.run.pk,
                    posted_by=self.user,
                    association=self.a,
                )
        except LedgerError as e:
            self.report.problem("", None, "", f"{label}: {e}")
            return
        if entry is None:
            ExternalRef.all_associations.filter(
                association=self.a, source=self.src, kind="opening"
            ).delete()
        else:
            self._set_ref("opening", "opening", entry.pk, fp)
        total = sum((ln.debit for ln in lines), ZERO)
        self.report.add(
            "opening",
            "updated" if old else "created",
            label,
            {"amount": ["" if old is None else _("replaced"), f"{total:,.2f}"]},
        )

    def apply(self):
        self.units_step()
        self.owners_step()
        self.tenants_step()
        if any(k in self.parsed for k in ("accounts", "trial_balance", "history", "balances")):
            self.ledger_setup()
            self.accounts_step()
            self.history_step()
            self.opening_step()


# ---------------------------------------------------------------- entry points


def _execute(run: ImportRun, *, dry_run: bool, user=None) -> Report:
    report = Report()
    parsed = parse_run(run, report)
    if run.mode == ImportRun.Mode.COMPARISON:
        if not dry_run and not report.problems:
            run.snapshot = snapshot_json(parsed)
        return report
    if report.problems:
        return report
    try:
        with transaction.atomic():
            Applier(run, parsed, report, user).apply()
            if dry_run or report.problems:
                raise _Rollback
    except _Rollback:
        pass
    return report


def preview(run: ImportRun) -> dict:
    """Dry run: report what a commit would do. Writes nothing but the run's report."""
    report = _execute(run, dry_run=True)
    run.report = report.as_dict()
    run.status = ImportRun.Status.PREVIEWED
    run.save(update_fields=["report", "status"])
    return run.report


def commit(run: ImportRun, user=None) -> dict:
    """Apply the run. All or nothing: any problem rolls every change back."""
    with transaction.atomic():
        run = ImportRun.all_associations.select_for_update().get(pk=run.pk)
        if run.is_done:
            return run.report
        report = _execute(run, dry_run=False, user=user)
        run.report = report.as_dict()
        if report.problems:
            run.status = ImportRun.Status.FAILED
        elif run.mode == ImportRun.Mode.COMPARISON:
            run.status = ImportRun.Status.STORED
        else:
            run.status = ImportRun.Status.COMMITTED
        run.finished_at = timezone.now()
        run.save()
    return run.report
