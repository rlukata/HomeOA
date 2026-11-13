# UI conventions

Shared pieces live in `homeoa/ui/templates/ui/components/` and `homeoa/ui/templatetags/ui.py`
(`{% load ui %}`). Styles are in `homeoa/ui/static/homeoa/base.css` and use only its tokens.
Do not add `<style>` blocks or `style=` attributes to app templates; add a small class to
`base.css` instead.

## Forms

```django
{% load i18n ui %}
<form method="post" class="form-grid">        {# or .form-stack (one column), .form-row (filter bar) #}
  {% csrf_token %}
  {% include "ui/components/form.html" %}      {# form_errors + hidden + every visible field #}
  <div class="form-actions">
    <button type="submit" class="btn btn-primary">{% translate "Save" %}</button>
    <a class="btn btn-ghost" href="...">{% translate "Cancel" %}</a>
  </div>
</form>
```

Pass another form with `{% include "ui/components/form.html" with form=notice_form %}`.
For a custom layout, render pieces yourself:

- `{% form_errors form %}`: non-field and hidden-field errors as a `role="alert"` callout.
- `{% field form.name %}`: label (`for`), control, help text (`id_<name>_helptext`) and
  errors (`id_<name>_error`). The control gets `aria-describedby` for both and
  `aria-invalid="true"` on error. Required fields show a `*` plus a screen-reader
  "(required)". Checkboxes render inline; radio and multi-checkbox widgets render as a
  `fieldset`/`legend`. Options: `full=True` (span the row; automatic for textareas and
  groups), `label="..."`, `hide_label=True` (visually hidden label).

GET filter bars with hand-written inputs use `<form class="form-row">` with
`<label>Text<input ...></label>`; a lone input elsewhere takes `class="input"`.

## Messages

`ui/base.html` renders `ui/components/messages.html` at the top of `<main>`: a
`role="status" aria-live="polite"` region with `id="messages"`. Use
`django.contrib.messages` in views; never print messages in app templates. An htmx
partial response can refresh it with `{% include "ui/components/messages.html" with oob=True %}`
(`hx-swap-oob`). Levels map to callouts: success `ok`, error `danger`, warning `warn`,
info `info`.

## Tables

Hand-written tables use the shared classes:

```django
<section class="card">
  <div class="table-wrap">
    <table class="t">
      <caption class="sr">{% translate "All units" %}</caption>
      <thead><tr><th scope="col">...</th><th scope="col" class="num">...</th></tr></thead>
      <tbody>... {% empty %}<tr><td colspan="N" class="empty-row">...</td></tr></tbody>
      <tfoot>...</tfoot>                      {# totals: bold, top rule #}
    </table>
  </div>
</section>
```

Every table has a caption (visually hidden is fine) and `scope` on header cells. Money and
counts go in `class="num"` cells. `tr.group` styles a group header row.
For plain text data, `{% include "ui/components/table.html" with caption=... headers=... rows=... empty=... %}`
renders the whole thing (`headers` items are labels or `(label, "num")` pairs).

## Badges, cards, empty states

- `{% badge "ok" label %}`: kinds `""`, `ok`, `warn`, `danger`, `info`, `violet`, `accent`.
- Cards: `.card` with `.card-h` (heading row), `.card-b` (body), `.card-f` (footer),
  `.card-pad` (padded card), `ul.rows` (bordered list rows).
- Empty state: `{% include "ui/components/empty.html" with message=_("No units yet.") action_url=... action_label=... %}`.
- Buttons: `.btn`, `.btn-primary`, `.btn-ghost`, `.btn-danger`, `.btn-sm`, `.btn-lg`, `.btn-back`.

## Dates

Use `homeoa.core.context.association_today(association=None)` for "today": the date in
the association's time zone (current association by default, UTC when none).
