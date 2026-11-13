from django.utils.translation import gettext_lazy as _
from pydantic import BaseModel, Field

from homeoa.plugins.manifest import PluginManifest


class LobSettings(BaseModel):
    webhook_secret: str = Field(
        "",
        title=_("Webhook secret"),
        description=_("Shared secret Lob signs tracking webhooks with. Stored encrypted."),
    )


manifest = PluginManifest(
    id="lob",
    name=_("Lob postal mail (test fake)"),
    version="0.1.0",
    core_requires=">=0.1,<0.2",
    settings_model=LobSettings,
    secret_fields=("webhook_secret",),
    providers={"PostalProvider": "homeoa_lob.provider.LobPostal"},
    webhook="homeoa_lob.webhook.LobWebhook",
    description=_(
        "Sends letters through Lob, with certified mail and delivery tracking. "
        "Only the built-in fake is available: nothing is really mailed."
    ),
)
