from django import forms
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from homeoa import rules
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.changes.replay import change_view

from .base import Kind
from .keys import KEYS
from .templatetags.rule_tags import format_value


class OverrideForm(forms.Form):
    key = forms.ChoiceField(label=gettext_lazy("Rule"), choices=())
    value = forms.CharField(
        label=gettext_lazy("New value"),
        required=False,
        help_text=gettext_lazy(
            "Numbers as 25.00 or 10; yes or no; lists separated by commas. "
            "Leave blank when clearing."
        ),
    )
    reason = forms.CharField(label=gettext_lazy("Reason"), max_length=500)
    action = forms.ChoiceField(choices=(("set", "set"), ("clear", "clear")))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["key"].choices = [(k, f"{m.label} ({k})") for k, m in KEYS.items()]


def parse_value(key: str, text: str):
    text = (text or "").strip()
    kind = KEYS[key].kind
    if text == "":
        return None
    if kind is Kind.BOOL:
        low = text.lower()
        if low in ("yes", "true", "1"):
            return True
        if low in ("no", "false", "0"):
            return False
        raise ValueError(_("Enter yes or no."))
    if kind in (Kind.ORDER, Kind.STEPS):
        return [part.strip() for part in text.split(",") if part.strip()]
    return text


def _page(request, form):
    association = request.association
    return render(
        request,
        "rules/list.html",
        {
            "current_hub": None,
            "subtabs": [],
            "pack": rules.active_pack(association),
            "rows": rules.all_values(association),
            "form": form,
        },
    )


# No settings-read code exists; any board member may view the rules.
@require_perm(perms.BOARD_SHELL)
def rule_list(request):
    return _page(request, OverrideForm())


@change_view(
    "policy.rule_value",
    gettext_lazy("Change an association rule value"),
    manage=perms.POLICY_PROPOSE,
    propose=perms.POLICY_PROPOSE,
    codes={perms.POLICY_PROPOSE},
    always=True,
)
@require_perm(perms.POLICY_PROPOSE)
@require_POST
def rule_override(request):
    association = request.association
    if association is None:
        raise Http404
    form = OverrideForm(request.POST)
    if form.is_valid():
        key, reason = form.cleaned_data["key"], form.cleaned_data["reason"]
        try:
            if form.cleaned_data["action"] == "clear":
                rules.clear_override(association, key, reason=reason)
            else:
                rules.set_override(
                    association, key, parse_value(key, form.cleaned_data["value"]), reason=reason
                )
        except ValueError as exc:
            form.add_error("value", str(exc))
        else:
            messages.success(request, _("Rule saved."))
            resolved = rules.resolve(association, key)
            if resolved.clamped:
                messages.warning(
                    request,
                    _(
                        "%(requested)s is above the legal maximum of %(cap)s (%(citation)s). "
                        "The maximum applies instead."
                    )
                    % {
                        "requested": format_value(key, resolved.requested),
                        "cap": format_value(key, resolved.value),
                        "citation": resolved.cap_citation,
                    },
                )
            return redirect("rules:index")
    return _page(request, form)


@require_perm(perms.SETTINGS_MANAGE)
def law_changes(request):
    """The active pack, its latest law check and the review list."""
    from . import citations, lawcheck

    association = request.association
    pack = rules.active_pack(association)
    index = citations.index(pack)
    return render(
        request,
        "rules/law_changes.html",
        {
            "current_hub": None,
            "subtabs": [],
            "pack": pack,
            "version": lawcheck.pack_version(association),
            "check": lawcheck.latest(association),
            "section_count": len(index.sections),
            "not_checked": [{"citation": c, "places": places} for c, places in index.other.items()],
        },
    )


@require_perm(perms.SETTINGS_MANAGE)
@require_POST
def law_changes_reviewed(request, pk):
    from . import lawcheck
    from .models import LawCheck

    check = get_object_or_404(LawCheck.objects, pk=pk)
    if check.reviewed_at is None:
        lawcheck.mark_reviewed(check, request.user)
        messages.success(request, _("Marked reviewed."))
    return redirect("rules:law_changes")
