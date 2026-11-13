"""Shared UI components: field/form_errors/badge tags and the messages partial."""

import datetime
from types import SimpleNamespace

import pytest
from django import forms
from django.contrib.messages import constants
from django.contrib.messages.storage.base import Message
from django.template import Context, Template, TemplateSyntaxError
from django.template.loader import render_to_string

from homeoa.core.context import association_context, association_today


class SampleForm(forms.Form):
    name = forms.CharField(label="Name", help_text="Full legal name")
    nickname = forms.CharField(label="Nickname", required=False)
    agree = forms.BooleanField(label="I agree", required=False)
    color = forms.ChoiceField(
        label="Colour", choices=[("r", "Red"), ("g", "Green")], widget=forms.RadioSelect
    )

    def clean(self):
        raise forms.ValidationError("Whole-form problem")


def render(src, **ctx):
    return Template("{% load ui %}" + src).render(Context(ctx))


def test_field_label_help_and_required():
    html = render("{% field form.name %}", form=SampleForm())
    assert '<label class="field-l" for="id_name">Name' in html
    assert 'id="id_name_helptext"' in html
    assert 'aria-describedby="id_name_helptext"' in html
    assert "(required)" in html and " required" in html
    assert "aria-invalid" not in html


def test_optional_field_has_no_required_marker():
    html = render("{% field form.nickname %}", form=SampleForm())
    assert "(required)" not in html and "aria-describedby" not in html


def test_field_errors_linked_with_aria_describedby():
    form = SampleForm(data={})
    form.is_valid()
    html = render("{% field form.name %}", form=form)
    assert 'aria-describedby="id_name_helptext id_name_error"' in html
    assert 'aria-invalid="true"' in html
    assert '<ul class="field-err" id="id_name_error"><li>This field is required.</li>' in html


def test_radio_group_uses_fieldset_and_checkbox_inline():
    form = SampleForm()
    html = render("{% field form.color %}", form=form)
    assert html.startswith("<fieldset") and "<legend" in html and "Colour" in html
    html = render("{% field form.agree %}", form=form)
    assert 'class="field check' in html and '<label for="id_agree">' in html


def test_form_errors_and_form_partial():
    form = SampleForm(data={"name": "x", "color": "r"})
    form.is_valid()
    assert 'role="alert"' in render("{% form_errors form %}", form=form)
    assert render("{% form_errors form %}", form=SampleForm()) == ""
    html = render_to_string("ui/components/form.html", {"form": form})
    assert "Whole-form problem" in html and 'for="id_nickname"' in html


def test_badge():
    assert render('{% badge "ok" "Paid" %}') == '<span class="badge ok">Paid</span>'
    assert render('{% badge "" x %}', x="<b>") == '<span class="badge">&lt;b&gt;</span>'
    with pytest.raises(TemplateSyntaxError):
        render('{% badge "nope" "x" %}')


def test_messages_partial():
    msgs = [Message(constants.SUCCESS, "Saved"), Message(constants.ERROR, "Broken")]
    html = render_to_string("ui/components/messages.html", {"messages": msgs})
    assert 'aria-live="polite"' in html and 'id="messages"' in html
    assert '<div class="callout ok">Saved</div>' in html
    assert '<div class="callout danger">Broken</div>' in html
    assert "hx-swap-oob" not in html
    oob = render_to_string("ui/components/messages.html", {"messages": [], "oob": True})
    assert 'hx-swap-oob="true"' in oob


def test_table_and_empty_partials():
    html = render_to_string(
        "ui/components/table.html",
        {"caption": "Things", "headers": ["Name", ("Amount", "num")], "rows": [["A", "1"]]},
    )
    assert '<caption class="sr">Things</caption>' in html
    assert '<th scope="col" class="num">Amount</th>' in html
    html = render_to_string(
        "ui/components/table.html", {"caption": "T", "headers": ["A", "B"], "rows": []}
    )
    assert 'colspan="2"' in html
    html = render_to_string(
        "ui/components/empty.html",
        {"message": "No units", "action_url": "/units/add/", "action_label": "Add"},
    )
    assert "No units" in html and 'href="/units/add/"' in html


def test_association_today(monkeypatch):
    from django.utils import timezone

    fixed = datetime.datetime(2026, 10, 7, 20, 0, tzinfo=datetime.UTC)
    monkeypatch.setattr(timezone, "now", lambda: fixed)
    kiri = SimpleNamespace(timezone="Pacific/Kiritimati")
    assert association_today(kiri) == datetime.date(2026, 10, 8)
    assert association_today(SimpleNamespace(timezone="Not/AZone")) == datetime.date(2026, 10, 7)
    assert association_today() == datetime.date(2026, 10, 7)
    with association_context(kiri):
        assert association_today() == datetime.date(2026, 10, 8)
