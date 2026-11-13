"""Sandbox vendors. Called by ``core.demo``.

Names and addresses are sample data; TINs and account numbers are obviously fake.
"""

from __future__ import annotations

from homeoa.ledger.models import Account

from . import services
from .models import Vendor, VendorContact

# name, category, expense code, 1099, fake EIN, contact (name, role, email, phone)
VENDORS = (
    (
        "Greenway Landscaping",
        "Grounds & landscaping",
        "5100",
        True,
        "000000101",
        ("Luis Ortega", "Owner", "luis@greenway.example", "206-555-0141"),
    ),
    (
        "Evergreen Gutter & Roof",
        "Gutters, roof moss",
        "5200",
        True,
        "000000102",
        ("Dana Whitfield", "Office manager", "office@evergreen.example", "206-555-0172"),
    ),
    (
        "Northwest Siding Co.",
        "Siding repair",
        "5200",
        True,
        "",
        ("Sam Patel", "Estimator", "bids@nwsiding.example", "425-555-0119"),
    ),
    (
        "Brightline Electric",
        "Electrical, lot lighting",
        "5200",
        True,
        "",
        ("Jo Brandt", "Electrician", "jo@brightline.example", "206-555-0188"),
    ),
    ("Cascade Mutual Insurance", "Master insurance policy", "5300", False, "", None),
    ("City of Tukwila Utilities", "Water & sewer", "5000", False, "", None),
    ("Seattle City Light", "Common-area electricity", "5000", False, "", None),
)


def seed(association) -> None:
    accounts = {a.code: a for a in Account.all_associations.filter(association=association)}
    for name, category, code, eligible, tin, contact in VENDORS:
        vendor = services.save_vendor(
            Vendor(
                association=association,
                name=name,
                category=category,
                default_expense_account=accounts.get(code),
                is_1099_eligible=eligible,
                legal_name=name if tin else "",
                tax_classification=Vendor.TaxClassification.LLC if tin else "",
                tin=tin or None,
                payee_name=name,
                remit_address_line1="PO Box 1000",
                remit_city="Tukwila",
                remit_region="WA",
                remit_postal_code="98168",
                account_number=f"CR-{len(name):04d}" if not eligible else None,
            )
        )
        if contact:
            c_name, role, email, phone = contact
            VendorContact.objects.create(
                association=association,
                vendor=vendor,
                name=c_name,
                role=role,
                email=email,
                phone=phone,
                is_primary=True,
            )
