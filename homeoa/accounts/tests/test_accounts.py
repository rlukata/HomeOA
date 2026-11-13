import re
from datetime import timedelta

import pyotp
import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from homeoa.access.testing import grant
from homeoa.accounts import policy, services, signals
from homeoa.accounts.models import MagicLinkToken, RecoveryCode
from homeoa.core.models import Association

# Transactional so the magic-link email (an on_commit job, run inline here) is sent.
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def inline_jobs(settings):
    settings.HOMEOA_JOBS_INLINE = True


@pytest.fixture
def assoc():
    return Association.objects.create(name="Cedar Row", short_name="CR")


@pytest.fixture
def user():
    return get_user_model().objects.create_user("Board@Example.org")


def link_from_mail():
    return re.search(r"https?://\S+", mail.outbox[-1].body).group(0)


def enrol(user):
    device = services.start_enrolment(user)
    codes = services.confirm_enrolment(device, pyotp.TOTP(device.secret).now())
    device.refresh_from_db()
    return device, codes


# --- user + tokens -----------------------------------------------------------------


def test_user_keyed_by_lowercase_email(user):
    assert user.email == "board@example.org"
    assert not user.has_usable_password()


def test_token_stored_hashed_and_single_use(user):
    raw = services.create_magic_link(user)
    token = MagicLinkToken.objects.get()
    assert raw not in token.token_hash and len(token.token_hash) == 64
    assert services.consume_magic_link(raw).user == user
    assert services.consume_magic_link(raw).reason == "used"


def test_token_expired_and_tampered(user):
    raw = services.create_magic_link(user)
    MagicLinkToken.objects.update(created_at=timezone.now() - timedelta(minutes=16))
    assert services.consume_magic_link(raw).reason == "expired"
    # Flip the first character deterministically so the tampered token always differs.
    # (The old ``raw[:-1] + "x"`` equalled ``raw`` whenever it already ended in "x",
    # about 1 run in 64, and then consume returned "expired" instead of "invalid".)
    tampered = ("B" if raw[0] == "A" else "A") + raw[1:]
    assert tampered != raw
    assert services.consume_magic_link(tampered).reason == "invalid"


def test_token_for_inactive_user_invalid(user):
    raw = services.create_magic_link(user)
    user.is_active = False
    user.save()
    assert services.consume_magic_link(raw).reason == "invalid"


# --- views ---------------------------------------------------------------------------


def test_anonymous_shell_redirects_to_login(client, assoc):
    resp = client.get("/")
    assert resp.status_code == 302 and resp["Location"].startswith("/login/")


def test_login_flow_logs_in_once(client, assoc, user):
    grant(user, association=assoc)
    resp = client.post("/login/", {"email": "board@example.org"})
    assert b"Check your email" in resp.content
    link = link_from_mail()
    before = client.session.session_key
    resp = client.post(link)
    assert resp.status_code == 302 and resp["Location"] == "/"
    assert client.session["_auth_user_id"] == str(user.pk)
    assert client.session.session_key != before
    assert client.get("/").status_code == 200

    client.logout()
    resp = client.post(link)
    assert resp.status_code == 400 and b"already been used" in resp.content


def test_expired_and_tampered_views_are_friendly(client, assoc, user):
    raw = services.create_magic_link(user)
    MagicLinkToken.objects.update(created_at=timezone.now() - timedelta(minutes=20))
    assert b"expired" in client.get(f"/login/verify/{raw}/").content
    assert b"no longer valid" in client.get("/login/verify/garbage/").content


def test_login_is_enumeration_safe(client, assoc, user):
    known = client.post("/login/", {"email": "board@example.org"})
    unknown = client.post("/login/", {"email": "nobody@example.org"})
    assert len(mail.outbox) == 1

    def strip(r):
        html = r.content.decode()
        return html.replace("board@example.org", "X").replace("nobody@example.org", "X")

    assert known.status_code == unknown.status_code == 200
    assert re.sub(r'value="[^"]+"', "", strip(known)) == re.sub(
        r'value="[^"]+"', "", strip(unknown)
    )


def test_rate_limit_per_email(client, assoc, user, monkeypatch):
    for _i in range(services.RATE_PER_EMAIL):
        assert client.post("/login/", {"email": "board@example.org"}).status_code == 200
    resp = client.post("/login/", {"email": "board@example.org"})
    assert resp.status_code == 429
    assert len(mail.outbox) == services.RATE_PER_EMAIL


def test_rate_limit_per_ip(client, assoc, monkeypatch):
    monkeypatch.setattr(services, "RATE_PER_IP", 3)
    for i in range(3):
        assert client.post("/login/", {"email": f"u{i}@example.org"}).status_code == 200
    assert client.post("/login/", {"email": "u9@example.org"}).status_code == 429


def test_login_signals(client, assoc, user):
    seen = []
    handler = lambda **kw: seen.append(kw["user"])  # noqa: E731
    signals.login_link_requested.connect(handler)
    try:
        client.post("/login/", {"email": "board@example.org"})
    finally:
        signals.login_link_requested.disconnect(handler)
    assert seen == [user]


def test_logout_requires_post(client, assoc, user):
    client.force_login(user)
    assert client.get("/logout/").status_code == 200
    assert "_auth_user_id" in client.session
    client.post("/logout/")
    assert "_auth_user_id" not in client.session


# --- TOTP ----------------------------------------------------------------------------


def test_totp_drift_one_step(user):
    device = services.start_enrolment(user)
    totp = pyotp.TOTP(device.secret)
    now = 1_800_000_000
    assert services.verify_totp(device, totp.at(now - 30), now=now)
    assert services.verify_totp(device, totp.at(now + 30), now=now)
    assert not services.verify_totp(device, totp.at(now + 90), now=now + 1)
    assert not services.verify_totp(device, totp.at(now - 60), now=now + 1)


def test_totp_code_not_replayable(user):
    device = services.start_enrolment(user)
    now = 1_800_000_000
    code = pyotp.TOTP(device.secret).at(now)
    assert services.verify_totp(device, code, now=now)
    assert not services.verify_totp(device, code, now=now)


def test_enrolment_makes_ten_hashed_recovery_codes(user):
    _device, codes = enrol(user)
    assert len(codes) == 10 and len(set(codes)) == 10
    stored = set(RecoveryCode.objects.values_list("code_hash", flat=True))
    assert not stored & set(codes)
    assert user.has_mfa


def test_recovery_code_single_use(user):
    _device, codes = enrol(user)
    assert services.use_recovery_code(user, codes[0].upper())
    assert not services.use_recovery_code(user, codes[0])
    assert services.use_recovery_code(user, codes[1].replace("-", ""))


def test_mfa_challenge_after_link(client, assoc, user):
    device, codes = enrol(user)
    resp = client.post(f"/login/verify/{services.create_magic_link(user)}/")
    assert resp["Location"] == "/login/mfa/"
    assert "_auth_user_id" not in client.session
    assert client.get("/").status_code == 302

    assert client.post("/login/mfa/", {"code": "000000"}).status_code == 200
    assert "_auth_user_id" not in client.session
    resp = client.post("/login/mfa/", {"code": codes[0]})
    assert resp.status_code == 302 and resp["Location"] == "/"
    assert client.session["_auth_user_id"] == str(user.pk)


def test_mfa_challenge_with_totp(client, assoc, user):
    device, _codes = enrol(user)
    device.last_used_step = 0
    device.save()
    client.post(f"/login/verify/{services.create_magic_link(user)}/")
    resp = client.post("/login/mfa/", {"code": pyotp.TOTP(device.secret).now()})
    assert resp.status_code == 302


def test_mfa_challenge_lockout(client, assoc, user):
    enrol(user)
    client.post(f"/login/verify/{services.create_magic_link(user)}/")
    for _i in range(5):
        resp = client.post("/login/mfa/", {"code": "000000"})
    assert resp.status_code == 400
    assert client.get("/login/mfa/")["Location"] == "/login/"


def test_mfa_challenge_without_pending_redirects(client, assoc):
    assert client.get("/login/mfa/")["Location"] == "/login/"


def test_enrolment_page(client, assoc, user):
    client.force_login(user)
    html = client.get("/account/mfa/").content.decode()
    secret = re.search(r'data-testid="totp-secret">([A-Z2-7]+)<', html).group(1)
    assert "Set up two-step sign-in" in html and "data:image/svg+xml" in html
    resp = client.post("/account/mfa/", {"action": "confirm", "code": pyotp.TOTP(secret).now()})
    assert b"Recovery codes" in resp.content
    assert user.has_mfa


# --- forced enrolment ------------------------------------------------------------------


@pytest.fixture
def board_check():
    policy.set_board_role_check(lambda user, association: user.email.startswith("board"))
    yield
    policy.set_board_role_check(None)


def test_policy_default_never_requires(assoc, user):
    assoc.settings = {"require_mfa_for_board": True}
    assert not policy.require_mfa_for_board(user, assoc)


def test_forced_enrolment(client, assoc, user, board_check):
    grant(user, association=assoc)
    assoc.settings = {"require_mfa_for_board": True}
    assoc.save()
    resp = client.post(f"/login/verify/{services.create_magic_link(user)}/")
    assert resp["Location"] == "/account/mfa/"
    assert client.get("/")["Location"] == "/account/mfa/"
    assert client.get("/money/")["Location"] == "/account/mfa/"
    html = client.get("/account/mfa/").content.decode()
    assert "requires two-step sign-in" in html and "Not now" not in html
    secret = re.search(r'data-testid="totp-secret">([A-Z2-7]+)<', html).group(1)
    client.post("/account/mfa/", {"action": "confirm", "code": pyotp.TOTP(secret).now()})
    assert client.get("/").status_code == 200
    # Can't turn it off while required.
    client.post("/account/mfa/", {"action": "disable"})
    assert user.has_mfa


def test_not_forced_when_setting_off_or_not_board(client, assoc, board_check):
    other = get_user_model().objects.create_user("owner@example.org")
    assoc.settings = {"require_mfa_for_board": True}
    assoc.save()
    resp = client.post(f"/login/verify/{services.create_magic_link(other)}/")
    assert resp["Location"] == "/"


# --- e2e commands --------------------------------------------------------------------


def test_e2e_commands_refuse_without_flag(assoc, user, monkeypatch):
    monkeypatch.delenv("HOMEOA_E2E", raising=False)
    with pytest.raises(CommandError):
        call_command("e2e_login_url", user.email)
    with pytest.raises(CommandError):
        call_command("e2e_age_magic_links", "--minutes", "16")


def test_e2e_commands(client, assoc, user, monkeypatch, capsys):
    monkeypatch.setenv("HOMEOA_E2E", "1")
    call_command("e2e_login_url", user.email)
    path = capsys.readouterr().out.strip()
    assert path.startswith("/login/verify/")
    call_command("e2e_age_magic_links", "--minutes", "16")
    assert b"expired" in client.post(path).content


def test_e2e_commands_allowed_in_sandbox(assoc, user, monkeypatch, capsys):
    monkeypatch.delenv("HOMEOA_E2E", raising=False)
    assoc.settings = {"sandbox_mode": True}
    assoc.save()
    call_command("e2e_login_url", user.email)
    assert capsys.readouterr().out.startswith("/login/verify/")


def test_get_does_not_consume_post_does(client, assoc, user):
    raw = services.create_magic_link(user)
    for _i in range(3):  # scanners may prefetch repeatedly
        resp = client.get(f"/login/verify/{raw}/")
        assert resp.status_code == 200
        assert "bo•••@example.org" in resp.content.decode()
    assert MagicLinkToken.objects.get().used_at is None
    assert "_auth_user_id" not in client.session
    assert client.post(f"/login/verify/{raw}/").status_code == 302
    assert MagicLinkToken.objects.get().used_at is not None
    assert b"already been used" in client.get(f"/login/verify/{raw}/").content


def test_verify_post_requires_csrf(assoc, user):
    from django.test import Client

    raw = services.create_magic_link(user)
    resp = Client(enforce_csrf_checks=True).post(f"/login/verify/{raw}/")
    assert resp.status_code == 403
    assert MagicLinkToken.objects.get().used_at is None


# --- public base URL and trusted-proxy client IP ------------------------------


def test_magic_link_uses_base_url_when_set(client, assoc, user, settings):
    settings.HOMEOA_BASE_URL = "https://hoa.example.org"
    settings.ALLOWED_HOSTS = ["testserver", "spoofed.example.com"]
    client.post("/login/", {"email": "board@example.org"}, HTTP_HOST="spoofed.example.com")
    assert link_from_mail().startswith("https://hoa.example.org/")


def test_magic_link_falls_back_to_request_origin(client, assoc, user, settings):
    settings.HOMEOA_BASE_URL = ""
    client.post("/login/", {"email": "board@example.org"})
    assert link_from_mail().startswith("http://testserver/")


def test_login_ip_from_trusted_proxy_only(client, assoc, user, settings):
    from homeoa.accounts.models import LoginRequest

    settings.HOMEOA_TRUSTED_PROXIES = ["10.0.0.1"]
    xff = "203.0.113.9"
    client.post(
        "/login/", {"email": "a@example.org"}, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR=xff
    )
    client.post(
        "/login/", {"email": "b@example.org"}, REMOTE_ADDR="198.51.100.4", HTTP_X_FORWARDED_FOR=xff
    )
    ips = dict(LoginRequest.objects.values_list("email", "ip"))
    assert ips == {"a@example.org": "203.0.113.9", "b@example.org": "198.51.100.4"}
