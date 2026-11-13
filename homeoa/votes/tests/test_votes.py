"""Owner votes, ballots, tally, threshold, quorum and audit (GOV-07)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from homeoa_rulepack_us_wa.manifest import manifest as wa_manifest

from homeoa import providers, rules
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.plugins import registry as plugin_registry
from homeoa.plugins import services as plugin_services
from homeoa.registry import services as registry
from homeoa.registry.models import Person, Unit
from homeoa.votes import services
from homeoa.votes.models import (
    Ballot,
    Channel,
    ClosedVoteError,
    Quorum,
    Threshold,
    Vote,
    VoteKind,
    VoteStatus,
)

UTC = dt.UTC
OPENS = dt.datetime(2026, 11, 1, tzinfo=UTC)
CLOSES = dt.datetime(2026, 11, 30, tzinfo=UTC)
DURING = dt.datetime(2026, 11, 10, tzinfo=UTC)
AFTER = dt.datetime(2026, 12, 2, tzinfo=UTC)

# The demo association's allocated interests, total 100.0000.
PILOT = {
    "A": "11.5556",
    "B": "11.5556",
    "C": "11.5556",
    "D": "10.6667",
    "E": "11.5556",
    "F": "11.1111",
    "G": "10.2221",
    "H": "10.2221",
    "I": "11.5556",
}


def _setup(a):
    owners = {}
    for number, pct in PILOT.items():
        unit = Unit.objects.create(association=a, number=number, allocated_interest=Decimal(pct))
        p = Person.objects.create(association=a, full_name=f"Owner {number}")
        registry.set_initial_ownership(unit, [p], dt.date(2020, 1, 1))
        owners[number] = (unit, p)
    return owners


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


@pytest.fixture
def units(assoc):
    return _setup(assoc)


@pytest.fixture
def board(assoc):
    u = get_user_model().objects.create_user(email="board@example.com", password="x-pass-12345")
    grant(u, "board", association=assoc)
    return u


def make_vote(a, **kw):
    kw.setdefault("title", "Repaint the hallway")
    kw.setdefault("kind", VoteKind.WRITTEN)
    kw.setdefault("opens_at", OPENS)
    kw.setdefault("closes_at", CLOSES)
    kw.setdefault("questions", [("Approve?", services.yes_no_abstain())])
    vote = services.create_vote(a, **kw)
    return services.open_vote(vote)


def choice(vote, label):
    q = vote.questions.get()
    return q, q.choices.get(label=label)


def cast(vote, units, number, label, **kw):
    unit, person = units[number]
    q, c = choice(vote, label)
    kw.setdefault("now", DURING)
    return services.cast_ballot(vote, unit, person, {q: c}, **kw)


def test_weighting_defaults_to_association_setting(assoc, units):
    assert make_vote(assoc).weighting == "per_unit"
    assoc.settings = {"vote_weighting": "allocated_interest"}
    assoc.save()
    assert make_vote(assoc).weighting == "allocated_interest"


def test_per_unit_vs_weighted_tally(assoc, units):
    per_unit = make_vote(assoc, threshold=Threshold.MAJORITY_ALL)
    weighted = make_vote(assoc, threshold=Threshold.MAJORITY_ALL, weighting="allocated_interest")
    yes = ["A", "B", "C", "E", "I"]  # 5 of 9 units; 57.7780% of interest
    for v in (per_unit, weighted):
        for n in yes:
            cast(v, units, n, "Yes")
        for n in ["D", "F", "G", "H"]:
            cast(v, units, n, "No")

    t = services.tally(per_unit)
    r = t.questions[0]
    assert t.total == Decimal("9")
    assert r.affirmative == Decimal("5") and r.passed is True

    t = services.tally(weighted)
    r = t.questions[0]
    assert t.total == Decimal("100.0000")
    assert r.affirmative == Decimal("57.7780")
    assert isinstance(r.affirmative, Decimal)
    assert r.passed is True


def test_threshold_of_all_votes_vs_cast(assoc, units):
    cast_v = make_vote(assoc, threshold=Threshold.MAJORITY_CAST)
    all_v = make_vote(assoc, threshold=Threshold.MAJORITY_ALL)
    for v in (cast_v, all_v):
        for n in ["A", "B", "C"]:
            cast(v, units, n, "Yes")
        cast(v, units, "D", "No")
        cast(v, units, "E", "Abstain")
    # 3 yes of 4 cast passes; 3 of 9 does not.
    rc = services.tally(cast_v).questions[0]
    assert rc.cast == Decimal("4") and rc.answered == Decimal("5")
    assert rc.passed is True
    assert services.tally(all_v).questions[0].passed is False


def test_majority_needs_more_than_half(assoc, units):
    v = make_vote(assoc)
    cast(v, units, "A", "Yes")
    cast(v, units, "B", "No")
    assert services.tally(v).questions[0].passed is False


def test_rule_pack_threshold_generic_default_and_override(assoc, units):
    v = make_vote(assoc, threshold=Threshold.RULE_PACK, pack_kind="audit_waiver")
    rule = services.threshold_rule(v)
    assert rule.source == "default" and rule.percent == Decimal("50")
    rules.set_override(assoc, "votes.audit_waiver.threshold_percent", "60", reason="test")
    rule = services.threshold_rule(v)
    assert rule.percent == Decimal("60") and rule.comparison == "more_than"
    for n in ["A", "B", "C", "D", "E"]:  # 5/9 = 55.6% < 60%
        cast(v, units, n, "Yes")
    assert services.tally(v).questions[0].passed is False
    cast(v, units, "F", "Yes")  # 6/9 = 66.7%
    assert services.tally(v).questions[0].passed is True


def test_wa_pack_threshold_recorded_in_snapshot(assoc, units):
    plugin_registry.register(wa_manifest)
    plugin_services.save_installation(assoc, "rulepack_us_wa", enabled=True, data={})
    providers.set_choice(assoc, "RulePack", "rulepack_us_wa")
    v = make_vote(
        assoc,
        threshold=Threshold.RULE_PACK,
        pack_kind="audit_waiver",
        weighting="allocated_interest",
    )
    for n in ["A", "B", "C", "E", "I"]:  # 57.7780% < 60%
        cast(v, units, n, "Yes")
    v = services.close_vote(v, now=AFTER)
    rule = v.result["threshold_rule"]
    assert rule["percent"] == "60" and "64.34.372" in rule["citation"]
    assert rule["verified"] == rules.resolve(assoc, "votes.audit_waiver.threshold_percent").verified
    assert rule["source"] == "us_wa_condo"
    assert v.result["questions"][0]["passed"] is False


def test_quorum(assoc, units):
    v = make_vote(assoc, quorum=Quorum.CUSTOM, quorum_percent=Decimal("50"))
    for n in ["A", "B", "C"]:
        cast(v, units, n, "Yes")
    t = services.tally(v)
    assert t.quorum_met is False and t.questions[0].passed is False
    cast(v, units, "D", "Abstain")
    cast(v, units, "E", "Abstain")  # abstentions count toward quorum
    t = services.tally(v)
    assert t.quorum_met is True and t.questions[0].passed is True


def test_quorum_from_pack_unset_means_none(assoc, units):
    v = make_vote(assoc, quorum=Quorum.RULE_PACK)
    assert services.quorum_rule(v).percent is None
    rules.set_override(assoc, "meetings.owner.quorum_percent", "25", reason="test")
    assert services.quorum_rule(v).percent == Decimal("25")


def test_same_owner_replacement_ballot(assoc, units):
    v = make_vote(assoc)
    first = cast(v, units, "A", "Yes")
    second = cast(v, units, "A", "No")
    first.refresh_from_db()
    assert first.superseded_by == second and second.active
    r = services.tally(v).questions[0]
    assert r.affirmative == Decimal("0") and r.cast == Decimal("1")
    assert AuditEvent.objects.filter(action="votes.ballot_replaced").exists()


def test_joint_owner_first_wins_unless_replaced(assoc, units):
    unit, owner = units["A"]
    spouse = Person.objects.create(association=assoc, full_name="Spouse A")
    registry.transfer_ownership(unit, [owner, spouse], dt.date(2026, 1, 1))
    v = make_vote(assoc)
    q, yes = choice(v, "Yes")
    _, no = choice(v, "No")
    services.cast_ballot(v, unit, owner, {q: yes}, now=DURING)
    with pytest.raises(services.BallotExistsError):
        services.cast_ballot(v, unit, spouse, {q: no}, now=DURING)
    assert services.tally(v).questions[0].affirmative == Decimal("1")
    services.cast_ballot(v, unit, spouse, {q: no}, now=DURING, replace=True)
    assert services.tally(v).questions[0].affirmative == Decimal("0")
    assert Ballot.objects.filter(vote=v).count() == 2
    assert AuditEvent.objects.filter(action="votes.ballot_replaced").count() == 1


def test_non_owner_cannot_vote(assoc, units):
    v = make_vote(assoc)
    unit, _ = units["A"]
    _, other = units["B"]
    q, yes = choice(v, "Yes")
    with pytest.raises(PermissionDenied):
        services.cast_ballot(v, unit, other, {q: yes}, now=DURING)


def test_paper_and_electronic_merge_without_double_count(assoc, units, board):
    v = make_vote(assoc)
    cast(v, units, "A", "Yes")
    scan = SimpleUploadedFile("a.pdf", b"%PDF-1.4 ballot", content_type="application/pdf")
    paper = cast(
        v, units, "A", "No", channel=Channel.PAPER, entered_by=board, scan=scan, cast_at=DURING
    )
    cast(
        v,
        units,
        "B",
        "Yes",
        channel=Channel.PAPER,
        entered_by=board,
        scan=SimpleUploadedFile("b.pdf", b"%PDF b"),
        cast_at=DURING,
    )
    assert paper.scan_file_key and paper.channel == "paper" and paper.entered_by == board
    t = services.tally(v)
    assert t.participating == Decimal("2")
    r = t.questions[0]
    assert r.affirmative == Decimal("1") and r.cast == Decimal("2")


def test_paper_needs_board_and_scan(assoc, units, board):
    v = make_vote(assoc)
    with pytest.raises(PermissionDenied):
        cast(v, units, "A", "Yes", channel=Channel.PAPER, scan=SimpleUploadedFile("a.pdf", b"x"))
    with pytest.raises(ValidationError):
        cast(v, units, "A", "Yes", channel=Channel.PAPER, entered_by=board)


def test_closed_vote_rejects_ballots(assoc, units):
    v = make_vote(assoc)
    cast(v, units, "A", "Yes")
    with pytest.raises(ClosedVoteError):
        cast(v, units, "B", "Yes", now=AFTER)
    v = services.close_vote(v, now=AFTER)
    assert v.status == VoteStatus.CLOSED
    with pytest.raises(ClosedVoteError):
        cast(v, units, "B", "Yes")
    with pytest.raises(ClosedVoteError):
        services.close_vote(v)
    v.title = "changed"
    with pytest.raises(ClosedVoteError):
        v.save()


def test_draft_vote_rejects_ballots(assoc, units):
    v = services.create_vote(
        assoc,
        title="Draft",
        kind=VoteKind.MEETING,
        opens_at=OPENS,
        closes_at=CLOSES,
        questions=[("Q", services.yes_no_abstain())],
    )
    with pytest.raises(ClosedVoteError):
        cast(v, units, "A", "Yes")


def test_close_snapshot_and_hash(assoc, units):
    v = make_vote(assoc, weighting="allocated_interest")
    cast(v, units, "A", "Yes")
    cast(v, units, "D", "No")
    v = services.close_vote(v, now=AFTER)
    snap = v.result
    assert len(snap["eligible"]) == 9 and snap["total"] == "100.0000"
    assert {e["number"]: e["weight"] for e in snap["eligible"]}["D"] == "10.6667"
    assert snap["questions"][0]["affirmative"] == "11.5556"
    assert len(v.ballots_hash) == 64 and services.verify(v)
    Ballot.objects.filter(vote=v).update(channel="paper")  # tampering
    assert not services.verify(v)
    assert AuditEvent.objects.filter(action="votes.vote_closed").exists()


def test_secret_flag_hides_choice(assoc, units):
    secret = make_vote(assoc, secret=True)
    open_v = make_vote(assoc)
    for v in (secret, open_v):
        cast(v, units, "A", "Yes")
    [row] = services.ballot_rows(secret)
    assert row.ballot.person == units["A"][1] and row.answers is None
    [row] = services.ballot_rows(open_v)
    assert list(row.answers.values())[0].label == "Yes"


def test_answer_must_belong_to_vote(assoc, units):
    v1, v2 = make_vote(assoc), make_vote(assoc)
    unit, person = units["A"]
    q2, yes2 = choice(v2, "Yes")
    with pytest.raises(ValidationError):
        services.cast_ballot(v1, unit, person, {q2: yes2}, now=DURING)


def test_rule_pack_threshold_requires_kind(assoc):
    with pytest.raises(ValidationError):
        make_vote(assoc, threshold=Threshold.RULE_PACK)


def test_isolation(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="A", short_name="A")
    b = Association.objects.create(name="B", short_name="B")
    with association_context(a):
        units_a = _setup(a)
        v = make_vote(a)
    with association_context(b):
        assert Vote.objects.count() == 0
        _setup(b)
        vb = make_vote(b)
        assert services.tally(vb).total == Decimal("9")
        unit, person = units_a["A"]
        q, yes = choice(vb, "Yes")
        with pytest.raises(ValidationError):
            services.cast_ballot(vb, unit, person, {q: yes}, now=DURING)
    with association_context(a):
        assert list(Vote.objects.all()) == [v]


def test_open_vote_records_opened_event_not_plain_update(assoc, units):
    vote = make_vote(assoc)
    events = AuditEvent.objects.filter(object_type="votes.vote", object_id=str(vote.pk))
    assert events.filter(action="votes.opened").count() == 1
    assert not events.filter(action=AuditEvent.ACTION_UPDATE).exists()
