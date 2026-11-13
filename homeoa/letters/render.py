"""Sandboxed letter renderer.

Templates use Django syntax, but only a small allow-list passes ``check()``:

* variables that are registered merge fields: ``{{ unit.number }}``, optionally with
  the filters ``upper``, ``lower``, ``title``, ``default:"text"``;
* ``{% if field %}`` / ``{% elif field %}`` / ``{% else %}`` / ``{% endif %}`` where the
  condition is merge fields joined by ``and`` / ``or`` / ``not``;
* ``{# comments #}``.

Anything else (other tags such as ``include``/``load``/``url``, other filters, attribute
access beyond ``group.name``) is rejected before a template is saved or rendered. The
context holds only plain values from the merge-field resolvers, never model objects.
"""

from __future__ import annotations

import dataclasses
import re

from django.template import Context, Engine
from django.template.base import Lexer, TokenType
from django.utils.translation import gettext as _

from . import merge

ALLOWED_FILTERS = frozenset({"upper", "lower", "title", "default"})
_VAR_RE = re.compile(r'^\s*([A-Za-z_]\w*\.[A-Za-z_]\w*)((?:\|\w+(?::"[^"{}%|]*")?)*)\s*$')
_FILTER_RE = re.compile(r'\|(\w+)(?::"([^"]*)")?')
_IF_WORDS = frozenset({"and", "or", "not"})

_engine = Engine(debug=False, string_if_invalid="", libraries={}, builtins=None)


@dataclasses.dataclass
class CheckResult:
    errors: list[str]
    unknown_fields: list[str]
    used_fields: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


class TemplateRejected(ValueError):
    def __init__(self, result: CheckResult):
        self.result = result
        super().__init__("; ".join(result.errors))


def check(*sources: str) -> CheckResult:
    errors: list[str] = []
    unknown: list[str] = []
    used: list[str] = []

    def field(key):
        if not merge.is_known(key):
            if key not in unknown:
                unknown.append(key)
        elif key not in used:
            used.append(key)

    for source in sources:
        depth = 0
        for token in Lexer(source or "").tokenize():
            if token.token_type in (TokenType.TEXT, TokenType.COMMENT):
                continue
            if token.token_type is TokenType.VAR:
                m = _VAR_RE.match(token.contents)
                if not m:
                    errors.append(
                        _("“{{ %(text)s }}” is not a merge field.") % {"text": token.contents}
                    )
                    continue
                field(m.group(1))
                for name, _arg in _FILTER_RE.findall(m.group(2)):
                    if name not in ALLOWED_FILTERS:
                        errors.append(
                            _("The filter “%(name)s” is not allowed in letters.") % {"name": name}
                        )
                continue
            # BLOCK
            bits = token.contents.split()
            tag = bits[0] if bits else ""
            if tag in ("if", "elif"):
                if tag == "if":
                    depth += 1
                elif depth == 0:
                    errors.append(_("“elif” without “if”."))
                if len(bits) < 2:
                    errors.append(_("“%(tag)s” needs a merge field.") % {"tag": tag})
                for bit in bits[1:]:
                    if bit in _IF_WORDS:
                        continue
                    if re.fullmatch(r"[A-Za-z_]\w*\.[A-Za-z_]\w*", bit):
                        field(bit)
                    else:
                        errors.append(
                            _(
                                "Only merge fields with and / or / not are allowed in “if”, "
                                "not “%(bit)s”."
                            )
                            % {"bit": bit}
                        )
            elif tag in ("else", "endif") and len(bits) == 1:
                if depth == 0:
                    errors.append(_("“%(tag)s” without “if”.") % {"tag": tag})
                elif tag == "endif":
                    depth -= 1
            else:
                errors.append(
                    _("The tag “{%% %(text)s %%}” is not allowed in letters.")
                    % {"text": token.contents}
                )
        if depth:
            errors.append(_("An “if” is missing its “endif”."))
    if unknown:
        errors.append(_("Unknown merge fields: %(fields)s.") % {"fields": ", ".join(unknown)})
    return CheckResult(errors=errors, unknown_fields=unknown, used_fields=used)


def render(source: str, values: dict, *, autoescape: bool = False) -> str:
    result = check(source)
    if not result.ok:
        raise TemplateRejected(result)
    template = _engine.from_string(source)
    return template.render(Context(values, autoescape=autoescape))


def render_letter(subject: str, body: str, ctx: merge.LetterContext) -> tuple[str, str]:
    result = check(subject, body)
    if not result.ok:
        raise TemplateRejected(result)
    values = merge.resolve_all(ctx, result.used_fields)
    return render(subject, values).strip(), render(body, values)
