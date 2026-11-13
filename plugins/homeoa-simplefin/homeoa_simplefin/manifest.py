from django.utils.translation import gettext_lazy as _
from pydantic import BaseModel, Field

from homeoa.plugins.manifest import PluginManifest


class SimpleFinSettings(BaseModel):
    access_url: str = Field(
        "",
        title=_("Access URL"),
        description=_("Set by Connect on the bank feed page. Stored encrypted."),
    )
    use_fake: bool = Field(
        False,
        title=_("Use the built-in fake bank (demo/testing)"),
        description=_("No network calls; returns sample transactions."),
    )


manifest = PluginManifest(
    id="simplefin",
    name=_("SimpleFIN bank feed"),
    version="0.1.0",
    core_requires=">=0.1,<0.2",
    settings_model=SimpleFinSettings,
    secret_fields=("access_url",),
    providers={"BankFeedProvider": "homeoa_simplefin.provider.SimpleFinFeed"},
    django_app="homeoa_simplefin",
    urls="homeoa_simplefin.urls",
    todos="homeoa_simplefin.todos.todo_items",
    description=_("Pulls bank transactions and balances automatically through SimpleFIN Bridge."),
)
