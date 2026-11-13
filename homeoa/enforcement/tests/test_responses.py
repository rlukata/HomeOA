"""Owners and tenants see and answer violations at their unit (VIO-06)."""

import pytest
from django.urls import reverse

from homeoa.access.testing import open_residents
from homeoa.dashboard import todos
from homeoa.enforcement import ladder, responses
from homeoa.enforcement.models import ViolationResponse, ViolationStep
from homeoa.enforcement.services import EnforcementError
from homeoa.enforcement.tests.test_ladder import PNG, _violation
from homeoa.meetings.tests.test_meetings import User, pilot  # noqa: F401
from homeoa.notify.models import Notification

OWNER_A = "ownera@cedarrow.example"
OWNER_C = "ownerc@cedarrow.example"
TENANT_E = "chris.doyle@cedarrow.example"
REPORTER = "secretary@cedarrow.example"


@pytest.fixture
def noticed(pilot):  # noqa: F811
    open_residents(pilot)
    v = _violation(pilot, "A")
    v.reported_by = User.objects.get(email=REPORTER)
    v.save()
    ladder.send_notice(v, ViolationStep.Kind.COURTESY)
    return v


def test_hidden_until_the_first_notice(pilot, client):  # noqa: F811
    open_residents(pilot)
    v = _violation(pilot, "A")
    client.force_login(User.objects.get(email=OWNER_A))
    assert client.get(reverse("violations_portal:detail", args=[v.pk])).status_code == 404
    ladder.send_notice(v, ViolationStep.Kind.COURTESY)
    assert client.get(reverse("violations_portal:detail", args=[v.pk])).status_code == 200


def test_owner_sees_the_violation_but_not_the_reporter(noticed, client):
    client.force_login(User.objects.get(email=OWNER_A))
    tab = client.get(reverse("portal:requests")).content.decode()
    assert reverse("violations_portal:detail", args=[noticed.pk]) in tab
    page = client.get(reverse("violations_portal:detail", args=[noticed.pk])).content.decode()
    assert "Dog off leash in the lobby" in page and "Rules §4.2" in page
    assert "Courtesy notice" in page
    assert "Secretary" not in page and REPORTER not in page
    photo = noticed.photos.get()
    url = reverse("violations_portal:photo", args=[noticed.pk, photo.document_id])
    assert client.get(url).status_code == 200


def test_other_owners_cannot_see_it(noticed, client):
    client.force_login(User.objects.get(email=OWNER_C))
    assert client.get(reverse("violations_portal:detail", args=[noticed.pk])).status_code == 404


def test_owner_disagrees_board_is_told_and_gets_a_hearing_todo(
    noticed, client, django_capture_on_commit_callbacks
):
    client.force_login(User.objects.get(email=OWNER_A))
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            reverse("violations_portal:respond", args=[noticed.pk]),
            {"kind": "disagree", "message": "My dog was on a leash.", "photos": []},
        )
    assert resp.status_code == 302
    r = ViolationResponse.objects.get()
    assert r.kind == "disagree" and r.violation == noticed
    assert Notification.objects.filter(type_key=responses.RESPONSE_TYPE).exists()
    ids = [t.id for t in todos.collect(noticed.association)]
    assert f"enforcement-dispute-{noticed.pk}" in ids
    noticed.refresh_from_db()
    assert noticed.status == "open"  # nothing changes until the board acts


def test_fixed_with_photo_shows_on_the_board_page(noticed, client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(User.objects.get(email=OWNER_A))
    client.post(
        reverse("violations_portal:respond", args=[noticed.pk]),
        {
            "kind": "fixed",
            "message": "Dog stays home now.",
            "photos": [SimpleUploadedFile("proof.png", PNG, content_type="image/png")],
        },
    )
    r = ViolationResponse.objects.get()
    assert r.photos.count() == 1
    assert f"enforcement-dispute-{noticed.pk}" not in [
        t.id for t in todos.collect(noticed.association)
    ]
    client.force_login(User.objects.get(email="treasurer@cedarrow.example"))
    page = client.get(reverse("enforcement:detail", args=[noticed.pk])).content.decode()
    assert "Dog stays home now." in page


def test_no_response_once_closed(noticed):
    noticed.status = "closed"
    noticed.save()
    with pytest.raises(EnforcementError):
        responses.respond(
            noticed,
            user=None,
            person=None,
            kind="fixed",
            message="done",
        )


def test_tenant_sees_and_answers_but_gets_no_letters(pilot, client):  # noqa: F811
    open_residents(pilot)
    v = _violation(pilot, "E")
    step = ladder.send_notice(v, ViolationStep.Kind.COURTESY)
    client.force_login(User.objects.get(email=TENANT_E))
    page = client.get(reverse("violations_portal:detail", args=[v.pk]))
    assert page.status_code == 200
    if step.letter_id:
        url = reverse("violations_portal:letter", args=[v.pk, step.pk])
        assert client.get(url).status_code == 404
    resp = client.post(
        reverse("violations_portal:respond", args=[v.pk]),
        {"kind": "fixed", "message": "Moved the bikes."},
    )
    assert resp.status_code == 302 and ViolationResponse.objects.filter(violation=v).exists()
