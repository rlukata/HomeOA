"""Search across everything (PLT-13).

Each kind of record is a *source* contributed to the ``SEARCH_SOURCES`` slot: core sources
in ``sources.py``, optional modules from their own ready hooks, so a module that is off is
not searched. A source is ``fn(user, association, query) -> list[Hit]``; it checks its own
permission and returns its best hits first. Names and numbers match by substring and, for
misspellings, by trigram word similarity (pg_trgm); the text inside documents by
PostgreSQL full-text search. Everything stays in the database; there is no search server.
"""

from __future__ import annotations

import dataclasses
import logging

from django.db.models import Q

from homeoa.plugins import slots

logger = logging.getLogger(__name__)

#: Hits shown per kind of record.
LIMIT = 8
#: Shorter queries are not searched.
MIN_LENGTH = 2
MAX_LENGTH = 200


@dataclasses.dataclass(frozen=True)
class Hit:
    group: str  # the kind of record, e.g. "Units"
    title: str
    url: str
    detail: str = ""
    snippet: str = ""  # safe HTML: matched words in <mark>


@dataclasses.dataclass(frozen=True)
class Group:
    label: str
    hits: list[Hit]


def clean(query: str) -> str:
    return " ".join((query or "").split())[:MAX_LENGTH]


def search(user, association, query: str) -> list[Group]:
    """Every source's hits for ``query``, grouped by kind, in slot order. A source that
    fails is logged and left out; the others still answer."""
    query = clean(query)
    if association is None or len(query) < MIN_LENGTH:
        return []
    groups = []
    for source in slots.values(slots.SEARCH_SOURCES, association):
        try:
            hits = list(source(user, association, query))[:LIMIT]
        except Exception:  # noqa: BLE001 - one broken source must not break search
            logger.exception("Search source %s failed", getattr(source, "__name__", source))
            continue
        if hits:
            groups.append(Group(hits[0].group, hits))
    return groups


def name_match(field: str, query: str) -> Q:
    """``field`` contains ``query``, or has a word close to it (a misspelled name)."""
    return Q(**{f"{field}__icontains": query}) | Q(**{f"{field}__trigram_word_similar": query})
