"""Permission codes and the default role matrix (feature-matrix "Role x capability").

Codes are plain strings grouped by capability. A code ending in ``.own`` is scoped: it
applies only to objects tied to one of the user's units (``services.units_for``).

Codes ending in ``.propose`` let a person ask for a change that only takes effect once the
board approves it (``homeoa.changes``). Officer titles add codes on top of the Board role
(``TITLE_PERMISSIONS``).
"""

from django.utils.translation import gettext_lazy as _

# Board shell (the 8 board hubs) and money.
BOARD_SHELL = "board.shell"
BALANCE_OWN = "balance.own"
FINANCE_SUMMARY = "finance.summary"
FINANCE_READ = "finance.read"
FINANCE_MANAGE = "finance.manage"
FINANCE_PROPOSE = "finance.propose"
BILLS_ENTER = "bills.enter"
BILLS_APPROVE = "bills.approve"
CPA_PACKAGE = "reports.cpa_package"
# Community.
ANNOUNCEMENTS_COMMITTEE = "announcements.committee"
ANNOUNCEMENTS_MANAGE = "announcements.manage"
CALENDAR_READ = "calendar.read"
CALENDAR_MANAGE = "calendar.manage"
DIRECTORY_READ = "directory.read"
LETTERS_MANAGE = "letters.manage"  # letter templates, owner letters, print batches
# Documents.
DOCUMENTS_TENANT = "documents.read_tenant"
DOCUMENTS_READ = "documents.read"
DOCUMENTS_BOARD = "documents.board"  # the documents hub, including board-only documents
DOCUMENTS_MANAGE = "documents.manage"  # upload, version, change visibility
# Requests.
VIOLATIONS_OWN = "violations.own"
VIOLATIONS_SUBMIT = "violations.submit"
VIOLATIONS_MANAGE = "violations.manage"
ARCH_SUBMIT = "architectural.submit"
ARCH_REVIEW = "architectural.review"
ARCH_DECIDE = "architectural.decide"
MAINTENANCE_SUBMIT = "maintenance.submit"
MAINTENANCE_MANAGE = "maintenance.manage"
# Governance.
VOTES_VOTE = "votes.vote"
VOTES_ADMINISTER = "votes.administer"
GOVERNANCE_READ = "governance.read"
GOVERNANCE_MANAGE = "governance.manage"
CONTACTS_READ = "governance.contacts"  # who is on the board and committees, and the message form
# Units and people (registry).
UNITS_OWN = "units.own"
UNITS_READ = "units.read"
UNITS_MANAGE = "units.manage"
VENDORS_READ = "vendors.read"
VENDORS_MANAGE = "vendors.manage"
VENDORS_PROPOSE = "vendors.propose"
# Settings.
SETTINGS_MANAGE = "settings.manage"
POLICY_PROPOSE = "policy.propose"  # approval rules, late fees, collections, change limits
PLUGINS_MANAGE = "plugins.manage"
ROLES_MANAGE = "roles.manage"
AUDIT_READ = "audit.read"
JOBS_READ = "jobs.read"
INSTANCE_MANAGE = "instance.manage"

LABELS = {
    BOARD_SHELL: _("Use the board workspace"),
    BALANCE_OWN: _("View and pay own balance"),
    FINANCE_SUMMARY: _("Read financial summary"),
    FINANCE_READ: _("Read association financial reports"),
    FINANCE_MANAGE: _("Manage finances"),
    FINANCE_PROPOSE: _("Propose money changes for board approval"),
    BILLS_ENTER: _("Enter bills"),
    BILLS_APPROVE: _("Approve bills"),
    CPA_PACKAGE: _("Download the year-end CPA package"),
    ANNOUNCEMENTS_COMMITTEE: _("Post committee announcements"),
    ANNOUNCEMENTS_MANAGE: _("Manage announcements"),
    CALENDAR_READ: _("Read calendar and announcements"),
    CALENDAR_MANAGE: _("Manage calendar"),
    DIRECTORY_READ: _("Read resident directory"),
    LETTERS_MANAGE: _("Write and mail letters to owners"),
    DOCUMENTS_TENANT: _("Read tenant-visible documents"),
    DOCUMENTS_READ: _("Read owner documents"),
    DOCUMENTS_BOARD: _("Read all documents, including board-only ones"),
    DOCUMENTS_MANAGE: _("Manage documents"),
    VIOLATIONS_OWN: _("See own violations"),
    VIOLATIONS_SUBMIT: _("Submit violations"),
    VIOLATIONS_MANAGE: _("Manage violations"),
    ARCH_SUBMIT: _("Submit architectural requests"),
    ARCH_REVIEW: _("Review architectural requests"),
    ARCH_DECIDE: _("Decide architectural requests"),
    MAINTENANCE_SUBMIT: _("Submit maintenance requests"),
    MAINTENANCE_MANAGE: _("Manage maintenance and work orders"),
    VOTES_VOTE: _("Vote"),
    VOTES_ADMINISTER: _("Administer owner votes"),
    GOVERNANCE_READ: _("Read board roster, resolutions and action items"),
    GOVERNANCE_MANAGE: _("Manage board roster, resolutions and action items"),
    CONTACTS_READ: _("See the board and committees and message them"),
    UNITS_OWN: _("See own unit"),
    UNITS_READ: _("Read units and people"),
    UNITS_MANAGE: _("Manage units and people"),
    VENDORS_READ: _("Read vendors"),
    VENDORS_MANAGE: _("Edit vendor contact details, contracts and insurance"),
    VENDORS_PROPOSE: _("Propose new vendors and payment-detail changes"),
    SETTINGS_MANAGE: _("Manage association settings"),
    POLICY_PROPOSE: _("Propose approval, late-fee and collection policy changes"),
    PLUGINS_MANAGE: _("Manage plugins"),
    ROLES_MANAGE: _("Propose role changes"),
    AUDIT_READ: _("Read audit log"),
    JOBS_READ: _("Read job history"),
    INSTANCE_MANAGE: _("Manage the instance"),
}
ALL = tuple(LABELS)

# Role slugs.
OWNER = "owner"
TENANT = "tenant"
COMMITTEE = "committee"
BOARD = "board"
CPA = "cpa"
SYS_ADMIN = "sys_admin"
MAINTENANCE_COMMITTEE = "maintenance_committee"

DERIVED_ROLES = (OWNER, TENANT)  # from registry data, never assigned by hand
READ_ONLY_ROLES = frozenset({CPA})  # every write is refused

# What each officer title adds to the Board role. The Secretary acts for the President, so
# the Secretary also gets anything only the President has (nothing today).
_PRESIDENT: frozenset[str] = frozenset()
TITLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "president": _PRESIDENT,
    "vice_president": frozenset(),
    "treasurer": frozenset({FINANCE_MANAGE, CPA_PACKAGE}),
    "secretary": frozenset({GOVERNANCE_MANAGE, DOCUMENTS_MANAGE}) | _PRESIDENT,
}

DEFAULT_ROLES: dict[str, dict] = {
    OWNER: {
        "name": _("Owner"),
        "permissions": frozenset(
            {
                BALANCE_OWN,
                FINANCE_SUMMARY,
                DOCUMENTS_TENANT,
                DOCUMENTS_READ,
                VIOLATIONS_OWN,
                ARCH_SUBMIT,
                VOTES_VOTE,
                MAINTENANCE_SUBMIT,
                CALENDAR_READ,
                DIRECTORY_READ,
                CONTACTS_READ,
                UNITS_OWN,
            }
        ),
    },
    TENANT: {
        "name": _("Tenant"),
        "permissions": frozenset(
            {
                DOCUMENTS_TENANT,
                VIOLATIONS_OWN,
                MAINTENANCE_SUBMIT,
                CALENDAR_READ,
                CONTACTS_READ,
                UNITS_OWN,
            }
        ),
    },
    COMMITTEE: {
        "name": _("Committee"),
        "permissions": frozenset(
            {
                BALANCE_OWN,
                ANNOUNCEMENTS_COMMITTEE,
                DOCUMENTS_TENANT,
                DOCUMENTS_READ,
                VIOLATIONS_SUBMIT,
                ARCH_REVIEW,
                VOTES_VOTE,
                CALENDAR_READ,
                DIRECTORY_READ,
                CONTACTS_READ,
            }
        ),
    },
    MAINTENANCE_COMMITTEE: {
        "name": _("Maintenance committee"),
        "member_limit": 3,
        # Runs maintenance from the board workspace: requests, work orders, one-off jobs
        # up to the association's limit, bills. New vendors go to the board for approval.
        "permissions": frozenset(
            {
                BOARD_SHELL,
                BALANCE_OWN,
                MAINTENANCE_MANAGE,
                VENDORS_READ,
                VENDORS_PROPOSE,
                BILLS_ENTER,
                DOCUMENTS_TENANT,
                DOCUMENTS_READ,
                CALENDAR_READ,
                DIRECTORY_READ,
                CONTACTS_READ,
            }
        ),
    },
    BOARD: {
        "name": _("Board"),
        # Officer titles add to this (TITLE_PERMISSIONS). Changes to money, vendors' payment
        # details, roles, ownership and policies go through change requests.
        "permissions": frozenset(
            {
                BOARD_SHELL,
                BALANCE_OWN,
                FINANCE_SUMMARY,
                FINANCE_READ,
                FINANCE_PROPOSE,
                BILLS_ENTER,
                BILLS_APPROVE,
                ANNOUNCEMENTS_MANAGE,
                LETTERS_MANAGE,
                CALENDAR_READ,
                CALENDAR_MANAGE,
                DIRECTORY_READ,
                DOCUMENTS_TENANT,
                DOCUMENTS_READ,
                DOCUMENTS_BOARD,
                VIOLATIONS_MANAGE,
                ARCH_DECIDE,
                MAINTENANCE_MANAGE,
                VOTES_ADMINISTER,
                GOVERNANCE_READ,
                CONTACTS_READ,
                UNITS_READ,
                UNITS_MANAGE,
                VENDORS_READ,
                VENDORS_MANAGE,
                VENDORS_PROPOSE,
                ROLES_MANAGE,
                POLICY_PROPOSE,
                AUDIT_READ,
                JOBS_READ,
            }
        ),
    },
    CPA: {
        "name": _("CPA (read-only)"),
        # Read-only: reports, the year-end package and vendors. The W-9 TIN
        # stays masked (reveal needs finance.manage); no owner contact data.
        "permissions": frozenset({FINANCE_SUMMARY, FINANCE_READ, CPA_PACKAGE, VENDORS_READ}),
    },
    SYS_ADMIN: {
        "name": _("System admin"),
        # Runs the server and the association's setup. No money or owner data by itself;
        # a person's roles add up. Superusers hold this role in every association.
        "permissions": frozenset(
            {
                INSTANCE_MANAGE,
                SETTINGS_MANAGE,
                PLUGINS_MANAGE,
                ROLES_MANAGE,
                POLICY_PROPOSE,
                CONTACTS_READ,
                AUDIT_READ,
                JOBS_READ,
            }
        ),
    },
}

# Hub id -> codes; any one of them shows the hub (ui.nav hook).
HUB_PERMISSIONS = {
    "home": (BOARD_SHELL, FINANCE_READ),
    "money": (FINANCE_READ,),
    "requests": (MAINTENANCE_MANAGE,),
    "community": (ANNOUNCEMENTS_MANAGE,),
    "governance": (VOTES_ADMINISTER, GOVERNANCE_READ),
    "units": (UNITS_READ,),
    "vendors": (VENDORS_READ, VENDORS_MANAGE),
    "documents": (DOCUMENTS_BOARD,),
}
