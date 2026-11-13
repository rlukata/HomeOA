"""Field-level encryption for sensitive data (OPS-04).

Public API
----------
``EncryptedTextField``
    A ``TextField`` whose value is stored as a Fernet token. Python sees ``str``.
``EncryptedJSONField``
    Same, for any JSON-serializable value (stored as an encrypted JSON string).
``encrypt(str) -> str`` / ``decrypt(str) -> str``
    The primitives, for values kept outside model fields.
``rotate_token(str) -> str``
    Re-encrypt a token under the newest key (used by ``manage.py rotate_field_keys``).
``encrypted_fields()``
    Every ``(model, field)`` pair using an encrypted field.

Keys
----
``settings.HOMEOA_FIELD_KEYS`` is a list of Fernet keys, newest first (from the
``HOMEOA_FIELD_KEYS`` environment variable, comma-separated). New values are encrypted
with the first key; every key can decrypt. To rotate: prepend a new key, run
``manage.py rotate_field_keys``, then drop the old key. Generate a key with
``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``.

Encrypted fields are not searchable or sortable in SQL. Audit diffs never contain their
values, only the marker ``"changed"`` (see ``homeoa.audit``).
"""

from __future__ import annotations

import json
from functools import cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.db import models


def _keys() -> list[str]:
    raw = getattr(settings, "HOMEOA_FIELD_KEYS", None) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return [k.strip() for k in raw if k and k.strip()]


@cache
def get_fernet() -> MultiFernet:
    keys = _keys()
    if not keys:
        raise ImproperlyConfigured("HOMEOA_FIELD_KEYS is not set; encrypted fields need a key.")
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except ValueError as exc:
        raise ImproperlyConfigured(f"HOMEOA_FIELD_KEYS contains an invalid key: {exc}") from exc


def _reset(*, setting, **kwargs):
    if setting == "HOMEOA_FIELD_KEYS":
        get_fernet.cache_clear()


setting_changed.connect(_reset)


def check_keys() -> None:
    """Raise ImproperlyConfigured unless usable keys are configured."""
    get_fernet()


def encrypt(value: str) -> str:
    return get_fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ImproperlyConfigured(
            "An encrypted value could not be decrypted with any key in HOMEOA_FIELD_KEYS."
        ) from exc


def rotate_token(token: str) -> str:
    return get_fernet().rotate(token.encode()).decode()


class EncryptedTextField(models.TextField):
    """Text encrypted at rest. ``None`` stays NULL; everything else is a Fernet token."""

    encrypted = True

    def __init__(self, *args, **kwargs):
        if kwargs.get("db_index") or kwargs.get("unique") or kwargs.get("primary_key"):
            raise ValueError("Encrypted fields cannot be indexed, unique or primary keys.")
        super().__init__(*args, **kwargs)

    def _serialize(self, value):
        return str(value)

    def _deserialize(self, text):
        return text

    def get_prep_value(self, value):
        if value is None:
            return None
        return encrypt(self._serialize(value))

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        return self._deserialize(decrypt(value))

    def to_python(self, value):
        return value

    def value_to_string(self, obj):
        # Serialization (dumpdata) keeps plaintext out of fixtures by emitting the token.
        value = self.value_from_object(obj)
        return None if value is None else self.get_prep_value(value)


class EncryptedJSONField(EncryptedTextField):
    """Any JSON-serializable value, encrypted at rest."""

    def _serialize(self, value):
        return json.dumps(value, sort_keys=True)

    def _deserialize(self, text):
        return json.loads(text)


def is_encrypted(field) -> bool:
    return bool(getattr(field, "encrypted", False))


def encrypted_fields():
    """Yield ``(model, field)`` for every concrete encrypted field in installed apps."""
    for model in apps.get_models():
        for field in model._meta.concrete_fields:
            if is_encrypted(field):
                yield model, field
