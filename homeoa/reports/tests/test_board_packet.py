"""Monthly board packet: one PDF for a month, filed board-only and emailed on month close."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.closing import services as closing
from homeoa.closing.tests.test_closing import assoc, post, user  # noqa: F401
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.documents.models import Document, Visibility
from homeoa.notify.models import Notification
from homeoa.plugins import registry as plugin_registry
from homeoa.reports import packet

User = get_user_model()
TREASURER = "treasurer@cedarrow.example"


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    plugin_registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        yield a


def test_packet_has_every_section(pilot):
    titles = [str(s.title) for s in packet.specs(pilot, 2026, 9)]
    for expected in (
        "Summary",
        "Income statement, month",
        "Income statement, year to date",
        "Balance sheet",
        "Receivables aging",
        "Bills paid",
        "Open work orders",
        "Open violations",
    ):
        assert expected in titles
    assert packet.build(pilot, 2026, 9).startswith(b"%PDF")


def test_filed_board_only_and_emailed_once_per_build(pilot, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        v1 = packet.file_and_send(pilot, 2026, 9)
    doc = v1.document
    assert doc.title == "Board packet · September 2026" and doc.visibility == Visibility.BOARD
    note = Notification.objects.filter(type_key=packet.PACKET_TYPE).first()
    assert note.attachments[0]["file_key"] == v1.file_key
    packet.file_and_send(pilot, 2026, 9)  # rebuilt: a new version of the same document
    assert Document.objects.filter(title=doc.title).count() == 1 and doc.versions.count() == 2


def test_closing_a_month_builds_the_packet(
    assoc,  # noqa: F811
    user,  # noqa: F811
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    calls = []
    monkeypatch.setattr(packet, "after_close", lambda a, y, m, user=None: calls.append((y, m)))
    post("1000", "4000", "100.00", dt.date(2026, 1, 15))
    with django_capture_on_commit_callbacks(execute=True):
        closing.close_month(assoc, 2026, 1, user=user)
    assert calls == [(2026, 1)]


def test_page_downloads_and_sends(pilot, client, django_capture_on_commit_callbacks):
    client.force_login(User.objects.get(email=TREASURER))
    url = reverse("reports:board_packet")
    assert client.get(url).status_code == 200
    resp = client.get(url, {"month": "2026-09", "download": "1"})
    assert resp["Content-Type"] == "application/pdf"
    with django_capture_on_commit_callbacks(execute=True):
        assert client.post(url, {"month": "2026-09"}).status_code == 302
    assert Document.objects.filter(title="Board packet · September 2026").exists()


def test_packet_sections_keep_their_order_and_owners(pilot):
    from homeoa.plugins import slots

    titles = [str(s.title) for s in packet.specs(pilot, 2026, 9)]
    assert titles == [
        "Summary",
        "Income statement, month",
        "Income statement, year to date",
        "Balance sheet",
        "Budget vs actual, year to date",
        "Receivables aging",
        "Bills paid",
        "Open work orders",
        "Open violations",
        "Deadlines in the next 60 days",
    ]
    summary = [str(r[0]) for r in packet.specs(pilot, 2026, 9)[0].rows]
    assert summary[-4:] == [
        "Owed by owners",
        "Bills paid this month",
        "Open work orders",
        "Open violations",
    ]
    modules = [c.module for c in slots.contributions(slots.BOARD_PACKET_SECTIONS)]
    assert modules == ["board_packet"] * 5 + ["work_orders", "violations", "board_packet"]
