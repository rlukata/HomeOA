"""WA condo rule values, checked against statute text on 2026-10-08 and 2026-10-11.

Quotes are copied verbatim from the statute text ("..." marks an omission). Until
2027-12-31 a condominium created before 2018-07-01 is under RCW 64.34 overlaid by the
RCW 64.90.365(1) sections; from 2028-01-01 all of RCW 64.90 applies.
Values the research does not quote stay ``verified=False``. Not legal advice.
"""

import datetime
from decimal import Decimal

from django.utils.translation import gettext_lazy as _

from homeoa.rules.base import (
    Basis,
    Frequency,
    LegalLimit,
    Obligation,
    ObligationExemption,
    ObligationPath,
    RequiredFeature,
    RulePack,
    RuleValue,
    SetupChoice,
)
from homeoa.rules.packs.setup import standard_sections

CHECKED = datetime.date(2026, 10, 8)
ANSWERS = datetime.date(2026, 10, 11)  # the legal backlog answers (research C)
TRANSITION = datetime.date(2028, 1, 1)  # RCW 64.34 repealed, all of RCW 64.90 applies
UNTIL_2028 = datetime.date(2027, 12, 31)
OVERLAY_2026 = datetime.date(2026, 1, 1)  # 2025 c 119: .445, .480(10), .502, .513, .580
ESSB_5129 = datetime.date(2026, 10, 16)  # 2025 c 119 checked against the session law


def _url(section):
    return f"https://app.leg.wa.gov/RCW/default.aspx?cite={section}"


def _v(  # noqa: PLR0913
    value,
    citation,
    section,
    quote="",
    *,
    note="",
    attorney=False,
    start=None,
    end=None,
    checked=CHECKED,
):
    """A statute value. Verified only when the research quotes the statute text."""
    return RuleValue(
        value=value,
        basis=Basis.STATUTE,
        verified=bool(quote),
        citation=citation,
        source_url=_url(section),
        quote=quote,
        checked_on=checked,
        note=note,
        attorney_review=attorney,
        effective_from=start,
        effective_to=end,
    )


def _now(*args, **kw):
    return _v(*args, end=UNTIL_2028, **kw)


def _2028(*args, **kw):
    return _v(*args, start=TRANSITION, **kw)


ENFORCEMENT_NOTE = (
    "The statute sets no number (checked 2026-10-11): the procedure comes from the "
    "declaration, bylaws or rules. Placeholder policy value. Attorney: hearing procedure."
)

# --- quotes (verbatim from the statutes) -------------------------------------------------
Q_372_1 = (
    "In the case of a condominium consisting of fewer than 50 units, an annual audit is "
    "also required but may be waived annually by unit owners other than the declarant of "
    "units to which 60 percent of the votes are allocated"
)
Q_530_2 = (
    "The financial statements of associations with annual assessments of $100,000 or more "
    "must be audited at least annually by a certified public accountant."
)
Q_530_2_WAIVER = (
    "an annual audit is also required but may be waived annually by unit owners … of units "
    "to which a majority of the votes in the association are allocated"
)
Q_304_1_K = (
    "after notice and an opportunity to be heard by the board of directors or by such "
    "representative designated by the board of directors and in accordance with such "
    "procedures as provided in the declaration or bylaws or rules and regulations adopted "
    "by the board of directors, levy reasonable fines in accordance with a previously "
    "established schedule thereof adopted by the board of directors and furnished to the "
    "owners"
)
Q_405_2_L = (
    "after notice and opportunity to be heard, impose and collect reasonable fines for "
    "violations of the governing documents in accordance with a previously established "
    "schedule of fines adopted by the board of directors and furnished to the owners "
    "pursuant to the requirements for notice in RCW 64.90.505"
)
Q_545_1 = (
    "An updated reserve study must be prepared annually. An updated reserve study must be "
    "prepared at least every third year by a reserve study professional and based upon a "
    "visual site inspection conducted by the reserve study professional."
)
Q_445_1_C = (
    "An association must provide notice to unit owners of the time, date, and place of "
    "each annual and special unit owners meeting not less than 14 days and not more than "
    "50 days before the meeting date."
)
Q_525_1_A = (
    "Within thirty days after adoption of any proposed budget for the common interest "
    "community, the board must provide a copy of the budget to all the unit owners and set "
    "a date for a meeting of the unit owners to consider ratification of the budget not "
    "less than fourteen nor more than fifty days after providing the budget."
)
Q_525_REJECT = (
    "Unless at that meeting the unit owners of units to which a majority of the votes in "
    "the association are allocated or any larger percentage specified in the declaration "
    "reject the budget, the budget and the assessments against the units included in the "
    "budget are ratified, whether or not a quorum is present."
)
Q_336_1 = (
    "a quorum is present throughout any meeting of the association if the owners of units "
    "to which twenty-five percent of the votes of the association are allocated are "
    "present in person or by proxy"
)
Q_425_2 = (
    "The association, within 10 days after a request by a unit owner, … shall furnish a "
    "resale certificate … a reasonable charge for the preparation of a resale certificate "
    "may not exceed $275. The association may charge a unit owner a nominal fee for "
    "updating a resale certificate within six months"
)
Q_640_2_B_FEE = (
    "a reasonable charge to the owner for the preparation of a resale certificate may not "
    "exceed $275, covering the direct costs of copying and providing such information, and "
    "may not include any additional charges for providing documents that are maintained in "
    "electronic form on a website, web portal, or application available to unit owners."
)
Q_640_2_B_UPDATE = (
    "The association may also charge a unit owner a nominal fee not to exceed $100 for "
    "updating a resale certificate within six months of the unit owner's previous resale "
    "certificate request."
)
Q_396_3_A = (
    "Notice to the association, the board of directors, or unit owners by electronic "
    "transmission is effective only upon those who have consented, in writing, to receive "
    "electronically transmitted notices under this chapter and have designated the "
    "address, location, or system to which such notices may be electronically transmitted"
)
Q_396_3_D = (
    "The consent of any owner is revoked if the association is unable to electronically "
    "transmit two consecutive notices and this inability becomes known to the secretary of "
    "the association or any other person responsible for giving the notice."
)
Q_515_3_A = (
    "...effective only upon unit owners and board members who have consented, in the form "
    "of a record, to receive electronically transmitted notices under this chapter and have "
    "designated in the consent the address, location, or system to which such notices may "
    "be electronically transmitted"
)
Q_364_17_A = (
    "No later than 30 days after an assessment becomes past due, an association must "
    "provide a notice of delinquency to a unit owner by first-class mail"
)
Q_364_17_A_I = (
    "Be mailed to the unit address and to any other address that a unit owner has provided "
    "to the association for the transmission of notice, and by email if the unit owner's "
    "electronic address is known to the association;"
)
Q_364_17_B = (
    "...until the 15th day after providing a unit owner with a notice of delinquency that "
    "meets the requirements in (a) of this subsection, an association may not: (i) Take any "
    "other action to collect a delinquent assessment"
)
Q_364_17_B_FEES = (
    "(A) The actual costs of printing and mailing the notice of delinquency; (B) An "
    "administrative fee of no more than $10 related to providing the notice of delinquency; "
    "and (C) A single late fee of no more than $50 or five percent of the amount of the "
    "unpaid assessment which triggered the fee, whichever is less."
)
Q_364_18_B = (
    "At or after the date that assessments have become past due for at least 90 days, but "
    "no sooner than 60 days after the first preforeclosure notice required in subsection "
    "(17)(a) of this section is mailed, the association has mailed, by first-class mail"
)
Q_364_18_A_I = (
    "Three months or more of assessments, not including fines, late charges, interest, "
    "attorneys' fees, or costs incurred by the association in connection with the "
    "collection of a delinquent owner's account"
)
Q_364_13 = (
    "The association may from time to time establish reasonable late charges and a rate of "
    "interest to be charged on all subsequent delinquent assessments or installments "
    "thereof. In the absence of another established nonusurious rate, delinquent "
    "assessments shall bear interest from the date of delinquency at the maximum rate "
    "permitted under RCW 19.52.020"
)
Q_19_52_020 = (
    "...does not exceed the higher of: (a) Twelve percent per annum; or (b) four percentage "
    "points above the equivalent coupon issue yield ... of the average bill rate for "
    "twenty-six week treasury bills"
)
Q_480_10 = (
    "An association must provide at least one method of accepting payment of assessments "
    "from unit owners at no charge or as a common expense."
)
Q_304_1_J = (
    "Impose and collect any payments, fees, or charges for the use, rental, or operation of "
    "the common elements, other than limited common elements described in RCW 64.34.204 "
    "(2) and (4), and for services provided to unit owners;"
)
Q_405_2_J = (
    "Impose and collect any reasonable payments, fees, or charges for: ... (ii) Services "
    "provided to unit owners;"
)
# Research C (2026-10-11): the legal backlog answers.
Q_372_4_A = (
    "(i) During reasonable business hours or at a mutually convenient time and location; and "
    "(ii) At the offices of the association or its managing agent."
)
Q_495_2_A_II = (
    "Upon 10 days' notice unless the size of the request or need to redact information "
    "reasonably requires a longer time, but in no event later than 21 days without a court "
    "order allowing a longer time."
)
Q_392_1 = (
    "Except as provided in RCW 64.90.080 and 64.90.545, a condominium association with ten "
    "or fewer unit owners is not required to follow the requirements under RCW 64.34.380 "
    "through 64.34.390 if two-thirds of the owners agree to exempt the association from the "
    "requirements."
)
Q_445_2_F = (
    "The notice must be given at least 14 days before the meeting and must state the time, "
    "date, place, and agenda of the meeting. Notwithstanding the foregoing, notice of a "
    "meeting to address an event or condition that could not have been reasonably foreseen "
    "and for which it is impracticable to provide notice as otherwise required by this "
    "chapter must be given at least seven days before the meeting and by means of electronic "
    "communication to unit owners whose electronic address or phone number is known to the "
    "association."
)
Q_450_1 = (
    "Unless the organizational documents provide otherwise, a quorum is present throughout "
    "any meeting of the unit owners if at the beginning of the meeting persons entitled to "
    "cast 20 percent of the votes in the association attend in person, by proxy, by means of "
    "communication under RCW 64.90.445(1) (e) or (f), or have voted by absentee ballot."
)
Q_525_3 = (
    "The board, at any time, may propose a special assessment. The assessment is effective "
    "only if the board follows the procedures for ratification of a budget described in "
    "subsection (1) of this section and the unit owners do not reject the proposed "
    "assessment."
)
Q_372_3_N = (
    "Ballots, proxies, absentee ballots, and other records related to voting by unit owners "
    "for one year after the election, action, or vote to which they relate"
)
Q_340_6_C = "The association must deliver a ballot to every unit owner with the notice."
Q_455_4_C = (
    "(ii) A ballot in a tangible medium to every unit owner except a unit owner that has "
    "consented in a record to electronic voting; and (iii) If the association allows "
    "electronic voting, instructions for electronic voting."
)
Q_640_2_A = (
    "The association, within 10 days after a request by a unit owner, and subject to the "
    "payment of any fees imposed pursuant to RCW 64.90.405(2)(m), must furnish a resale "
    "certificate"
)
Q_515_3_D = (
    "The consent of any unit owner or board member is revoked if: The association is unable "
    "to electronically transmit two consecutive notices given by the association in "
    "accordance with the consent"
)
Q_485_22_B = (
    "At or after the date that assessments have become past due for at least 90 days, but "
    "no sooner than 60 days after the first preforeclosure notice required in subsection "
    "(21)(a) of this section is mailed, the association has mailed, by first-class mail"
)
Q_FORECLOSURE_MIN_AMOUNT = (
    "$2,000 of assessments, not including fines, late charges, interest, attorneys' fees, "
    "or costs incurred by the association in connection with the collection of a "
    "delinquent owner's account"
)
Q_FORECLOSURE_WAIT = (
    "At least 90 days have elapsed from the date the minimum amount required in (a) of this "
    "subsection has accrued"
)
Q_528_B = "Such tax shall be equal to 30 percent of the homeowners association taxable income"
Q_528_C_1_B = (
    "60 percent or more of the gross income of such organization for the taxable year "
    "consists solely of amounts received as membership dues, fees, or assessments from"
)
Q_528_C_1_C = (
    "90 percent or more of the expenditures of the organization for the taxable year are "
    "expenditures for the acquisition, construction, management, maintenance, and care of "
    "association property"
)
Q_528_D_2_A = "there shall be allowed a specific deduction of $100"
Q_1005_10_D_1 = (
    "When a preauthorized electronic fund transfer from the consumer's account will vary in "
    "amount from the previous transfer under the same authorization or from the "
    "preauthorized amount, the designated payee or the financial institution shall send the "
    "consumer written notice of the amount and date of the transfer at least 10 days before "
    "the scheduled date of transfer."
)
Q_372_2 = (
    "Any reserve funds of an association shall be kept in a segregated account and any "
    "transaction affecting such funds, including the issuance of checks, shall require the "
    "signature of at least two persons who are officers or directors of the association."
)
Q_535_3_A = (
    "Except for investments and transfers between separate reserve accounts held by the "
    "same association, every disbursement of reserve funds requires: (a) The signature of "
    "at least two persons who are officers or directors of the association"
)
Q_485_18 = (
    "If the association does not establish such a rate, delinquent assessments bear "
    "interest from the date of delinquency at the maximum rate calculated under RCW "
    "19.52.020 on the date on which the assessments became delinquent."
)
Q_364_15 = (
    "The statement shall be furnished within fifteen days after receipt of the request and "
    "is binding on the association, the board of directors, and every unit owner"
)
Q_485_11 = (
    "The statement must be furnished within 15 days after receipt of the request and is "
    "binding on the association, the board, and every unit owner"
)
Q_502_6 = (
    "If, under subsection (5) of this section, the board determines by a two-thirds vote "
    "that a special assessment is necessary: (a) The assessment becomes effective "
    "immediately or in accordance with the terms of the vote"
)
Q_445_2_E = (
    "The board must provide at least 15 minutes at the beginning of each meeting for unit "
    "owners to comment about agenda items before the board votes."
)
Q_445_3_C = "Any votes of the board members are conducted by roll call or other verbal vote;"
# RCW 64.90.513(3)(c) (EV charging stations) and 64.90.580(3)(c) (heat pumps) read the same.
Q_DEEMED_60 = (
    "If an application is not denied in writing within 60 days from the date of receipt of "
    "the application, the application is deemed approved, unless that delay is the result "
    "of a reasonable request for additional information."
)
Q_445_3_D = (
    "Any person entitled to participate in the meeting is given the option of participating "
    "by telephone."
)

FEE_NOTE = (
    "Only if the declaration permits it and a free way to pay exists (ACH absorbed as a "
    "common expense, or check). No WA statute found that bans or caps convenience fees; "
    "card-network rules still apply. The board must also opt in on the payments plugin."
)
INTEREST_CAP_NOTE = (
    "Cap = the higher of 12% or the 26-week T-bill yield + 4 points (RCW 19.52.020(1)). "
    "12.00% per the WSR State Maximum Interest Rate for August, September (WSR 26-17) and "
    "October 2026 (WSR 26-19); refresh monthly from the WSR. Confirm with an attorney if "
    "the declaration sets a higher rate."
)
NO_ORDER_NOTE = (
    "No WA statute sets a payment application order (RCW 64.34, 64.90, 61.24 searched, "
    "2026-10-08). The declaration or a board collection policy decides; statute favors "
    "oldest assessments first (RCW 64.34.364(18)(a)(i), (19))."
)
Q_364_NOTICE_TEXT = (
    "THIS IS A NOTICE OF DELINQUENCY FOR PAST DUE ASSESSMENTS FROM THE UNIT OWNERS' "
    "ASSOCIATION TO WHICH YOUR HOME BELONGS. THIS NOTICE IS ONE STEP IN A PROCESS THAT "
    "COULD RESULT IN YOUR LOSING YOUR HOME."
)
Q_485_NOTICE_TEXT = (
    "THIS IS A NOTICE OF DELINQUENCY FOR PAST DUE ASSESSMENTS FROM THE UNIT OWNERS "
    "ASSOCIATION TO WHICH YOUR HOME BELONGS. THIS NOTICE IS ONE STEP IN A PROCESS THAT "
    "COULD RESULT IN YOUR LOSING YOUR HOME."
)
DELINQ = "RCW 64.34.364"
DELINQ_2028 = "RCW 64.90.485"


def _delinquency(value, sub, quote, sub_2028, quote_2028=None):
    """The same rule in RCW 64.34.364 now and RCW 64.90.485 from 2028. The 2028 wording is
    identical unless ``quote_2028`` is given (research C, section 26)."""
    return (
        _now(value, f"{DELINQ}{sub}", "64.34.364", quote),
        _2028(
            value,
            f"{DELINQ_2028}{sub_2028}",
            "64.90.485",
            quote_2028 or quote,
            checked=ANSWERS,
        ),
    )


FEDERAL_1120H = "Federal, so it applies in every state. CPA: confirm before the first filing."


def _federal(value, section, quote):
    """A U.S. federal value (Form 1120-H), checked against 26 U.S.C. 528 on 2026-10-11."""
    return RuleValue(
        value=value,
        basis=Basis.STATUTE,
        verified=True,
        citation=f"26 U.S.C. 528{section}",
        source_url="https://www.law.cornell.edu/uscode/text/26/528",
        quote=quote,
        checked_on=ANSWERS,
        note=FEDERAL_1120H,
    )


values = {
    # Form 1120-H (federal homeowners association election)
    "tax.1120h.exempt_income_min_pct": _federal(Decimal("60"), "(c)(1)(B)", Q_528_C_1_B),
    "tax.1120h.expenditure_min_pct": _federal(Decimal("90"), "(c)(1)(C)", Q_528_C_1_C),
    "tax.1120h.specific_deduction": _federal(Decimal("100.00"), "(d)(2)(A)", Q_528_D_2_A),
    "tax.1120h.rate": _federal(Decimal("30"), "(b)", Q_528_B),
    # Audit (RCW 64.34.372(1) now; RCW 64.90.530(2) from 2028)
    "audit.cpa_audit_revenue_threshold": (
        _now(
            None,
            "RCW 64.34.372(1)",
            "64.34.372",
            note="Under RCW 64.34 the audit turns on unit count (50+ units), not revenue.",
        ),
        _2028(
            Decimal("100000.00"),
            "RCW 64.90.530(2)",
            "64.90.530",
            Q_530_2,
            note="CPI adjustment under RCW 64.90.065 is unclear; no official adjusted figure.",
            attorney=True,
        ),
    ),
    "votes.audit_waiver.threshold_percent": (
        _now(
            Decimal("60"),
            "RCW 64.34.372(1)",
            "64.34.372",
            Q_372_1,
            note="60% of all allocated votes (not votes cast), renewed every year.",
        ),
        _2028(
            Decimal("50"),
            "RCW 64.90.530(2)",
            "64.90.530",
            Q_530_2_WAIVER,
            note="A majority of all votes in the association: more than 50%. Annual.",
        ),
    ),
    # Comparison from the statute wording (research C, section 8).
    "votes.audit_waiver.threshold_comparison": (
        _now(
            "gte",
            "RCW 64.34.372(1)",
            "64.34.372",
            Q_372_1,
            note="Owners of units with 60 percent of the votes: at least 60%.",
            checked=ANSWERS,
        ),
        _2028(
            "gt",
            "RCW 64.90.530(2)",
            "64.90.530",
            Q_530_2_WAIVER,
            note="A majority of the votes: more than 50%.",
            checked=ANSWERS,
        ),
    ),
    # Fines
    "fines.due_process_steps": (
        _now(
            ("adopted_fine_schedule", "written_notice", "opportunity_to_be_heard"),
            "RCW 64.34.304(1)(k)",
            "64.34.304",
            Q_304_1_K,
        ),
        _2028(
            ("adopted_fine_schedule", "written_notice", "opportunity_to_be_heard"),
            "RCW 64.90.405(2)(l)",
            "64.90.405",
            Q_405_2_L,
            note="Schedule furnished using the RCW 64.90.505 rule-making notice.",
        ),
    ),
    "fines.requires_prior_adopted_schedule": (
        _now(True, "RCW 64.34.304(1)(k)", "64.34.304", Q_304_1_K),
        _2028(True, "RCW 64.90.405(2)(l)", "64.90.405", Q_405_2_L),
    ),
    "fines.requires_notice": (
        _now(True, "RCW 64.34.304(1)(k)", "64.34.304", Q_304_1_K),
        _2028(True, "RCW 64.90.405(2)(l)", "64.90.405", Q_405_2_L),
    ),
    "fines.requires_hearing": (
        _now(True, "RCW 64.34.304(1)(k)", "64.34.304", Q_304_1_K),
        _2028(True, "RCW 64.90.405(2)(l)", "64.90.405", Q_405_2_L),
    ),
    # Violation escalation. Neither RCW 64.34.304(1)(k) nor RCW 64.90.405(2)(l)
    # fixes a courtesy step, a cure period or a notice-to-hearing interval: "procedures as
    # provided in the declaration or bylaws or rules". Policy placeholders, unverified.
    "enforcement.courtesy_notice_required": RuleValue(
        value=True,
        basis=Basis.POLICY,
        verified=False,
        citation="RCW 64.34.304(1)(k); RCW 64.90.405(2)(l)",
        source_url=_url("64.34.304"),
        note=ENFORCEMENT_NOTE,
        attorney_review=True,
    ),
    "enforcement.cure_days": RuleValue(
        value=14,
        basis=Basis.POLICY,
        verified=False,
        citation="RCW 64.34.304(1)(k); RCW 64.90.405(2)(l)",
        source_url=_url("64.34.304"),
        note=ENFORCEMENT_NOTE,
        attorney_review=True,
    ),
    "enforcement.hearing_notice_min_days": RuleValue(
        value=10,
        basis=Basis.POLICY,
        verified=False,
        citation="RCW 64.34.304(1)(k); RCW 64.90.405(2)(l)",
        source_url=_url("64.34.304"),
        note=ENFORCEMENT_NOTE,
        attorney_review=True,
    ),
    # Records inspection: RCW 64.34.372(4) sets no deadline, so until 2028 HomeOA uses the
    # RCW 64.90.495 days as board policy (research C, section 5). Calendar days.
    "records.inspection_days": (
        RuleValue(
            value=10,
            basis=Basis.POLICY,
            citation="RCW 64.34.372(4)",
            source_url=_url("64.34.372"),
            quote=Q_372_4_A,
            checked_on=ANSWERS,
            note="No deadline in RCW 64.34 today; HomeOA uses the 2028 rule early.",
            effective_to=UNTIL_2028,
        ),
        _2028(10, "RCW 64.90.495(2)(a)(ii)", "64.90.495", Q_495_2_A_II, checked=ANSWERS),
    ),
    "records.inspection_max_days": (
        RuleValue(
            value=21,
            basis=Basis.POLICY,
            citation="RCW 64.34.372(4)",
            source_url=_url("64.34.372"),
            quote=Q_372_4_A,
            checked_on=ANSWERS,
            note="No deadline in RCW 64.34 today; HomeOA uses the 2028 rule early.",
            effective_to=UNTIL_2028,
        ),
        _2028(
            21,
            "RCW 64.90.495(2)(a)(ii)",
            "64.90.495",
            Q_495_2_A_II,
            note="Longest allowed when the request is large or needs redaction.",
            checked=ANSWERS,
        ),
    ),
    # Reserve funds: two signatures, RCW 64.34.372(2) now and RCW 64.90.535(3)(a) from 2028.
    "reserves.spend_min_approvals": (
        _now(2, "RCW 64.34.372(2)", "64.34.372", Q_372_2, checked=ANSWERS),
        _2028(
            2,
            "RCW 64.90.535(3)(a)",
            "64.90.535",
            Q_535_3_A,
            note=(
                "Added by 2025 c 119 s 24. Each disbursement also needs invoices and the "
                "reserve study component it pays for (RCW 64.90.535(3)(b))."
            ),
            checked=ESSB_5129,
        ),
    ),
    # Reserve study (RCW 64.90.545 applies since 2018-07-01)
    "reserves.study_required": _v(
        True,
        "RCW 64.90.545(1)",
        "64.90.545",
        Q_545_1,
        note=(
            "Exemptions in RCW 64.90.545(2), unless the governing documents require a study: "
            "nonresidential units only; nominal reserve costs; only middle housing with no "
            "on-site wastewater or stormwater reserve components (added by 2026 c 96); study "
            "cost over 10% of the budget. The RCW 64.34.392 small-association exemption does "
            'not override it: that section starts "Except as provided in RCW 64.90.080 and '
            '64.90.545" (RCW 64.90.080 is now RCW 64.90.365).'
        ),
        attorney=True,
    ),
    # Meetings (RCW 64.90.445 since 2026-01-01; RCW 64.34.332 repealed)
    "notices.owner_meeting.min_days": _v(
        14, "RCW 64.90.445(1)(c)", "64.90.445", Q_445_1_C, start=OVERLAY_2026
    ),
    "notices.owner_meeting.max_days": _v(
        50, "RCW 64.90.445(1)(c)", "64.90.445", Q_445_1_C, start=OVERLAY_2026
    ),
    "notices.special_meeting.min_days": _v(
        14, "RCW 64.90.445(1)(c)", "64.90.445", Q_445_1_C, start=OVERLAY_2026
    ),
    "notices.special_meeting.max_days": _v(
        50, "RCW 64.90.445(1)(c)", "64.90.445", Q_445_1_C, start=OVERLAY_2026
    ),
    # Remote meetings and owner comments (RCW 64.90.445 since 2026-01-01)
    "notices.board_meeting.emergency_to_known_addresses": _v(
        True,
        "RCW 64.90.445(2)(f)",
        "64.90.445",
        Q_445_2_F,
        start=OVERLAY_2026,
        note="Every owner with a known email, consent or not (research C, section 9).",
        checked=ANSWERS,
    ),
    "meetings.board.owner_comment_minutes": _v(
        15, "RCW 64.90.445(2)(e)", "64.90.445", Q_445_2_E, start=OVERLAY_2026, checked=ANSWERS
    ),
    # EV chargers and heat pumps: deemed approved after 60 days (apply from 2026-01-01 and
    # stay in RCW 64.90 after 2028). Attorney: whether time waiting on the owner's answer
    # pauses the 60 days (HomeOA's reading) or restarts them.
    "arc.ev_charger.deemed_approval_days": _v(
        60,
        "RCW 64.90.513(3)(c)",
        "64.90.513",
        Q_DEEMED_60,
        start=OVERLAY_2026,
        checked=ANSWERS,
        attorney=True,
    ),
    "arc.heat_pump.deemed_approval_days": _v(
        60,
        "RCW 64.90.580(3)(c)",
        "64.90.580",
        Q_DEEMED_60,
        start=OVERLAY_2026,
        checked=ANSWERS,
        attorney=True,
    ),
    "meetings.remote.phone_option_required": _v(
        True, "RCW 64.90.445(3)(d)", "64.90.445", Q_445_3_D, start=OVERLAY_2026, checked=ANSWERS
    ),
    "meetings.remote.board_votes_by_roll_call": _v(
        True, "RCW 64.90.445(3)(c)", "64.90.445", Q_445_3_C, start=OVERLAY_2026, checked=ANSWERS
    ),
    # Board meetings (RCW 64.90.445(2) since 2026-01-01)
    "notices.board_meeting.min_days": _v(
        14,
        "RCW 64.90.445(2)(f)",
        "64.90.445",
        Q_445_2_F,
        start=OVERLAY_2026,
        note="Unless the meeting is in a schedule given to owners.",
        checked=ANSWERS,
    ),
    "notices.board_meeting.emergency_min_days": _v(
        7,
        "RCW 64.90.445(2)(f)",
        "64.90.445",
        Q_445_2_F,
        start=OVERLAY_2026,
        note=(
            "Unforeseeable events: electronic notice to every owner with a known email or "
            "phone, not only consenting owners. A true emergency (RCW 64.90.502) allows "
            "notice that is practicable under the circumstances."
        ),
        attorney=True,
        checked=ANSWERS,
    ),
    "meetings.owner.quorum_percent": (
        _now(
            Decimal("25"),
            "RCW 64.34.336(1)",
            "64.34.336",
            Q_336_1,
            note="Or a larger percentage in the bylaws.",
        ),
        _2028(
            Decimal("20"),
            "RCW 64.90.450(1)",
            "64.90.450",
            Q_450_1,
            note="Unless the organizational documents provide otherwise.",
            checked=ANSWERS,
        ),
    ),
    # Budget (RCW 64.90.525 since 2018)
    "budget.distribute_within_days": _v(30, "RCW 64.90.525(1)(a)", "64.90.525", Q_525_1_A),
    "budget.ratification.min_days": _v(14, "RCW 64.90.525(1)(a)", "64.90.525", Q_525_1_A),
    "budget.ratification.max_days": _v(50, "RCW 64.90.525(1)(a)", "64.90.525", Q_525_1_A),
    "budget.rejection_percent_of_all_votes": _v(
        Decimal("50"),
        "RCW 64.90.525(1)",
        "64.90.525",
        Q_525_REJECT,
        note="Rejected by more than 50% of all votes, or a larger share in the declaration.",
    ),
    "budget.rejection_comparison": _v(
        "gt",
        "RCW 64.90.525(1)",
        "64.90.525",
        Q_525_REJECT,
        note="A majority of all votes: more than 50%.",
        checked=ANSWERS,
    ),
    "budget.ratification.quorum_required": _v(False, "RCW 64.90.525(1)", "64.90.525", Q_525_REJECT),
    # Emergencies (RCW 64.90.502 since 2026-01-01): no ratification after a 2/3 board vote.
    "special_assessments.emergency_board_vote": _v(
        "two_thirds",
        "RCW 64.90.502(6)",
        "64.90.502",
        Q_502_6,
        start=OVERLAY_2026,
        note="Only for an emergency as defined in RCW 64.90.502(1); owners told promptly.",
        checked=ANSWERS,
    ),
    # A special assessment takes effect only after the budget ratification procedure.
    "special_assessments.ratification_required": _v(
        True,
        "RCW 64.90.525(3)",
        "64.90.525",
        Q_525_3,
        note=(
            "Exception: in an emergency (RCW 64.90.502) a two-thirds board vote makes a "
            "special assessment effective without ratification. Attorney: the declaration's "
            "rejection percentage."
        ),
        attorney=True,
        checked=ANSWERS,
    ),
    # Owner votes (RCW 64.34.340 / .372 now; RCW 64.90.455 / .495 from 2028)
    "votes.ballot_retention_months": (
        _now(
            12,
            "RCW 64.34.372(3)(n)",
            "64.34.372",
            Q_372_3_N,
            note="Ballots and proxies: 1 year.",
            checked=ANSWERS,
        ),
        _2028(
            12,
            "RCW 64.90.495(1)(o)",
            "64.90.495",
            Q_372_3_N,
            note="Ballots and proxies: 1 year.",
            checked=ANSWERS,
        ),
    ),
    "votes.electronic.express_authority": (
        _now(
            False,
            "RCW 64.34.340(6)(c)",
            "64.34.340",
            Q_340_6_C,
            note=(
                "RCW 64.34.340 does not mention electronic ballots. Electronic delivery works "
                "for owners who consented to electronic notice (RCW 64.34.396(3)(a)); the "
                "nonprofit act accepts a ballot in the form of a record (RCW 24.03A.480)."
            ),
            attorney=True,
            checked=ANSWERS,
        ),
        _2028(
            True,
            "RCW 64.90.455(4)(c)",
            "64.90.455",
            Q_455_4_C,
            note="Expressly allowed from 2028-01-01, with secret ballots (RCW 64.90.455(9)(a)).",
            checked=ANSWERS,
        ),
    ),
    "votes.paper_ballot_for_nonconsenting": (
        _now(
            True,
            "RCW 64.34.340(6)(c); RCW 64.34.396(3)(a)",
            "64.34.340",
            Q_340_6_C,
            note="Every owner gets a ballot; electronically only if they consented.",
            checked=ANSWERS,
        ),
        _2028(
            True,
            "RCW 64.90.455(4)(c)(ii)",
            "64.90.455",
            Q_455_4_C,
            note="Paper ballot to every owner who has not consented in a record.",
            checked=ANSWERS,
        ),
    ),
    # Resale certificate (RCW 64.34.425 now; RCW 64.90.640 from 2028)
    "resale.certificate_days": (
        _now(10, "RCW 64.34.425(2)", "64.34.425", Q_425_2),
        _2028(10, "RCW 64.90.640(2)(a)", "64.90.640", Q_640_2_A, checked=ANSWERS),
    ),
    "resale.certificate_form": (
        _now(
            "wa_64_34_425",
            "RCW 64.34.425(1)",
            "64.34.425",
            Q_425_2,
            note="Items (a) to (t) follow the statute text (checked 2026-10-11).",
            checked=ANSWERS,
        ),
        _2028(
            "wa_64_90_640",
            "RCW 64.90.640(1)",
            "64.90.640",
            Q_640_2_A,
            note="Items (a) to (z) follow the statute text (checked 2026-10-11).",
            checked=ANSWERS,
        ),
    ),
    "resale.fee_cap": (
        _now(Decimal("275.00"), "RCW 64.34.425(2)", "64.34.425", Q_425_2),
        _2028(
            Decimal("275.00"),
            "RCW 64.90.640(2)(b)",
            "64.90.640",
            Q_640_2_B_FEE,
            note="CPI-adjustable under RCW 64.90.065; no official adjusted figure.",
            attorney=True,
        ),
    ),
    "resale.update_fee_cap": (
        _now(
            None,
            "RCW 64.34.425(2)",
            "64.34.425",
            Q_425_2,
            note='A "nominal" fee with no dollar cap.',
            attorney=True,
        ),
        _2028(
            Decimal("100.00"),
            "RCW 64.90.640(2)(b)",
            "64.90.640",
            Q_640_2_B_UPDATE,
            note="CPI-adjustable under RCW 64.90.065; no official adjusted figure.",
            attorney=True,
        ),
    ),
    "resale.update_window_months": (
        _now(6, "RCW 64.34.425(2)", "64.34.425", Q_425_2),
        _2028(6, "RCW 64.90.640(2)(b)", "64.90.640", Q_640_2_B_UPDATE),
    ),
    # Electronic notice
    "notices.electronic_requires_consent": (
        _now(True, "RCW 64.34.396(3)(a)", "64.34.396", Q_396_3_A),
        _2028(True, "RCW 64.90.515(3)(a)", "64.90.515", Q_515_3_A),
    ),
    "notices.electronic.auto_revoke_after_failures": (
        _now(2, "RCW 64.34.396(3)(d)", "64.34.396", Q_396_3_D),
        _2028(2, "RCW 64.90.515(3)(d)", "64.90.515", Q_515_3_D, checked=ANSWERS),
    ),
    "notices.electronic.prior_vendor_consent_reusable": _v(
        False,
        "RCW 64.34.396(3)(a)",
        "64.34.396",
        Q_396_3_A,
        note=(
            "Analysis, no authority on point: a prior manager's e-delivery checkbox may not "
            "be consent to notices under the Act. Collect fresh consent."
        ),
        attorney=True,
    ),
    # Delinquency (RCW 64.34.364(17)-(18), 2025 c 393, effective 2026-01-01)
    "delinquency.first_notice.max_days_past_due": _delinquency(
        30, "(17)(a)", Q_364_17_A, "(21)(a)"
    ),
    "delinquency.notice.mail_required": _delinquency(True, "(17)(a)", Q_364_17_A, "(21)(a)"),
    "delinquency.notice.email_copy_if_known": _delinquency(
        True, "(17)(a)(i)", Q_364_17_A_I, "(21)(a)(i)"
    ),
    "delinquency.notice.statutory_text": (
        _now("wa_64_34_364_17a_iii", f"{DELINQ}(17)(a)(iii)", "64.34.364", Q_364_NOTICE_TEXT),
        _2028("wa_64_90_485_21a_iii", f"{DELINQ_2028}(21)(a)(iii)", "64.90.485", Q_485_NOTICE_TEXT),
    ),
    "delinquency.standstill_days": _delinquency(15, "(17)(b)", Q_364_17_B, "(21)(b)"),
    "delinquency.second_notice.min_days_past_due": _delinquency(
        90, "(18)(b)", Q_364_18_B, "(22)(b)", Q_485_22_B
    ),
    "delinquency.second_notice.min_days_after_first": _delinquency(
        60, "(18)(b)", Q_364_18_B, "(22)(b)", Q_485_22_B
    ),
    "delinquency.foreclosure.min_months_assessments": _delinquency(
        3, "(18)(a)(i)", Q_364_18_A_I, "(22)(a)(i)"
    ),
    "delinquency.foreclosure.min_amount": (
        _now(
            Decimal("2000.00"),
            "RCW 64.34.364(18)(a)(ii)",
            "64.34.364",
            Q_FORECLOSURE_MIN_AMOUNT,
            note="The greater of 3 months of assessments or $2,000.",
            checked=ANSWERS,
        ),
        _2028(
            Decimal("2000.00"),
            "RCW 64.90.485(22)(a)(ii)",
            "64.90.485",
            Q_FORECLOSURE_MIN_AMOUNT,
            checked=ANSWERS,
        ),
    ),
    "delinquency.foreclosure.min_days_after_threshold": (
        _now(90, "RCW 64.34.364(18)(c)", "64.34.364", Q_FORECLOSURE_WAIT, checked=ANSWERS),
        _2028(90, "RCW 64.90.485(22)(c)", "64.90.485", Q_FORECLOSURE_WAIT, checked=ANSWERS),
    ),
    # Late fees before the first notice + 15 days (caps on association settings)
    "late_fees.pre_notice.max_amount": _delinquency(
        Decimal("50.00"), "(17)(b)", Q_364_17_B_FEES, "(21)(b)"
    ),
    "late_fees.pre_notice.max_percent": _delinquency(
        Decimal("5"), "(17)(b)", Q_364_17_B_FEES, "(21)(b)"
    ),
    "late_fees.pre_notice.admin_fee_max": _delinquency(
        Decimal("10.00"), "(17)(b)", Q_364_17_B_FEES, "(21)(b)"
    ),
    "late_fees.cap": _now(
        None,
        "RCW 64.34.364(13)",
        "64.34.364",
        Q_364_13,
        note='"Reasonable" late charges; no general dollar cap outside the notice window.',
    ),
    # Interest
    "interest.statutory_default_applies": (
        _now(True, "RCW 64.34.364(13)", "64.34.364", Q_364_13, checked=ANSWERS),
        _2028(True, "RCW 64.90.485(18)", "64.90.485", Q_485_18, checked=ANSWERS),
    ),
    "statements.unpaid_assessments_days": (
        _now(15, "RCW 64.34.364(15)", "64.34.364", Q_364_15, checked=ANSWERS),
        _2028(15, "RCW 64.90.485(11)", "64.90.485", Q_485_11, checked=ANSWERS),
    ),
    "interest.annual_rate": _now(
        None,
        "RCW 64.34.364(13)",
        "64.34.364",
        Q_364_13,
        note=(
            "The board or declaration sets the rate, up to the RCW 19.52.020 maximum. If no "
            "rate is established, the statute applies the maximum (research C, section 3)."
        ),
    ),
    "interest.annual_rate_max": _v(
        Decimal("12.00"),
        "RCW 19.52.020(1)",
        "19.52.020",
        Q_19_52_020,
        note=INTEREST_CAP_NOTE,
        attorney=True,
    ),
    # Payments
    "payments.free_method_required": _v(
        True, "RCW 64.90.480(10)", "64.90.480", Q_480_10, start=OVERLAY_2026
    ),
    "payments.fee_pass_through_allowed": (
        _v(
            True,
            "RCW 64.34.304(1)(j); RCW 64.90.480(10)",
            "64.34.304",
            Q_304_1_J,
            note=FEE_NOTE,
            attorney=True,
            start=OVERLAY_2026,
            end=UNTIL_2028,
        ),
        _2028(
            True,
            "RCW 64.90.405(2)(j); RCW 64.90.480(10)",
            "64.90.405",
            Q_405_2_J,
            note=FEE_NOTE,
            attorney=True,
        ),
    ),
    "payments.allocation_order": RuleValue(value=None, basis=Basis.POLICY, note=NO_ORDER_NOTE),
    # Federal (Regulation E), not a Washington statute.
    "autopay.notice_days": RuleValue(
        value=10,
        basis=Basis.STATUTE,
        verified=True,
        citation="12 CFR 1005.10(d)(1)",
        source_url="https://www.ecfr.gov/current/title-12/part-1005/section-1005.10",
        quote=Q_1005_10_D_1,
        checked_on=ANSWERS,
        note="Notice of a varying preauthorized debit at least 10 days before it.",
    ),
    "payments.allocation_mode": RuleValue(value=None, basis=Basis.POLICY, note=NO_ORDER_NOTE),
}

# --- compliance obligations ------------------------------------------------------
NO_DATE = (
    "The statute sets no calendar date; HomeOA asks for the decision by "
    "the fiscal year end. "
    "Attorney: waiver procedure."
)
_audit_paths_now = (
    ObligationPath(
        id="audit",
        label=_("Annual audit"),
        help=_("Upload the audit report."),
        citation="RCW 64.34.372(1)",
        source_url=_url("64.34.372"),
    ),
    ObligationPath(
        id="waiver",
        label=_("Owner vote waiving the audit"),
        help=_("Owners holding the required share of all votes waive this year's audit."),
        citation="RCW 64.34.372(1)",
        source_url=_url("64.34.372"),
        threshold_key="votes.audit_waiver.threshold_percent",
    ),
)
_audit_paths_2028 = (
    ObligationPath(
        id="audit",
        label=_("Annual audit"),
        help=_(
            "Required by a CPA when annual assessments are $100,000 or more; upload the "
            "audit report."
        ),
        citation="RCW 64.90.530(2)",
        source_url=_url("64.90.530"),
    ),
    ObligationPath(
        id="waiver",
        label=_("Owner vote waiving the audit"),
        help=_(
            "Only when annual assessments are under $100,000: owners holding a majority of "
            "all votes waive this year's audit."
        ),
        citation="RCW 64.90.530(2)",
        source_url=_url("64.90.530"),
        threshold_key="votes.audit_waiver.threshold_percent",
    ),
)
_reserve_exemptions = (
    ObligationExemption(
        id="nominal_costs",
        question=_(
            "Are the association's reserve costs nominal (total replacement cost of reserve "
            "components under 50% of the annual budget, excluding reserve contributions, "
            "for a condo with stacked units; 75% otherwise)?"
        ),
        citation="RCW 64.90.545(2)(b), RCW 64.90.010(35)",
        source_url=_url("64.90.545"),
    ),
    ObligationExemption(
        id="cost_over_10_percent",
        question=_(
            "Would the reserve study or update cost more than 10% of the association's "
            "annual budget?"
        ),
        citation="RCW 64.90.545(2)(d)",
        source_url=_url("64.90.545"),
    ),
)
RESERVE_NOTE = (
    "Exemptions apply unless the governing documents require otherwise. The RCW 64.34.392 "
    "small-association exemption does not override RCW 64.90.545 (research C, section 18)."
)

obligations = (
    Obligation(
        id="annual_audit",
        title=_("Annual audit or waiver vote"),
        description=_("Each year the books are audited, or owners vote to waive the audit."),
        citation="RCW 64.34.372(1)",
        source_url=_url("64.34.372"),
        opens_days_before=120,
        paths=_audit_paths_now,
        verified=True,
        attorney_review=True,
        note=NO_DATE,
        effective_to=UNTIL_2028,
    ),
    Obligation(
        id="annual_audit",
        title=_("Annual audit or waiver vote"),
        description=_("Each year the books are audited, or owners vote to waive the audit."),
        citation="RCW 64.90.530(2)",
        source_url=_url("64.90.530"),
        opens_days_before=120,
        paths=_audit_paths_2028,
        verified=True,
        attorney_review=True,
        note=NO_DATE + " The $100,000 threshold may be CPI-adjusted (RCW 64.90.065).",
        effective_from=TRANSITION,
    ),
    Obligation(
        id="reserve_study_update",
        title=_("Annual reserve study update"),
        description=_("An updated reserve study is prepared every year."),
        citation="RCW 64.90.545(1)",
        source_url=_url("64.90.545"),
        opens_days_before=120,
        paths=(
            ObligationPath(
                id="update",
                label=_("Updated reserve study"),
                help=_("Upload the updated study."),
                citation="RCW 64.90.545(1)",
                source_url=_url("64.90.545"),
            ),
        ),
        exemptions=_reserve_exemptions,
        verified=True,
        attorney_review=True,
        note=RESERVE_NOTE,
    ),
    Obligation(
        id="reserve_study_site_visit",
        title=_("Reserve study with a professional site visit"),
        description=_(
            "At least every third year the update is prepared by a reserve study "
            "professional after a visual site inspection."
        ),
        citation="RCW 64.90.545(1)",
        source_url=_url("64.90.545"),
        frequency=Frequency.EVERY_N_YEARS,
        every_years=3,
        opens_days_before=180,
        paths=(
            ObligationPath(
                id="professional_site_visit",
                label=_("Professional study with site visit"),
                help=_("Upload the professional's study."),
                citation="RCW 64.90.545(1)",
                source_url=_url("64.90.545"),
            ),
        ),
        exemptions=_reserve_exemptions,
        verified=True,
        attorney_review=True,
        note=RESERVE_NOTE,
    ),
    Obligation(
        id="budget_ratification",
        title=_("Budget: send to owners and hold the ratification meeting"),
        description=_(
            "Within 30 days after adopting next year's budget the board sends it to all "
            "owners and sets a ratification meeting 14 to 50 days later."
        ),
        citation="RCW 64.90.525(1)(a)",
        source_url=_url("64.90.525"),
        opens_days_before=120,
        paths=(
            ObligationPath(
                id="ratified",
                label=_("Budget sent and ratification meeting held"),
                help=_("Upload the budget summary sent to owners and the meeting minutes."),
                citation="RCW 64.90.525(1)(a)",
                source_url=_url("64.90.525"),
                threshold_key="budget.distribute_within_days",
            ),
        ),
        verified=True,
        note=(
            "The statute counts from the board's adoption, not a calendar date; HomeOA "
            "asks for it before the fiscal year starts."
        ),
    ),
)

# Association setup wizard: the standard questions with Washington suggestions and
# the statute behind them.
C = SetupChoice
setup = standard_sections(
    {
        "late_fees.flat_amount": {
            "choices": (
                C(value=Decimal("0"), label=_("No late fee")),
                C(value=Decimal("10"), label="$10.00"),
                C(value=Decimal("15"), label="$15.00"),
                C(value=Decimal("25"), label="$25.00"),
            ),
            "suggested": Decimal("15"),
            "citation": "RCW 64.34.364(17)(b)",
            "source_url": _url("64.34.364"),
            "note": _(
                "Until 15 days after the first delinquency notice the fee is limited to the "
                "lesser of $50 or 5% of the late assessment; HomeOA trims it to that "
                "automatically. Outside that window the law only says reasonable."
            ),
        },
        "late_fees.interest_apr": {
            "choices": (
                C(value=Decimal("0"), label=_("No interest (0%)")),
                C(value=Decimal("6"), label="6%"),
                C(value=Decimal("12"), label=_("12% (the legal maximum in 2026)")),
            ),
            "citation": "RCW 64.34.364(13)",
            "source_url": _url("64.34.364"),
            "note": _(
                "If the board sets no rate, the law charges the maximum (RCW 19.52.020, "
                "12% in 2026). Adopting 0% here is what stops that."
            ),
            "law": LegalLimit(maximum="rule:interest.annual_rate_max", attorney_review=True),
        },
        "payments.allocation": {
            "note": _("No Washington statute sets an order; check the declaration."),
            "attorney_review": True,
        },
        "rule:delinquency.reminder_days": {
            "choices": (
                C(
                    value=None,
                    label=_("No separate reminder; the balance shows on the monthly statement"),
                    explain=_("Lower risk."),
                ),
                C(
                    value=15,
                    label=_("An information-only reminder 15 days after the due date"),
                    explain=_("It may count as a collection step before the first notice."),
                ),
            ),
            "suggested": None,
            "citation": "RCW 64.34.364(17)(b)",
            "source_url": _url("64.34.364"),
            "note": _(
                "Whether a reminder before the statutory first notice is an action to collect "
                "is unsettled."
            ),
            "attorney_review": True,
        },
        "rule:enforcement.hearing_notice_min_days": {
            "citation": "RCW 64.34.304(1)(k)",
            "source_url": _url("64.34.304"),
            "note": _("The law requires notice and an opportunity to be heard; it sets no days."),
            "attorney_review": True,
        },
        "fines.first_amount": {
            "citation": "RCW 64.34.304(1)(k)",
            "source_url": _url("64.34.304"),
            "note": _(
                "Fines must follow a schedule adopted and delivered to owners beforehand. "
                "The law sets no amounts."
            ),
        },
        "bills.two_approvals_from": {
            "note": _(
                "Bills paid from reserves always need two approvals (RCW 64.34.372(2); "
                "RCW 64.90.535(3)(a) from 2028), whatever you choose here."
            ),
        },
        "rule:arc.decision_days": {
            "note": _(
                "EV charger and heat pump requests are approved by law if not denied in "
                "writing within 60 days (RCW 64.90.513, 64.90.580)."
            ),
        },
        "rule:records.inspection_days": {
            "citation": "RCW 64.90.495(2)(a)(ii)",
            "source_url": _url("64.90.495"),
            "note": _("Required from 2028; a good practice before then."),
        },
        "rule:records.inspection_max_days": {
            "citation": "RCW 64.90.495(2)(a)(ii)",
            "source_url": _url("64.90.495"),
        },
        "rule:meetings.owner.quorum_percent": {
            "choices": (
                C(value=Decimal("25"), label=_("25% (the legal default)")),
                C(value=Decimal("50"), label="50%"),
            ),
            "citation": "RCW 64.34.336(1)",
            "source_url": _url("64.34.336"),
            "note": _(
                "Or the larger percentage in your bylaws. From 2028 the default becomes 20% "
                "(RCW 64.90.450); revisit this then."
            ),
        },
        "resale.fee": {
            "choices": (
                C(value=Decimal("0"), label=_("No fee")),
                C(value=Decimal("100"), label="$100.00"),
                C(value=Decimal("275"), label=_("$275.00 (the legal maximum)")),
            ),
            "citation": "RCW 64.34.425(2)",
            "source_url": _url("64.34.425"),
            "law": LegalLimit(maximum="rule:resale.fee_cap"),
        },
        "consent.adopt": {
            "citation": "RCW 64.34.396",
            "source_url": _url("64.34.396"),
            "note": _("Owners must consent in writing, naming the email address."),
            "attorney_review": True,
        },
    }
)

# Features the law requires. They are core and always on; citations are the ones this
# pack already uses for their rule values, unverified as feature requirements.
# Meeting minutes have no citation of their own here yet.
required_features = (
    RequiredFeature(
        id="records_requests",
        label=_("Records requests"),
        citation="RCW 64.34.372(4); RCW 64.90.495(2)(a)(ii)",
    ),
    RequiredFeature(
        id="resale_certificates",
        label=_("Resale certificates"),
        citation="RCW 64.34.425; RCW 64.90.640",
    ),
    RequiredFeature(
        id="budget_ratification",
        label=_("Budget ratification"),
        citation="RCW 64.90.525(1)(a)",
    ),
    RequiredFeature(
        id="meetings_and_minutes",
        label=_("Meetings and minutes"),
        citation="RCW 64.90.445",
    ),
)

pack = RulePack(
    id="us_wa_condo",
    name=_("Washington condominium"),
    jurisdiction="US-WA",
    values=values,
    obligations=obligations,
    setup=setup,
    # Every legally required feature is core, so no optional module is required here.
    required_modules={},
    required_features=required_features,
    # When RCW 64.90 takes over from RCW 64.34 and which of its sections apply before then.
    applies_under="RCW 64.34.076; RCW 64.90.360; RCW 64.90.365",
    law_checked_through=ANSWERS,
    change_source="homeoa_rulepack_us_wa.lawwatch.RcwCiteAffectedSource",
    description=_(
        "Washington condominium rules checked against the statute text on app.leg.wa.gov "
        "on Oct 8, 2026 and Oct 11, 2026: RCW 64.34 with the RCW 64.90.365 overlay until "
        "Dec 31, 2027, then RCW 64.90 from Jan 1, 2028. "
        'Values marked "confirm with attorney" depend on the declaration or unclear '
        "drafting. Not legal advice."
    ),
)
