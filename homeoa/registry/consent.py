"""Electronic-notice consent text (REG-04).

RCW 64.34.396 lets the association give notice electronically only to an owner who
consented in writing, naming the address. The text also carries the ESIGN consumer
disclosures (15 U.S.C. 7001(c)): the right to paper, the scope of the consent, how to
withdraw, how to update the address, how to get a paper copy, and the hardware and
software needed.

The wording is a draft: ``ATTORNEY_REVIEW`` stays True until counsel has reviewed it, and
the pages that show it say so. A wording change gets a new ``VERSION``; each consent
record stores the version the owner accepted.
"""

from django.utils.translation import gettext_lazy as _

VERSION = "2026-10-08-draft"
ATTORNEY_REVIEW = True


def disclosures() -> list:
    """The consent text as a list of paragraphs (``{email}`` is filled by the template)."""
    return [
        _(
            "You may receive association notices, including notices the law requires, on "
            "paper by first-class mail. You have the right to keep getting them on paper."
        ),
        _(
            "This consent covers official association notices sent to you as an owner. "
            "Delinquency notices required by law are still mailed to you on paper."
        ),
        _(
            "You can withdraw this consent at any time on your profile page in "
            "the resident site or by writing to the board. Withdrawal takes effect right away."
        ),
        _(
            "To change the email address, update it on your profile page and give consent "
            "again for the new address. Until then notices go by mail."
        ),
        _("You can ask the board for a paper copy of any notice sent to you by email."),
        _(
            "You need an email account and a device that can open web pages and PDF files. "
            "If two emails in a row to you fail, your consent ends and notices go by mail."
        ),
    ]
