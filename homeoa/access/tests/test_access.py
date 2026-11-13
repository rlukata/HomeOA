import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import RequestFactory

from homeoa.access import permissions as P
from homeoa.access import services
from homeoa.access.models import Role, RoleAssignment
from homeoa.access.testing import grant, open_residents
from homeoa.accounts import policy
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.registry import services as reg
from homeoa.registry.models import Occupancy, Person, Unit
from homeoa.ui import nav

User = get_user_model()


@pytest.fixture
def assoc(db):
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    with association_context(a):
        yield a


@pytest.fixture
def today(assoc):
    return services.today_for(assoc)


@pytest.fixture
def residents(assoc):
    return open_residents(assoc)


def user(email):
    return User.objects.create_user(email)


def unit(number):
    return Unit.objects.create(number=str(number), allocated_interest=Decimal("9"))


def owner_of(u, email, start=dt.date(2020, 1, 1)):
    usr = user(email)
    person = Person.objects.create(full_name=email, email=email, user=usr)
    reg.set_initial_ownership(u, [person], start)
    return usr, person


# --- the default matrix (feature-matrix "Role x capability") ---------------------------

# capability code -> roles that hold it.
MC = "maintenance_committee"
MATRIX = {
    P.BOARD_SHELL: {"board", MC},
    P.BALANCE_OWN: {"owner", "committee", MC, "board"},
    P.FINANCE_READ: {"board", "cpa"},
    P.FINANCE_SUMMARY: {"owner", "board", "cpa"},
    P.FINANCE_PROPOSE: {"board"},
    P.BILLS_ENTER: {"board", MC},
    P.BILLS_APPROVE: {"board"},
    P.ANNOUNCEMENTS_COMMITTEE: {"committee"},
    P.ANNOUNCEMENTS_MANAGE: {"board"},
    P.DOCUMENTS_TENANT: {"owner", "tenant", "committee", MC, "board"},
    P.DOCUMENTS_READ: {"owner", "committee", MC, "board"},
    P.DOCUMENTS_BOARD: {"board"},
    P.VIOLATIONS_OWN: {"owner", "tenant"},
    P.VIOLATIONS_SUBMIT: {"committee"},
    P.VIOLATIONS_MANAGE: {"board"},
    P.ARCH_SUBMIT: {"owner"},
    P.ARCH_REVIEW: {"committee"},
    P.ARCH_DECIDE: {"board"},
    P.VOTES_VOTE: {"owner", "committee"},
    P.VOTES_ADMINISTER: {"board"},
    P.MAINTENANCE_SUBMIT: {"owner", "tenant"},
    P.MAINTENANCE_MANAGE: {"board", MC},
    P.CALENDAR_READ: {"owner", "tenant", "committee", MC, "board"},
    P.CALENDAR_MANAGE: {"board"},
    P.DIRECTORY_READ: {"owner", "committee", MC, "board"},
    P.CONTACTS_READ: {"owner", "tenant", "committee", MC, "board", "sys_admin"},
    P.GOVERNANCE_READ: {"board"},
    P.VENDORS_READ: {"board", MC, "cpa"},
    P.VENDORS_PROPOSE: {"board", MC},
    P.VENDORS_MANAGE: {"board"},
    P.ROLES_MANAGE: {"board", "sys_admin"},
    P.POLICY_PROPOSE: {"board", "sys_admin"},
    P.SETTINGS_MANAGE: {"sys_admin"},
    P.LETTERS_MANAGE: {"board"},
    P.PLUGINS_MANAGE: {"sys_admin"},
    P.AUDIT_READ: {"board", "sys_admin"},
    P.JOBS_READ: {"board", "sys_admin"},
    P.INSTANCE_MANAGE: {"sys_admin"},
    # Officer titles add these on top of the Board role; no role holds them alone.
    P.FINANCE_MANAGE: set(),
    P.GOVERNANCE_MANAGE: set(),
    P.DOCUMENTS_MANAGE: set(),
}


@pytest.mark.parametrize("code,holders", MATRIX.items())
@pytest.mark.parametrize("role", list(P.DEFAULT_ROLES))
def test_default_matrix(role, code, holders):
    assert (code in P.DEFAULT_ROLES[role]["permissions"]) == (role in holders)


def test_tenant_never_gets_finance_votes_or_directory():
    tenant = P.DEFAULT_ROLES["tenant"]["permissions"]
    banned = {P.FINANCE_READ, P.FINANCE_SUMMARY, P.BALANCE_OWN, P.VOTES_VOTE, P.DIRECTORY_READ}
    assert not tenant & banned


def test_superuser_is_sys_admin_without_association_data(assoc):
    admin = User.objects.create_superuser("root@example.org")
    assert services.permissions_for(admin) == P.DEFAULT_ROLES["sys_admin"]["permissions"]
    assert "sys_admin" in services.roles_for(admin)
    assert not services.has_perm(admin, P.UNITS_READ)
    assert not services.has_perm(admin, P.FINANCE_READ)
    assert not services.has_association_perm(admin, P.AUDIT_READ)


def test_sys_admin_can_be_assigned_and_adds_to_other_roles(assoc):
    u = user("admin@example.org")
    grant(u, "sys_admin", association=assoc)
    grant(u, association=assoc, title="treasurer")
    assert services.has_perm(u, P.PLUGINS_MANAGE)
    assert services.has_perm(u, P.FINANCE_MANAGE)
    assert services.has_association_perm(u, P.AUDIT_READ)


def test_roles_seeded_for_new_association(assoc):
    slugs = set(Role.all_associations.filter(association=assoc).values_list("slug", flat=True))
    assert slugs == set(P.DEFAULT_ROLES)


# --- explicit assignments ---------------------------------------------------------------


def test_board_assignment_and_officer_title(assoc):
    u = user("treasurer@example.org")
    a = grant(u, association=assoc, title="treasurer")
    assert a.get_title_display() == "Treasurer"
    assert services.has_perm(u, P.FINANCE_READ)
    assert services.is_board_member(u, assoc)
    assert "board" in services.roles_for(u)


def test_date_bounded_assignment(assoc, today):
    u = user("b@example.org")
    a = grant(u, association=assoc, start=today + dt.timedelta(days=1))
    assert not services.has_perm(u, P.FINANCE_READ)
    a.start, a.end = today - dt.timedelta(days=10), today - dt.timedelta(days=1)
    a.save()
    services.clear_cache(u)
    assert not services.has_perm(u, P.FINANCE_READ)
    a.end = today
    a.save()
    services.clear_cache(u)
    assert services.has_perm(u, P.FINANCE_READ)


def test_end_assignment_stops_access_today(assoc, today):
    u = user("b@example.org")
    a = grant(u, association=assoc, start=today - dt.timedelta(days=5))
    services.end_assignment(a)
    assert a.end == today - dt.timedelta(days=1)
    assert not services.has_perm(u, P.FINANCE_READ)
    b = grant(u, association=assoc)  # starts today: ending it removes it
    assert services.end_assignment(b) is None
    assert not RoleAssignment.objects.filter(pk=b.pk).exists()


def test_derived_roles_cannot_be_assigned(assoc):
    with pytest.raises(ValueError):
        grant(user("x@example.org"), "owner", association=assoc)


def test_role_scoped_to_its_association(assoc):
    u = user("b@example.org")
    grant(u, association=assoc)
    other = Association.objects.create(name="Other", short_name="O")
    assert not services.has_perm(u, P.FINANCE_READ, association=other)


def test_custom_role(assoc):
    role = Role.objects.create(slug="garden", name="Garden", permissions=[P.CALENDAR_MANAGE])
    u = user("g@example.org")
    grant(u, role.slug, association=assoc)
    assert services.permissions_for(u) == {P.CALENDAR_MANAGE}


def test_assignments_are_audited(assoc):
    u = user("b@example.org")
    a = grant(u, association=assoc)
    assert AuditEvent.objects.filter(object_type="access.roleassignment", object_id=a.pk).exists()


# --- derived owner / tenant (registry) and resident access ------------------------------


def test_owner_and_treasurer_at_once(residents):
    u3 = unit(3)
    usr, _ = owner_of(u3, "o3@example.org")
    grant(usr, association=residents, title="treasurer")
    assert services.roles_for(usr) == {"owner", "board"}
    assert services.units_for(usr) == [u3]


def test_residents_disabled_grants_nothing(assoc):
    u3 = unit(3)
    usr, _ = owner_of(u3, "o3@example.org")
    assert services.permissions_for(usr) == frozenset()
    assert services.units_for(usr) == [u3]  # still computed


def test_own_scope(residents):
    u3, u4 = unit(3), unit(4)
    usr, _ = owner_of(u3, "o3@example.org")
    assert services.has_perm(usr, P.UNITS_OWN, u3)
    assert not services.has_perm(usr, P.UNITS_OWN, u4)
    occ = Occupancy(unit=u4)
    assert not services.has_perm(usr, P.VIOLATIONS_OWN, occ)
    assert not services.has_perm(usr, P.UNITS_READ)


def test_past_owner_loses_access_day_after(residents, today):
    start = today - dt.timedelta(days=30)
    u3, u4 = unit(3), unit(4)
    selling_tomorrow, _ = owner_of(u3, "o3@example.org", start=start)
    sold_yesterday, _ = owner_of(u4, "o4@example.org", start=start)
    buyer = Person.objects.create(full_name="Buyer")
    reg.transfer_ownership(u3, [buyer], today + dt.timedelta(days=1))  # period ends today
    reg.transfer_ownership(u4, [buyer], today)  # period ended yesterday
    assert "owner" in services.roles_for(selling_tomorrow)
    assert services.roles_for(sold_yesterday) == frozenset()
    assert not services.has_perm(sold_yesterday, P.UNITS_OWN, u4)


def test_tenant_derived(residents):
    u5 = unit(5)
    usr = user("t@example.org")
    person = Person.objects.create(full_name="Tenant", user=usr)
    reg.add_occupancy(u5, person, dt.date(2024, 1, 1))
    assert services.roles_for(usr) == {"tenant"}
    assert not services.has_perm(usr, P.FINANCE_SUMMARY)
    assert services.has_perm(usr, P.MAINTENANCE_SUBMIT)
    assert services.units_for(usr) == [u5]


# --- MFA hook ---------------------------------------------------------------------------


def test_mfa_hook(assoc):
    board, other = user("b@example.org"), user("o@example.org")
    grant(board, association=assoc)
    assert not policy.require_mfa_for_board(board, assoc)
    assoc.settings = {"require_mfa_for_board": True}
    assert policy.require_mfa_for_board(board, assoc)
    assert not policy.require_mfa_for_board(other, assoc)


# --- nav, 403, /me/ ---------------------------------------------------------------------


def test_nav_filtering(assoc):
    rf = RequestFactory()
    req = rf.get("/")
    req.association = assoc
    req.user = user("cpa@example.org")
    grant(req.user, "cpa", association=assoc)
    assert [h.id for h in nav.get_hubs(req)] == ["home", "money", "vendors"]
    req.user = user("nobody@example.org")
    assert nav.get_hubs(req) == []


def test_resident_403_and_me_redirect(client, residents):
    usr, _ = owner_of(unit(3), "o3@example.org")
    unit(4)
    client.force_login(usr)
    resp = client.get("/")
    assert resp.status_code == 302 and resp["Location"] == "/me/"
    assert client.get("/me/").status_code == 200
    for url in (
        "/money/",
        "/units/",
        "/units/4/",
        "/units/people/",
        "/money/reports/trial-balance/",
        "/settings/roles/",
        "/settings/plugins/",
        "/settings/audit/",
        "/settings/jobs/",
        "/governance/votes/",
    ):
        assert client.get(url).status_code == 403, url
    html = client.get("/me/").content.decode()
    assert ">Money</a>" not in html
    assert client.get("/units/3/").status_code == 403  # own unit too: residents use /me/


def test_board_sees_everything(client, assoc):
    usr = user("b@example.org")
    grant(usr, association=assoc)
    client.force_login(usr)
    html = client.get("/").content.decode()
    assert ">Money</a>" in html and ">Documents</a>" in html
    for url in ("/settings/roles/", "/settings/audit/", "/settings/jobs/", "/settings/"):
        assert client.get(url).status_code == 200, url


def test_cpa_reads_reports_not_units(client, assoc):
    usr = user("cpa@example.org")
    grant(usr, "cpa", association=assoc)
    client.force_login(usr)
    assert client.get("/money/reports/trial-balance/").status_code == 200
    assert client.get("/units/").status_code == 403


def test_anonymous_redirects_to_login(client, assoc):
    resp = client.get("/settings/roles/")
    assert resp.status_code == 302 and resp["Location"].startswith("/login/")


# --- roles UI ---------------------------------------------------------------------------


@pytest.fixture
def board_client(client, assoc):
    usr = user("board@example.org")
    grant(usr, association=assoc)
    client.force_login(usr)
    return client


def test_roles_page_assign_and_end(board_client, assoc, today):
    html = board_client.get("/settings/roles/").content.decode()
    assert "Require MFA for board roles" in html and "board@example.org" in html
    board_role = Role.objects.get(slug="board")
    resp = board_client.post(
        "/settings/roles/assign/",
        {
            "email": "secretary@example.org",
            "role": board_role.pk,
            "title": "secretary",
            "start": today.isoformat(),
        },
    )
    assert resp.status_code == 302
    sec = User.objects.get(email="secretary@example.org")
    a = RoleAssignment.objects.get(user=sec)
    assert a.title == "secretary" and services.is_board_member(sec, assoc)
    board_client.post(f"/settings/roles/{a.pk}/end/")
    services.clear_cache(sec)
    assert not services.is_board_member(sec, assoc)


def test_roles_form_rejects_derived_role(board_client):
    owner_role = Role.objects.get(slug="owner")
    resp = board_client.post(
        "/settings/roles/assign/",
        {"email": "x@example.org", "role": owner_role.pk, "start": "2026-01-01"},
    )
    assert resp.status_code == 200 and not RoleAssignment.objects.filter(role=owner_role).exists()


def test_roles_page_edit_title_in_place(board_client, assoc):
    """Edit changes the row itself; the audit log keeps what it was before."""
    member = user("member@example.org")
    a = grant(member, association=assoc, title="president")
    resp = board_client.post(
        f"/settings/roles/{a.pk}/edit/",
        {"title": "treasurer", "start": a.start.isoformat(), "end": ""},
    )
    assert resp.status_code == 302
    a.refresh_from_db()
    assert a.title == "treasurer"
    assert RoleAssignment.objects.filter(user=member).count() == 1
    services.clear_cache(member)
    assert services.has_perm(member, P.FINANCE_MANAGE, association=assoc)
    html = board_client.get(f"/settings/roles/{a.pk}/edit/").content.decode()
    assert "President → Treasurer" in html


def test_roles_page_edit_rejects_end_before_start(board_client, assoc, today):
    a = grant(user("member@example.org"), association=assoc, start=today)
    resp = board_client.post(
        f"/settings/roles/{a.pk}/edit/",
        {
            "title": "",
            "start": today.isoformat(),
            "end": (today - dt.timedelta(days=1)).isoformat(),
        },
    )
    assert resp.status_code == 200
    a.refresh_from_db()
    assert a.end is None


def test_one_officer_title_per_person(assoc, today):
    member = user("member@example.org")
    first = grant(member, association=assoc, title="treasurer", start=today)
    with pytest.raises(ValidationError):
        grant(member, association=assoc, title="president", start=today)
    grant(member, association=assoc, start=today)  # a second board term with no title is fine
    services.end_assignment(first, end=today)
    grant(member, association=assoc, title="president", start=today + dt.timedelta(days=1))


def test_roles_page_edit_refuses_a_second_title(board_client, assoc):
    member = user("member@example.org")
    grant(member, association=assoc, title="treasurer")
    other = grant(member, association=assoc)
    resp = board_client.post(
        f"/settings/roles/{other.pk}/edit/",
        {"title": "president", "start": other.start.isoformat(), "end": ""},
    )
    assert resp.status_code == 200 and "only one officer title" in resp.content.decode()
    other.refresh_from_db()
    assert other.title == ""


def test_roles_page_shows_each_message_once(board_client, assoc):
    a = grant(user("member@example.org"), association=assoc)
    resp = board_client.post(f"/settings/roles/{a.pk}/end/", follow=True)
    assert resp.content.decode().count("Role ended.") == 1


def test_custom_role_ui(board_client):
    board_client.post(
        "/settings/roles/custom/",
        {"name": "Garden committee", "permissions": [P.CALENDAR_MANAGE, P.CALENDAR_READ]},
    )
    role = Role.objects.get(slug="garden-committee")
    assert role.permission_set == {P.CALENDAR_MANAGE, P.CALENDAR_READ}


def test_mfa_toggle(board_client, assoc):
    board_client.post("/settings/roles/security/", {"require_mfa_for_board": "on"})
    assoc.refresh_from_db()
    assert assoc.settings["require_mfa_for_board"] is True
    assert AuditEvent.objects.filter(action="association.mfa_policy").exists()


# --- grant_role command -----------------------------------------------------------------


def test_grant_role_command(assoc, capsys):
    call_command("grant_role", "Treasurer@Example.org", "board", "--title", "treasurer")
    u = User.objects.get(email="treasurer@example.org")
    assert services.is_board_member(u, assoc)
    call_command("grant_role", "treasurer@example.org", "board")
    assert RoleAssignment.objects.filter(user=u).count() == 1
    assert "already holds" in capsys.readouterr().out
    with pytest.raises(CommandError):
        call_command("grant_role", "x@example.org", "owner")
    with pytest.raises(CommandError):
        call_command("grant_role", "x@example.org", "nope")


def test_grant_role_creates_association(db):
    with pytest.raises(CommandError):
        call_command("grant_role", "b@example.org", "board")
    call_command("grant_role", "b@example.org", "board", "--create-association", "Cedar Row")
    a = Association.objects.get()
    assert a.short_name == "CR"
    assert services.is_board_member(User.objects.get(email="b@example.org"), a)


def test_owner_gets_403_on_unit_pages_including_own(client, residents):
    usr, _ = owner_of(unit(3), "o3@example.org")
    unit(4)
    client.force_login(usr)
    assert client.get("/units/3/").status_code == 403
    assert client.get("/units/4/").status_code == 403


def test_instance_admin_audit_log_sees_only_instance_events(client, assoc):
    from homeoa import audit

    admin = User.objects.create_superuser("root@example.org")
    audit.record("payment.record", object_type="payments.payment", object_id=1, association=assoc)
    audit.record("association.settings", assoc, message="settings change")
    with association_context(None):
        audit.record("login.failed", object_type="accounts.user", object_id=99, association=None)
    client.force_login(admin)
    html = client.get("/settings/audit/").content.decode()
    assert AuditEvent.all_associations.get(action="login.failed").association_id is None
    assert "login.failed" in html
    assert "payment.record" not in html and "association.settings" not in html
    grant(admin, association=assoc)
    html = client.get("/settings/audit/").content.decode()
    assert "payment.record" in html


# --- money access ------------------------------------------------------------------


def test_treasurer_title_grants_finance_manage(assoc):
    plain = User.objects.create_user("b@example.org")
    grant(plain, association=assoc)
    treas = User.objects.create_user("t@example.org")
    grant(treas, association=assoc, title="treasurer")
    assert services.has_perm(plain, P.FINANCE_READ)
    assert not services.has_perm(plain, P.FINANCE_MANAGE)
    assert services.has_perm(treas, P.FINANCE_MANAGE)


def test_officer_titles_add_their_duties(assoc):
    """Treasurer: money changes and the CPA package. Secretary: records and documents (and
    anything only the President has). Vice president and President: board access only."""
    holders = {}
    for title in ("president", "vice_president", "treasurer", "secretary"):
        holders[title] = user(f"{title}@example.org")
        grant(holders[title], association=assoc, title=title)
    has = services.has_perm
    assert has(holders["treasurer"], P.FINANCE_MANAGE) and has(holders["treasurer"], P.CPA_PACKAGE)
    assert has(holders["secretary"], P.GOVERNANCE_MANAGE)
    assert has(holders["secretary"], P.DOCUMENTS_MANAGE)
    for title in ("president", "vice_president", "secretary"):
        assert not has(holders[title], P.FINANCE_MANAGE)
    for title in ("president", "vice_president", "treasurer"):
        assert not has(holders[title], P.GOVERNANCE_MANAGE)
        assert not has(holders[title], P.DOCUMENTS_MANAGE)
    assert services.finance_managers(assoc)["treasurers"][0].user == holders["treasurer"]


def test_officer_titles_only_on_board_roles(assoc):
    with pytest.raises(ValidationError, match="only for board members"):
        grant(user("c@example.org"), "committee", association=assoc, title="secretary")


def test_maintenance_committee_holds_at_most_three(assoc, today):
    members = [user(f"m{i}@example.org") for i in range(4)]
    for m in members[:3]:
        grant(m, "maintenance_committee", association=assoc, start=today)
    with pytest.raises(ValidationError, match="at most 3"):
        grant(members[3], "maintenance_committee", association=assoc, start=today)
    seat = RoleAssignment.objects.filter(user=members[0]).get()
    services.end_assignment(seat, end=today)
    grant(members[3], "maintenance_committee", association=assoc, start=today + dt.timedelta(1))
    assert services.has_perm(members[1], P.MAINTENANCE_MANAGE)
    assert not services.has_perm(members[1], P.BILLS_APPROVE)


def test_custom_committee_member_limit(board_client, assoc, today):
    board_client.post(
        "/settings/roles/custom/",
        {"name": "Garden", "permissions": [P.CALENDAR_READ], "member_limit": "1"},
    )
    garden = Role.objects.get(slug="garden")
    assert garden.limit == 1
    grant(user("g1@example.org"), garden, association=assoc, start=today)
    with pytest.raises(ValidationError):
        grant(user("g2@example.org"), garden, association=assoc, start=today)


def test_roles_page_warns_without_treasurer(assoc, client):
    u = User.objects.create_user("b@example.org")
    grant(u, association=assoc)
    client.force_login(u)
    html = client.get("/settings/roles/").content.decode()
    assert "No Treasurer is assigned" in html
    grant(User.objects.create_user("t@example.org"), association=assoc, title="treasurer")
    html = client.get("/settings/roles/").content.decode()
    assert "No Treasurer is assigned" not in html and "t@example.org · Treasurer" in html


def test_role_changes_wait_for_another_board_member(board_client, assoc, today):
    """With a second board member, assigning a role is a change request until approved."""
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest

    other = user("other@example.org")
    grant(other, association=assoc)
    board_role = Role.objects.get(slug="board")
    resp = board_client.post(
        "/settings/roles/assign/",
        {
            "email": "new@example.org",
            "role": board_role.pk,
            "title": "secretary",
            "start": today.isoformat(),
        },
        follow=True,
    )
    assert "Sent for board approval" in resp.content.decode()
    assert not RoleAssignment.objects.filter(user__email="new@example.org").exists()
    change = ChangeRequest.objects.get()
    assert "Make new@example.org Secretary" in change.summary
    changes.approve(change, other)
    a = RoleAssignment.objects.get(user__email="new@example.org")
    assert a.title == "secretary"


def test_role_change_errors_show_before_proposing(board_client, assoc, today):
    from homeoa.changes.models import ChangeRequest

    grant(user("other@example.org"), association=assoc)
    member = user("member@example.org")
    grant(member, association=assoc, title="treasurer")
    resp = board_client.post(
        "/settings/roles/assign/",
        {
            "email": "member@example.org",
            "role": Role.objects.get(slug="board").pk,
            "title": "president",
            "start": today.isoformat(),
        },
    )
    assert resp.status_code == 200 and "only one officer title" in resp.content.decode()
    assert not ChangeRequest.objects.exists()


def test_nobody_approves_a_change_to_their_own_role(board_client, assoc):
    from homeoa.changes import services as changes

    member = user("member@example.org")
    a = grant(member, association=assoc)
    third = user("third@example.org")
    grant(third, association=assoc)
    board_client.post(
        f"/settings/roles/{a.pk}/edit/",
        {"title": "treasurer", "start": a.start.isoformat(), "end": ""},
    )
    change = changes.pending(assoc).get()
    assert not changes.can_decide(change, member)
    assert changes.waiting_on(change) == [third]
