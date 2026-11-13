"""Community > Polls for the board; poll pages for residents."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view
from homeoa.core.context import association_today

from . import polls
from .models import Poll
from .views import _render

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class PollForm(forms.Form):
    question = forms.CharField(label=gettext_lazy("Question"), max_length=300)
    details = forms.CharField(
        label=gettext_lazy("Details (optional)"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    choices = forms.CharField(
        label=gettext_lazy("Choices, one per line"),
        widget=forms.Textarea(attrs={"rows": 4}),
        initial="Yes\nNo",
    )
    opens_on = forms.DateField(label=gettext_lazy("Opens on"), widget=DATE)
    closes_on = forms.DateField(label=gettext_lazy("Closes on"), widget=DATE)
    include_tenants = forms.BooleanField(label=gettext_lazy("Tenants answer too"), required=False)
    show_results = forms.BooleanField(
        label=gettext_lazy("Show results to residents after it closes"),
        required=False,
        initial=True,
    )

    def clean_choices(self):
        return polls.parse_choices(self.cleaned_data["choices"])


@require_perm(perms.ANNOUNCEMENTS_MANAGE)
def index(request):
    today = association_today(request.association)
    form = PollForm(request.POST or None, initial={"opens_on": today})
    if request.method == "POST" and form.is_valid():
        try:
            poll = polls.create(request.association, user=request.user, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, _("Poll created."))
            return redirect("community:poll", pk=poll.pk)
    rows = list(Poll.objects.all())
    for p in rows:
        p.open_now = p.is_open(today)
    response = _render(request, "community/polls.html", {"polls": rows, "form": form}, "polls")
    if request.method == "POST":
        response.status_code = 400
    return response


@require_perm(perms.ANNOUNCEMENTS_MANAGE)
def detail(request, pk):
    poll = get_object_or_404(Poll.objects, pk=pk)
    today = association_today(request.association)
    return _render(
        request,
        "community/poll.html",
        {"poll": poll, "results": polls.results(poll), "open_now": poll.is_open(today)},
        "polls",
    )


@require_POST
@require_perm(perms.ANNOUNCEMENTS_MANAGE)
def close(request, pk):
    poll = get_object_or_404(Poll.objects, pk=pk)
    polls.close_now(poll, association_today(request.association))
    messages.success(request, _("Poll closed."))
    return redirect("community:poll", pk=poll.pk)


@resident_view
def resident_poll(request, pk):
    from homeoa.portal.views.base import _render as portal_render
    from homeoa.portal.views.mine import my_person

    poll = get_object_or_404(Poll.objects, pk=pk)
    today = association_today(request.association)
    units = polls.units_for(request.user, poll, today)
    if not units:
        from django.http import Http404

        raise Http404
    if request.method == "POST":
        unit = next((u for u in units if u.number == request.POST.get("unit")), units[0])
        try:
            polls.answer(
                poll,
                user=request.user,
                person=my_person(request),
                unit=unit,
                choice=int(request.POST.get("choice", "-1")),
                on=today,
            )
            messages.success(request, _("Thanks, your answer is saved."))
        except (ValueError, ValidationError) as exc:
            messages.error(request, getattr(exc, "messages", [str(exc)])[0])
        return redirect("community:resident_poll", pk=poll.pk)
    answers = {a.unit_id: a for a in poll.answers.filter(unit__in=units)}
    closed = poll.is_closed(today)
    return portal_render(
        request,
        "home",
        "community/resident_poll.html",
        poll=poll,
        units=[(u, answers.get(u.pk)) for u in units],
        open_now=poll.is_open(today),
        results=polls.results(poll) if closed and poll.show_results else None,
    )
