import importlib
from decimal import Decimal
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import OperationalError, transaction

from homeoa.core.context import association_context, get_current_association
from homeoa.core.models import Association
from tests.scoped.models import Widget


@pytest.mark.parametrize("name", ["base", "dev", "test"])
def test_settings_import(name):
    importlib.import_module(f"homeoa.settings.{name}")


def test_prod_settings_require_secret(monkeypatch):
    import homeoa.settings.base as base

    monkeypatch.setattr(base, "SECRET_KEY", "")
    with pytest.raises(ImproperlyConfigured):
        importlib.reload(importlib.import_module("homeoa.settings.prod"))


def _load_e2e():
    import sys

    sys.modules.pop("homeoa.settings.e2e", None)
    return importlib.import_module("homeoa.settings.e2e")


def test_e2e_settings_guard(monkeypatch):
    monkeypatch.delenv("HOMEOA_E2E", raising=False)
    with pytest.raises(ImproperlyConfigured):
        _load_e2e()
    monkeypatch.setenv("HOMEOA_E2E", "0")
    with pytest.raises(ImproperlyConfigured):
        _load_e2e()


def test_e2e_settings_defaults(monkeypatch):
    monkeypatch.setenv("HOMEOA_E2E", "1")
    monkeypatch.delenv("HOMEOA_E2E_MAIL_DIR", raising=False)
    monkeypatch.delenv("HOMEOA_E2E_DB_NAME", raising=False)
    mod = _load_e2e()
    assert mod.DATABASES["default"]["NAME"] == "homeoa_e2e"
    assert mod.EMAIL_BACKEND == "django.core.mail.backends.filebased.EmailBackend"
    assert mod.EMAIL_FILE_PATH.endswith("/.work/e2e-mail")
    assert mod.DEBUG is False
    assert mod.SESSION_COOKIE_SECURE is False and mod.CSRF_COOKIE_SECURE is False
    assert "whitenoise.middleware.WhiteNoiseMiddleware" in mod.MIDDLEWARE


def test_e2e_settings_file_mail(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEOA_E2E", "1")
    monkeypatch.setenv("HOMEOA_E2E_MAIL_DIR", str(tmp_path))
    monkeypatch.setenv("HOMEOA_E2E_DB_NAME", "custom_e2e")
    mod = _load_e2e()
    assert mod.EMAIL_BACKEND == "django.core.mail.backends.filebased.EmailBackend"
    assert str(tmp_path) == mod.EMAIL_FILE_PATH
    assert mod.DATABASES["default"]["NAME"] == "custom_e2e"


@pytest.mark.django_db
def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.django_db
def test_healthz_db_down(client):
    with mock.patch("homeoa.core.views.connection.cursor", side_effect=OperationalError("down")):
        resp = client.get("/healthz")
    assert resp.status_code == 503


@pytest.fixture
def two_assocs(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    b = Association.objects.create(name="Other HOA", short_name="OH")
    Widget.all_associations.create(association=a, name="a1", amount=Decimal("1.00"))
    Widget.all_associations.create(association=b, name="b1", amount=Decimal("2.00"))
    return a, b


def test_scoped_manager_isolates_associations(two_assocs):
    a, b = two_assocs
    with association_context(a):
        assert [w.name for w in Widget.objects.all()] == ["a1"]
    with association_context(b):
        assert [w.name for w in Widget.objects.all()] == ["b1"]
    assert Widget.all_associations.count() == 2


def test_scoped_manager_fails_closed_without_association(two_assocs):
    assert get_current_association() is None
    assert Widget.objects.count() == 0


def test_save_defaults_to_current_association(two_assocs):
    a, _b = two_assocs
    with association_context(a):
        w = Widget.objects.create(name="a2")
    assert w.association == a
    with pytest.raises(ValueError):
        Widget(name="orphan").save()


def test_middleware_sets_current_association(two_assocs, client):
    from django.contrib.auth import get_user_model

    client.force_login(get_user_model().objects.create_user("mw@example.org"))
    resp = client.get("/")
    assert resp.wsgi_request.association == two_assocs[0]
    assert get_current_association() is None  # reset after the request


def test_money_field_rejects_float(two_assocs):
    a, _b = two_assocs
    with pytest.raises(TypeError), transaction.atomic():
        Widget.all_associations.create(association=a, name="f", amount=1.5)
    w = Widget.all_associations.create(association=a, name="d", amount=Decimal("10.10"))
    w.refresh_from_db()
    assert w.amount == Decimal("10.10")


def test_association_validation(db):
    assoc = Association(name="X", short_name="X", timezone="Mars/Base", fiscal_year_start_month=13)
    with pytest.raises(ValidationError) as exc:
        assoc.full_clean()
    assert {"timezone", "fiscal_year_start_month"} <= set(exc.value.message_dict)
