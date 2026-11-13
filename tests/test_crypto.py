import importlib
import sys

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection

from homeoa.core import crypto
from homeoa.core.context import association_context
from homeoa.core.models import Association
from tests.scoped.models import Secret


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Tukwila Condo", short_name="TC")
    with association_context(a):
        yield a


def _raw(obj, column):
    with connection.cursor() as cur:
        cur.execute(f"SELECT {column} FROM scoped_secret WHERE id = %s", [obj.pk])  # noqa: S608
        return cur.fetchone()[0]


def test_round_trip_and_unreadable_in_db(assoc):
    s = Secret.objects.create(label="a", note="SSN 123-45-6789", data={"acct": "9876", "n": 2})
    raw = _raw(s, "note")
    assert "123-45" not in raw
    assert "9876" not in _raw(s, "data")
    s = Secret.objects.get(pk=s.pk)
    assert s.note == "SSN 123-45-6789"
    assert s.data == {"acct": "9876", "n": 2}


def test_null_stays_null(assoc):
    s = Secret.objects.create(label="a", note=None, data=None)
    assert _raw(s, "note") is None
    assert Secret.objects.get(pk=s.pk).note is None


def test_encrypted_field_cannot_be_unique():
    with pytest.raises(ValueError):
        crypto.EncryptedTextField(unique=True)


def test_rotation(assoc, settings):
    old_key = settings.HOMEOA_FIELD_KEYS[0]
    s = Secret.objects.create(label="a", note="secret", data=[1, 2])
    old_token = _raw(s, "note")
    new_key = Fernet.generate_key().decode()
    settings.HOMEOA_FIELD_KEYS = [new_key, old_key]
    # Old values still decrypt before rotation.
    assert Secret.objects.get(pk=s.pk).note == "secret"
    call_command("rotate_field_keys", stdout=open("/dev/null", "w"))  # noqa: SIM115
    assert _raw(s, "note") != old_token
    # After rotation the old key can be dropped.
    settings.HOMEOA_FIELD_KEYS = [new_key]
    s = Secret.objects.get(pk=s.pk)
    assert s.note == "secret"
    assert s.data == [1, 2]


def test_wrong_key_fails_loudly(assoc, settings):
    s = Secret.objects.create(label="a", note="secret")
    settings.HOMEOA_FIELD_KEYS = [Fernet.generate_key().decode()]
    with pytest.raises(ImproperlyConfigured):
        Secret.objects.get(pk=s.pk)


def test_missing_keys_fail(settings):
    settings.HOMEOA_FIELD_KEYS = []
    with pytest.raises(ImproperlyConfigured):
        crypto.encrypt("x")
    settings.HOMEOA_FIELD_KEYS = ["not-a-key"]
    with pytest.raises(ImproperlyConfigured):
        crypto.check_keys()


def _reload_prod(monkeypatch, keys):
    import homeoa.settings.base as base

    monkeypatch.setattr(base, "SECRET_KEY", "prod-secret")
    monkeypatch.setattr(base, "HOMEOA_FIELD_KEYS", keys)
    sys.modules.pop("homeoa.settings.prod", None)
    return importlib.import_module("homeoa.settings.prod")


def test_prod_requires_field_keys(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="HOMEOA_FIELD_KEYS"):
        _reload_prod(monkeypatch, [])
    with pytest.raises(ImproperlyConfigured, match="invalid"):
        _reload_prod(monkeypatch, ["bogus"])
    assert _reload_prod(monkeypatch, [Fernet.generate_key().decode()])
    sys.modules.pop("homeoa.settings.prod", None)
