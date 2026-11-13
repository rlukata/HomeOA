"""Import run helpers used by views, tests and the parallel-run diff."""

from __future__ import annotations

import copy
import hashlib

from django.db import transaction
from django.utils.translation import gettext as _

from .models import AccountMapping, ImportFile, ImportRun, MappingProfile, Source
from .parsing import KINDS, guess_columns, read_csv
from .profiles import default_config


class UploadError(ValueError):
    pass


def latest_profile(association, source) -> MappingProfile | None:
    return (
        MappingProfile.all_associations.filter(association=association, source=source)
        .order_by("-updated_at", "-pk")
        .first()
    )


def config_for_files(base: dict, files: list[ImportFile], source: str) -> dict:
    """Fit a saved or default config to the uploaded files: which file is which kind, and
    column guesses for kinds the config has no columns for."""
    config = copy.deepcopy(base)
    config.setdefault("files", {})
    config.setdefault("columns", {})
    used = {}
    for f in files:
        headers, _rows = read_csv(f.content)
        kind = config["files"].get(f.name, "")
        if not kind:
            # Match by the saved columns of a kind, then by header guessing.
            for k, cols in config["columns"].items():
                want = {c for c in cols.values() if c}
                if want and want <= set(headers) and k not in used.values():
                    kind = k
                    break
        if not kind and source == Source.CSV:
            best = max(
                KINDS.values(),
                key=lambda k: len(guess_columns(k, headers)) / max(len(k.fields), 1),
            )
            if guess_columns(best, headers) and best.name not in used.values():
                kind = best.name
        used[f.name] = kind
        if kind and not config["columns"].get(kind):
            config["columns"][kind] = guess_columns(KINDS[kind], headers)
    config["files"] = used
    return config


def account_choices_from_saved(association, source, config: dict) -> dict:
    """Overlay saved account mappings (they win over profile defaults)."""
    accounts = dict(config.get("accounts", {}))
    for m in AccountMapping.all_associations.filter(association=association, source=source):
        accounts[m.external_code] = {"account": m.account.code, "fund": m.fund.code}
    return accounts


@transaction.atomic
def create_run(association, *, source, mode, as_of, uploads, user=None) -> ImportRun:
    """``uploads``: iterable of (name, bytes)."""
    profile = latest_profile(association, source)
    run = ImportRun.all_associations.create(
        association=association,
        source=source,
        mode=mode,
        as_of=as_of,
        profile=profile,
        created_by=user if getattr(user, "pk", None) else None,
    )
    names = set()
    files = []
    for name, data in uploads:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise UploadError(_("%(f)s is not a UTF-8 CSV file.") % {"f": name}) from None
        if name in names:
            raise UploadError(_("%(f)s was uploaded twice.") % {"f": name})
        names.add(name)
        files.append(
            ImportFile.all_associations.create(
                association=association,
                run=run,
                name=name,
                content=text,
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(text),
            )
        )
    if not files:
        raise UploadError(_("Choose at least one CSV file."))
    base = profile.config if profile else default_config(source)
    run.config = config_for_files(base, files, source)
    run.config["accounts"] = account_choices_from_saved(association, source, run.config)
    run.save(update_fields=["config"])
    return run


def save_profile(run: ImportRun, name: str) -> MappingProfile:
    """Save the run's mapping as a named profile, reused by the next upload."""
    config = copy.deepcopy(run.config)
    profile, _created = MappingProfile.all_associations.update_or_create(
        association=run.association,
        source=run.source,
        name=name,
        defaults={"config": config},
    )
    run.profile = profile
    run.save(update_fields=["profile"])
    return profile


def comparison_snapshot(association, as_of=None) -> ImportRun | None:
    """Latest stored comparison snapshot (for the parallel-run diff)."""
    qs = ImportRun.all_associations.filter(association=association, status=ImportRun.Status.STORED)
    if as_of is not None:
        qs = qs.filter(as_of=as_of)
    return qs.order_by("-as_of", "-pk").first()
