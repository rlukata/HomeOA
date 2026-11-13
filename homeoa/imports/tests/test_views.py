import re
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.access.testing import grant
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.imports.models import AccountMapping, ImportRun, MappingProfile
from homeoa.ledger.services import unit_balance
from homeoa.registry.models import Unit

BASE = Path(__file__).parents[1] / "fixtures" / "econdo_sample"


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


@pytest.fixture
def treasurer(client, assoc):
    user = get_user_model().objects.create_user("treasurer@example.org")
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    return client


def files():
    return [
        SimpleUploadedFile(p.name, p.read_bytes(), content_type="text/csv")
        for p in sorted(BASE.glob("*.csv"))
    ]


def upload(client, mode="commit"):
    resp = client.post(
        "/units/import/",
        {"source": "econdo", "mode": mode, "as_of": "2026-10-15", "files": files()},
    )
    assert resp.status_code == 302, resp.content.decode()[:3000]
    return resp["Location"]


def form_values(html):
    """Collect the mapping form's current select values, like a browser would submit."""
    form = html[html.index('<form method="post">') :]
    data = {}
    for m in re.finditer(
        r'<select class="input" id="[^"]+" name="([^"]+)">(.*?)</select>', form, re.S
    ):
        sel = re.search(r'<option value="([^"]*)" selected', m.group(2))
        first = re.search(r'<option value="([^"]*)"', m.group(2))
        data[m.group(1)] = sel.group(1) if sel else (first.group(1) if first else "")
    for m in re.finditer(r'<input type="hidden" name="(histname-\d+)" value="([^"]*)"', form):
        data[m.group(1)] = m.group(2)
    data["profile_name"] = "AppFolio-style default"
    return data


def test_full_flow_upload_map_preview_commit(treasurer, assoc):
    mapping_url = upload(treasurer)
    html = treasurer.get(mapping_url).content.decode()
    assert "Saved mapping" in html and "Save mapping" in html
    data = form_values(html)
    resp = treasurer.post(mapping_url, {**data, "action": "save"})
    assert resp.status_code == 302
    assert MappingProfile.objects.get().name == "AppFolio-style default"
    resp = treasurer.post(mapping_url, {**data, "action": "preview"}, follow=True)
    html = resp.content.decode()
    assert "11 units" in html and "Commit import" in html
    assert Unit.objects.count() == 0
    run = ImportRun.objects.get()
    resp = treasurer.post(f"/units/import/{run.pk}/commit/", follow=True)
    assert "Import complete" in resp.content.decode()
    assert Unit.objects.count() == 11
    assert unit_balance(Unit.objects.get(number="7").pk) == Decimal("780.04")
    assert AccountMapping.objects.filter(external_code="1200", account__code="1100").exists()

    # Re-import: the saved mapping is preselected and the preview shows no changes.
    mapping_url = upload(treasurer)
    html = treasurer.get(mapping_url).content.decode()
    assert re.search(r'<option value="\d+" selected>AppFolio-style default</option>', html)
    resp = treasurer.post(mapping_url, {**form_values(html), "action": "preview"}, follow=True)
    assert "No changes" in resp.content.decode()


def test_comparison_mode_saves_snapshot(treasurer, assoc):
    mapping_url = upload(treasurer, mode="comparison")
    html = treasurer.get(mapping_url).content.decode()
    resp = treasurer.post(mapping_url, {**form_values(html), "action": "preview"}, follow=True)
    assert "Save snapshot" in resp.content.decode()
    run = ImportRun.objects.get()
    resp = treasurer.post(f"/units/import/{run.pk}/commit/", follow=True)
    assert "Snapshot saved" in resp.content.decode()
    assert Unit.objects.count() == 0


def test_errors_are_listed_with_file_row_column(treasurer, assoc):
    bad = SimpleUploadedFile("unit_directory.csv", b"Unit,Building,Allocated Interest %\n1,A,abc\n")
    resp = treasurer.post(
        "/units/import/",
        {"source": "econdo", "mode": "commit", "as_of": "2026-10-15", "files": [bad]},
    )
    mapping_url = resp["Location"]
    html = treasurer.get(mapping_url).content.decode()
    resp = treasurer.post(mapping_url, {**form_values(html), "action": "preview"}, follow=True)
    html = resp.content.decode()
    assert 'data-testid="import-problems"' in html
    assert "unit_directory.csv" in html and "Allocated Interest %" in html
    assert "Commit import" not in html


def test_needs_finance_manage(client, assoc):
    user = get_user_model().objects.create_user("member@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    assert client.get("/units/import/").status_code == 403


def test_upload_page_renders(treasurer):
    html = treasurer.get("/units/import/").content.decode()
    assert "CSV files" in html and "Upload" in html
