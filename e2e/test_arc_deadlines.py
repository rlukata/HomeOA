"""ARC deadlines, reminders and decision letters (ARC-03, ARC-04). Generic pack:
arc.decision_days 30, reminders 7 and 1 days before. Fixed clock Oct 7, so
decide_by = Nov 6, 2026. Seed: owner of unit A has e-notice consent."""

import time

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, SECRETARY_EMAIL
from helpers import m2
from helpers.seed import run_script
from test_arc import _submit_on_behalf

DUE = "Decide by Nov 6, 2026"
REMINDERS = (
    "import json\n"
    "from homeoa.notify.models import Notification as N\n"
    "q = N.all_associations.filter(type_key='arc.decision_reminder')\n"
    "print('E2EJSON' + json.dumps("
    "{'n': q.count(), 'people': q.values('recipient').distinct().count()}))\n"
)


def _decide(page, outcome="Approved"):
    page.locator("#d-out").select_option(label=outcome)
    page.get_by_role("button", name="Record decision").click()
    expect(page.get_by_text("Decision recorded.")).to_be_visible()


def test_decision_letter_emailed_and_downloadable(page, seed, login, axe, tmp_path, mail):
    m2.residents_enabled(True)  # Owner email stays a preview while residents are off
    login(BOARD_EMAIL)
    url = _submit_on_behalf(page, tmp_path, unit="A", files=False)
    expect(page.get_by_test_id("decide-by")).to_contain_text(DUE)
    axe(page)
    _decide(page)
    m2.drain_jobs()
    page.goto(url)
    link = page.get_by_test_id("decision-letter").get_by_role("link", name="Decision letter (PDF)")
    expect(link).to_be_visible()
    msg = mail.wait_for(m2.OWNERA_EMAIL)
    assert any(ct == "application/pdf" for _n, ct in msg.attachments), msg.attachments
    pk = url.rstrip("/").rsplit("/", 1)[-1]
    r = page.request.get(f"/me/arc/{pk}/letter/", max_redirects=0)
    assert r.status in (302, 403, 404), r.status  # board user is not the unit owner


def test_overdue_todo_and_reminders(page, seed, login, tmp_path, mail):
    login(BOARD_EMAIL)
    url = _submit_on_behalf(page, tmp_path, unit="D", files=False)
    expect(page.get_by_test_id("decide-by")).to_contain_text(DUE)
    # 7 days before decide_by: first reminder. Run twice: the job is idempotent.
    m2.run_scheduled("2026-10-30")
    m2.run_scheduled("2026-10-30")
    got = run_script(REMINDERS, "count reminders")
    assert got["n"] >= 1 and got["n"] == got["people"], got
    # Reviewer reminders are email too, without e-notice consent. Reviewers with an
    # login (treasurer, secretary) get one each. The president (unit I) has a contact
    # email but no login, so notify does not count him as board: with residents off his
    # delivery is held as "preview" and no email goes (by design).
    m2.drain_jobs()
    sent = []
    for _ in range(20):
        sent = [m for m in mail.messages() if m.subject.startswith("Decide by")]
        if len(sent) >= 2:
            break
        time.sleep(0.5)
    to = sorted(a for m in sent for a in m.to)
    assert to == sorted([BOARD_EMAIL, SECRETARY_EMAIL]), to
    held = run_script(
        "import json\n"
        "from homeoa.notify.models import Delivery\n"
        "d = Delivery.all_associations.filter(channel='email',"
        " notification__recipient__full_name='Rafael Ortiz',"
        " notification__type_key='arc.decision_reminder')"
        ".values_list('status', flat=True)\n"
        "print('E2EJSON' + json.dumps(sorted(set(d))))\n",
        "president delivery",
    )
    assert held == ["preview"], held
    run_script(
        "import datetime as dt, json\n"
        "from homeoa.arc.models import ArcRequest\n"
        "n = ArcRequest.all_associations.update(decide_by=dt.date(2026, 10, 1))\n"
        "print('E2EJSON' + json.dumps({'n': n}))\n",
        "backdate decide_by",
    )
    page.goto(url)
    expect(page.get_by_test_id("decide-by")).to_contain_text("Overdue")
    page.goto("/")
    expect(page.get_by_text("ARC decision overdue: Unit D")).to_be_visible()


def test_info_request_pauses_clock(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    _submit_on_behalf(page, tmp_path, unit="G", files=False)
    page.locator("#i-body").fill("Please send the paint color code.")
    page.get_by_role("button", name="Ask the owner").click()
    expect(page.get_by_test_id("decide-by")).to_contain_text(
        "Clock restarts when the owner answers"
    )
