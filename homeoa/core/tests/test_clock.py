import datetime as dt

import pytest
from django.core.exceptions import ImproperlyConfigured

from homeoa.core import demo
from homeoa.core.context import association_today


def test_fixed_today_off_by_default(settings):
    settings.HOMEOA_FIXED_TODAY = ""
    assert association_today() == dt.datetime.now(dt.UTC).date()


def test_fixed_today_honored(settings):
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    assert association_today() == dt.date(2026, 10, 7)


def test_fixed_today_honored_with_debug(settings):
    settings.HOMEOA_FIXED_TODAY_ALLOWED = False
    settings.DEBUG = True
    settings.HOMEOA_FIXED_TODAY = "2026-01-02"
    assert association_today() == dt.date(2026, 1, 2)


def test_fixed_today_refused_without_permission(settings):
    settings.HOMEOA_FIXED_TODAY_ALLOWED = False
    settings.DEBUG = False
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    with pytest.raises(ImproperlyConfigured):
        association_today()


def test_fixed_today_bad_value(settings):
    settings.HOMEOA_FIXED_TODAY = "10/07/2026"
    with pytest.raises(ImproperlyConfigured):
        association_today()


def test_prod_settings_refuse_fixed_today(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("HOMEOA_FIXED_TODAY", "2026-10-07")
    monkeypatch.setenv("HOMEOA_SECRET_KEY", "x")
    monkeypatch.setenv("HOMEOA_FIELD_KEYS", "ZxBFQzV0wh4mnXQim1_pLdCRfc-HXr6s8f-IZO3m-ow=")
    sys.modules.pop("homeoa.settings.prod", None)
    with pytest.raises(ImproperlyConfigured, match="HOMEOA_FIXED_TODAY"):
        importlib.import_module("homeoa.settings.prod")
    sys.modules.pop("homeoa.settings.prod", None)


def test_wipe_refused_without_setting(settings):
    settings.HOMEOA_ALLOW_DEMO_RESET = False
    with pytest.raises(demo.SeedRefused, match="HOMEOA_ALLOW_DEMO_RESET"):
        demo.wipe()
