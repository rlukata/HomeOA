"""CPA role: read-only finance, reports and vendors; 403 on every write."""

import datetime as dt
import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from homeoa.access import permissions as P
from homeoa.access import services
from homeoa.access.testing import grant
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.vendors import services as vendor_services
from homeoa.vendors.models import Vendor

# URL prefixes of the finance, vendor and closing areas.
PREFIXES = ("money/", "vendors/")
DUMMY = {"int": "1", "str": "A", "slug": "acme", "path": "x"}


def _walk(patterns, prefix=""):
    for p in patterns:
        route = prefix + str(p.pattern)
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, route)
        elif isinstance(p, URLPattern):
            yield route


def finance_urls() -> list[str]:
    urls = set()
    for route in _walk(get_resolver().url_patterns):
        if not route.startswith(PREFIXES):
            continue
        path = re.sub(r"<(?:(\w+):)?\w+>", lambda m: DUMMY.get(m.group(1) or "str", "1"), route)
        urls.add("/" + path.lstrip("^").rstrip("$"))
    return sorted(urls)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        vendor_services.save_vendor(
            Vendor(name="Acme", slug="acme", tin="911234567", is_1099_eligible=True)
        )
        yield a


@pytest.fixture
def cpa(assoc):
    user = get_user_model().objects.create_user(email="cpa@example.org")
    grant(user, P.CPA, association=assoc, start=dt.date(2020, 1, 1))
    return user


def test_cpa_permissions(cpa, assoc):
    codes = services.permissions_for(cpa, assoc)
    assert {P.FINANCE_READ, P.VENDORS_READ, P.CPA_PACKAGE} <= codes
    for code in (P.FINANCE_MANAGE, P.VENDORS_MANAGE, P.UNITS_READ, P.DIRECTORY_READ):
        assert code not in codes
    assert services.is_read_only(cpa, assoc)


def test_board_is_not_read_only(assoc):
    user = get_user_model().objects.create_user(email="b@example.org")
    grant(user, association=assoc, start=dt.date(2020, 1, 1))
    assert not services.is_read_only(user, assoc)


def test_cpa_gets_403_on_every_finance_post(client, cpa):
    urls = finance_urls()
    assert len(urls) > 40  # the whole list, not a sample
    assert "/money/close/month/" in urls and "/vendors/acme/tin/" in urls
    client.force_login(cpa)
    failures = [u for u in urls if client.post(u, {}).status_code != 403]
    assert failures == []


def test_cpa_reads_reports_and_vendors_with_masked_tin(client, cpa):
    client.force_login(cpa)
    for name in ("reports:index", "reports:trial_balance", "closing:index", "vendors:index"):
        assert client.get(reverse(name)).status_code == 200, name
    page = client.get(reverse("vendors:detail", args=["acme"])).content.decode()
    assert "4567" in page and "911234567" not in page
    assert reverse("vendors:reveal_tin", args=["acme"]) not in page
    assert reverse("vendors:edit", args=["acme"]) not in page
    assert client.get(reverse("vendors:w9", args=["acme"])).status_code == 403
    assert client.get(reverse("registry:index")).status_code == 403


def test_cpa_sees_no_write_buttons(client, cpa):
    client.force_login(cpa)
    for name in ("closing:index", "vendors:index", "dashboard:index", "reports:index"):
        page = client.get(reverse(name)).content.decode()
        assert 'method="post"' not in page.lower(), name
        assert reverse("payments:new") not in page, name
        assert reverse("vendors:add") not in page, name


def test_cpa_finance_pages_have_no_post_forms(client, cpa):
    client.force_login(cpa)
    pages = [u for u in finance_urls() if "/1/" not in u and "/A/" not in u and "acme" not in u]
    seen, bad = 0, []
    for url in pages:
        resp = client.get(url)
        if resp.status_code != 200 or "html" not in resp.get("Content-Type", ""):
            continue
        seen += 1
        if 'method="post"' in resp.content.decode().lower():
            bad.append(url)
    assert bad == []
    assert seen > 10


def test_cpa_can_download_package(client, cpa):
    client.force_login(cpa)
    resp = client.get(reverse("reports:cpa_package"), {"year": 2026, "download": 1})
    assert resp.status_code == 200
