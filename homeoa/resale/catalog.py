"""Resale certificate forms: the items a certificate must answer, per statute.

A rule-pack key (``resale.certificate_form``, effective-dated) picks the form in force;
core never decides that by itself. Each item has a ``source`` naming what HomeOA can
pre-fill (``prefill.SOURCES`` or the RESALE_CERTIFICATE_ANSWERS slot) or ``""`` for
items only the board can answer.

The Washington items follow the statute text item by item (RCW 64.34.425(1) until
2027-12-31, RCW 64.90.640(1) from 2028-01-01), checked on 2026-10-11. Labels are shortened, but
keep every element the statute asks for; fixed disclosures are pre-filled word for word.
"""

from __future__ import annotations

import dataclasses

from django.utils.translation import gettext_lazy as _


@dataclasses.dataclass(frozen=True)
class Item:
    key: str  # the statute's paragraph letter, e.g. "a"
    label: object
    source: str = ""  # a prefill source, or "" when the board answers
    text_id: str = ""  # for source "statutory": the verbatim block in homeoa.letters.statutory


@dataclasses.dataclass(frozen=True)
class CertificateForm:
    id: str
    citation: str
    source_url: str
    items: tuple[Item, ...]
    verified: bool = False
    note: str = ""


WA_64_34_425 = CertificateForm(
    id="wa_64_34_425",
    citation="RCW 64.34.425(1)",
    source_url="https://app.leg.wa.gov/RCW/default.aspx?cite=64.34.425",
    verified=True,
    note="Items (a) to (t) checked against the statute text on 2026-10-11.",
    items=(
        Item(
            "a",
            _(
                "Any right of first refusal or other restraint on selling the unit in the "
                "declaration"
            ),
        ),
        Item(
            "b",
            _(
                "The monthly common expense assessment; unpaid common expense or special "
                "assessments now due from the seller; special assessments levied on the unit "
                "and not yet due"
            ),
            "unit_charges",
        ),
        Item(
            "c",
            _(
                "Common expenses or special assessments against any unit more than 30 days past "
                "due (current to within 45 days)"
            ),
        ),
        Item(
            "d",
            _(
                "Obligations of the association more than 30 days past due (current to "
                "within 45 days)"
            ),
        ),
        Item("e", _("Any other fees payable by unit owners")),
        Item(
            "f",
            _(
                "Repair or replacement costs over 5% of the annual budget that the board has "
                "approved"
            ),
        ),
        Item(
            "g",
            _("Reserves for repair or replacement, and any part designated for specific projects"),
            "reserves",
        ),
        Item(
            "h",
            _("Last year's annual financial statement, with the audit report if one was prepared"),
        ),
        Item(
            "i",
            _(
                "A balance sheet and a revenue and expense statement on the accrual basis, "
                "current to within 120 days"
            ),
        ),
        Item("j", _("The current operating budget"), "budget"),
        Item(
            "k",
            _(
                "Unsatisfied judgments against the association, and the status of pending suits "
                "or legal proceedings where it is a plaintiff or defendant"
            ),
        ),
        Item(
            "l",
            _("Insurance coverage provided for the benefit of unit owners"),
            "insurance",
        ),
        Item(
            "m",
            _(
                "Whether any alterations or improvements to the unit or its limited common "
                "elements violate the declaration"
            ),
            "violations",
        ),
        Item(
            "n",
            _(
                "Units still owned by the declarant, whether control has passed to the owners, "
                "and the date"
            ),
        ),
        Item(
            "o",
            _(
                "Whether there are health or building code violations for the unit, its limited "
                "common elements or any other part of the condominium"
            ),
        ),
        Item(
            "p",
            _(
                "The remaining term of any leasehold affecting the condominium, and how it is "
                "extended or renewed"
            ),
        ),
        Item(
            "q",
            _(
                "Copies of the declaration, bylaws, rules, the current reserve study (if any), "
                "and other information reasonably requested by lenders"
            ),
            "governing_documents",
        ),
        Item(
            "r",
            _(
                "Whether a qualified warranty (RCW 64.35) covers the condominium, and its "
                "claim history"
            ),
        ),
        Item(
            "s",
            _(
                "Requirements for EV charging stations in the unit or its limited common "
                "elements: application status, insurance, maintenance and costs"
            ),
        ),
        Item(
            "t",
            _(
                "If there is no current reserve study, the statutory disclosure, word for word "
                "(delete it if the association has one)"
            ),
            "statutory",
            "wa_reserve_study_disclosure",
        ),
    ),
)

WA_64_90_640 = CertificateForm(
    id="wa_64_90_640",
    citation="RCW 64.90.640(1)",
    source_url="https://app.leg.wa.gov/RCW/default.aspx?cite=64.90.640",
    verified=True,
    note=(
        'Items (a) to (z) checked against the statute text on 2026-10-11. Write "NONE" or '
        '"RECORDS UNAVAILABLE" for each category not provided.'
    ),
    items=(
        Item(
            "a",
            _(
                "Any right of first refusal or other restraint on selling the unit in the "
                "declaration"
            ),
        ),
        Item(
            "b",
            _(
                "For the seller's unit: any assessment now due, delinquent assessments, and "
                "special assessments levied and not yet due"
            ),
            "unit_charges",
        ),
        Item(
            "c",
            _(
                "Assessments against any unit more than 30 days past due (current to within "
                "45 days)"
            ),
        ),
        Item(
            "d",
            _(
                "Monetary obligations of the association more than 30 days past due (current "
                "to within 45 days)"
            ),
        ),
        Item("e", _("Any other fees payable to the association by unit owners")),
        Item(
            "f",
            _(
                "Expenditures or repair and replacement costs reasonably expected to exceed 5% "
                "of the board-approved annual budget, whether or not owners must approve them"
            ),
        ),
        Item(
            "g",
            _("Whether the association has a reserve study prepared under RCW 64.90.545 and .550"),
        ),
        Item(
            "h",
            _("The annual financial statement, with the most recent audit report available"),
        ),
        Item("i", _("The most recent balance sheet and revenue and expense statement")),
        Item("j", _("The current operating budget"), "budget"),
        Item(
            "k",
            _(
                "Unsatisfied judgments against the association, and the status of legal actions "
                "where it is a party or a claimant (RCW 64.50.010)"
            ),
        ),
        Item(
            "l",
            _("Insurance carried by the association, and the insurance broker or agent's contact"),
            "insurance",
        ),
        Item(
            "m",
            _(
                "Whether the board has given or received notice in a record that uses, "
                "occupancies, alterations or improvements in the unit or its limited common "
                "elements violate the governing documents"
            ),
            "violations",
        ),
        Item(
            "n",
            _(
                "Units still owned by the declarant, whether control has passed to the owners, "
                "and the date"
            ),
        ),
        Item(
            "o",
            _(
                "Uncured notices from a government agency of environmental, health or building "
                "code violations for the unit, its limited common elements or the community"
            ),
        ),
        Item(
            "p",
            _(
                "The remaining term of any leasehold affecting the community, and how it is "
                "extended or renewed"
            ),
        ),
        Item("q", _("Restrictions in the declaration on the amount an owner may receive on sale")),
        Item("r", _("Cooperatives only: an accountant's statement on tax deductibility, if any")),
        Item("s", _("Any pending sale or encumbrance of common elements")),
        Item(
            "t",
            _("How any restriction on using, occupying or renting the unit affects this unit"),
        ),
        Item(
            "u",
            _(
                "Copies of the declaration, organizational documents, rules, board policies "
                "and resolutions in effect, the last 12 months of board and association minutes "
                "(less exempt information), the most current reserve study, and information "
                "reasonably requested by lenders"
            ),
            "governing_documents",
        ),
        Item(
            "v",
            _(
                "Whether a qualified warranty (RCW 64.35) covers the units or common elements, "
                "and known claims"
            ),
        ),
        Item("w", _("Any age-related occupancy restrictions")),
        Item(
            "x",
            _(
                "Requirements for EV charging stations in the unit or its limited common "
                "elements: application status, insurance, maintenance and costs"
            ),
        ),
        Item(
            "y",
            _(
                "If there is no reserve study under RCW 64.90.545 and .550, the statutory "
                "disclosure, word for word (delete it if the association has one)"
            ),
            "statutory",
            "wa_reserve_study_disclosure",
        ),
        Item(
            "z",
            _("The statutory notice to purchasers, word for word in conspicuous type"),
            "statutory",
            "wa_64_90_640_1_z",
        ),
    ),
)

# A signed statement of a unit's unpaid assessments, for an owner or a lender. The deadline
# comes from the rule pack (``statements.unpaid_assessments_days``).
UNPAID_STATEMENT = CertificateForm(
    id="unpaid_statement",
    citation="",
    source_url="",
    verified=True,
    note="Assessments only; other charges are listed apart.",
    items=(
        Item(
            "1",
            _("Unpaid assessments against the unit (regular and special) as of this statement"),
            "unpaid_assessments",
        ),
        Item(
            "2",
            _("Other unpaid amounts (late fees, interest, fines, other charges)"),
            "other_unpaid",
        ),
    ),
)

FORMS: dict[str, CertificateForm] = {
    f.id: f
    for f in (
        WA_64_34_425,
        WA_64_90_640,
        UNPAID_STATEMENT,
        CertificateForm(
            id="generic",
            citation="",
            source_url="",
            note="For associations without a jurisdiction form; edit the answers freely.",
            items=(
                Item(
                    "1",
                    _("Monthly assessment, unpaid amounts and special assessments"),
                    "unit_charges",
                ),
                Item("2", _("Other fees payable by unit owners")),
                Item("3", _("Reserves"), "reserves"),
                Item("4", _("Current operating budget"), "budget"),
                Item("5", _("Insurance"), "insurance"),
                Item("6", _("Open violations at the unit"), "violations"),
                Item("7", _("Pending lawsuits and unsatisfied judgments")),
                Item("8", _("Governing documents"), "governing_documents"),
            ),
        ),
    )
}
CHOICES: tuple[str, ...] = tuple(FORMS)


def get(form_id: str) -> CertificateForm:
    return FORMS[form_id]
