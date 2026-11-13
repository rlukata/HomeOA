import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from homeoa import events
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.registry import services
from homeoa.registry.models import (
    ImmutableRecordError,
    NoticePreference,
    Occupancy,
    OwnershipPeriod,
    Person,
    Unit,
)

D = datetime.date

# Sample roster.
SAMPLE = [
    (1, "A", "Maya Nguyen", "8.60"),
    (2, "A", "James Okafor", "9.68"),
    (3, "A", "Priya Raman", "8.60"),
    (4, "A", "Daniel Kim", "9.68"),
    (5, "A", "Sofia Herrera", "8.60"),
    (6, "A", "Tom & Lisa Becker", "9.68"),
    (7, "B", "Abdi Warsame", "9.68"),
    (8, "B", "Elena Petrova", "8.60"),
    (9, "B", "Marcus Lee", "8.60"),
    (10, "B", "Hannah Sato", "9.68"),
    (11, "B", "Rafael Ortiz", "8.60"),
]


@pytest.fixture
def assoc(db):
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    with association_context(a):
        yield a


@pytest.fixture
def roster(assoc):
    units = {}
    for number, bldg, owner, share in SAMPLE:
        unit = Unit.objects.create(
            number=str(number), building=bldg, allocated_interest=Decimal(share)
        )
        person = Person.objects.create(full_name=owner, email=f"u{number}@example.org")
        services.set_initial_ownership(unit, [person], D(2020, 1, 1))
        units[number] = unit
    chris = Person.objects.create(full_name="Chris Doyle")
    services.add_occupancy(units[5], chris, D(2025, 6, 1))
    return units


@pytest.fixture
def shell(client, assoc):
    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("board@example.org")
    grant(user, association=assoc)
    client.force_login(user)


# ------------------------------------------------------------- models / services


def test_sample_interests_total_100(roster, assoc):
    assert services.interest_total(assoc) == Decimal("100.00")
    assert services.interest_total_ok(assoc)


def test_interest_total_off_is_reported(roster, assoc):
    roster[1].allocated_interest = Decimal("8.61")
    roster[1].save()
    assert services.interest_total(assoc) == Decimal("100.01")
    assert not services.interest_total_ok(assoc)


def test_unit_number_unique_per_association(assoc):
    Unit.objects.create(number="1")
    with pytest.raises(IntegrityError), transaction.atomic():
        Unit.objects.create(number="1")
    other = Association.objects.create(name="Other", short_name="O")
    Unit.all_associations.create(association=other, number="1")


def test_interest_range_constraint(assoc):
    with pytest.raises(IntegrityError), transaction.atomic():
        Unit.objects.create(number="9", allocated_interest=Decimal("100.5"))


def test_person_owns_two_units_and_unit_two_owners(assoc):
    a, b = Unit.objects.create(number="1"), Unit.objects.create(number="2")
    tom = Person.objects.create(full_name="Tom Becker")
    lisa = Person.objects.create(full_name="Lisa Becker")
    services.set_initial_ownership(a, [tom, lisa], D(2020, 1, 1))
    services.set_initial_ownership(b, [(tom, Decimal("100"))], D(2021, 1, 1))
    assert services.current_owners(a, D(2024, 1, 1)) == [tom, lisa]
    shares = OwnershipPeriod.objects.get(unit=a).shares.all()
    assert sum(s.percent for s in shares) == Decimal("100")
    assert services.units_owned_by(tom, D(2024, 1, 1)) == [a, b]
    assert services.units_owned_by(tom, D(2020, 6, 1)) == [a]


def test_three_owners_equal_shares_sum_exactly_100(assoc):
    u = Unit.objects.create(number="1")
    people = [Person.objects.create(full_name=f"P{i}") for i in range(3)]
    p = services.set_initial_ownership(u, people, D(2020, 1, 1))
    assert sum(s.percent for s in p.shares.all()) == Decimal("100")


def test_share_validation(assoc):
    u = Unit.objects.create(number="1")
    x = Person.objects.create(full_name="X")
    y = Person.objects.create(full_name="Y")
    with pytest.raises(ValidationError):
        services.set_initial_ownership(u, [(x, Decimal("60")), (y, Decimal("30"))], D(2020, 1, 1))
    with pytest.raises(ValidationError):
        services.set_initial_ownership(u, [], D(2020, 1, 1))
    assert not OwnershipPeriod.objects.filter(unit=u).exists()


def test_transfer_keeps_history_and_resolves_by_date(roster):
    unit = roster[3]
    prior = services.current_owners(unit, D(2026, 9, 30))[0]
    pat = Person.objects.create(full_name="Pat Newowner")
    received = []
    events.subscribe("ownership.transferred", received.append)
    try:
        new = services.transfer_ownership(unit, [pat], D(2026, 10, 1))
    finally:
        events.unsubscribe("ownership.transferred", received.append)
    old = OwnershipPeriod.objects.get(unit=unit, start=D(2020, 1, 1))
    assert old.end == D(2026, 9, 30)
    assert new.start == D(2026, 10, 1) and new.end is None
    assert services.current_owners(unit, D(2026, 9, 30)) == [prior]
    assert services.current_owners(unit, D(2026, 10, 1)) == [pat]
    assert services.ownership_period_on(unit, D(2019, 1, 1)) is None
    assert services.units_owned_by(prior, D(2026, 10, 2)) == []
    (event,) = received
    assert event.unit_id == unit.pk
    assert event.previous_period_id == old.pk
    assert event.new_period_id == new.pk
    assert event.owner_ids == [pat.pk]


def test_transfer_cannot_rewrite_history(roster):
    pat = Person.objects.create(full_name="Pat")
    with pytest.raises(ValidationError):
        services.transfer_ownership(roster[3], [pat], D(2020, 1, 1))
    with pytest.raises(ValidationError):
        services.transfer_ownership(roster[3], [pat], D(2019, 1, 1))
    assert OwnershipPeriod.objects.get(unit=roster[3]).end is None


def test_initial_ownership_only_once(roster):
    with pytest.raises(ValidationError):
        services.set_initial_ownership(roster[1], [Person.objects.first()], D(2021, 1, 1))


def test_one_open_period_per_unit_db_constraint(roster):
    with pytest.raises(IntegrityError), transaction.atomic():
        OwnershipPeriod.objects.create(unit=roster[1], start=D(2023, 1, 1))


def test_period_overlap_validation(roster):
    p = OwnershipPeriod(
        association=roster[1].association, unit=roster[1], start=D(2019, 1, 1), end=D(2020, 6, 1)
    )
    with pytest.raises(ValidationError):
        p.full_clean()


def test_tenant_on_unit_5(roster):
    (occ,) = services.current_occupants(roster[5], D(2026, 1, 1), kind=Occupancy.Kind.TENANT)
    assert occ.person.full_name == "Chris Doyle"
    assert occ.start == D(2025, 6, 1)
    assert services.current_occupants(roster[5], D(2025, 1, 1)) == []


def test_occupancy_end_before_start_rejected(roster):
    with pytest.raises(ValidationError):
        services.add_occupancy(roster[1], Person.objects.first(), D(2025, 1, 1), end=D(2024, 1, 1))


def test_consent_history_is_append_only(assoc):
    p = Person.objects.create(full_name="Maya", email="maya@example.org")
    first = services.record_notice_preference(
        p, NoticePreference.Delivery.EMAIL, source=NoticePreference.Source.PORTAL
    )
    second = services.record_notice_preference(
        p, NoticePreference.Delivery.POSTAL, source=NoticePreference.Source.PAPER
    )
    assert services.current_notice_preference(p) == second
    assert NoticePreference.objects.filter(person=p).count() == 2
    first.refresh_from_db()
    assert first.delivery == "email" and first.e_delivery_consent and first.email == p.email
    assert not second.e_delivery_consent
    first.note = "edited"
    with pytest.raises(ImmutableRecordError):
        first.save()
    with pytest.raises(ImmutableRecordError):
        first.delete()


def test_email_notices_need_email_and_consent(assoc):
    p = Person.objects.create(full_name="No Email")
    with pytest.raises(ValidationError):
        services.record_notice_preference(
            p, NoticePreference.Delivery.EMAIL, source=NoticePreference.Source.PORTAL
        )
    p.email = "x@example.org"
    with pytest.raises(ValidationError):
        services.record_notice_preference(p, "email", e_delivery_consent=False)


def test_vote_weight_modes(roster, assoc):
    unit = roster[2]
    assert services.vote_weight(unit) == Decimal("1")  # pilot: one vote per unit
    assoc.settings = {"vote_weighting": "allocated_interest"}
    assoc.save()
    unit.refresh_from_db()
    assert services.vote_weight(unit) == Decimal("9.68")
    assoc.settings = {"vote_weighting": "custom"}
    assoc.save()
    unit.refresh_from_db()
    unit.vote_weight = Decimal("2")
    assert services.vote_weight(unit) == Decimal("2")


def test_person_links_to_custom_user(assoc):
    model = get_user_model()
    field = model.USERNAME_FIELD
    user = model.objects.create_user(**{field: "maya@example.org"}, password="pw-test")  # noqa: S106
    p = Person.objects.create(full_name="Maya", user=user)
    assert user.registry_people.get() == p
    with pytest.raises(IntegrityError), transaction.atomic():
        Person.objects.create(full_name="Dup", user=user)


def test_scoped_to_association(roster):
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        assert Unit.objects.count() == 0
        assert Person.objects.count() == 0


def test_writes_are_audited(roster):
    pat = Person.objects.create(full_name="Pat")
    services.transfer_ownership(roster[3], [pat], D(2026, 10, 1))
    types = set(AuditEvent.objects.values_list("object_type", flat=True))
    assert {
        "registry.unit",
        "registry.person",
        "registry.ownershipperiod",
        "registry.ownershipshare",
        "registry.occupancy",
    } <= types
    assert AuditEvent.objects.filter(
        object_type="registry.ownershipperiod", action="update"
    ).exists()


# ------------------------------------------------------------- views


def test_unit_list(client, shell, roster):
    html = client.get("/units/").content.decode()
    assert html.count("<tr") == 12  # header + 11
    assert "8.60%" in html and "9.68%" in html
    assert "100.00%" in html
    assert "Chris Doyle" in html
    assert "not 100%" not in html


def test_unit_list_warns_when_total_off(client, shell, roster):
    roster[1].allocated_interest = Decimal("1")
    roster[1].save()
    assert "not 100%" in client.get("/units/").content.decode()


def test_unit_page(client, shell, roster):
    html = client.get("/units/5/").content.decode()
    assert 'data-testid="current-owner">Sofia Herrera' in html
    assert "Chris Doyle" in html
    assert 'aria-labelledby="history-h"' in html and "Ownership history" in html
    assert client.get("/units/99/").status_code == 404


def test_transfer_view(client, shell, roster):
    assert (
        "New owner name"
        in client.get("/units/3/transfer/", HTTP_HX_REQUEST="true").content.decode()
    )
    assert "New owner name" in client.get("/units/3/?transfer=1").content.decode()
    resp = client.post(
        "/units/3/transfer/",
        {
            "new_owner_name": "Pat Newowner",
            "new_owner_email": "pat@cedarrow.example",
            "effective_date": "2026-10-01",
        },
    )
    assert resp.status_code == 302
    unit = roster[3]
    assert [p.full_name for p in services.current_owners(unit, D(2026, 10, 5))] == ["Pat Newowner"]
    html = client.get("/units/3/").content.decode()
    assert "Priya Raman" in html and "2026-09-30" in html
    # Bad date re-renders with an error and changes nothing.
    resp = client.post(
        "/units/3/transfer/", {"new_owner_name": "Z", "effective_date": "2020-01-01"}
    )
    assert resp.status_code == 200
    assert OwnershipPeriod.objects.filter(unit=unit).count() == 2


def test_transfer_reuses_person_by_email(client, shell, roster):
    client.post(
        "/units/3/transfer/",
        {
            "new_owner_name": "Maya N",
            "new_owner_email": "U1@example.org",
            "effective_date": "2026-10-01",
        },
    )
    maya = Person.objects.get(email="u1@example.org")
    assert services.units_owned_by(maya, D(2026, 10, 2)) == [roster[1], roster[3]]


def test_interests_bulk_save(client, shell, roster, assoc):
    data = {f"unit_{u.pk}": str(u.allocated_interest) for u in roster.values()}
    data[f"unit_{roster[1].pk}"] = "9.00"
    resp = client.post("/units/interests/", data)
    assert resp.status_code == 200  # warns, does not save
    assert "not 100%" in resp.content.decode()
    roster[1].refresh_from_db()
    assert roster[1].allocated_interest == Decimal("8.60")
    resp = client.post("/units/interests/", {**data, "save_anyway": "on"})
    assert resp.status_code == 302
    roster[1].refresh_from_db()
    assert roster[1].allocated_interest == Decimal("9.00")
    data[f"unit_{roster[1].pk}"] = "8.60"
    assert client.post("/units/interests/", data).status_code == 302
    assert services.interest_total_ok(assoc)


def test_add_unit_and_occupant(client, shell, assoc):
    resp = client.post(
        "/units/add/", {"number": "12", "allocated_interest": "0", "vote_weight": "1"}
    )
    assert resp.status_code == 302
    resp = client.post(
        "/units/12/occupants/add/",
        {"name": "Jordan Ellis", "kind": "tenant", "start": "2026-01-01"},
    )
    assert resp.status_code == 302
    occ = Occupancy.objects.get()
    assert occ.person.full_name == "Jordan Ellis"
    resp = client.post(f"/units/12/occupants/{occ.pk}/end/", {"end": "2026-06-30"})
    occ.refresh_from_db()
    assert occ.end == D(2026, 6, 30)


def test_people_pages_and_consent(client, shell, roster):
    html = client.get("/units/people/").content.decode()
    assert "Maya Nguyen" in html and "Chris Doyle" in html
    maya = Person.objects.get(full_name="Maya Nguyen")
    assert client.get(f"/units/people/{maya.pk}/").status_code == 200
    # The board cannot record email notices without a signed paper form.
    client.post(f"/units/people/{maya.pk}/notices/", {"delivery": "email", "source": "board"})
    assert NoticePreference.objects.filter(person=maya).count() == 0
    client.post(f"/units/people/{maya.pk}/notices/", {"delivery": "postal", "source": "board"})
    client.post(f"/units/people/{maya.pk}/notices/", {"delivery": "postal", "source": "paper"})
    assert NoticePreference.objects.filter(person=maya).count() == 2
    resp = client.post(
        f"/units/people/{maya.pk}/edit/", {"full_name": "Maya T. Nguyen", "email": "m@example.org"}
    )
    assert resp.status_code == 302
    maya.refresh_from_db()
    assert maya.full_name == "Maya T. Nguyen"


def test_pages_need_login(client, roster):
    resp = client.get("/units/")
    assert resp.status_code == 302 and resp.url.startswith("/login/")


BLOCK_MSG = "Charges are already posted after this date."


@pytest.fixture
def blocker():
    def block(unit, owners, effective_date):
        raise ValidationError(BLOCK_MSG)

    services.register_transfer_validator(block)
    yield block
    services.unregister_transfer_validator(block)


def test_no_validators_transfer_unchanged(roster, monkeypatch):
    # Other apps (billing) register validators at startup; test the bare path.
    monkeypatch.setattr(services, "_transfer_validators", [])
    pat = Person.objects.create(full_name="Pat")
    services.transfer_ownership(roster[3], [pat], D(2026, 10, 1))
    assert services.current_owners(roster[3], D(2026, 10, 1)) == [pat]


def test_validator_called_with_unit_owners_date(roster):
    seen = []

    def spy(unit, owners, effective_date):
        seen.append((unit.pk, list(owners), effective_date))

    services.register_transfer_validator(spy)
    try:
        pat = Person.objects.create(full_name="Pat")
        services.transfer_ownership(roster[3], [pat], D(2026, 10, 1))
    finally:
        services.unregister_transfer_validator(spy)
    assert seen == [(roster[3].pk, [pat], D(2026, 10, 1))]


def test_blocking_validator_leaves_db_untouched(roster, blocker):
    pat = Person.objects.create(full_name="Pat")
    before = AuditEvent.objects.count()
    with pytest.raises(ValidationError, match=BLOCK_MSG):
        services.transfer_ownership(roster[3], [pat], D(2026, 10, 1))
    period = OwnershipPeriod.objects.get(unit=roster[3])
    assert period.end is None
    assert AuditEvent.objects.count() == before


def test_validator_error_shows_on_form(client, shell, roster, blocker):
    resp = client.post(
        "/units/3/transfer/",
        {
            "new_owner_name": "Pat New",
            "new_owner_email": "pat@x.example",
            "effective_date": "2026-10-01",
        },
    )
    assert resp.status_code == 200
    assert BLOCK_MSG in resp.content.decode()
    assert OwnershipPeriod.objects.filter(unit=roster[3]).count() == 1
    assert not Person.objects.filter(full_name="Pat New").exists()


def test_registry_form_uses_shared_components(client, shell, roster):
    from django.urls import reverse

    html = client.get(reverse("registry:unit_add")).content.decode()
    assert 'class="form-grid"' in html and 'class="field' in html
    assert "<style" not in html and "reg-form" not in html
    assert 'id="messages"' in html and 'aria-live="polite"' in html
    # Submitting empty shows errors linked to the control.
    resp = client.post(reverse("registry:unit_add"), {"building": "C"})
    html = resp.content.decode()
    assert 'id="id_number_error"' in html
    assert 'aria-invalid="true"' in html


def test_ownership_transfer_waits_for_a_second_board_member(client, shell, roster, assoc):
    """A transfer decides votes, balances and statements, so it is a change request even
    for the board; it takes effect once another board member approves."""
    from homeoa.access.testing import grant
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest

    other = get_user_model().objects.create_user("other@example.org")
    grant(other, association=assoc)
    resp = client.post(
        "/units/3/transfer/",
        {"new_owner_name": "Pat Newowner", "effective_date": "2026-10-01"},
    )
    change = ChangeRequest.objects.get()
    assert resp["Location"] == f"/governance/changes/{change.pk}/"
    assert change.summary == "Transfer Unit 3 to Pat Newowner"
    unit = roster[3]
    assert [p.full_name for p in services.current_owners(unit, D(2026, 10, 5))] == ["Priya Raman"]
    changes.approve(change, other)
    assert [p.full_name for p in services.current_owners(unit, D(2026, 10, 5))] == ["Pat Newowner"]
