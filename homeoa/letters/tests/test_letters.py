import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa.access import permissions as perms
from homeoa.access.services import assign_role, ensure_default_roles
from homeoa.access.testing import open_residents
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.letters import merge, pdf, render, seeds, services
from homeoa.letters.letterhead import set_letterhead
from homeoa.letters.models import ImmutableLetterError, Letter, LetterTemplate
from homeoa.providers import get_provider
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import set_initial_ownership

AS_OF = dt.date(2026, 10, 7)


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row Condominium", short_name="CR")
    ensure_default_roles(a)
    with association_context(a):
        yield a


@pytest.fixture
def unit(assoc):
    u = Unit.objects.create(
        association=assoc,
        number="A",
        address_line1="100 Cedar Row, Unit A",
        city="Olympia",
        region="WA",
        postal_code="98501",
    )
    owner = Person.objects.create(
        association=assoc,
        full_name="Jordan Rivera",
        address_line1="9 Elm St",
        city="Kent",
        region="WA",
        postal_code="98030",
    )
    co = Person.objects.create(association=assoc, full_name="Sam Rivera")
    set_initial_ownership(u, [owner, co], dt.date(2020, 1, 1))
    return u


def ctx(assoc, unit=None):
    return services.context_for(assoc, unit, AS_OF)


# --- merge + sandbox ---------------------------------------------------------------------


def test_merge_rendering(assoc, unit):
    subject, body = render.render_letter(
        "Unit {{ unit.number }}",
        "Dear {{ owner.name }},\n{{ owner.mailing_address }}\nOwed {{ balance.total }} "
        "on {{ letter.date }} to {{ association.name|upper }}."
        "{% if association.email %} mail{% else %} none{% endif %}",
        ctx(assoc, unit),
    )
    assert subject == "Unit A"
    assert "Dear Jordan Rivera & Sam Rivera," in body
    assert "9 Elm St, Kent, WA 98030" in body
    assert "Owed $0.00" in body
    assert "CEDAR ROW CONDOMINIUM" in body
    assert body.endswith(" none")


def test_balance_field_uses_ledger(assoc, unit, monkeypatch):
    monkeypatch.setattr("homeoa.ledger.services.unit_balance", lambda *a, **k: Decimal("1234.5"))
    _s, body = render.render_letter("", "{{ balance.total }}", ctx(assoc, unit))
    assert body == "$1,234.50"
    assert merge.money(Decimal("-3")) == "-$3.00"


def test_no_html_escaping_in_letter_text(assoc, unit):
    unit.number = "A&B"
    _s, body = render.render_letter("", "{{ unit.number }}", ctx(assoc, unit))
    assert body == "A&B"


def test_unknown_fields_flagged():
    result = render.check("{{ unit.number }}", "{{ owner.ssn }} {{ unit.colour }}")
    assert not result.ok
    assert result.unknown_fields == ["owner.ssn", "unit.colour"]


@pytest.mark.parametrize(
    "source",
    [
        "{% load static %}",
        "{% include 'ui/base.html' %}",
        "{% url 'letters:index' %}",
        "{% debug %}",
        "{% for x in unit.number %}{% endfor %}",
        "{{ unit.number|safe }}",
        "{{ unit.number|date:'Y' }}",
        "{{ unit }}",
        "{{ unit.number.upper }}",
        "{{ unit.__class__ }}",
        "{% if unit.number == 'A' %}x{% endif %}",
        "{% if unit.number %}x",
        "{% endif %}",
    ],
)
def test_sandbox_rejects_disallowed(source):
    assert not render.check(source).ok
    with pytest.raises(render.TemplateRejected):
        render.render(source, {})


def test_sandbox_allows_filters_and_if():
    assert render.check(
        '{# note #}{{ owner.name|default:"Owner"|title }}'
        "{% if not owner.name or unit.number %}x{% elif balance.total %}y{% else %}z{% endif %}"
    ).ok


def test_registry_extensible():
    merge.register("violation.title", "Violation", lambda c: "Noise", sample="Noise")
    try:
        assert render.check("{{ violation.title }}").ok
        assert render.render("{{ violation.title }}", merge.sample_values()) == "Noise"
    finally:
        merge.unregister("violation.title")
    with pytest.raises(ValueError):
        merge.register("bad", "Bad", lambda c: "")


# --- versions + seeds ----------------------------------------------------------------------


def test_versioning(assoc):
    t = services.create_template(assoc, name="Hello", subject="Hi", body="v1 {{ unit.number }}")
    assert t.current_version.number == 1
    v2 = services.save_version(t, subject="Hi", body="v2", note="tweak")
    t.refresh_from_db()
    assert (v2.number, t.current_version_id) == (2, v2.pk)
    assert list(t.versions.values_list("number", flat=True)) == [2, 1]
    with pytest.raises(ImmutableLetterError):
        v2.save()
    with pytest.raises(render.TemplateRejected):
        services.save_version(t, subject="", body="{{ nope.nope }}")
    assert t.versions.count() == 2
    assert AuditEvent.objects.filter(action="letter_template.version").count() == 2


def test_seed_defaults(assoc):
    assert services.ensure_default_templates(assoc) == 5
    assert services.ensure_default_templates(assoc) == 0
    kinds = set(LetterTemplate.objects.values_list("kind", flat=True))
    assert kinds == {"statement_cover", "reminder", "late_notice", "demand", "delinquency_notice"}
    assert all(t.draft_wording for t in LetterTemplate.objects.all())
    for t in LetterTemplate.objects.all():
        assert render.check(t.current_version.subject, t.current_version.body).ok


# --- PDF + generation ----------------------------------------------------------------------


def test_pdf_layout_fits_window_envelope():
    box = pdf.ADDRESS_BOX
    # #10 window seen on a tri-folded letter: about 2.04in to 3.17in from the top, 0.875in+.
    assert box["left"] >= 0.875 and box["left"] + box["width"] <= 0.875 + 4.5
    assert box["top"] >= 2.04 and box["top"] + box["height"] <= 3.17
    assert box["height"] * 72 >= pdf.ADDRESS_MAX_LINES * pdf.ADDRESS_LEADING


def test_pdf_bytes(assoc, unit):
    set_letterhead(assoc, address_lines=["PO Box 100", "Olympia, WA 98501"], email="b@x.org")
    t = services.create_template(
        assoc, name="Long", subject="S", body="\n\n".join(["Paragraph text."] * 80)
    )
    data, meta = services.render_pdf(t.current_version, ctx(assoc, unit))
    assert data.startswith(b"%PDF-")
    assert meta["address_lines"] == [
        "Jordan Rivera & Sam Rivera",
        "9 Elm St",
        "Kent, WA 98030",
    ]
    assert data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") >= 2 or b"/Count 2" in data


def test_generate_letter_stores_and_audits(assoc, unit):
    services.ensure_default_templates(assoc)
    t = LetterTemplate.objects.get(key="reminder")
    user = get_user_model().objects.create_user(email="bd@example.com")
    letter = services.generate_letter(t, unit, as_of=AS_OF, user=user)
    assert letter.template_version == t.current_version
    assert letter.unit == unit and letter.as_of == AS_OF
    stored = get_provider("FileStorage", assoc).get(letter.file_key)
    assert stored.startswith(b"%PDF-") and len(stored) == letter.size
    assert letter.file_key.startswith(str(assoc.pk))
    event = AuditEvent.objects.get(action="letter.generate")
    assert event.object_id == str(letter.pk) and event.actor_id == user.pk
    assert event.changes["version"] == 1
    with pytest.raises(ImmutableLetterError):
        letter.save()
    # A later edit makes a new version; the old letter keeps pointing at v1.
    services.save_version(t, subject="x", body="y")
    letter.refresh_from_db()
    assert letter.template_version.number == 1


# --- views ---------------------------------------------------------------------------------


@pytest.fixture
def board_client(client, assoc):
    user = get_user_model().objects.create_user(email="board@example.org")
    assign_role(user, assoc, perms.BOARD, start=AS_OF - dt.timedelta(days=30))
    client.force_login(user)
    return client


def test_views_flow(board_client, assoc, unit):
    resp = board_client.get("/settings/letters/")
    assert resp.status_code == 200 and "Demand letter" in resp.content.decode()
    resp = board_client.post(
        "/settings/letters/new/",
        {"name": "Bad", "kind": "general", "subject": "", "body": "{{ owner.ssn }}"},
    )
    assert resp.status_code == 400 and "owner.ssn" in resp.content.decode()
    assert not LetterTemplate.objects.filter(name="Bad").exists()
    resp = board_client.post(
        "/settings/letters/new/",
        {"name": "Hello", "kind": "general", "subject": "Hi", "body": "Unit {{ unit.number }}"},
    )
    t = LetterTemplate.objects.get(name="Hello")
    assert resp.status_code == 302
    resp = board_client.post(
        "/settings/letters/preview/",
        {"subject": "Hi", "body": "Unit {{ unit.number }}", "unit": unit.pk},
    )
    html = resp.content.decode()
    assert "Unit A" in html and "Jordan Rivera" in html
    resp = board_client.post("/settings/letters/preview/", {"body": "{% load x %}"})
    assert "not allowed" in resp.content.decode()
    resp = board_client.post(
        f"/settings/letters/{t.pk}/",
        {"name": "Hello", "kind": "general", "subject": "Hi", "body": "v2", "note": "n"},
    )
    t.refresh_from_db()
    assert t.current_version.number == 2
    resp = board_client.get(f"/settings/letters/{t.pk}/preview.pdf?unit={unit.pk}")
    assert resp["Content-Type"] == "application/pdf"
    resp = board_client.post(
        f"/settings/letters/{t.pk}/generate/", {"unit": unit.pk, "as_of": "2026-10-07"}
    )
    letter = Letter.objects.get()
    resp = board_client.get(f"/settings/letters/letter/{letter.pk}.pdf")
    assert resp.content.startswith(b"%PDF-")


def test_views_need_permission(client, assoc):
    user = get_user_model().objects.create_user(email="nobody@example.org")
    client.force_login(user)
    assert client.get("/settings/letters/").status_code == 403


# --- courtesy reminder ----------------------------------------------------------------


def _reminder():
    return next(s for s in seeds.DEFAULTS if s["key"] == "reminder")


def test_default_reminder_is_information_only():
    spec = _reminder()
    text = f"{spec['subject']}\n{spec['body']}".lower()
    for word in ("late", "fee", "interest", "foreclos", "lien", "collection"):
        assert word not in text, word


def test_reminder_says_how_to_pay(assoc, unit):
    services.ensure_default_templates(assoc)
    version = LetterTemplate.objects.get(key="reminder").current_version
    _, body = services.preview(version.subject, version.body, ctx(assoc, unit))
    assert "check payable to Cedar Row Condominium" in body
    assert "/me/pay/" not in body  # residents are off
    open_residents(assoc)
    _, body = services.preview(version.subject, version.body, ctx(assoc, unit))
    assert "Pay page" in body and "/me/pay/" in body


def test_unedited_reminder_upgraded_edited_left_alone(assoc):
    services.ensure_default_templates(assoc)
    old_body = seeds.PREVIOUS_BODIES["reminder"][0]
    t = LetterTemplate.objects.get(key="reminder")
    services.save_version(t, subject=t.current_version.subject, body=old_body)
    services.ensure_default_templates(assoc)
    t.refresh_from_db()
    assert t.current_version.body == str(_reminder()["body"])
    n = t.versions.count()
    services.ensure_default_templates(assoc)
    assert t.versions.count() == n  # idempotent
    services.save_version(t, subject=t.current_version.subject, body="Board wording.")
    services.ensure_default_templates(assoc)
    t.refresh_from_db()
    assert t.current_version.body == "Board wording."
