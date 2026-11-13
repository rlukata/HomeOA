"""Provider interfaces (PLG-03) and their no-plugin defaults.

Interfaces (ABCs with typed request/result dataclasses; money is ``Decimal``)
-----------------------------------------------------------------------------
=====================  ==========================================  =====================
Interface name         Module / class                              Default (owner task)
=====================  ==========================================  =====================
``PaymentProvider``    ``providers.payments.PaymentProvider``      manual entry
``BankFeedProvider``   ``providers.bankfeed.BankFeedProvider``     OFX/CSV upload
``EmailChannel``       ``providers.email.EmailChannel``            SMTP via Django mail
``PostalProvider``     ``providers.postal.PostalProvider``         print PDF batch
``SignatureProvider``  ``providers.esign.SignatureProvider``       click-to-sign
``Importer``           ``providers.importer.Importer``             CSV mapper
``FileStorage``        ``providers.storage.FileStorage``           local disk
``OcrProvider``        ``providers.ocr.OcrProvider``               none (scans by title)
``AiProvider``         ``providers.ai.AiProvider``                 none (AI features hidden)
``RulePack``           ``rules.base.RulePack``                     generic pack
=====================  ==========================================  =====================

Resolving
---------
``get_provider(kind, association)`` (alias ``get``) is deterministic. ``kind`` is the
interface name or class. *Candidates* are loaded plugins that declare ``kind`` in
``PluginManifest.providers`` and are enabled for the association.

1. If the board chose a plugin for ``kind`` (``association.settings["providers"][kind]``,
   set with ``set_choice`` from ``/settings/plugins/``), use it while it is a candidate;
   if it is not (turned off), use the default. ``"default"`` forces the default.
2. With no choice and exactly one candidate, use it.
3. With no choice and several candidates, use the default; ``warnings(association)``
   reports it on ``/settings/plugins/`` until the board picks one.
4. ``RulePack`` is explicit only: exactly one active pack, the generic pack unless the
   board chose a plugin pack.
 A plugin's dotted path may name a class, which is called as
``cls(installation)`` with the association's ``PluginInstallation``, or a ready object,
which is returned as is. Defaults are classes called with no arguments.

Plugins declare implementations like::

    providers={"BankFeedProvider": "homeoa_simplefin.provider.SimpleFinFeed"}

File downloads
--------------
``LocalDiskStorage.signed_url`` links to ``/files/<token>/`` (no login; the signed,
expiring token is the capability).
"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, str] = {
    "PaymentProvider": "homeoa.providers.defaults.manual.ManualPayments",
    "BankFeedProvider": "homeoa.providers.defaults.fileupload.FileUploadFeed",
    "EmailChannel": "homeoa.providers.defaults.smtp.SmtpEmail",
    "PostalProvider": "homeoa.providers.defaults.postal.PrintBatchPostal",
    "SignatureProvider": "homeoa.providers.defaults.clicksign.ClickToSign",
    "Importer": "homeoa.providers.defaults.csvimport.CsvMapper",
    "FileStorage": "homeoa.providers.defaults.localdisk.LocalDiskStorage",
    "OcrProvider": "homeoa.providers.defaults.noocr.NoOcr",
    "AiProvider": "homeoa.providers.defaults.noai.NoAi",
    "RulePack": "homeoa.rules.packs.generic.pack",
}


def _name(kind: Any) -> str:
    name = kind if isinstance(kind, str) else getattr(kind, "__name__", "")
    if name not in DEFAULTS:
        raise KeyError(f"Unknown provider interface {kind!r}. Known: {', '.join(DEFAULTS)}.")
    return name


def get_default(kind: Any) -> Any:
    from django.utils.module_loading import import_string

    target = import_string(DEFAULTS[_name(kind)])
    return target() if isinstance(target, type) else target


SETTINGS_KEY = "providers"
DEFAULT_CHOICE = "default"
EXPLICIT_ONLY = frozenset({"RulePack"})


def offering(kind: Any) -> list[str]:
    """Ids of loaded plugins that implement ``kind`` (enabled or not), sorted."""
    from homeoa.plugins import registry

    return sorted(pid for pid, _path in registry.providers(_name(kind)))


def candidates(kind: Any, association) -> list[str]:
    """Ids of plugins implementing ``kind`` that are enabled for ``association``."""
    from homeoa.plugins import services

    if association is None:
        return []
    return [pid for pid in offering(kind) if services.is_enabled(association.pk, pid)]


def choice(kind: Any, association) -> str:
    """The board's choice for ``kind``: a plugin id, ``"default"`` or ``""`` (none)."""
    if association is None:
        return ""
    data = (association.settings or {}).get(SETTINGS_KEY) or {}
    return str(data.get(_name(kind), "")) if isinstance(data, dict) else ""


def plugin_for(kind: Any, association) -> str | None:
    """Id of the plugin that serves ``kind`` for ``association``, or None (default)."""
    name = _name(kind)
    cands = candidates(name, association)
    chosen = choice(name, association)
    if chosen:
        return chosen if chosen in cands else None
    if name in EXPLICIT_ONLY:
        return None
    return cands[0] if len(cands) == 1 else None


def warnings(association) -> list[dict[str, Any]]:
    """Kinds where the resolver fell back to the default because the board must act."""
    out = []
    for name in DEFAULTS:
        cands = candidates(name, association)
        chosen = choice(name, association)
        if chosen and chosen != DEFAULT_CHOICE and chosen not in cands:
            out.append({"kind": name, "reason": "chosen_off", "plugin": chosen})
        elif not chosen and name not in EXPLICIT_ONLY and len(cands) > 1:
            out.append({"kind": name, "reason": "ambiguous", "candidates": cands})
    return out


def set_choice(association, kind: Any, plugin_id: str) -> None:
    """Record the board's choice (``""`` clears it, ``"default"`` forces the default)."""
    from homeoa import audit

    name = _name(kind)
    if plugin_id not in ("", DEFAULT_CHOICE) and plugin_id not in offering(name):
        raise ValueError(f"Plugin {plugin_id!r} does not provide {name}.")
    settings = dict(association.settings or {})
    data = dict(settings.get(SETTINGS_KEY) or {})
    old = data.get(name, "")
    if plugin_id:
        data[name] = plugin_id
    else:
        data.pop(name, None)
    settings[SETTINGS_KEY] = data
    association.settings = settings
    association.save(update_fields=["settings"])
    audit.record("providers.choice", association, changes={name: [old, plugin_id]})


def get_provider(kind: Any, association=None) -> Any:
    from django.utils.module_loading import import_string

    from homeoa.plugins import registry, services

    name = _name(kind)
    plugin_id = plugin_for(name, association)
    if plugin_id is None:
        return get_default(name)
    target = import_string(registry.get(plugin_id).providers[name])
    if isinstance(target, type):
        return target(services.installation_for(association, plugin_id))
    return target


get = get_provider
