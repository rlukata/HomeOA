"""Money > Bank > SimpleFIN: connect (claim a setup token), link accounts, see sync status."""

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.banking.models import BankAccount
from homeoa.banking.views import _shell
from homeoa.plugins import registry
from homeoa.plugins import services as plugins

from . import sync as svc
from .client import ClaimError, SimpleFinError
from .models import SimpleFinAccount

IGNORE = "ignore"


class ConnectForm(forms.Form):
    setup_token = forms.CharField(
        label=gettext_lazy("Setup token"),
        widget=forms.Textarea(attrs={"rows": 3, "autocomplete": "off", "spellcheck": "false"}),
        help_text=gettext_lazy(
            "From SimpleFIN Bridge: My account > Apps > New connection. It works once."
        ),
    )


def _available(request):
    if registry.get(svc.PLUGIN_ID) is None or request.association is None:
        raise PermissionDenied


def _can(request, perm):
    return has_perm(request.user, perm, association=request.association)


@require_perm(perms.FINANCE_READ)
def index(request):
    _available(request)
    assoc = request.association
    inst = plugins.installation_for(assoc, svc.PLUGIN_ID)
    links = list(SimpleFinAccount.objects.select_related("bank_account"))
    taken = {link.bank_account_id for link in links if link.bank_account_id}
    ctx = _shell(
        _("SimpleFIN bank feed"),
        installation=inst,
        enabled=bool(inst and inst.enabled),
        connected=bool(inst and inst.enabled and (inst.secrets or {}).get("access_url")),
        fake=svc.use_fake(inst),
        conn=svc.Connection.objects.first(),
        links=links,
        bank_accounts=BankAccount.objects.filter(active=True),
        taken=taken,
        form=ConnectForm(),
        can_connect=_can(request, perms.PLUGINS_MANAGE),
        can_manage=_can(request, perms.FINANCE_MANAGE),
        ignore=IGNORE,
    )
    return render(request, "homeoa_simplefin/index.html", ctx)


@require_POST
@require_perm(perms.PLUGINS_MANAGE)
def connect(request):
    _available(request)
    form = ConnectForm(request.POST)
    if form.is_valid():
        try:
            result = svc.connect(
                request.association, form.cleaned_data["setup_token"], user=request.user
            )
        except ClaimError:
            messages.error(
                request,
                _(
                    "That setup token didn't work. Tokens work only once; "
                    "make a new one in SimpleFIN Bridge."
                ),
            )
        except SimpleFinError:
            messages.error(request, _("We couldn't reach SimpleFIN. Try again in a minute."))
        else:
            messages.success(request, _("Bank feed connected."))
            if result.unmapped:
                messages.info(request, _("Link each account below to a HomeOA bank account."))
    else:
        messages.error(request, _("Paste the setup token."))
    return redirect("homeoa_simplefin:index")


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def link_accounts(request):
    _available(request)
    accounts = {str(a.pk): a for a in BankAccount.objects.filter(active=True)}
    links = list(SimpleFinAccount.objects.select_related("bank_account"))
    chosen = {}
    for link in links:
        value = request.POST.get(f"link-{link.pk}", "")
        if value and value != IGNORE and value not in accounts:
            messages.error(request, _("Choose a bank account from the list."))
            return redirect("homeoa_simplefin:index")
        chosen[link.pk] = value
    used = [v for v in chosen.values() if v and v != IGNORE]
    if len(used) != len(set(used)):
        messages.error(request, _("Each bank account can be linked to only one feed account."))
        return redirect("homeoa_simplefin:index")
    # Unlink first so swapping two accounts doesn't trip the one-to-one constraint.
    for link in links:
        value = chosen[link.pk]
        target = accounts.get(value)
        if link.bank_account_id and (target is None or target.pk != link.bank_account_id):
            svc.map_account(link, None, ignore=value == IGNORE, user=request.user)
    for link in links:
        value = chosen[link.pk]
        target = accounts.get(value)
        if target is not None and link.bank_account_id != target.pk:
            svc.map_account(link, target, user=request.user)
        elif target is None and link.ignored != (value == IGNORE):
            svc.map_account(link, None, ignore=value == IGNORE, user=request.user)
    messages.success(request, _("Account links saved. The next sync imports linked accounts."))
    return redirect("homeoa_simplefin:index")
