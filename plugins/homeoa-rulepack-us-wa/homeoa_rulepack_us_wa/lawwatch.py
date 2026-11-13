"""Washington's law change source: the Legislature's public web services.

For each section the pack relies on, the RCW cite-affected service
(``GetLegislationAffectingRcwCite``) is asked once per biennium (``2025-26``), from the
biennium containing ``since`` to the current one. It names every bill that would affect
the section, passed or not, so the session law service (``GetSessionLawByBill``) is asked
once per bill: a bill with no session law (introduced, died or fully vetoed) is dropped,
a kept bill gets its session law (``2025 c 119``) and effective date when the reply gives
a single one. The service gives no amended/added/repealed action, so the reviewer reads
the bill. Bills in the first biennium are all listed, even those passed before
``since``, so an amendment not yet reviewed is never missed.
"""

from __future__ import annotations

import datetime
import urllib.error
import urllib.request
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from homeoa.rules.lawwatch import ChangeSource, LawChange, LawSourceError, network_allowed

URL = "https://wslwebservices.leg.wa.gov/RcwCiteAffectedService.asmx"
SESSION_LAW_URL = "https://wslwebservices.leg.wa.gov/SessionLawService.asmx"
NS = "http://WSLWebServices.leg.wa.gov/"
OPERATION = "GetLegislationAffectingRcwCite"
SESSION_LAW_OPERATION = "GetSessionLawByBill"
TIMEOUT = 30  # seconds, on every call
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"


def biennium(year: int) -> str:
    """The biennium containing ``year``: sessions start in odd years (``2025-26``)."""
    start = year if year % 2 else year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def bienniums(since: datetime.date, today: datetime.date) -> list[str]:
    start = int(biennium(since.year)[:4])
    end = int(biennium(today.year)[:4])
    return [biennium(year) for year in range(start, end + 1, 2)]


def _soap(url: str, operation: str, fields: dict[str, str]) -> urllib.request.Request:
    inner = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in fields.items())
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP_NS}">'
        f'<soap:Body><{operation} xmlns="{NS}">{inner}</{operation}></soap:Body>'
        "</soap:Envelope>"
    ).encode()
    return urllib.request.Request(  # noqa: S310 (fixed https URLs above)
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{NS}{operation}"',
        },
    )


def build_request(biennium_: str, section: str) -> urllib.request.Request:
    return _soap(URL, OPERATION, {"biennium": biennium_, "rcwCite": section})


def build_session_law_request(biennium_: str, bill_number: str) -> urllib.request.Request:
    return _soap(
        SESSION_LAW_URL,
        SESSION_LAW_OPERATION,
        {"biennium": biennium_, "billNumber": bill_number},
    )


def _text(node, name: str) -> str:
    found = node.find(f"{{{NS}}}{name}")
    return (found.text or "").strip() if found is not None else ""


def _root(body: bytes):
    try:
        # The reply comes from one fixed government host over HTTPS; expat refuses
        # entity expansion attacks.
        root = ElementTree.fromstring(body)  # noqa: S314
    except ElementTree.ParseError as exc:
        raise LawSourceError(f"The Legislature's service sent an unreadable reply: {exc}") from exc
    fault = root.find(f".//{{{SOAP_NS}}}Fault")
    if fault is not None:
        raise LawSourceError(
            f"The Legislature's service refused the request: {fault.findtext('faultstring', '')}"
        )
    return root


def _bills(body: bytes) -> list[tuple[str, str, str]]:
    """(biennium, bill number, label) for each bill in a cite-affected reply."""
    out = []
    for info in _root(body).iter(f"{{{NS}}}LegislationInfo"):
        bien = _text(info, "Biennium")
        bill = _text(info, "DisplayNumber") or _text(info, "BillId")
        out.append((bien, _text(info, "BillNumber"), f"{bill} ({bien})" if bien else bill))
    return out


def _bill_url(bien: str, number: str) -> str:
    return (
        f"https://app.leg.wa.gov/billsummary?BillNumber={number}&Year={bien[:4]}"
        if number and bien
        else ""
    )


def parse_response(section: str, body: bytes) -> list[LawChange]:
    """The bills in a ``GetLegislationAffectingRcwCite`` reply, as changes to ``section``
    (passed or not; session law and effective date empty)."""
    return [
        LawChange(section=section, bill=label, url=_bill_url(bien, number))
        for bien, number, label in _bills(body)
    ]


NO_SESSION_LAW_FAULT = "No session law data found"


def _no_session_law(body: bytes) -> bool:
    try:
        root = ElementTree.fromstring(body)  # noqa: S314 (see _root)
    except ElementTree.ParseError:
        return False
    fault = root.find(f".//{{{SOAP_NS}}}Fault")
    return fault is not None and (fault.findtext("faultstring") or "").startswith(
        NO_SESSION_LAW_FAULT
    )


def parse_session_law(body: bytes) -> tuple[str, datetime.date | None] | None:
    """``(session law, effective date)`` from a ``GetSessionLawByBill`` reply, or
    ``None`` when the bill did not become law. The date is ``None`` when the reply has
    several effective dates or none.

    For a bill that did not become law the service answers with a SOAP fault saying no
    session law data was found; that fault means "not law", any other fault is an error."""
    if _no_session_law(body):
        return None
    result = _root(body).find(f".//{{{NS}}}GetSessionLawByBillResult")
    if result is None:
        return None
    chapter, year = _text(result, "ChapterNumber"), _text(result, "Year")
    if not chapter or chapter == "0" or not year or year == "0":
        return None
    if _text(result, "Veto") == "true":
        return None
    effective = None
    raw = _text(result, "EffectiveDate")
    if raw and _text(result, "MultipleEffectiveDates") != "true":
        try:
            effective = datetime.date.fromisoformat(raw[:10])
        except ValueError:
            effective = None
        if effective is not None and effective.year < 1900:  # the service's empty date
            effective = None
    return f"{year} c {chapter}", effective


class RcwCiteAffectedSource(ChangeSource):
    def __init__(self, opener=None, today: datetime.date | None = None):
        self.opener = opener
        self.today = today

    def _fetch(self, request: urllib.request.Request) -> bytes:
        if self.opener is not None:
            return self.opener(request)
        if not network_allowed():
            raise LawSourceError("Network calls to the Legislature are turned off here.")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
                return response.read()
        except urllib.error.HTTPError as exc:
            # A SOAP fault comes back as HTTP 500 with the reason in the body.
            body = exc.read()
            if body:
                return body
            raise LawSourceError(f"The Legislature's service answered {exc.code}.") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LawSourceError(f"Could not reach the Legislature's service: {exc}") from exc

    def affected(self, sections, since: datetime.date) -> list[LawChange]:
        today = self.today or datetime.date.today()
        laws: dict[tuple[str, str], tuple[str, datetime.date | None] | None] = {}
        out = []
        for bien in bienniums(since, today):
            for section in sections:
                for bill_bien, number, label in _bills(self._fetch(build_request(bien, section))):
                    key = (bill_bien or bien, number)
                    if key not in laws:
                        laws[key] = parse_session_law(self._fetch(build_session_law_request(*key)))
                    law = laws[key]
                    if law is None:
                        continue
                    out.append(
                        LawChange(
                            section=section,
                            bill=label,
                            session_law=law[0],
                            effective=law[1],
                            url=_bill_url(*key),
                        )
                    )
        return out
