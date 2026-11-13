from typing import Literal

from django.utils.translation import gettext_lazy as _
from pydantic import BaseModel, Field

from homeoa.plugins.manifest import PluginManifest


class AnthropicSettings(BaseModel):
    api_key: str = Field(
        "",
        title=_("API key"),
        description=_(
            "An Anthropic API key (sk-ant-...), ideally with a monthly spending limit set in "
            "the Anthropic console. Stored encrypted. Unused with the fake."
        ),
    )
    model: Literal["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"] = Field(
        "claude-opus-5-5",
        title=_("Model"),
        description=_(
            "Opus reads scans and photos best (about 2 cents an invoice); Sonnet and Haiku "
            "cost less."
        ),
    )


manifest = PluginManifest(
    id="anthropic",
    name=_("Claude (Anthropic)"),
    version="0.1.0",
    core_requires=">=0.1,<0.2",
    settings_model=AnthropicSettings,
    secret_fields=("api_key",),
    providers={"AiProvider": "homeoa_anthropic.provider.ClaudeAi"},
    description=_(
        "AI assistance with Claude: fills in forms from documents, such as a new bill from "
        "an invoice, for a person to check. Each use sends that document to Anthropic. The "
        "board also turns on AI assistance under Features."
    ),
)
