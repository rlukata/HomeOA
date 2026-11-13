"""Channel interface (extension point). Plugins/apps register more channels (email, postal).

A channel's ``send(delivery)`` hands the message to its provider and returns a provider id
(or ``""``), or raises ``ChannelError``. The engine records status and timestamps.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

INAPP = "inapp"
PREVIEW = "preview"
EMAIL = "email"
POSTAL = "postal"
EXTERNAL = (EMAIL, POSTAL)


class ChannelError(Exception):
    pass


class Channel:
    key: str = ""
    label = ""
    #: True when ``send`` completes delivery itself (in-app); else the status stays "sent".
    delivers_immediately = False
    #: True when ``send`` only queues a job that later moves the delivery from "queued"
    #: to "sent" or "failed" itself (email); the engine then leaves the status alone.
    deferred = False

    def send(self, delivery) -> str:
        raise NotImplementedError


class InAppChannel(Channel):
    """The stored notification is the in-app message; nothing to hand off."""

    key = INAPP
    label = _("In app")
    delivers_immediately = True

    def send(self, delivery) -> str:
        return ""


class PreviewChannel(Channel):
    """Renders and records only; never sends. Used for previews and gated recipients."""

    key = PREVIEW
    label = _("Preview")

    def send(self, delivery) -> str:
        raise ChannelError("the preview channel never sends")


_channels: dict[str, Channel] = {}


def register(channel: Channel) -> None:
    _channels[channel.key] = channel


def unregister(key: str) -> None:
    _channels.pop(key, None)


def get(key: str) -> Channel | None:
    return _channels.get(key)


def register_defaults() -> None:
    from .email import EmailChannel

    register(InAppChannel())
    register(PreviewChannel())
    register(EmailChannel())
