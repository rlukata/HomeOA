"""Notification engine (PLT-05): event -> template -> channel, respecting preferences.

Apps register notification types (``homeoa.notify.types.register``) and call
``homeoa.notify.services.notify``. Channels are pluggable (``homeoa.notify.channels``);
the no-plugin default ships ``inapp`` and ``preview``.
"""
