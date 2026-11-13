"""Every section page in the board workspace shows its hub's row of section tabs."""

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from homeoa.access.testing import grant
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ui import nav

User = get_user_model()


@pytest.fixture
def board_client(db, client, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    with association_context(a):
        usr = User.objects.create_user("treasurer@example.org", password="x-pass-12345")
        grant(usr, association=a, title="treasurer")
        client.force_login(usr)
        yield client, usr, a


def test_every_section_shows_its_tabs(board_client):
    client, usr, assoc = board_client
    request = RequestFactory().get("/")
    request.user, request.association = usr, assoc
    checked = []
    for hub in nav.get_hubs(request):
        for i, tab in enumerate(hub.subtabs):
            if tab.permission is not None and not tab.permission(request):
                continue
            url = hub.subtab_url(i)
            resp = client.get(url)
            if resp.status_code != 200:
                continue
            on = [t["url"] for t in resp.context["subtabs"] if t["on"]]
            assert on == [url], f"{hub.id} / {tab.slug} ({url}) highlights {on}"
            checked.append(url)
    assert len(checked) > 10
