import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa.access import permissions as perms
from homeoa.access.services import assign_role, ensure_default_roles
from homeoa.access.testing import open_residents
from homeoa.core import residents_enabled
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos as dashboard_todos
from homeoa.notify import channels as ch
from homeoa.notify import services, types
from homeoa.notify.models import Delivery, Notification
from homeoa.registry.models import NoticePreference, Person

AS_OF = dt.date(2026, 10, 7)

OPTIONAL = types.NotificationType(
    key="test.reminder",
    label="Reminder",
    default_channels=("inapp", "email"),
    subject_template="Hi {{ recipient.full_name }}, due {{ amount }}",
    body_template="As of {{ as_of }}",
)
MANDATORY = types.NotificationType(
    key="test.legal",
    label="Legal notice",
    default_channels=("email",),
    mandatory=True,
)
BOARD_ONLY = types.NotificationType(
    key="test.board", label="Board alert", audience=types.AUDIENCE_BOARD
)


@pytest.fixture(autouse=True)
def registered():
    for t in (OPTIONAL, MANDATORY, BOARD_ONLY):
        types.register(t)
    yield
    for t in (OPTIONAL, MANDATORY, BOARD_ONLY):
        types.unregister(t.key)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    ensure_default_roles(a)
    with association_context(a):
        yield a


def person(assoc, name, consent=None, email="x@example.com"):
    p = Person.objects.create(association=assoc, full_name=name, email=email)
    if consent is not None:
        NoticePreference.objects.create(
            association=assoc,
            person=p,
            delivery="email" if consent else "postal",
            e_delivery_consent=consent,
            email=email if consent else "",
            source="portal",
        )
    return p


@pytest.fixture
def board_person(assoc):
    user = get_user_model().objects.create_user(email="bd@example.com")
    assign_role(user, assoc, perms.BOARD, start=AS_OF - dt.timedelta(days=30))
    p = person(assoc, "Board Member", consent=True)
    p.user = user
    p.save()
    return p


def enable_residents(assoc):
    open_residents(assoc)


def statuses(note):
    return {d.channel: d.status for d in note.deliveries.all()}


def test_residents_enabled_helper(assoc):
    assert residents_enabled(assoc) is False
    assert residents_enabled(None) is False
    enable_residents(assoc)
    assert residents_enabled(assoc) is True


def test_type_registry_rejects_conflicts():
    with pytest.raises(ValueError):
        types.register(types.NotificationType(key="test.legal", label="Other"))
    with pytest.raises(LookupError):
        types.get("nope")
    with pytest.raises(ValueError):
        types.NotificationType(key="x", label="x", audience="everyone")


def test_routing_by_consent(assoc):
    consenting = person(assoc, "Ann", consent=True)
    paper = person(assoc, "Bob", consent=False)
    unknown = person(assoc, "Cy")
    assert services.channels_for(OPTIONAL, consenting) == ["inapp", "email"]
    assert services.channels_for(OPTIONAL, paper) == ["inapp"]
    assert services.channels_for(MANDATORY, consenting) == ["email"]
    assert services.channels_for(MANDATORY, paper) == ["postal"]
    assert services.channels_for(MANDATORY, unknown) == ["postal"]


def test_consent_uses_latest_record(assoc):
    p = person(assoc, "Ann", consent=True)
    NoticePreference.objects.create(
        association=assoc, person=p, delivery="postal", e_delivery_consent=False
    )
    assert services.channels_for(MANDATORY, p) == ["postal"]


def test_optional_can_be_switched_off(assoc):
    p = person(assoc, "Ann", consent=True)
    services.set_preference(p, OPTIONAL.key, "email", False)
    services.set_preference(p, OPTIONAL.key, "inapp", False)
    assert services.channels_for(OPTIONAL, p) == []
    enable_residents(assoc)
    assert services.notify(OPTIONAL.key, [p], {"amount": "$10"}, AS_OF) == []
    assert Notification.objects.count() == 0


def test_mandatory_cannot_be_turned_off_but_can_change_channel(assoc):
    p = person(assoc, "Ann", consent=True)
    with pytest.raises(services.MandatoryNoticeError):
        services.set_preference(p, MANDATORY.key, "email", False)
    services.set_preference(p, MANDATORY.key, "postal", True)
    services.set_preference(p, MANDATORY.key, "email", False)
    assert services.channels_for(MANDATORY, p) == ["postal"]
    services.set_preference(p, MANDATORY.key, "inapp", True)
    assert services.channels_for(MANDATORY, p) == ["inapp", "postal"]


def test_mandatory_still_goes_out_if_preferences_drift(assoc):
    p = person(assoc, "Ann", consent=True)
    from homeoa.notify.models import Preference

    Preference.objects.create(
        association=assoc, person=p, type_key=MANDATORY.key, channel="email", enabled=False
    )
    assert services.channels_for(MANDATORY, p) == ["email"]


def test_residents_off_creates_preview_and_never_sends(assoc, board_person, monkeypatch):
    sent = []

    class Spy(ch.Channel):
        key = "email"

        def send(self, delivery):
            sent.append(delivery.pk)
            return "msg-1"

    monkeypatch.setitem(ch._channels, "email", Spy())
    owner = person(assoc, "Ann", consent=True)
    owner_note, board_note = services.notify(
        OPTIONAL.key, [owner, board_person], {"amount": "$10"}, AS_OF
    )
    assert statuses(owner_note) == {"inapp": "preview", "email": "preview"}
    assert statuses(board_note) == {"inapp": "delivered", "email": "sent"}
    assert len(sent) == 1
    email = board_note.deliveries.get(channel="email")
    assert email.provider_id == "msg-1" and email.sent_at is not None
    assert board_note.subject == "Hi Board Member, due $10"
    assert board_note.body == "As of Oct. 7, 2026"


def test_residents_on_delivers_and_unknown_channel_stays_queued(assoc, monkeypatch):
    enable_residents(assoc)
    monkeypatch.delitem(ch._channels, "email")
    owner = person(assoc, "Ann", consent=True)
    [note] = services.notify(OPTIONAL.key, [owner], {"amount": "$10"}, AS_OF)
    assert statuses(note) == {"inapp": "delivered", "email": "queued"}


def test_idempotency(assoc, board_person):
    first = services.notify(OPTIONAL.key, [board_person], {}, AS_OF, obj="unit:3", period="2026-10")
    again = services.notify(OPTIONAL.key, [board_person], {}, AS_OF, obj="unit:3", period="2026-10")
    assert first[0].pk == again[0].pk
    assert Notification.objects.count() == 1
    assert Delivery.objects.count() == 2  # inapp + email
    other = services.notify(OPTIONAL.key, [board_person], {}, AS_OF, obj="unit:3", period="2026-11")
    assert other[0].pk != first[0].pk


def test_board_only_type_skips_residents(assoc, board_person):
    owner = person(assoc, "Ann")
    notes = services.notify(BOARD_ONLY.key, [owner, board_person], {}, AS_OF)
    assert [n.recipient for n in notes] == [board_person]


def test_failed_delivery_becomes_board_todo_and_retry(assoc, board_person, monkeypatch):
    class Broken(ch.Channel):
        key = "email"

        def send(self, delivery):
            raise ch.ChannelError("smtp down")

    monkeypatch.setitem(ch._channels, "email", Broken())
    [note] = services.notify(OPTIONAL.key, [board_person], {"amount": "$1"}, AS_OF)
    failed = note.deliveries.get(channel="email")
    assert failed.status == "failed" and failed.error == "smtp down"
    items = [i for i in dashboard_todos.collect(assoc) if i.source == "notify"]
    assert [i.id for i in items] == [f"notify-failed-{failed.pk}"]

    monkeypatch.setitem(ch._channels, "email", ch.InAppChannel())
    services.retry(failed)
    assert failed.status == "delivered"
    assert not [i for i in dashboard_todos.collect(assoc) if i.source == "notify"]


def test_preview_renders_without_storing(assoc):
    r = services.preview(OPTIONAL.key, {"amount": "$5", "recipient": {"full_name": "Ann"}})
    assert r.subject == "Hi Ann, due $5"
    assert Notification.objects.count() == 0


def test_scoped_to_association(assoc, board_person):
    services.notify(OPTIONAL.key, [board_person], {}, AS_OF)
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        assert Notification.objects.count() == 0


# --- board recipients without a linked Person ---------------------------------


def test_board_recipients_include_users_without_person(assoc):
    User = get_user_model()
    linked = User.objects.create_user("linked@example.com")
    bare = User.objects.create_user("bare@example.com")
    resident = User.objects.create_user("res@example.com")
    assign_role(linked, assoc, perms.BOARD, start=AS_OF - dt.timedelta(days=30))
    assign_role(bare, assoc, perms.BOARD, start=AS_OF - dt.timedelta(days=30))
    mine = Person.objects.create(association=assoc, full_name="Linked", user=linked)
    Person.objects.create(association=assoc, full_name="Res", user=resident)

    people = services.board_recipients(assoc)
    assert {p.user_id for p in people} == {linked.pk, bare.pk}
    assert mine in people
    fallback = next(p for p in people if p.user_id == bare.pk)
    assert fallback.email == "bare@example.com"

    first = services.notify("test.board", people, as_of=AS_OF, obj=assoc)
    assert len(first) == 2
    again = services.notify("test.board", services.board_recipients(assoc), as_of=AS_OF, obj=assoc)
    assert {n.pk for n in again} == {n.pk for n in first}
    assert Notification.objects.filter(type_key="test.board").count() == 2
    assert Person.objects.filter(user=bare).count() == 1
