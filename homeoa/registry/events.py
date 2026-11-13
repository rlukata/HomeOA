"""Registry domain events."""

import dataclasses
import datetime as dt

from homeoa.events import Event


@dataclasses.dataclass(frozen=True, kw_only=True)
class OwnershipTransferred(Event):
    name = "ownership.transferred"
    unit_id: int
    previous_period_id: int | None
    new_period_id: int
    effective_date: dt.date
    owner_ids: list[int]


@dataclasses.dataclass(frozen=True, kw_only=True)
class NoticePreferenceChanged(Event):
    name = "registry.notice_preference_changed"
    person_id: int
    preference_id: int
    delivery: str
    e_delivery_consent: bool
