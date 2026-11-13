"""Letter services: templates with versions, preview, generation to stored PDFs."""

from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.db.models import Max
from django.utils.formats import date_format
from django.utils.text import slugify
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.providers import get_provider

from . import merge, pdf, render, seeds
from .letterhead import letterhead
from .models import Letter, LetterTemplate, LetterTemplateVersion, LetterTranslation

DRAFT_NOTE = "Draft wording, not legal advice"


def _check(subject: str, body: str) -> None:
    result = render.check(subject, body)
    if not result.ok:
        raise render.TemplateRejected(result)


@transaction.atomic
def save_version(template: LetterTemplate, *, subject: str, body: str, user=None, note=""):
    """Validate and save new wording as the next version; it becomes current."""
    _check(subject, body)
    LetterTemplate.all_associations.select_for_update().get(pk=template.pk)
    last = template.versions.aggregate(n=Max("number"))["n"] or 0
    version = LetterTemplateVersion.objects.create(
        association=template.association,
        template=template,
        number=last + 1,
        subject=subject,
        body=body,
        note=note,
        created_by=user if user is not None and user.is_authenticated else None,
    )
    template.current_version = version
    template.save(update_fields=["current_version"])
    audit.record(
        "letter_template.version",
        template,
        changes={"version": version.number},
        message=note,
    )
    return version


@transaction.atomic
def create_template(
    association, *, name, subject, body, kind="general", key="", user=None, draft_wording=False
) -> LetterTemplate:
    _check(subject, body)
    key = key or slugify(name)[:50] or "letter"
    base, n = key, 2
    while LetterTemplate.all_associations.filter(association=association, key=key).exists():
        key = f"{base}-{n}"
        n += 1
    template = LetterTemplate.objects.create(
        association=association, key=key, name=name, kind=kind, draft_wording=draft_wording
    )
    save_version(template, subject=subject, body=body, user=user, note=_("Created"))
    return template


def ensure_default_templates(association) -> int:
    """Create any missing seed template. Returns how many were created.

    A seed template whose wording the board never edited (still a previous default) gets
    the current default as a new version.
    """
    created = 0
    for spec in seeds.DEFAULTS:
        existing = (
            LetterTemplate.all_associations.filter(association=association, key=spec["key"])
            .select_related("current_version")
            .first()
        )
        if existing is not None:
            _upgrade_default(existing, spec)
            continue
        create_template(
            association,
            key=spec["key"],
            kind=spec["kind"],
            name=str(spec["name"]),
            subject=str(spec["subject"]),
            body=str(spec["body"]),
            draft_wording=True,
        )
        created += 1
    return created


def _upgrade_default(template: LetterTemplate, spec) -> None:
    current = template.current_version
    previous = seeds.PREVIOUS_BODIES.get(spec["key"], [])
    if current is None or current.body not in previous or current.body == str(spec["body"]):
        return
    save_version(
        template,
        subject=current.subject,
        body=str(spec["body"]),
        note=_("Updated default wording"),
    )


def context_for(association, unit=None, as_of: dt.date | None = None, **extra):
    from homeoa.core.context import association_today

    return merge.LetterContext(
        association=association,
        unit=unit,
        as_of=as_of or association_today(association),
        extra=extra,
    )


def preview(subject: str, body: str, ctx: merge.LetterContext | None) -> tuple[str, str]:
    """Rendered (subject, body). With no context, sample values are used."""
    if ctx is None:
        result = render.check(subject, body)
        if not result.ok:
            raise render.TemplateRejected(result)
        values = merge.sample_values()
        return render.render(subject, values).strip(), render.render(body, values)
    return render.render_letter(subject, body, ctx)


def recipient(ctx: merge.LetterContext) -> tuple[str, list[str]]:
    name = merge.owner_names(ctx)
    lines = merge.mailing_address_lines(ctx)
    return name, ([name] if name else []) + lines


def render_pdf(
    version: LetterTemplateVersion,
    ctx: merge.LetterContext,
    *,
    tables=None,
    address_lines=None,
    locked_text: str = "",
) -> tuple[bytes, dict]:
    """``locked_text`` (a statutory block) is printed verbatim after the body, outside
    the template, so the board's wording can never change it. ``address_lines`` mails
    to an address other than the primary owner's (the owner's name is kept)."""
    subject, body = render.render_letter(version.subject, version.body, ctx)
    if locked_text:
        body = f"{body.rstrip()}\n\n{locked_text}"
    name, address = recipient(ctx)
    if address_lines is not None:
        address = ([name] if name else []) + list(address_lines)
    data = pdf.build_pdf(
        letterhead=letterhead(ctx.association),
        address_lines=address,
        date_text=date_format(ctx.as_of, "F j, Y"),
        subject=subject,
        body=body,
        footer_note=DRAFT_NOTE if version.template.draft_wording else "",
        title=f"{version.template.name} · {ctx.unit.number if ctx.unit else ''}",
        tables=tables,
    )
    return data, {"subject": subject, "recipient_name": name, "address_lines": address}


@transaction.atomic
def generate_letter(
    template: LetterTemplate,
    unit,
    *,
    as_of=None,
    user=None,
    tables=None,
    address_lines=None,
    locked_text: str = "",
    appendix_pdfs: list[bytes] | None = None,
    **extra,
) -> Letter:
    """Render the current version for ``unit``, store the PDF and record the letter.

    ``tables`` (see ``pdf.build_pdf``) are printed after the body, e.g. a statement."""
    version = template.current_version
    association = template.association
    ctx = context_for(association, unit, as_of, **extra)
    data, meta = render_pdf(
        version, ctx, tables=tables, address_lines=address_lines, locked_text=locked_text
    )
    if appendix_pdfs:
        data = append_pdfs(data, appendix_pdfs)
    storage = get_provider("FileStorage", association)
    stored = storage.put(
        association.pk,
        f"{template.key}-{unit.number}-{ctx.as_of.isoformat()}.pdf",
        data,
        content_type="application/pdf",
        folder="letters",
    )
    letter = Letter.objects.create(
        association=association,
        template_version=version,
        unit=unit,
        recipient_name=meta["recipient_name"][:300],
        address_lines=meta["address_lines"],
        subject=meta["subject"][:200],
        as_of=ctx.as_of,
        file_key=stored.key,
        sha256=stored.sha256,
        size=stored.size,
        generated_by=user if user is not None and user.is_authenticated else None,
    )
    audit.record(
        "letter.generate",
        letter,
        changes={
            "template": template.key,
            "version": version.number,
            "unit": unit.number,
            "as_of": ctx.as_of.isoformat(),
            "sha256": stored.sha256,
        },
        actor=user,
    )
    return letter


def letter_bytes(letter: Letter) -> bytes:
    return get_provider("FileStorage", letter.association).get(letter.file_key)


def append_pdfs(data: bytes, extra: list[bytes]) -> bytes:
    """``data`` followed by every page of each PDF in ``extra``."""
    import io

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for blob in [data, *extra]:
        writer.append(PdfReader(io.BytesIO(blob)))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# --- translations ----------------------------------------------------------------


class TranslationError(Exception):
    pass


def current_translation(template: LetterTemplate, language: str) -> LetterTranslation | None:
    """The newest approved translation of ``template`` into ``language``."""
    return (
        LetterTranslation.all_associations.filter(template=template, language=language)
        .order_by("-uploaded_at", "-pk")
        .first()
    )


@transaction.atomic
def upload_translation(
    template: LetterTemplate,
    language: str,
    *,
    text: str = "",
    pdf_bytes: bytes | None = None,
    filename: str = "",
    note: str = "",
    user=None,
) -> LetterTranslation:
    """Record a board-approved translation (text or PDF). Audited."""
    import io

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    text = (text or "").strip()
    if not text and not pdf_bytes:
        raise TranslationError(_("Upload a PDF or paste the translated text."))
    association = template.association
    translation = LetterTranslation(
        association=association,
        template=template,
        language=language,
        text=text,
        note=(note or "")[:200],
        uploaded_by=user if user is not None and user.is_authenticated else None,
    )
    if pdf_bytes:
        try:
            PdfReader(io.BytesIO(pdf_bytes)).pages[0]
        except (PdfReadError, IndexError, ValueError) as exc:
            raise TranslationError(_("That file is not a readable PDF.")) from exc
        stored = get_provider("FileStorage", association).put(
            association.pk,
            f"{template.key}-{language}.pdf",
            pdf_bytes,
            content_type="application/pdf",
            folder="letters/translations",
        )
        translation.filename = (filename or f"{template.key}-{language}.pdf")[:200]
        translation.file_key = stored.key
        translation.sha256 = stored.sha256
        translation.size = stored.size
    translation.save()
    audit.record(
        "letter.translation_upload",
        translation,
        changes={
            "template": template.key,
            "language": language,
            "kind": "pdf" if pdf_bytes else "text",
            "sha256": translation.sha256,
        },
        actor=user,
    )
    return translation


def translation_bytes(translation: LetterTranslation) -> bytes:
    return get_provider("FileStorage", translation.association).get(translation.file_key)
