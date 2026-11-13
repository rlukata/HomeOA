from django import template
from django.utils.translation import gettext_lazy as _

from homeoa import providers
from homeoa.plugins import registry

register = template.Library()

KIND_LABELS = {
    "PaymentProvider": _("Online payments"),
    "BankFeedProvider": _("Bank feed"),
    "EmailChannel": _("Email"),
    "PostalProvider": _("Postal mail"),
    "SignatureProvider": _("E-signatures"),
    "Importer": _("Data import"),
    "FileStorage": _("File storage"),
    "OcrProvider": _("Reading scans (OCR)"),
    "AiProvider": _("AI assistance"),
    "RulePack": _("Rule pack"),
}


@register.inclusion_tag("providers/choices.html", takes_context=True)
def provider_choices(context):
    """The "Use for" selects and resolver warnings shown on /settings/plugins/."""
    request = context.get("request")
    association = getattr(request, "association", None)
    rows = []
    for kind in providers.DEFAULTS:
        offering = providers.offering(kind)
        if not offering:
            continue
        enabled = set(providers.candidates(kind, association))
        rows.append(
            {
                "kind": kind,
                "label": KIND_LABELS.get(kind, kind),
                "explicit": kind in providers.EXPLICIT_ONLY,
                "choice": providers.choice(kind, association),
                "active": providers.plugin_for(kind, association) or "",
                "options": [
                    {"id": pid, "name": registry.get(pid).name, "enabled": pid in enabled}
                    for pid in offering
                ],
            }
        )
    warn = []
    for w in providers.warnings(association) if association else []:
        w = dict(w, label=KIND_LABELS.get(w["kind"], w["kind"]))
        warn.append(w)
    return {"rows": rows, "warnings": warn, "csrf_token": context.get("csrf_token")}
