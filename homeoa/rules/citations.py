"""Every statute section a rule pack relies on, and where the pack cites it.

``index(pack)`` walks the pack's rule values, setup questions (their citation and the
citations inside their note and help text), obligations, required features, required
modules and the sections that decide which law applies. RCW citations are split into
sections (``RCW 64.34.364(17)(b)`` relies on ``64.34.364``); other citations (federal
law, IRS) are kept apart because no change source checks them.
"""

from __future__ import annotations

import dataclasses
import re

from django.utils.translation import gettext

from .base import RulePack

# A Washington section number: title.chapter.section (``64.90.010``, ``64.38.025A``).
_SECTION = re.compile(r"\b(\d{1,2}[A-Z]?\.\d{2,3}[A-Z]?\.\d{3,4}[A-Z]?)(?![\d.])")
_RCW = re.compile(r"\bRCW\b")
# Non-RCW citations recognised inside free text.
_OTHER = re.compile(
    r"\b\d+\s+U\.S\.C\.\s*§?\s*\d+[A-Za-z0-9()]*|\b\d+\s+C\.?F\.?R\.?\s*(?:§\s*)?[\d.]+[a-z0-9()]*"
)


@dataclasses.dataclass(frozen=True, order=True)
class Place:
    """Where a pack cites a section: ``kind`` (value, setup, obligation, feature,
    module, pack), the item's ``key`` and a readable ``label``."""

    kind: str
    key: str
    label: str = dataclasses.field(default="", compare=False)

    def to_json(self) -> dict:
        return {"kind": self.kind, "key": self.key, "label": self.label}


@dataclasses.dataclass
class CitationIndex:
    """``sections[section]`` and ``other[citation]`` list the places citing them."""

    sections: dict[str, list[Place]] = dataclasses.field(default_factory=dict)
    other: dict[str, list[Place]] = dataclasses.field(default_factory=dict)

    def add(self, text: str, place: Place, *, free_text: bool = False) -> None:
        parsed = parse(text, free_text=free_text)
        for section in parsed.sections:
            _append(self.sections, section, place)
        for citation in parsed.other:
            _append(self.other, citation, place)


@dataclasses.dataclass(frozen=True)
class Parsed:
    sections: tuple[str, ...] = ()
    other: tuple[str, ...] = ()


def _append(target: dict[str, list[Place]], key: str, place: Place) -> None:
    places = target.setdefault(key, [])
    if place not in places:
        places.append(place)


def _unique(items) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def parse(text, *, free_text: bool = False) -> Parsed:
    """The RCW sections and the other citations in ``text``.

    A citation field is split on ``;``: a part naming RCW gives every section number in
    it (``RCW 64.90.513, 64.90.580``); any other part is one non-RCW citation. In free
    text (notes, help) only RCW sections and recognised federal citations are taken.
    """
    text = str(text or "")
    if free_text:
        sections = []
        for match in _RCW.finditer(text):
            # The section numbers that follow "RCW", up to the next sentence.
            tail = re.split(r"[.;:]\s|\)\s+[a-z]", text[match.end() :], maxsplit=1)[0]
            sections.extend(_SECTION.findall(tail))
        other = [" ".join(m.group(0).split()) for m in _OTHER.finditer(text)]
        return Parsed(_unique(sections), _unique(other))
    sections, other = [], []
    for part in (p.strip() for p in text.split(";")):
        if not part:
            continue
        if _RCW.search(part):
            sections.extend(_SECTION.findall(part))
        else:
            other.append(part)
    return Parsed(_unique(sections), _unique(other))


def index(pack: RulePack) -> CitationIndex:
    """The citation index of ``pack``; sections sorted, places in pack order."""
    out = CitationIndex()
    for key, items in pack.values.items():
        place = Place("value", key, key)
        for item in items:
            out.add(item.citation, place)
            out.add(item.note, place, free_text=True)
    for section in pack.setup:
        for question in section.questions:
            place = Place("setup", question.target, str(question.label))
            out.add(question.citation, place)
            out.add(question.note, place, free_text=True)
            out.add(question.help, place, free_text=True)
    for ob in pack.obligations:
        place = Place("obligation", ob.id, str(ob.title))
        out.add(ob.citation, place)
        out.add(ob.note, place, free_text=True)
        for path in ob.paths:
            out.add(path.citation, place)
        for exemption in ob.exemptions:
            out.add(exemption.citation, place)
            out.add(exemption.note, place, free_text=True)
    for feature in pack.required_features:
        out.add(feature.citation, Place("feature", feature.id, str(feature.label)))
    for module, citation in pack.required_modules.items():
        out.add(citation, Place("module", module, module))
    out.add(pack.applies_under, Place("pack", "applies_under", gettext("Which law applies")))
    out.sections = dict(sorted(out.sections.items(), key=lambda kv: _sort_key(kv[0])))
    out.other = dict(sorted(out.other.items()))
    return out


def _sort_key(section: str) -> tuple:
    return tuple((int(re.sub(r"\D", "", p) or 0), p) for p in section.split("."))
