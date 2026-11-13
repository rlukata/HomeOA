"""Document library pages (DOC-01, GOV-01): board hub under /documents/, the
resident Docs tab, and the one download view every audience goes through."""

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view

from . import services as docs
from . import welcome
from .forms import UploadForm, VersionForm, VisibilityForm
from .models import Category, Document

# Hub section slug -> category filter ("" for all).
SECTION_CATEGORY = {
    "all": "",
    "governing": Category.GOVERNING,
    "financial": Category.FINANCIAL,
    "meetings": Category.MINUTES,
}
VIS_BADGE = {"public": "ok", "tenants": "accent", "owners": "info", "board": "danger"}


def _hub_render(request, template, context, tab="all"):
    from homeoa.ui.nav import get_hub

    hub = get_hub("documents")
    slugs = [t.slug for t in hub.subtabs]
    index = slugs.index(tab) if tab in slugs else -1
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": i == index}
        for i, t in enumerate(hub.subtabs)
    ]
    can_manage = docs.can_manage(request.user, request.association)
    return render(
        request,
        template,
        {"current_hub": hub, "subtabs": tabs, "can_manage": can_manage, **context},
    )


def _rows(qs):
    rows = list(qs)
    for d in rows:
        d.badge = VIS_BADGE.get(d.visibility, "")
    return rows


def _list(request, section):
    category = request.GET.get("category", SECTION_CATEGORY.get(section, ""))
    if category not in Category.values:
        category = ""
    query = request.GET.get("q", "").strip()
    rows = _rows(
        docs.visible_documents(request.user, request.association, category=category, query=query)
    )
    snippets = docs.text_snippets(rows, query)
    for d in rows:
        d.found_on_page, d.snippet = snippets.get(d.pk, (None, ""))
    context = {
        "documents": rows,
        "q": query,
        "category": category,
        "categories": [("", _("All")), *Category.choices],
        "section": section,
    }
    template = "documents/_rows.html" if request.htmx else "documents/list.html"
    return _hub_render(request, template, context, tab=section)


def section_view(section):
    @require_perm(perms.DOCUMENTS_BOARD)
    def view(request):
        return _list(request, section)

    view.__name__ = f"documents_{section}"
    return view


def _document(request, public_id) -> Document:
    return get_object_or_404(
        Document.all_associations.select_related("current_version"),
        association=request.association,
        public_id=public_id,
    )


@require_perm(perms.DOCUMENTS_MANAGE)
def upload(request):
    """Bulk upload: several files, one shared category and visibility."""
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        files = request.FILES.getlist("files")
        data = form.cleaned_data
        errors = []
        for f in files:
            try:
                docs.check_upload(f.name, f.size)
            except docs.UploadRejected as exc:
                errors.extend(exc.messages)
        for e in errors:
            form.add_error("files", e)
        if not errors:
            single = len(files) == 1
            for f in files:
                docs.attach(
                    request.association,
                    filename=f.name,
                    content=f.read(),
                    title=data["title"] if single else "",
                    category=data["category"],
                    visibility=data["visibility"],
                    governing_kind=data["governing_kind"],
                    effective_date=data["effective_date"],
                    note=data["note"],
                    user=request.user,
                )
            messages.success(
                request,
                ngettext("%(n)s document uploaded.", "%(n)s documents uploaded.", len(files))
                % {"n": len(files)},
            )
            return redirect("documents:index")
    return _hub_render(request, "documents/upload.html", {"form": form})


@require_perm(perms.DOCUMENTS_BOARD)
def detail(request, public_id):
    doc = _document(request, public_id)
    doc.badge = VIS_BADGE.get(doc.visibility, "")
    return _hub_render(
        request,
        "documents/detail.html",
        {
            "document": doc,
            "versions": list(doc.versions.all()),
            "version_form": VersionForm(),
            "visibility_form": VisibilityForm(
                initial={"visibility": doc.visibility, "in_welcome_packet": doc.in_welcome_packet}
            ),
        },
    )


@require_POST
@require_perm(perms.DOCUMENTS_MANAGE)
def new_version(request, public_id):
    doc = _document(request, public_id)
    form = VersionForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, _("Choose a file to upload."))
        return redirect("documents:detail", public_id=doc.public_id)
    f = form.cleaned_data["file"]
    try:
        v = docs.attach(
            request.association,
            filename=f.name,
            content=f.read(),
            effective_date=form.cleaned_data["effective_date"],
            note=form.cleaned_data["note"],
            user=request.user,
            document=doc,
        )
    except docs.UploadRejected as exc:
        for m in exc.messages:
            messages.error(request, m)
    else:
        messages.success(request, _("Version %(n)s uploaded.") % {"n": v.number})
    return redirect("documents:detail", public_id=doc.public_id)


@require_POST
@require_perm(perms.DOCUMENTS_MANAGE)
def change_visibility(request, public_id):
    doc = _document(request, public_id)
    form = VisibilityForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for m in errors:
                messages.error(request, m)
        return redirect("documents:detail", public_id=doc.public_id)
    data = form.cleaned_data
    changed = docs.set_visibility(doc, data["visibility"], user=request.user)
    changed = welcome.set_in_packet(doc, data["in_welcome_packet"], user=request.user) or changed
    if changed:
        messages.success(request, _("Saved."))
    return redirect("documents:detail", public_id=doc.public_id)


def download(request, public_id, number=None):
    """The current version (whoever the visibility allows; signed out too when public) or
    an older version (board only). Refusals get the login page or 404, never the file."""
    if getattr(request, "association", None) is None:
        raise Http404
    doc = _document(request, public_id)
    version = doc.current_version
    if number is not None:
        version = get_object_or_404(doc.versions.all(), number=number)
    if version is None or not docs.can_download(request.user, request.association, version):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        raise Http404
    resp = HttpResponse(docs.read_file(version), content_type=version.content_type)
    disp = "inline" if version.content_type == "application/pdf" else "attachment"
    safe = version.file_name.replace('"', "")
    resp["Content-Disposition"] = f'{disp}; filename="{safe}"'
    resp["Referrer-Policy"] = "no-referrer"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    return resp


@resident_view
def portal_docs(request):
    """Resident Docs tab (``/me/docs/``): only what the viewer's role may see."""
    from homeoa.access.services import today_for
    from homeoa.insurance.services import owner_summary
    from homeoa.portal.views.base import _render

    association = request.association
    rows = _rows(docs.visible_documents(request.user, association))
    insurance = owner_summary(request.user, association, today_for(association))
    return _render(
        request, "docs", "documents/portal_list.html", documents=rows, insurance=insurance
    )
