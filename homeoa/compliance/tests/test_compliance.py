"""Compliance obligations from the rule pack."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from homeoa_rulepack_us_wa.manifest import manifest as wa_manifest

from homeoa import providers
from homeoa.access.testing import grant
from homeoa.compliance import services
from homeoa.compliance.models import ObligationPeriod, PeriodStatus
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.jobs import periodic_jobs
from homeoa.plugins import registry
from homeoa.plugins import services as plugin_services
from homeoa.rules.base import Obligation, ObligationPath, RulePack, RulePackError

OCT_2026 = dt.date(2026, 10, 1)
OCT_2028 = dt.date(2028, 10, 1)


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    registry.register(wa_manifest)
    plugin_services.save_installation(a, "rulepack_us_wa", enabled=True, data={})
    providers.set_choice(a, "RulePack", "rulepack_us_wa")
    with association_context(a):
        yield a


def period(obligation_id, year):
    return ObligationPeriod.objects.get(obligation_id=obligation_id, year=year)


def compliance_todos(a):
    return [t for t in todos.collect(a) if t.source == "compliance"]


def test_job_registered_and_opens_periods(assoc):
    assert "compliance.open_periods" in periodic_jobs()
    opened = services.run(assoc, OCT_2026)
    ids = {p.obligation_id for p in opened}
    assert ids == {
        "annual_audit",
        "reserve_study_update",
        "reserve_study_site_visit",
        "budget_ratification",
    }
    assert services.run(assoc, OCT_2026) == []  # idempotent
    audit = period("annual_audit", 2026)
    assert audit.due_date == dt.date(2026, 12, 31)
    assert audit.status == PeriodStatus.OPEN and not audit.path_id


def test_not_opened_before_window(assoc):
    services.run(assoc, dt.date(2026, 3, 1))
    assert not ObligationPeriod.objects.exists()


def test_audit_paths_2026_and_2028_switch(assoc):
    services.run(assoc, OCT_2026)
    p26 = period("annual_audit", 2026)
    assert p26.citation == "RCW 64.34.372(1)"
    paths = {pv.id: pv for pv in services.paths(p26)}
    assert set(paths) == {"audit", "waiver"}
    assert paths["waiver"].citation == "RCW 64.34.372(1)"
    assert str(paths["waiver"].threshold.value) == "60"

    services.run(assoc, OCT_2028)
    p28 = period("annual_audit", 2028)
    assert p28.citation == "RCW 64.90.530(2)"
    paths = {pv.id: pv for pv in services.paths(p28)}
    assert paths["waiver"].citation == "RCW 64.90.530(2)"
    assert str(paths["waiver"].threshold.value) == "50"


def test_waiver_path_with_vote_link_completes(assoc):
    services.run(assoc, OCT_2026)
    p = period("annual_audit", 2026)
    with pytest.raises(services.ComplianceError):
        services.complete(p)
    services.choose_path(p, "waiver")
    [todo] = [t for t in compliance_todos(assoc) if t.id == f"compliance-{p.pk}"]
    assert todo.title.startswith("Record evidence")
    with pytest.raises(services.ComplianceError):
        services.complete(p)
    with pytest.raises(services.ComplianceError):
        services.add_evidence(p, url="javascript:alert(1)")
    services.add_evidence(p, url="/governance/votes/1/", note="Waiver vote, 7 of 9 units")
    services.complete(p, on=dt.date(2026, 11, 20))
    p.refresh_from_db()
    assert p.status == PeriodStatus.DONE and p.closed_on == dt.date(2026, 11, 20)
    assert not [t for t in compliance_todos(assoc) if t.id == f"compliance-{p.pk}"]
    with pytest.raises(services.ComplianceError):
        services.choose_path(p, "audit")


def test_unknown_path_refused(assoc):
    services.run(assoc, OCT_2026)
    with pytest.raises(services.ComplianceError):
        services.choose_path(period("annual_audit", 2026), "nope")


def test_exemption_yes_closes_period(assoc):
    services.run(assoc, OCT_2026)
    p = period("reserve_study_update", 2026)
    services.answer_exemption(p, "nominal_costs", False)
    p.refresh_from_db()
    assert p.status == PeriodStatus.OPEN and p.exemption_answers == {"nominal_costs": False}
    services.answer_exemption(p, "cost_over_10_percent", True, on=OCT_2026)
    p.refresh_from_db()
    assert p.status == PeriodStatus.EXEMPT
    assert p.exemption_id == "cost_over_10_percent"
    assert p.closed_on == OCT_2026
    assert not [t for t in compliance_todos(assoc) if t.id == f"compliance-{p.pk}"]


def test_missed_deadline_todo(assoc):
    services.run(assoc, OCT_2026)
    p = period("annual_audit", 2026)
    [todo] = [t for t in compliance_todos(assoc) if t.id == f"compliance-{p.pk}"]
    assert todo.title.startswith("Choose how to meet")
    services.run(assoc, dt.date(2027, 1, 2))
    p.refresh_from_db()
    assert p.missed
    [todo] = [t for t in compliance_todos(assoc) if t.id == f"compliance-{p.pk}"]
    assert todo.title.startswith("Missed deadline")
    assert todo.kind == "enforce"
    assert todos.group_of(todo, dt.date(2027, 1, 2)) == "today"


def test_site_visit_every_three_years(assoc):
    services.run(assoc, OCT_2026)
    p = period("reserve_study_site_visit", 2026)
    services.choose_path(p, "professional_site_visit")
    services.add_evidence(p, url="https://example.org/study.pdf")
    services.complete(p)
    services.run(assoc, dt.date(2027, 10, 1))
    services.run(assoc, dt.date(2028, 10, 1))
    assert not ObligationPeriod.objects.filter(
        obligation_id="reserve_study_site_visit", year__in=[2027, 2028]
    ).exists()
    services.run(assoc, dt.date(2029, 10, 1))
    assert period("reserve_study_site_visit", 2029).is_open


def test_generic_pack_has_no_obligations(db):
    a = Association.objects.create(name="Plain", short_name="P")
    with association_context(a):
        assert services.run(a, OCT_2026) == []


def test_pack_validation():
    path = ObligationPath(id="a", label="A")
    with pytest.raises(RulePackError):
        RulePack(
            id="x",
            name="x",
            jurisdiction="",
            values={},
            obligations=(Obligation(id="o", title="o"),),
        )
    with pytest.raises(RulePackError):
        RulePack(
            id="x",
            name="x",
            jurisdiction="",
            values={},
            obligations=(
                Obligation(id="o", title="o", paths=(path,), effective_to=dt.date(2027, 12, 31)),
                Obligation(id="o", title="o", paths=(path,), effective_from=dt.date(2027, 6, 1)),
            ),
        )


# --- views ------------------------------------------------------------------------------


@pytest.fixture
def board_client(client, assoc):
    user = get_user_model().objects.create_user(email="board@example.org")
    grant(user, association=assoc, start=dt.date(2026, 1, 1))
    client.force_login(user)
    return client


def test_pages_and_actions(board_client, assoc, settings):
    settings.HOMEOA_FIXED_TODAY = OCT_2026.isoformat()
    resp = board_client.post("/governance/compliance/check/")
    assert resp.status_code == 302
    resp = board_client.get("/governance/compliance/")
    assert resp.status_code == 200
    assert b"Annual audit or waiver vote" in resp.content
    p = period("annual_audit", 2026)
    url = f"/governance/compliance/{p.pk}/"
    resp = board_client.get(url)
    assert resp.status_code == 200
    assert b"RCW 64.34.372(1)" in resp.content
    assert b"Confirm with attorney" in resp.content

    act = url + "act/"
    board_client.post(act, {"action": "path", "path": "audit"})
    upload = SimpleUploadedFile("audit-2026.pdf", b"%PDF-1.4 x", content_type="application/pdf")
    board_client.post(act, {"action": "evidence", "file": upload, "note": "CPA report"})
    ev = p.evidence.get()
    resp = board_client.get(f"/governance/compliance/evidence/{ev.pk}/")
    assert resp.status_code == 200 and resp.content == b"%PDF-1.4 x"
    board_client.post(act, {"action": "complete"})
    p.refresh_from_db()
    assert p.status == PeriodStatus.DONE and p.path_id == "audit"

    r = period("reserve_study_update", 2026)
    board_client.post(
        f"/governance/compliance/{r.pk}/act/",
        {"action": "exemption", "exemption": "nominal_costs", "answer": "yes"},
    )
    r.refresh_from_db()
    assert r.status == PeriodStatus.EXEMPT


def test_owner_cannot_act(client, assoc):
    services.run(assoc, OCT_2026)
    p = period("annual_audit", 2026)
    user = get_user_model().objects.create_user(email="owner@example.org")
    client.force_login(user)
    resp = client.post(f"/governance/compliance/{p.pk}/act/", {"action": "path", "path": "audit"})
    assert resp.status_code in (302, 403)
    p.refresh_from_db()
    assert not p.path_id


def test_due_date_follows_fiscal_year_end(assoc):
    assoc.fiscal_year_start_month = 7
    assoc.save(update_fields=["fiscal_year_start_month"])
    services.run(assoc, dt.date(2027, 4, 1))
    p = period("annual_audit", 2027)
    assert p.due_date == dt.date(2027, 6, 30)
    assert p.opens_on == dt.date(2027, 6, 30) - dt.timedelta(days=120)
