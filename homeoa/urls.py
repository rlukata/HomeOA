from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from homeoa.access.invitations import urls as invitation_urls
from homeoa.core.views import healthz, new_association
from homeoa.plugins.registry import discover
from homeoa.portal.preview import urls as preview_urls

# Each app mounts at its own URL prefix. Apps whose routes span several
# prefixes mount at "" and spell out full paths. events has no URLs.
# Order matters: specific prefixes first, the ui hub catch-all last.
urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("settings/association/new/", new_association, name="new_association"),
    path("", include("homeoa.accounts.urls")),  # /login/ /logout/ /account/
    path("", include("homeoa.imports.urls")),  # /units/import/ /money/reports/parallel-run/
    path("", include("homeoa.plugins.urls")),  # /settings/plugins/ /hooks/
    path("money/assessments/", include("homeoa.billing.urls")),
    path("money/", include("homeoa.payments.urls")),  # /money/payments/ /money/adjustments/
    path("money/bills/", include("homeoa.ap.urls")),
    path("money/bank/", include("homeoa.banking.urls")),
    path("money/budget/", include("homeoa.budget.urls")),
    path("money/reports/", include("homeoa.reports.urls")),
    path("money/close/", include("homeoa.closing.urls")),
    path("", include("homeoa.notify.urls")),  # /units/<number>/comms/
    path("documents/insurance/", include("homeoa.insurance.urls")),
    path("", include("homeoa.documents.urls")),  # /units/<number>/file/
    path(
        "", include("homeoa.statements.urls")
    ),  # /money/statements/ /money/delinquency/ /settings/delinquency/
    path("units/<str:number>/preview/", include(preview_urls)),  # board preview
    path("units/<str:number>/invitations/", include(invitation_urls)),
    path("governance/compliance/", include("homeoa.compliance.urls")),
    path("governance/board/", include("homeoa.governance.urls")),
    path("", include("homeoa.governance.contact_urls")),  # /community/messages/ /me/contacts/
    path("governance/changes/", include("homeoa.changes.urls")),
    path("", include("homeoa.registry.directory_urls")),  # /community/directory/ /me/directory/
    path("", include("homeoa.registry.portal_urls")),  # /me/records/
    path("requests/violations/", include("homeoa.enforcement.urls")),
    path("governance/votes/", include("homeoa.votes.urls")),
    path("governance/meetings/", include("homeoa.meetings.urls")),
    path("units/resale/", include("homeoa.resale.urls")),
    path("units/", include("homeoa.registry.urls")),
    path("vendors/", include("homeoa.vendors.urls")),
    path("search/", include("homeoa.search.urls")),
    path("money/bills/assist/", include("homeoa.assist.urls")),
    path("community/", include("homeoa.community.urls")),
    path("", include("homeoa.arc.urls")),  # /requests/architectural/ /me/arc/
    path("", include("homeoa.maintenance.urls")),  # /requests/maintenance/ /me/maintenance/
    path("me/votes/", include("homeoa.votes.portal_urls")),
    path("me/violations/", include("homeoa.enforcement.portal_urls")),
    path("me/resale/", include("homeoa.resale.portal_urls")),
    path("me/", include("homeoa.portal.urls")),
    path("settings/roles/", include("homeoa.access.urls")),
    path("settings/audit/", include("homeoa.audit.urls")),
    path("settings/jobs/", include("homeoa.jobs.urls")),
    path("settings/rules/", include("homeoa.rules.urls")),
    path("settings/setup/", include("homeoa.setup.urls")),
    path("settings/letters/", include("homeoa.letters.urls")),
    path("", include("homeoa.esign.urls")),  # /governance/signatures/ /sign/
    path("files/", include("homeoa.providers.urls")),  # signed file downloads
    path("settings/providers/", include("homeoa.providers.settings_urls")),
    path("", include("homeoa.dashboard.urls")),  # / and the /money/ overview
]

# Plugin pages (PluginManifest.urls), for plugins listed in HOMEOA_PLUGINS.
for _manifest in discover(settings.HOMEOA_PLUGINS).values():
    if _manifest.urls:
        urlpatterns.insert(-1, path(f"plugins/{_manifest.id}/", include(_manifest.urls)))

if settings.HOMEOA_ADMIN_ENABLED:
    urlpatterns.append(path("admin/", admin.site.urls))

# Placeholders for hubs nobody has built yet. Must stay last.
urlpatterns.append(path("", include("homeoa.ui.urls")))
