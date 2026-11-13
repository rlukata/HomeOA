"""Passkeys (PLT-02) against a software authenticator.

``SoftAuthenticator`` builds real WebAuthn responses (ES256, "none" attestation) the way a
browser and platform authenticator would, so py_webauthn verifies them end to end.
"""

import hashlib
import json
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from webauthn.helpers import bytes_to_base64url

from homeoa.access.testing import grant
from homeoa.accounts import passkeys, services
from homeoa.accounts.models import PasskeyCredential
from homeoa.audit.models import AuditEvent
from homeoa.core.models import Association

pytestmark = pytest.mark.django_db

RP = passkeys.RelyingParty(rp_id="testserver", origin="http://testserver")


class SoftAuthenticator:
    def __init__(self, cred_id=b"cred-0001", rp=RP, user_verified=True):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = cred_id
        self.rp = rp
        self.count = 0
        self.flags_uv = 0x04 if user_verified else 0

    def _cose(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")}
        )

    def _client_data(self, kind, challenge, origin=None) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin or self.rp.origin}
        ).encode()

    def _rp_hash(self) -> bytes:
        return hashlib.sha256(self.rp.rp_id.encode()).digest()

    def create(self, options: dict) -> dict:
        auth_data = (
            self._rp_hash()
            + bytes([0x41 | self.flags_uv])
            + struct.pack(">I", self.count)
            + bytes(16)
            + struct.pack(">H", len(self.cred_id))
            + self.cred_id
            + self._cose()
        )
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cid = bytes_to_base64url(self.cred_id)
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(
                    self._client_data("webauthn.create", options["challenge"])
                ),
                "attestationObject": bytes_to_base64url(att),
                "transports": ["internal"],
            },
        }

    def get(self, options: dict, *, count=None, origin=None) -> dict:
        self.count = self.count + 1 if count is None else count
        auth_data = self._rp_hash() + bytes([0x01 | self.flags_uv]) + struct.pack(">I", self.count)
        client = self._client_data("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.cred_id)
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(client),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(sig),
                "userHandle": None,
            },
        }


@pytest.fixture
def assoc():
    return Association.objects.create(name="Cedar Row", short_name="CR")


@pytest.fixture
def user():
    return get_user_model().objects.create_user("board@example.org")


def _register(user, auth, name="Phone"):
    options, challenge = passkeys.registration_options(user, RP)
    return passkeys.register(user, auth.create(json.loads(options)), challenge, RP, name)


def _post(client, url, data=None):
    return client.post(url, json.dumps(data or {}), content_type="application/json")


# --- services ------------------------------------------------------------------------


def test_register_and_authenticate(user):
    auth = SoftAuthenticator()
    pk = _register(user, auth)
    assert pk.name == "Phone" and bytes(pk.credential_id) == b"cred-0001"
    assert user.has_mfa and not user.has_totp
    options, challenge = passkeys.authentication_options(RP)
    assert passkeys.authenticate(auth.get(json.loads(options)), challenge, RP) == pk
    pk.refresh_from_db()
    assert pk.sign_count == 1 and pk.last_used_at is not None


def test_registration_requires_user_verification_and_right_origin(user):
    with pytest.raises(passkeys.PasskeyError):
        _register(user, SoftAuthenticator(user_verified=False))
    other = SoftAuthenticator(rp=passkeys.RelyingParty("evil.example", "https://evil.example"))
    with pytest.raises(passkeys.PasskeyError):
        _register(user, other)
    assert not PasskeyCredential.objects.exists()


def test_registration_challenge_mismatch(user):
    auth = SoftAuthenticator()
    options, _ = passkeys.registration_options(user, RP)
    _, other_challenge = passkeys.registration_options(user, RP)
    with pytest.raises(passkeys.PasskeyError):
        passkeys.register(user, auth.create(json.loads(options)), other_challenge, RP, "x")


def test_duplicate_credential_refused(user):
    auth = SoftAuthenticator()
    _register(user, auth)
    with pytest.raises(passkeys.PasskeyError) as exc:
        _register(user, auth)
    assert exc.value.reason == "duplicate"


def test_counter_regression_rejected(user):
    auth = SoftAuthenticator()
    _register(user, auth)
    options, challenge = passkeys.authentication_options(RP)
    passkeys.authenticate(auth.get(json.loads(options), count=5), challenge, RP)
    options, challenge = passkeys.authentication_options(RP)
    with pytest.raises(passkeys.PasskeyError) as exc:
        passkeys.authenticate(auth.get(json.loads(options), count=5), challenge, RP)
    assert exc.value.reason == "counter"
    assert PasskeyCredential.objects.get().sign_count == 5


def test_wrong_origin_and_unknown_and_other_user(user):
    auth = SoftAuthenticator()
    _register(user, auth)
    options, challenge = passkeys.authentication_options(RP)
    bad = auth.get(json.loads(options), origin="https://evil.example")
    with pytest.raises(passkeys.PasskeyError) as exc:
        passkeys.authenticate(bad, challenge, RP)
    assert exc.value.reason == "invalid"
    stranger = SoftAuthenticator(cred_id=b"nope")
    with pytest.raises(passkeys.PasskeyError) as exc:
        passkeys.authenticate(stranger.get(json.loads(options)), challenge, RP)
    assert exc.value.reason == "unknown"
    other = get_user_model().objects.create_user("other@example.org")
    options, challenge = passkeys.authentication_options(RP, other)
    with pytest.raises(passkeys.PasskeyError) as exc:
        passkeys.authenticate(auth.get(json.loads(options)), challenge, RP, user=other)
    assert exc.value.reason == "unknown"


# --- views ---------------------------------------------------------------------------


def _register_via_views(client, auth, name="Laptop"):
    options = _post(client, "/account/passkeys/options/").json()
    return _post(
        client, "/account/passkeys/register/", {"name": name, "credential": auth.create(options)}
    )


def _login_via_views(client, auth, **kw):
    options = _post(client, "/login/passkey/options/").json()
    return _post(client, "/login/passkey/", {"credential": auth.get(options, **kw)})


def test_login_page_offers_passkey(client, assoc):
    resp = client.get("/login/")
    assert b"Sign in with a passkey" in resp.content and b"passkeys.js" in resp.content


def test_register_rename_remove_audited(client, assoc, user):
    grant(user, association=assoc)
    client.force_login(user)
    auth = SoftAuthenticator()
    resp = _register_via_views(client, auth)
    assert resp.status_code == 200 and resp.json()["redirect"] == "/account/passkeys/"
    pk = PasskeyCredential.objects.get(user=user)
    assert b"Laptop" in client.get("/account/passkeys/").content
    client.post("/account/passkeys/", {"id": pk.pk, "action": "rename", "name": "Work laptop"})
    pk.refresh_from_db()
    assert pk.name == "Work laptop"
    client.post("/account/passkeys/", {"id": pk.pk, "action": "remove"})
    assert not PasskeyCredential.objects.exists()
    actions = set(AuditEvent.all_associations.values_list("action", flat=True))
    assert {"auth.passkey_registered", "auth.passkey_renamed", "auth.passkey_removed"} <= actions


def test_challenge_is_single_use(client, assoc, user):
    client.force_login(user)
    auth = SoftAuthenticator()
    options = _post(client, "/account/passkeys/options/").json()
    cred = auth.create(options)
    assert _post(client, "/account/passkeys/register/", {"credential": cred}).status_code == 200
    replay = _post(client, "/account/passkeys/register/", {"credential": cred})
    assert replay.status_code == 400


def test_cannot_remove_other_users_passkey(client, assoc, user):
    pk = _register(user, SoftAuthenticator())
    other = get_user_model().objects.create_user("other@example.org")
    client.force_login(other)
    assert client.post("/account/passkeys/", {"id": pk.pk, "action": "remove"}).status_code == 404
    assert PasskeyCredential.objects.exists()


def test_passkey_login(client, assoc, user):
    grant(user, association=assoc)
    auth = SoftAuthenticator()
    _register(user, auth)
    resp = _login_via_views(client, auth)
    assert resp.status_code == 200 and resp.json()["redirect"] == "/"
    assert client.session["_auth_user_id"] == str(user.pk)
    actions = set(AuditEvent.all_associations.values_list("action", flat=True))
    assert {"auth.passkey_login", "auth.login"} <= actions


def test_passkey_login_failure_audited(client, assoc, user):
    auth = SoftAuthenticator()
    _register(user, auth)
    _login_via_views(client, auth, count=3)
    client.logout()
    resp = _login_via_views(client, auth, count=3)
    assert resp.status_code == 400 and "error" in resp.json()
    assert "_auth_user_id" not in client.session
    ev = AuditEvent.all_associations.filter(action="auth.passkey_failed").get()
    assert ev.changes["reason"] == "counter" and ev.object_id == str(user.pk)


def test_passkey_satisfies_board_mfa(client, assoc, user):
    assoc.settings = {"require_mfa_for_board": True}
    assoc.save()
    grant(user, association=assoc)
    auth = SoftAuthenticator()
    _register(user, auth)
    resp = _login_via_views(client, auth)
    assert resp.json()["redirect"] == "/"  # not sent to MFA enrolment
    assert client.get("/").status_code == 200


def test_board_member_can_enrol_with_passkey_instead_of_totp(client, assoc, user):
    assoc.settings = {"require_mfa_for_board": True}
    assoc.save()
    grant(user, association=assoc)
    raw = services.create_magic_link(user)
    resp = client.post(f"/login/verify/{raw}/")
    assert resp["Location"] == "/account/mfa/"
    assert client.get("/account/passkeys/").status_code == 200  # allowed while enrolling
    auth = SoftAuthenticator()
    assert _register_via_views(client, auth).status_code == 200
    assert client.get("/").status_code == 200
    # The last factor can't be removed while MFA is required.
    pk = PasskeyCredential.objects.get()
    resp = client.post("/account/passkeys/", {"id": pk.pk, "action": "remove"})
    assert resp.status_code == 200 and PasskeyCredential.objects.exists()


def test_magic_link_then_passkey_as_second_step(client, assoc, user):
    grant(user, association=assoc)
    auth = SoftAuthenticator()
    _register(user, auth)
    raw = services.create_magic_link(user)
    resp = client.post(f"/login/verify/{raw}/")
    assert resp["Location"] == "/login/mfa/"
    page = client.get("/login/mfa/")
    assert b"Use my passkey" in page.content and b"id_code" not in page.content
    # Another user's passkey can't complete this user's second step.
    other = get_user_model().objects.create_user("other@example.org")
    stranger = SoftAuthenticator(cred_id=b"other")
    _register(other, stranger)
    assert _login_via_views(client, stranger).status_code == 400
    resp = _login_via_views(client, auth)
    assert resp.status_code == 200 and client.session["_auth_user_id"] == str(user.pk)
    ev = AuditEvent.all_associations.get(action="auth.mfa_passed")
    assert ev.changes["method"] == "passkey"


def test_resident_still_gated_after_passkey_login(client, assoc):
    """A passkey only proves identity; residents still get no access while off."""
    resident = get_user_model().objects.create_user("owner@example.org")
    auth = SoftAuthenticator()
    _register(resident, auth)
    resp = _login_via_views(client, auth)
    assert resp.status_code == 200
    from homeoa.access.services import permissions_for

    assert permissions_for(resident, assoc) == frozenset()


# --- recovery codes with passkeys ---------------------------------------------------


def test_first_passkey_issues_recovery_codes_once(client, assoc, user):
    client.force_login(user)
    _register_via_views(client, SoftAuthenticator())
    page = client.get("/account/passkeys/")
    codes = page.context["codes"]
    assert len(codes) == 10 and codes[0].encode() in page.content
    assert user.recovery_codes.count() == 10
    assert client.get("/account/passkeys/").context["codes"] is None  # shown once
    _register_via_views(client, SoftAuthenticator(cred_id=b"cred-2"), name="Key 2")
    assert client.get("/account/passkeys/").context["codes"] is None  # not reissued
    assert user.recovery_codes.count() == 10
    actions = list(AuditEvent.all_associations.values_list("action", flat=True))
    assert actions.count("auth.recovery_codes_regenerated") == 1


def test_no_new_codes_when_totp_already_issued_them(client, assoc, user):
    services.regenerate_recovery_codes(user)
    client.force_login(user)
    _register_via_views(client, SoftAuthenticator())
    assert client.get("/account/passkeys/").context["codes"] is None


def test_recovery_code_accepted_at_second_step(client, assoc, user):
    grant(user, association=assoc)
    client.force_login(user)
    _register_via_views(client, SoftAuthenticator())
    code = client.get("/account/passkeys/").context["codes"][0]
    client.logout()
    raw = services.create_magic_link(user)
    assert client.post(f"/login/verify/{raw}/")["Location"] == "/login/mfa/"
    page = client.get("/login/mfa/")
    assert b"id_code" in page.content and b"Use my passkey" in page.content
    resp = client.post("/login/mfa/", {"code": code})
    assert resp.status_code == 302 and client.session["_auth_user_id"] == str(user.pk)
    ev = AuditEvent.all_associations.get(action="auth.mfa_passed")
    assert ev.changes["method"] == "recovery"


def test_removing_last_factor_drops_codes(client, assoc, user):
    client.force_login(user)
    _register_via_views(client, SoftAuthenticator())
    pk = PasskeyCredential.objects.get()
    client.post("/account/passkeys/", {"id": pk.pk, "action": "remove"})
    assert not user.recovery_codes.exists()


def test_relying_party_dev_uses_request_host(rf, settings):
    settings.HOMEOA_WEBAUTHN_RP_ID = ""
    settings.HOMEOA_WEBAUTHN_ORIGIN = ""
    settings.HOMEOA_WEBAUTHN_HOST_FALLBACK = True
    rp = passkeys.relying_party(rf.get("/", HTTP_HOST="localhost:8000"))
    assert rp == passkeys.RelyingParty(rp_id="localhost", origin="http://localhost:8000")


def test_relying_party_prod_uses_base_url_not_host(rf, settings):
    settings.HOMEOA_WEBAUTHN_RP_ID = ""
    settings.HOMEOA_WEBAUTHN_ORIGIN = ""
    settings.HOMEOA_WEBAUTHN_HOST_FALLBACK = False
    settings.HOMEOA_BASE_URL = "https://hoa.example.org"
    settings.ALLOWED_HOSTS = ["*"]
    rp = passkeys.relying_party(rf.get("/", HTTP_HOST="evil.example.com"))
    assert rp == passkeys.RelyingParty(rp_id="hoa.example.org", origin="https://hoa.example.org")


def test_relying_party_explicit_settings_win(rf, settings):
    settings.HOMEOA_WEBAUTHN_RP_ID = "example.org"
    settings.HOMEOA_WEBAUTHN_ORIGIN = "https://hoa.example.org:8443"
    settings.HOMEOA_WEBAUTHN_HOST_FALLBACK = False
    settings.HOMEOA_BASE_URL = "https://other.example.net"
    rp = passkeys.relying_party(rf.get("/"))
    assert rp == passkeys.RelyingParty(rp_id="example.org", origin="https://hoa.example.org:8443")


def test_relying_party_prod_without_config_refuses(rf, settings):
    from django.core.exceptions import ImproperlyConfigured

    settings.HOMEOA_WEBAUTHN_RP_ID = ""
    settings.HOMEOA_WEBAUTHN_ORIGIN = ""
    settings.HOMEOA_WEBAUTHN_HOST_FALLBACK = False
    settings.HOMEOA_BASE_URL = ""
    with pytest.raises(ImproperlyConfigured):
        passkeys.relying_party(rf.get("/"))
