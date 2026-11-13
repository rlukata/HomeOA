"""Demo master policy for seed_demo: a made-up carrier, renewing next March."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from homeoa.documents.models import Document

from . import services
from .models import MasterPolicy


def seed(association) -> None:
    certificate = Document.all_associations.filter(
        association=association, title="Master insurance certificate 2026"
    ).first()
    services.save(
        MasterPolicy(
            association=association,
            carrier="Evergreen Mutual (demo)",
            policy_number="EM-CR-2026",
            start_date=dt.date(2026, 3, 1),
            end_date=dt.date(2027, 3, 1),
            deductible=Decimal("10000.00"),
            liability_limit=Decimal("2000000.00"),
            premium=Decimal("10717.00"),
            agent_name="Pat Broker (demo)",
            agent_email="agent@evergreen.example",
            certificate=certificate,
        )
    )
