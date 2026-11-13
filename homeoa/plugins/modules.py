"""Which optional modules an association has turned on.

An association with no switch row for a module has it off, so a new association starts
with every optional module off. Test settings may turn that default on with
``HOMEOA_MODULES_DEFAULT_ON``; a row always wins over the default.

Switches are read once per association object and kept on it, so asking about several
modules in one request costs one query. ``set_module`` refreshes that copy.
"""

from __future__ import annotations

from contextlib import nullcontext
from fnmatch import fnmatchcase

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa.audit.context import audit_context, get_audit_actor

from . import registry
from .models import ModuleSwitch

_CACHE_ATTR = "_homeoa_module_switches"


class UnknownModule(ValueError):
    """The module id is not one of the optional modules HomeOA ships."""


class ModuleChangeRefused(Exception):
    """A module switch may not change. ``reasons`` lists every reason in plain words and
    ``reason`` joins them.

    The details name what stands in the way: ``citation`` (the rule pack's reason the
    module is required), ``required_by`` (switched-on modules that need this one),
    ``open_items`` (the module's records still in flight) and ``missing`` (modules this
    one needs that are off)."""

    def __init__(self, reasons, *, open_items=(), citation=None, required_by=(), missing=()):
        self.reasons = list(reasons)
        self.reason = " ".join(self.reasons)
        super().__init__(self.reason)
        self.open_items = list(open_items)
        self.citation = citation
        self.required_by = list(required_by)
        self.missing = list(missing)


def _name(module_id: str) -> str:
    return str(registry.module(module_id).name)


def _refusal(association, module_id: str, on: bool) -> ModuleChangeRefused | None:
    """Every reason ``module_id`` may not be switched to ``on`` for ``association``, or
    None when nothing stands in the way."""
    from .slots import open_items

    switches = _switches(association)
    name = _name(module_id)
    if on:
        missing = [m for m in registry.module(module_id).requires if not _on(switches, m)]
        if not missing:
            return None
        reason = _("%(module)s needs %(missing)s turned on first.") % {
            "module": name,
            "missing": ", ".join(map(_name, missing)),
        }
        return ModuleChangeRefused([reason], missing=missing)
    reasons = []
    citation = required_by_pack(association, module_id)
    if citation:
        reasons.append(
            _("%(module)s is required by law (%(citation)s) and cannot be turned off.")
            % {"module": name, "citation": citation}
        )
    required_by = [
        m.id for m in registry.all_modules() if module_id in m.requires and _on(switches, m.id)
    ]
    if required_by:
        reasons.append(
            _("%(module)s cannot be turned off while %(others)s is on, which needs it.")
            % {"module": name, "others": ", ".join(map(_name, required_by))}
        )
    items = open_items(module_id, association)
    if items:
        reasons.append(
            _("%(module)s cannot be turned off while it has open items: %(items)s.")
            % {"module": name, "items": "; ".join(str(i.description) for i in items)}
        )
    if not reasons:
        return None
    return ModuleChangeRefused(
        reasons, open_items=items, citation=citation, required_by=required_by
    )


def _check(module_id: str) -> None:
    if module_id not in {m.id for m in registry.all_modules()}:
        raise UnknownModule(f"Unknown module {module_id!r}.")


def _switches(association) -> dict[str, bool]:
    if isinstance(association, int):
        # Events carry only the association's id; there is no object to keep a copy on.
        return dict(
            ModuleSwitch.all_associations.filter(association_id=association).values_list(
                "module_id", "enabled"
            )
        )
    cached = getattr(association, _CACHE_ATTR, None)
    if cached is None:
        cached = dict(
            ModuleSwitch.all_associations.filter(association=association).values_list(
                "module_id", "enabled"
            )
        )
        setattr(association, _CACHE_ATTR, cached)
    return cached


def is_on(association, module_id: str) -> bool:
    """Whether ``module_id`` is turned on for ``association`` (an object or its id)."""
    _check(module_id)
    return _on(_switches(association), module_id)


def _on(switches: dict[str, bool], module_id: str) -> bool:
    default = bool(getattr(settings, "HOMEOA_MODULES_DEFAULT_ON", False))
    return switches.get(module_id, default)


def switches_for(association):
    """``fn(module_id) -> bool`` answering for ``association`` (an object or its id) from
    one read of its switches, for callers asking about many modules at once."""
    switches = _switches(association)
    return lambda module_id: _on(switches, module_id)


def turned_on_at(association, module_id: str):
    """When ``module_id`` was last turned on for ``association``, or None when it is off or
    on only by default (no switch row)."""
    return (
        ModuleSwitch.all_associations.filter(
            association=association, module_id=module_id, enabled=True
        )
        .values_list("changed_at", flat=True)
        .first()
    )


def enabled_modules(association) -> set[str]:
    """The ids of every optional module turned on for ``association``."""
    return {m.id for m in registry.all_modules() if is_on(association, m.id)}


def set_module(association, module_id: str, on: bool, *, user, change_request=None):
    """Turn a module on or off and return its switch row.

    This is the one place that decides whether a switch may change; the audit log
    records who changed it, when, and from what to what. A change is refused with
    ``ModuleChangeRefused`` when the module still has open items, the rule pack requires
    it, another switched-on module needs it, or (turning on) a module it needs is off. A
    call that changes nothing is never refused.
    """
    _check(module_id)
    switch = ModuleSwitch.all_associations.filter(
        association=association, module_id=module_id
    ).first() or ModuleSwitch(association=association, module_id=module_id)
    if switch.pk is not None and switch.enabled == bool(on):
        # Nothing changes: keep when it was last turned on (scheduler catch-up reads it).
        return switch
    if switch.pk is not None or _on(_switches(association), module_id) != bool(on):
        refused = _refusal(association, module_id, bool(on))
        if refused is not None:
            raise refused
    switch.enabled = bool(on)
    switch.changed_by = user
    switch.changed_at = timezone.now()
    switch.change_request = change_request
    ctx = get_audit_actor()
    acting = (
        audit_context(actor=user, ip=ctx.ip, request_id=ctx.request_id, label=ctx.label)
        if user is not None
        else nullcontext()
    )
    with acting:
        switch.save()
    _switches(association)[module_id] = switch.enabled
    return switch


def required_by_pack(association, module_id: str) -> str | None:
    """The citation that makes ``module_id`` mandatory under the association's rule
    pack, or None when the pack does not require it."""
    from homeoa.rules import active_pack

    _check(module_id)
    return active_pack(association).required_modules.get(module_id)


def page_modules(view_name: str) -> list[str]:
    """The modules whose ``pages`` list the URL name ``view_name`` (``"namespace:name"``)."""
    return [m.id for m in registry.all_modules() if any(fnmatchcase(view_name, p) for p in m.pages)]


def page_on(association, view_name: str) -> bool:
    """Whether the page named ``view_name`` is shown: it belongs to no module, or to at
    least one module turned on for ``association``."""
    owners = page_modules(view_name)
    if not owners or association is None:
        return True
    on = switches_for(association)
    return any(on(m) for m in owners)


def set_refusal(association, wanted: dict[str, bool]) -> ModuleChangeRefused | None:
    """Every reason the switches in ``wanted`` ({module id: on}) cannot all be applied
    together, judged against the set they would leave, or None when nothing stands in
    the way. Modules missing from ``wanted`` keep their current switch."""
    from .slots import open_items

    for module_id in wanted:
        _check(module_id)
    switches = _switches(association)
    all_modules = registry.all_modules()
    now = {m.id: _on(switches, m.id) for m in all_modules}
    final = {**now, **{k: bool(v) for k, v in wanted.items()}}
    changes = {m for m in final if final[m] != now[m]}
    reasons, items, missing_all, required_by_all = [], [], [], []
    citation = None
    for m in all_modules:
        if m.id in changes and final[m.id]:
            missing = [r for r in m.requires if not final.get(r, False)]
            if missing:
                missing_all.extend(missing)
                reasons.append(
                    _("%(module)s needs %(missing)s turned on.")
                    % {"module": _name(m.id), "missing": ", ".join(map(_name, missing))}
                )
    for m in all_modules:
        if final[m.id] or (m.id not in changes and m.id not in wanted):
            continue
        name = _name(m.id)
        pack_citation = required_by_pack(association, m.id)
        if pack_citation:
            citation = citation or pack_citation
            reasons.append(
                _("%(module)s is required by law (%(citation)s) and cannot be turned off.")
                % {"module": name, "citation": pack_citation}
            )
        if m.id not in changes:
            continue
        staying = [
            o.id for o in all_modules if m.id in o.requires and final[o.id] and o.id not in changes
        ]
        if staying:
            required_by_all.extend(staying)
            reasons.append(
                _("%(module)s cannot be turned off while %(others)s is on, which needs it.")
                % {"module": name, "others": ", ".join(map(_name, staying))}
            )
        found = open_items(m.id, association)
        if found:
            items.extend(found)
            reasons.append(
                _("%(module)s cannot be turned off while it has open items: %(items)s.")
                % {"module": name, "items": "; ".join(str(i.description) for i in found)}
            )
    if not reasons:
        return None
    return ModuleChangeRefused(
        reasons,
        open_items=items,
        citation=citation,
        required_by=required_by_all,
        missing=missing_all,
    )


def set_modules(association, wanted: dict[str, bool], *, user, change_request=None) -> None:
    """Apply ``wanted`` ({module id: on}) all together, or nothing.

    Raises ``ModuleChangeRefused`` with every reason from :func:`set_refusal` before
    changing anything. Otherwise switches go through :func:`set_module` in a safe order:
    modules being turned on start with the ones they need, modules being turned off start
    with the ones that need them.
    """
    refused = set_refusal(association, wanted)
    if refused is not None:
        raise refused
    by_id = {m.id: m for m in registry.all_modules()}

    def needs_first(ids):
        ordered, seen = [], set()

        def visit(i):
            if i in seen:
                return
            seen.add(i)
            for r in by_id[i].requires:
                if r in ids:
                    visit(r)
            ordered.append(i)

        for i in sorted(ids):
            visit(i)
        return ordered

    turn_on = needs_first({k for k, v in wanted.items() if v})
    turn_off = list(reversed(needs_first({k for k, v in wanted.items() if not v})))
    try:
        with transaction.atomic():
            for module_id in turn_on:
                set_module(association, module_id, True, user=user, change_request=change_request)
            for module_id in turn_off:
                set_module(association, module_id, False, user=user, change_request=change_request)
    except Exception:
        # The rows rolled back; forget the copy kept on the association object.
        if not isinstance(association, int):
            setattr(association, _CACHE_ATTR, None)
        raise
