"""Statutory notice blocks: mandated wording that letters print verbatim (locked).

Each block is a ``.txt`` file in this folder, copied once from the official statute page
(see ``source_url``); never retyped and never board-editable. A rule-pack key (e.g.
``delinquency.notice.statutory_text``, effective-dated) picks which block is in
force; core never decides that by itself.

Blocks stay in English: they are the statute's own words. A translation for an owner's
preferred language is a board-uploaded document, never machine translation.

Contact lines: the statute's form leaves dotted blanks ("Telephone: . . . Website: . . .")
that the association fills with numbers obtained from the Department of Commerce. Each
such line is filled, in order, from the association's contact settings; a missing value
is printed as a visible placeholder and reported, so a preview can be marked.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

_DIR = Path(__file__).resolve().parent
CONTACT_LINE = re.compile(r"^Telephone:\s*(?:\.\s*)+Website:\s*(?:\.\s*)+$")

# Association.settings key holding the contact numbers for the delinquency notice.
CONTACTS_SETTING = "delinquency_contacts"
CONTACT_SLOTS: tuple[tuple[str, object], ...] = (
    ("foreclosure_hotline", gettext_lazy("Statewide foreclosure hotline")),
    ("hud", gettext_lazy("U.S. Department of Housing and Urban Development (HUD)")),
    ("legal_aid", gettext_lazy("Statewide civil legal aid hotline")),
)


@dataclasses.dataclass(frozen=True)
class StatutoryText:
    id: str
    citation: str
    source_url: str
    filename: str
    note: str = ""

    @property
    def text(self) -> str:
        return (_DIR / self.filename).read_text(encoding="utf-8")


TEXTS: dict[str, StatutoryText] = {
    t.id: t
    for t in (
        StatutoryText(
            id="wa_64_34_364_17a_iii",
            citation="RCW 64.34.364(17)(a)(iii)",
            source_url="https://app.leg.wa.gov/RCW/default.aspx?cite=64.34.364",
            filename="wa_64_34_364_17a_iii.txt",
            note="Copied from the statute page on 2026-10-08 (2025 c 393 s 12).",
        ),
        StatutoryText(
            id="wa_64_90_485_21a_iii",
            citation="RCW 64.90.485(21)(a)(iii)",
            source_url="https://app.leg.wa.gov/RCW/default.aspx?cite=64.90.485",
            filename="wa_64_90_485_21a_iii.txt",
            note="Copied from the statute page on 2026-10-08.",
        ),
        StatutoryText(
            id="wa_reserve_study_disclosure",
            citation="RCW 64.34.425(1)(t); RCW 64.90.640(1)(y)",
            source_url="https://app.leg.wa.gov/RCW/default.aspx?cite=64.90.640",
            filename="wa_reserve_study_disclosure.txt",
            note="Copied from the statute page on 2026-10-11; the two sections are identical.",
        ),
        StatutoryText(
            id="wa_64_90_640_1_z",
            citation="RCW 64.90.640(1)(z)",
            source_url="https://app.leg.wa.gov/RCW/default.aspx?cite=64.90.640",
            filename="wa_64_90_640_1_z.txt",
            note="Copied from the statute page on 2026-10-11 (2026 c 194 s 1).",
        ),
    )
}
CHOICES: tuple[str, ...] = tuple(TEXTS)


def get(text_id: str) -> StatutoryText:
    return TEXTS[text_id]


def contacts(association) -> dict[str, dict[str, str]]:
    raw = (association.settings or {}).get(CONTACTS_SETTING) or {}
    out = {}
    for slot, _label in CONTACT_SLOTS:
        item = raw.get(slot) or {}
        out[slot] = {
            "phone": str(item.get("phone") or "").strip(),
            "url": str(item.get("url") or "").strip(),
        }
    return out


def contacts_missing(association) -> bool:
    return any(not (c["phone"] and c["url"]) for c in contacts(association).values())


def placeholder() -> str:
    return _("[to fill in from the Department of Commerce]")


def render(text_id: str, association) -> tuple[str, bool]:
    """The block with its contact lines filled. Returns ``(text, contacts_missing)``."""
    filled = list(contacts(association).values())
    missing = False
    out, n = [], 0
    for line in get(text_id).text.splitlines():
        if CONTACT_LINE.match(line):
            c = filled[n] if n < len(filled) else {"phone": "", "url": ""}
            n += 1
            phone, url = c["phone"] or placeholder(), c["url"] or placeholder()
            missing = missing or not (c["phone"] and c["url"])
            line = f"Telephone: {phone} Website: {url}"
        out.append(line)
    return "\n".join(out), missing
