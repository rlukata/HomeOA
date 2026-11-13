"""Every optional module, switched off on the full demo association, disappears from
everywhere core reaches it: its pages, the board menu, Home cards and to-dos, the owner
portal, daily jobs, event handlers, the setup wizard, the board packet and the resale
certificate.

The modules are read from the registry, so a new module is covered without editing this
file. Each check looks for the module's own slot contributions in the output, so a check
that does not apply to a module (it has no jobs, say) passes trivially.
"""

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest
from django.urls import URLResolver, get_resolver, reverse
from django.urls.exceptions import NoReverseMatch

from homeoa.core.models import Association
from homeoa.plugins import modules, registry, slots
from homeoa.portal.tests.test_requests_tab import BOARD, OWNER_A, _login, _on
from homeoa.portal.tests.test_requests_tab import pilot as pilot  # noqa: F401

MODULE_IDS = [m.id for m in registry.all_modules()]
PORTAL_PAGES = ("/me/", "/me/more/", "/me/requests/", "/me/pay/")
AS_OF = dt.date(2026, 10, 7)

# Sample values for URL converters, by converter class name.
SAMPLES = {
    "IntConverter": 1,
    "StringConverter": "A",
    "SlugConverter": "a",
    "PathConverter": "a",
    "UUIDConverter": uuid.UUID(int=1),
}


def test_every_shipped_module_is_covered():
    assert MODULE_IDS, "the module registry is empty at collection time"


def _url_names(resolver=None, namespace="", converters=None):
    """``(view name, sample kwargs or None)`` for every named URL in the URLconf; None
    means a converter has no sample value, so the URL cannot be built."""
    resolver = resolver or get_resolver()
    converters = dict(converters or {})
    for p in resolver.url_patterns:
        found = {**converters, **getattr(p.pattern, "converters", {})}
        if isinstance(p, URLResolver):
            ns = ":".join(x for x in (namespace, p.namespace) if x)
            yield from _url_names(p, ns, found)
        elif p.name:
            name = f"{namespace}:{p.name}" if namespace else p.name
            kwargs = {}
            for key, conv in found.items():
                sample = SAMPLES.get(type(conv).__name__)
                if sample is None:
                    kwargs = None
                    break
                kwargs[key] = sample
            yield name, kwargs


def _module_pages():
    """One parameter per (module, page): the page's sample URL, or a skip reason."""
    out = []
    seen = set()
    for name, kwargs in _url_names():
        for module_id in modules.page_modules(name):
            if (module_id, name) in seen:
                continue
            seen.add((module_id, name))
            url, reason = None, ""
            if kwargs is None:
                reason = "no sample value for a converter"
            else:
                try:
                    url = reverse(name, kwargs=kwargs)
                except NoReverseMatch:
                    reason = "cannot be reversed with sample kwargs"
            marks = [pytest.mark.skip(reason=reason)] if reason else []
            pid = f"{module_id}-{name}" + (f"-skipped({reason})" if reason else "")
            out.append(pytest.param(module_id, name, url, marks=marks, id=pid))
    return out


@pytest.fixture
def bare(db, settings, tmp_path):
    """The demo association with its board logins only: page routing needs no records,
    since a switched-off module's page is refused before its view runs."""
    from homeoa.core import demo
    from homeoa.core.context import association_context

    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = demo.seed(bare=True)
    with association_context(a):
        yield a


def _reload(association):
    return Association.objects.get(pk=association.pk)


def _switch_off(association, module_id, monkeypatch):
    """Turn ``module_id`` off, first turning off every module that needs it. Open items
    would refuse the change; that refusal has tests of its own, so here they are ignored."""
    monkeypatch.setattr(slots, "open_items", lambda module_id, association: [])
    for other in registry.all_modules():
        if module_id in other.requires and modules.is_on(_reload(association), other.id):
            _switch_off(association, other.id, monkeypatch)
    modules.set_module(_reload(association), module_id, False, user=None)


def _mine(slot, module_id):
    """The module's own contributions to ``slot``, whether or not it is on."""
    return [c for c in slots.contributions(slot) if c.module == module_id]


def _templates(response):
    return {t.name for t in response.templates}


# --- pages -----------------------------------------------------------------------------


@pytest.mark.parametrize(("module_id", "view_name", "url"), _module_pages())
def test_page_of_a_switched_off_module_404s(client, bare, monkeypatch, module_id, view_name, url):
    _switch_off(bare, module_id, monkeypatch)
    still_on = [m for m in modules.page_modules(view_name) if modules.is_on(_reload(bare), m)]
    if still_on:
        pytest.skip(f"also listed by {', '.join(still_on)}, which stays on")
    _login(client, BOARD)
    assert client.get(url).status_code == 404
    assert client.post(url).status_code == 404


@pytest.mark.parametrize("module_id", MODULE_IDS)
def test_first_page_of_a_switched_on_module_opens(client, pilot, module_id):
    _on(pilot)
    names = [name for name, kwargs in _url_names() if kwargs == {}]
    pages = [n for n in names if module_id in modules.page_modules(n)]
    if not pages:
        pytest.skip("the module has no page without URL arguments")
    url = reverse(pages[0])
    codes = []
    for email in (BOARD, OWNER_A):
        _login(client, email)
        codes.append(client.get(url).status_code)
    # A page that only takes POST answers a GET with 405, which still means it was reached.
    assert {200, 302, 405} & set(codes), (url, codes)


# --- everything else core reaches ------------------------------------------------------


@pytest.mark.parametrize("module_id", MODULE_IDS)
def test_switched_off_module_leaves_no_trace_in_core(client, pilot, monkeypatch, module_id):
    from homeoa.dashboard import todos
    from homeoa.events import subscribers
    from homeoa.jobs import run_due
    from homeoa.jobs.models import ScheduledRun
    from homeoa.portal.contributions import PortalPart, RequestKind
    from homeoa.registry.models import Unit
    from homeoa.reports import packet
    from homeoa.resale import prefill
    from homeoa.setup import services as setup
    from homeoa.ui.nav import _menu_tabs, get_hubs

    _on(pilot)
    _switch_off(pilot, module_id, monkeypatch)
    a = _reload(pilot)

    # Board menu: none of the module's sections, in the menu data or on the hub's page.
    _login(client, BOARD)
    for c in _mine(slots.MENU_TABS, module_id):
        assert c.value.subtab.slug not in [t.slug for t in _menu_tabs(c.value.hub, a)]
    for hub in get_hubs():
        response = client.get(hub.url)
        if response.status_code != 200:
            continue
        html = response.content.decode()
        for c in _mine(slots.MENU_TABS, module_id):
            if c.value.subtab.url_name and c.value.hub == hub.id:
                url = reverse(c.value.subtab.url_name)
                if url != hub.url:
                    assert f'href="{url}"' not in html, (hub.id, url)

    # Home: no card template of the module is rendered, and none of its to-dos is listed.
    response = client.get("/")
    assert response.status_code == 200
    cards = {c.value for c in _mine(slots.HOME_CARDS, module_id)}
    assert not cards & _templates(response)
    sources = {c.value[0] for c in _mine(slots.HOME_TODOS, module_id)}
    assert not sources & {item.source for item in todos.collect(a)}

    # Owner portal: none of the module's parts or request buttons.
    parts = [c.value for c in _mine(slots.OWNER_PORTAL, module_id)]
    part_templates = {p.template for p in parts if isinstance(p, PortalPart)}
    testids = {p.new_testid for p in parts if isinstance(p, RequestKind)}
    _login(client, OWNER_A)
    for page in PORTAL_PAGES:
        response = client.get(page)
        assert response.status_code == 200, page
        assert not part_templates & _templates(response), page
        for testid in testids:
            assert f'data-testid="{testid}"' not in response.content.decode(), page

    # Daily jobs: the module's jobs do not run.
    jobs = [c.value for c in _mine(slots.DAILY_JOBS, module_id)]
    if jobs:
        run_due(as_of=AS_OF, association=a, names=jobs)
        assert not ScheduledRun.all_associations.filter(association=a, job_name__in=jobs).exists()

    # Events: dispatch leaves out the module's handlers.
    for c in _mine(slots.EVENT_HANDLERS, module_id):
        name, handler, _mode = c.value
        assert (handler, _mode) not in subscribers(name, a.pk)

    # Setup wizard: no section or question about the module, and no answer applier.
    for section in setup.sections(a):
        assert section.module != module_id
        assert all(q.module != module_id for q in section.questions)
    assert all(c.module != module_id for c in slots.contributions(slots.WIZARD_SECTIONS, a))

    # Board packet: none of the module's sections.
    builders = [c.value for c in _mine(slots.BOARD_PACKET_SECTIONS, module_id)]
    if builders:
        start, end = packet._bounds(2026, 9)
        ctx = packet.PacketContext(a, start, end, packet.statements.fiscal_year_start(a, end))
        theirs = {str(t.title) for build in builders for t in build(ctx).tables}
        assert not theirs & {str(t.title) for t in packet.specs(a, 2026, 9)}

    # Resale certificate: the module's items say HomeOA does not track them.
    unit = Unit.objects.first()
    for c in _mine(slots.RESALE_CERTIFICATE_ANSWERS, module_id):
        key = c.value[0]
        answer = prefill._answer(SimpleNamespace(source=key), unit, AS_OF)
        assert answer == prefill.off_note(key) != ""


# --- demo and E2E data -----------------------------------------------------------------


@pytest.mark.parametrize("bare_seed", [False, True], ids=["full", "bare"])
def test_demo_seed_turns_every_module_on_where_new_associations_start_with_none(
    db, settings, tmp_path, bare_seed
):
    from homeoa.core import demo

    # Dev and E2E settings leave modules off by default, unlike the unit-test settings.
    settings.HOMEOA_MODULES_DEFAULT_ON = False
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = AS_OF.isoformat()
    a = demo.seed(bare=bare_seed)
    assert modules.enabled_modules(_reload(a)) == set(MODULE_IDS)
