"""Plugin system (PLG-01, PLG-02, PLG-04 handlers, PLG-05, PLG-06).

Writing a plugin
----------------
A plugin is a separate installable package (usually under ``plugins/<dist-name>/``) that
declares an entry point in the ``homeoa.plugins`` group pointing at a ``PluginManifest``::

    [project.entry-points."homeoa.plugins"]
    simplefin = "homeoa_simplefin.manifest:manifest"

    # homeoa_simplefin/manifest.py  (no Django model imports at module level)
    from pydantic import BaseModel
    from homeoa.plugins.manifest import EventHandler, PluginManifest

    class Settings(BaseModel):
        access_url: str
        account_hint: str = ""

    manifest = PluginManifest(
        id="simplefin", name="SimpleFIN", version="0.1.0", core_requires=">=0.1,<0.2",
        settings_model=Settings, secret_fields=("access_url",),
        providers={"BankFeedProvider": "homeoa_simplefin.provider.SimpleFinFeed"},
        event_handlers=(EventHandler("payment.received", "homeoa_simplefin.handlers.x"),),
        webhook="homeoa_simplefin.hooks.Webhook",
        django_app="homeoa_simplefin",
    )

The entry-point name should equal the manifest ``id``.

Enabling
--------
``settings.HOMEOA_PLUGINS`` (env ``HOMEOA_PLUGINS``, comma-separated entry-point or
distribution names) lists the plugins to load. Others are never imported. At startup each
listed plugin's ``core_requires`` is checked against ``manifest.CORE_VERSION``; a mismatch
raises ``ImproperlyConfigured`` with a clear message. Loaded plugins are then enabled per
association at ``/settings/plugins/`` (``PluginInstallation`` rows; audited).

Settings and secrets
--------------------
The settings form is generated from ``settings_model``. Fields named in ``secret_fields``
are stored in ``PluginInstallation.secrets`` (``EncryptedJSONField``), rendered as
write-only password inputs and never shown again; leaving one blank keeps it.

Event handlers
--------------
``EventHandler(event, "dotted.path", mode)`` subscribes through ``homeoa.events`` and only
runs for associations where the plugin is enabled.

Inbound webhooks
----------------
``POST /hooks/<plugin_id>/<association_id>/`` (no login, CSRF exempt). The plugin's
``webhook`` object (see ``manifest.WebhookHandler``) must implement
``verify_signature(request, installation)`` (False -> 400), ``event_id(request)`` and
``handle(request, installation)``. Each event id is processed once per association
(``WebhookDelivery``); repeats get 200 without calling ``handle``. If ``handle`` raises,
the delivery is rolled back (500) so the sender's retry is processed.

Python API: ``homeoa.plugins.registry`` (``get``, ``all_plugins``, ``providers``) and
``homeoa.plugins.services`` (``is_enabled``, ``installation_for``, ``save_installation``).
"""
