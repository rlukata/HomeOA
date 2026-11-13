"""Default templates. Draft wording, not legal advice: the board must review them.

Collection wording (late notices, demand letters) is governed by state law; Washington
requirements have not been reviewed by an attorney, so these
templates carry no statutory deadlines or amounts and are flagged ``draft_wording``.
"""

from django.utils.translation import gettext_lazy as _

DEFAULTS = [
    {
        "key": "statement-cover",
        "kind": "statement_cover",
        "name": _("Statement cover letter"),
        "subject": _("Your account statement from {{ association.name }}"),
        "body": _(
            "Dear {{ owner.name }},\n\n"
            "Enclosed is your account statement for unit {{ unit.number }} as of "
            "{{ letter.date }}. Your balance is {{ balance.total }}.\n\n"
            "If you have questions about your account, contact the board"
            "{% if association.email %} at {{ association.email }}{% endif %}.\n\n"
            "Sincerely,\nThe Board of Directors\n{{ association.name }}"
        ),
    },
    {
        "key": "reminder",
        "kind": "reminder",
        "name": _("Payment reminder"),
        "subject": _("Friendly reminder: balance due for unit {{ unit.number }}"),
        "body": _(
            # Information only. Balance, how to pay and whom to ask; no fees,
            # charges, deadlines or consequences.
            "Dear {{ owner.name }},\n\n"
            "This is a friendly note that our records show a balance of {{ balance.total }} "
            "on the account for unit {{ unit.number }} as of {{ letter.date }}.\n\n"
            "How to pay:\n"
            "{% if association.pay_url %}- Online, on the Pay page of the owner portal: "
            "{{ association.pay_url }}\n{% endif %}"
            "- By check payable to {{ association.name }}"
            "{% if association.mailing_address %}, mailed to "
            "{{ association.mailing_address }}{% endif %}\n\n"
            "If you have already paid, thank you. If you have questions about your "
            "account, contact the board"
            "{% if association.email %} at {{ association.email }}{% endif %}"
            "{% if association.phone %} or {{ association.phone }}{% endif %}.\n\n"
            "Sincerely,\nThe Board of Directors\n{{ association.name }}"
        ),
    },
    {
        "key": "late-notice",
        "kind": "late_notice",
        "name": _("Late notice"),
        "subject": _("Late notice: unit {{ unit.number }}"),
        "body": _(
            "Dear {{ owner.name }},\n\n"
            "Your account for unit {{ unit.number }} is past due. As of {{ letter.date }} "
            "the balance is {{ balance.total }}. Late charges may apply under the "
            "association's governing documents and collection policy.\n\n"
            "Please pay the balance promptly or contact the board to discuss a payment "
            "plan.\n\n"
            "Sincerely,\nThe Board of Directors\n{{ association.name }}"
        ),
    },
    {
        "key": "demand",
        "kind": "demand",
        "name": _("Demand letter"),
        "subject": _("Demand for payment: unit {{ unit.number }}"),
        "body": _(
            "Dear {{ owner.name }},\n\n"
            "The association demands payment of {{ balance.total }}, the balance owed on "
            "the account for unit {{ unit.number }} as of {{ letter.date }}.\n\n"
            "[Board: insert the deadline, the next collection steps and any notices "
            "required by state law and your collection policy before sending.]\n\n"
            "Sincerely,\nThe Board of Directors\n{{ association.name }}"
        ),
    },
    {
        # The statutory block (homeoa.letters.statutory) is appended after this wording,
        # verbatim and locked; the board edits only the cover wording here.
        "key": "wa-delinquency-notice",
        "kind": "delinquency_notice",
        "name": _("Notice of delinquency (statutory)"),
        "subject": _("Notice of delinquency: unit {{ unit.number }}"),
        "body": _(
            "Dear {{ owner.name }},\n\n"
            "The assessments for unit {{ unit.number }} are past due. As of "
            "{{ letter.date }} the balance on the account is {{ balance.total }}.\n\n"
            "Please pay the balance or contact the board"
            "{% if association.email %} at {{ association.email }}{% endif %} to discuss "
            "a payment plan. The notice required by law follows.\n\n"
            "Sincerely,\nThe Board of Directors\n{{ association.name }}"
        ),
    },
]


#: Earlier default wording, by template key. ``ensure_default_templates`` moves a template
#: to the current default only while its current wording is still one of these (the board
#: has not edited it); an edited template is left alone.
PREVIOUS_BODIES = {
    "reminder": [
        "Dear {{ owner.name }},\n\n"
        "Our records show a balance of {{ balance.total }} on the account for unit "
        "{{ unit.number }} as of {{ letter.date }}. If you have already paid, thank "
        "you, and please disregard this reminder.\n\n"
        "Sincerely,\nThe Board of Directors\n{{ association.name }}",
    ],
}
