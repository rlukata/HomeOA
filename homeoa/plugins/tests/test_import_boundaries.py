"""Core may not import an optional module's code; a module may import another module's
code only when it requires that module. Imports of an app's models are always fine.

URLconfs and settings are where pages and apps are wired up, so they may name module
code; tests may exercise any module."""

from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path

from django.conf import settings
from django.utils.module_loading import import_string

REPO = Path(__file__).resolve().parents[3]
DOTTED = re.compile(r"^homeoa(\.\w+)+$")


def module_code(manifests) -> dict[str, tuple[str, ...]]:
    """Module id -> the code patterns its manifest declares."""
    return {m.id: m.code for m in manifests}


def owner(path: str, code: dict[str, tuple[str, ...]]) -> str | None:
    """The module whose code ``path`` (relative to the repo) is, or None for core."""
    name = path.rsplit("/", 1)[-1]
    if name == "models.py" or "/migrations/" in path:
        return None
    for module_id, patterns in code.items():
        for pattern in patterns:
            if pattern.endswith(".py"):
                if fnmatch.fnmatchcase(path, pattern):
                    return module_id
            elif path == f"{pattern}.py" or path.startswith(f"{pattern}/"):
                return module_id
    return None


def source_file(dotted: str) -> str | None:
    """The file that defines ``dotted`` or its longest importable prefix, if in the repo."""
    parts = dotted.split(".")
    while parts:
        base = "/".join(parts)
        for path in (f"{base}.py", f"{base}/__init__.py"):
            if (REPO / path).is_file():
                return path
        parts.pop()
    return None


def _module_name(path: str) -> str:
    parts = path[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imported_names(source: str, path: str):
    """``(line, dotted name)`` for every import in ``source`` (at any depth) and every
    ``homeoa.…`` dotted-path string literal. Relative imports are made absolute."""
    tree = ast.parse(source, filename=path)
    package = _module_name(path)
    if not path.endswith("__init__.py"):
        package = package.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            # ``from package import submodule`` imports the submodule, not the package.
            for alias in node.names:
                name = f"{prefix}.{alias.name}"
                file = source_file(name)
                yield node.lineno, name if file and file != source_file(prefix) else prefix
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and DOTTED.match(node.value)
        ):
            yield node.lineno, node.value


def wiring(path: str) -> bool:
    """URLconfs, settings and tests, which may name any module's code."""
    return path.endswith("/urls.py") or path.startswith("homeoa/settings/") or "/tests/" in path


def violations(path: str, source: str, manifests) -> list[str]:
    """Each forbidden import in ``source`` (the file at ``path``) as ``file:line -> name``."""
    if wiring(path):
        return []
    code = module_code(manifests)
    requires = {m.id: set(m.requires) for m in manifests}
    entry = {m.id: _entry_file(m) for m in manifests}
    me = owner(path, code)
    out = []
    for line, name in imported_names(source, path):
        file = source_file(name)
        target = owner(file, code) if file else None
        if target is None or target == me:
            continue
        if me is not None and target in requires[me]:
            continue
        # Settings name each module's manifest by its dotted path; that is how core finds it.
        if file == entry[target]:
            continue
        out.append(f"{path}:{line} -> {name}")
    return sorted(set(out))


def _entry_file(manifest) -> str:
    """The file that holds the manifest (``settings.HOMEOA_MODULES`` points at it)."""
    for dotted in settings.HOMEOA_MODULES:
        if import_string(dotted) is manifest:
            return dotted.rpartition(".")[0].replace(".", "/") + ".py"
    return ""


def _manifests():
    return [import_string(dotted) for dotted in settings.HOMEOA_MODULES]


def _python_files():
    for file in sorted((REPO / "homeoa").rglob("*.py")):
        yield file.relative_to(REPO).as_posix()


def test_core_does_not_import_optional_module_code():
    manifests = _manifests()
    found = []
    for path in _python_files():
        found += violations(path, (REPO / path).read_text(), manifests)
    assert not found, "Imports across a module boundary:\n" + "\n".join(found)


def test_every_module_declares_its_code():
    assert all(m.code for m in _manifests())


def test_a_core_import_of_module_code_is_caught():
    source = (
        "from homeoa.enforcement.services import check_photo\n"
        "def f():\n"
        "    from ..registry import directory\n"
        "    import_string('homeoa.reports.packet.after_close')\n"
        "from homeoa.enforcement.models import Violation\n"
    )
    found = violations("homeoa/billing/example.py", source, _manifests())
    assert found == [
        "homeoa/billing/example.py:1 -> homeoa.enforcement.services",
        "homeoa/billing/example.py:3 -> homeoa.registry.directory",
        "homeoa/billing/example.py:4 -> homeoa.reports.packet.after_close",
    ]


def test_urlconfs_and_tests_may_name_module_code():
    source = "from homeoa.registry import directory_views\n"
    manifests = _manifests()
    assert violations("homeoa/registry/urls.py", source, manifests) == []
    assert violations("homeoa/registry/tests/test_x.py", source, manifests) == []


def test_a_module_may_import_only_the_modules_it_requires():
    manifests = _manifests()
    source = "from homeoa.governance.committees_module import MANIFEST\n"
    assert violations("homeoa/arc/example.py", source, manifests) == []
    source = "from homeoa.registry.directory import rows\n"
    assert violations("homeoa/arc/example.py", source, manifests) != []
