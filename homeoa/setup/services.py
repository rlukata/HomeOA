"""Association setup wizard: the board's policy questions, section by section.

The questions, choices, suggested answers and citations come from the active rule pack
(``RulePack.setup``). Finishing a section writes a draft board resolution with the policy
spelled out and a change request (kind ``setup.policy``) that it backs; adopting the
resolution applies the answers (``targets.apply``). Nothing changes before adoption.

A section's state is read from its change requests: adopted (the latest applied one),
waiting for adoption (a pending one) or not started.
"""

from __future__ import annotations

import dataclasses
import functools
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.utils.formats import number_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import rules
from homeoa.changes import kinds
from homeoa.changes import services as changes
from homeoa.changes.models import ChangeRequest
from homeoa.plugins import slots
from homeoa.rules.base import LegalLimit, SetupQuestion, SetupSection

from . import targets

KIND = "setup.policy"
OTHER = "__other__"


class SetupError(ValueError):
    def __init__(self, message="", *, reasons=()):
        self.reasons = list(reasons) or ([str(message)] if message else [])
        super().__init__(message or " ".join(self.reasons))


FEATURES = "features"
MODULE_PREFIX = targets.MODULE_PREFIX


def _deemed_approval_note(association) -> str:
    """With design approval off, the law may still approve EV charger and heat pump
    requests that are not denied in time; say so where the pack sets those deadlines."""
    found = []
    for kind, label in (
        ("ev_charger", gettext_lazy("EV charger")),
        ("heat_pump", gettext_lazy("heat pump")),
    ):
        resolved = rules.resolve(association, f"arc.{kind}.deemed_approval_days")
        if resolved.value is not None:
            found.append((label, resolved.value, resolved.citation))
    if not found:
        return ""
    if len(found) == 2 and found[0][1] == found[1][1]:
        return _(
            "With design approval off, EV charger and heat pump requests are still approved "
            "by law if not denied in writing within %(days)s days (%(citation)s)."
        ) % {"days": found[0][1], "citation": "; ".join(c for _l, _d, c in found if c)}
    return " ".join(
        _(
            "With design approval off, %(kind)s requests are still approved by law if not "
            "denied in writing within %(days)s days (%(citation)s)."
        )
        % {"kind": label, "days": days, "citation": citation}
        for label, days, citation in found
    )


def features_section(association) -> SetupSection:
    """The optional modules as on/off questions. A module the rule pack requires is set
    on by law, with the pack's citation."""
    from homeoa.plugins import registry

    required = rules.active_pack(association).required_modules
    questions = []
    for m in registry.all_modules():
        law = None
        if m.id in required:
            law = LegalLimit(fixed=True, citation=required[m.id])
        questions.append(
            SetupQuestion(
                target=MODULE_PREFIX + m.id,
                label=m.name,
                kind="bool",
                help=m.description,
                suggested=False,
                note=_deemed_approval_note(association) if m.id == "arc" else "",
                law=law,
            )
        )
    return SetupSection(
        key=FEATURES,
        title=gettext_lazy("Features"),
        intro=gettext_lazy(
            "The board picks the optional parts of HomeOA it wants. A part that is off is "
            "hidden from the menu, Home and the owner portal; its records are kept."
        ),
        questions=tuple(questions),
    )


def is_feature(q: SetupQuestion) -> bool:
    return q.target.startswith(MODULE_PREFIX)


def wanted_modules(answers: dict) -> dict[str, bool]:
    """{module id: on} from a Features section's answers."""
    return {
        k[len(MODULE_PREFIX) :]: bool(v) for k, v in answers.items() if k.startswith(MODULE_PREFIX)
    }


def _bound(association, q: SetupQuestion, raw):
    """(value, citation, source url, rule) of one legal bound today; value ``None`` when
    it does not bind (absent, or a rule with no value in force); rule ``None`` for a
    plain number."""
    if raw is None:
        return None, "", "", None
    if isinstance(raw, str):
        rule = rules.pack_rule(association, raw.removeprefix("rule:"))
        if rule is None or rule.value is None:
            return None, "", "", None
        value = Decimal(str(rule.value))
        cite, url = rule.citation, rule.source_url
    else:
        value, cite, url, rule = Decimal(str(raw)), "", "", None
    return (int(value) if q.kind == "days" else value), cite, url, rule


def _resolved_law(association, q: SetupQuestion) -> SetupQuestion:
    """The question with its legal bounds read for today: plain numbers, the citation
    filled from the rules when the limit has none, and no limit when no bound binds."""
    law = q.law
    if law is None or not law.is_range:
        return q
    lo, lo_cite, lo_url, lo_rule = _bound(association, q, law.minimum)
    hi, hi_cite, hi_url, hi_rule = _bound(association, q, law.maximum)
    if lo is None and hi is None:
        return dataclasses.replace(q, law=None)
    citation = law.citation or "; ".join(dict.fromkeys(c for c in (lo_cite, hi_cite) if c))
    # A limit read from the pack's rules is as verified as those rules, and needs the
    # attorney when one of them does.
    bound_rules = [r for v, r in ((lo, lo_rule), (hi, hi_rule)) if v is not None and r]
    verified = law.verified or (
        len(bound_rules) == sum(v is not None for v in (lo, hi))
        and all(r.verified for r in bound_rules)
    )
    attorney = law.attorney_review or any(r.attorney_review for r in bound_rules)
    return dataclasses.replace(
        q,
        law=dataclasses.replace(
            law,
            minimum=lo,
            maximum=hi,
            citation=citation,
            source_url=law.source_url or lo_url or hi_url,
            verified=verified,
            attorney_review=attorney,
        ),
    )


def sections(association) -> tuple[SetupSection, ...]:
    """The sections the wizard shows: Features first, then the pack's. Pack sections
    about a module that is off are left out, and so are the questions about one. Legal
    bounds that point at rules are read for the association's today."""
    on = functools.partial(slots.module_on, association=association)
    return (features_section(association),) + tuple(
        dataclasses.replace(
            s,
            questions=tuple(_resolved_law(association, q) for q in s.questions if on(q.module)),
        )
        for s in rules.active_pack(association).setup
        if on(s.module)
    )


def section(association, key: str) -> SetupSection | None:
    return next((s for s in sections(association) if s.key == key), None)


@dataclass
class SectionState:
    section: SetupSection
    adopted: ChangeRequest | None
    pending: ChangeRequest | None

    @property
    def done(self) -> bool:
        return self.adopted is not None


def _changes(association, key):
    return ChangeRequest.all_associations.filter(
        association=association, kind=KIND, payload__section=key
    ).select_related("resolution")


def state(association, s: SetupSection) -> SectionState:
    qs = _changes(association, s.key)
    return SectionState(
        section=s,
        adopted=qs.filter(status=ChangeRequest.Status.APPLIED).order_by("-closed_at").first(),
        pending=qs.filter(status=ChangeRequest.Status.PENDING).order_by("-proposed_at").first(),
    )


def states(association) -> list[SectionState]:
    return [state(association, s) for s in sections(association)]


# ------------------------------------------------------------------ answers


def _number(kind: str, raw) -> object:
    text = str(raw).strip().replace("$", "").replace(",", "").rstrip("%").strip()
    if kind == "days":
        try:
            value = int(text)
        except ValueError as exc:
            raise SetupError(_("Enter a whole number of days.")) from exc
        if value < 0:
            raise SetupError(_("The number can't be negative."))
        return value
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise SetupError(_("Enter an amount.")) from exc
    if value < 0 or (kind == "percent" and value > 100):
        raise SetupError(
            _("Enter a number from 0 to 100.")
            if kind == "percent"
            else _("The amount can't be negative.")
        )
    return value.quantize(Decimal("0.01")) if kind == "money" else value


def to_json(value):
    return str(value) if isinstance(value, Decimal) else value


def field_name(i: int) -> str:
    return f"q{i}"


def is_fixed(q: SetupQuestion) -> bool:
    return q.law is not None and q.law.is_fixed


def is_ranged(q: SetupQuestion) -> bool:
    """A number answer with a legal minimum or maximum: asked as one exact amount."""
    return q.law is not None and q.law.is_range and q.kind in ("money", "days", "percent")


def shown_amount(q: SetupQuestion, value) -> str:
    """A number in the question's unit, ignoring the choice labels."""
    return shown(dataclasses.replace(q, choices=()), value)


def check_law(q: SetupQuestion, value) -> None:
    """Refuse an answer the law does not allow, naming the statute."""
    law = q.law
    if law is None or law.is_fixed:
        return
    if law.allowed is not None:
        if not any(str(a) == str(value) for a in law.allowed):
            raise SetupError(_("Not allowed by %(citation)s.") % {"citation": law.citation})
        return
    if value is None:
        return
    number = Decimal(str(value))
    if law.minimum is not None and number < Decimal(str(law.minimum)):
        raise SetupError(
            _("At least %(bound)s (%(citation)s).")
            % {"bound": shown_amount(q, law.minimum), "citation": law.citation}
        )
    if law.maximum is not None and number > Decimal(str(law.maximum)):
        raise SetupError(
            _("At most %(bound)s (%(citation)s).")
            % {"bound": shown_amount(q, law.maximum), "citation": law.citation}
        )


def parse(s: SetupSection, data) -> tuple[dict, dict]:
    """Read the posted answers: ({target: JSON value}, {field name: error}). An answer
    the law sets takes the law's value whatever is posted; one outside what the law
    allows is refused."""
    answers, errors = {}, {}
    for i, q in enumerate(s.questions):
        name = field_name(i)
        raw = data.get(name, "")
        if is_fixed(q):
            answers[q.target] = to_json(q.law.fixed)
            continue
        try:
            if is_ranged(q):
                value = _number(q.kind, raw)
            elif raw == OTHER and q.other:
                value = _number(q.kind, data.get(f"{name}-other", ""))
            elif q.choices:
                match = next((c for c in q.choices if str(c.value) == raw), None)
                if match is None:
                    raise SetupError(_("Choose one of the answers."))
                value = match.value
            elif q.kind == "bool":
                value = raw in ("on", "true", "1", "yes")
            else:
                value = _number(q.kind, raw)
            check_law(q, value)
        except SetupError as exc:
            errors[name] = str(exc)
            continue
        answers[q.target] = to_json(value)
    return answers, errors


def initial(association, s: SetupSection) -> dict:
    """{target: value}: the answers last sent for adoption, else the suggestions. The
    Features section starts from a waiting request's answers, else the current switches."""
    st = state(association, s)
    if s.key == FEATURES:
        from homeoa.plugins import modules

        saved = (st.pending.payload or {}).get("answers", {}) if st.pending else {}
        on = modules.switches_for(association)
        values = {
            q.target: saved.get(q.target, on(q.target[len(MODULE_PREFIX) :])) for q in s.questions
        }
    else:
        last = st.pending or st.adopted
        saved = (last.payload or {}).get("answers", {}) if last else {}
        values = {q.target: saved.get(q.target, to_json(q.suggested)) for q in s.questions}
    values.update({q.target: to_json(q.law.fixed) for q in s.questions if is_fixed(q)})
    return values


def shown(q: SetupQuestion, value) -> str:
    """An answer in words, for the resolution and the summary."""
    match = next((c for c in q.choices if str(c.value) == str(value)), None)
    if match is not None:
        return str(match.label)
    if q.kind == "money":
        return f"${Decimal(str(value)):,.2f}"
    if q.kind == "percent":
        return f"{number_format(Decimal(str(value)).normalize())}%"
    if q.kind == "days":
        return _("%(n)s days") % {"n": value}
    if q.kind == "bool" and is_feature(q):
        return _("On") if value else _("Off")
    if q.kind == "bool":
        return _("Yes") if value else _("No")
    return str(value)


def resolution_text(association, s: SetupSection, answers: dict) -> str:
    lines = [
        _(
            "The board of directors of %(association)s adopts the following %(title)s, "
            "effective on adoption."
        )
        % {"association": association.name, "title": str(s.title).lower()},
        "",
    ]
    for n, q in enumerate((q for q in s.questions if q.target in answers), start=1):
        line = f"{n}. {q.label}: {shown(q, answers[q.target])}."
        if is_fixed(q):
            line = f"{n}. {q.label}: {shown(q, answers[q.target])} " + _(
                "(required by %(citation)s)."
            ) % {"citation": q.law.citation}
        elif q.law is not None:
            line = f"{n}. {q.label}: {shown(q, answers[q.target])} " + _(
                "(within the legal limit, %(citation)s)."
            ) % {"citation": q.law.citation}
        if q.citation and q.citation != getattr(q.law, "citation", None):
            line += " " + _("(See %(citation)s.)") % {"citation": q.citation}
        lines.append(line)
    return "\n".join(lines)


def propose(association, s: SetupSection, answers: dict, *, user) -> ChangeRequest:
    """Draft the resolution and the change it backs. One pending draft per section."""
    from homeoa.governance import services as gov

    if state(association, s).pending is not None:
        raise SetupError(
            _("This section already waits for its resolution. Withdraw that request first.")
        )
    if s.key == FEATURES:
        from homeoa.plugins import modules

        refused = modules.set_refusal(association, wanted_modules(answers))
        if refused is not None:
            raise SetupError(reasons=refused.reasons)
    title = _("Policy: %(title)s") % {"title": s.title}
    resolution = gov.create_resolution(
        association, title=title, text=resolution_text(association, s, answers), user=user
    )
    details = [(q.label, shown(q, answers[q.target])) for q in s.questions if q.target in answers]
    return changes.propose(
        association,
        KIND,
        {"section": s.key, "title": str(s.title), "answers": answers},
        user=user,
        summary=title,
        details=details,
        resolution=resolution,
    )


def _apply(change) -> str:
    from django.urls import reverse

    p = change.payload
    targets.apply(
        change.association,
        p["answers"],
        resolution=change.resolution,
        title=p.get("title", ""),
        change_request=change,
    )
    return reverse("setup:index")


def register() -> None:
    kinds.register(
        kinds.Kind(KIND, gettext_lazy("Association setup policy"), _apply, by_resolution=True)
    )


def todo_items(association) -> list[dict]:
    from django.urls import reverse

    all_states = states(association)
    if not all_states:
        return []
    done = sum(1 for st in all_states if st.done)
    if done == len(all_states):
        return []
    return [
        {
            "id": "setup-progress",
            "kind": "gov",
            "title": _("Finish association setup: %(done)s of %(total)s policies adopted")
            % {"done": done, "total": len(all_states)},
            "sub": _("Answer the policy questions; each section becomes a board resolution."),
            "url": reverse("setup:index"),
            "action": _("Continue"),
            "perm": "policy.propose",
        }
    ]
