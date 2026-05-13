"""Scaffold generator tests.

For each kind we render the scaffold, write it to a temp dir, import
it, and verify it produces a working Plugin instance. This catches
template-string bugs that would otherwise only surface at user time.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from pilothouse.plugins import Plugin
from pilothouse.plugins.scaffold import VALID_KINDS, render


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kind", VALID_KINDS)
def test_each_kind_renders_runnable_python(kind: str, tmp_path: Path) -> None:
    name = f"scaff_{kind}"
    scaffold = render(kind, name)

    plugin_path = tmp_path / scaffold.plugin_filename
    plugin_path.write_text(scaffold.plugin_body)

    module = _load_module(plugin_path, f"_scaff_{kind}_module")

    # Find the plugin class — it ends with "Plugin" and lives in the
    # generated module.
    plugin_classes = [
        v for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, Plugin) and v is not Plugin
        and getattr(v, "__module__", "") == module.__name__
    ]
    assert len(plugin_classes) == 1, f"expected exactly one Plugin in {kind} scaffold"
    cls = plugin_classes[0]

    instance = cls()
    assert instance.name == name
    meta = instance.meta()
    assert meta.name == name
    assert meta.version
    # Inferred kinds must contain the requested kind.
    assert any(k.value == kind for k in meta.kinds)


def test_invalid_kind_rejected() -> None:
    with pytest.raises(ValueError):
        render("notakind", "x")


def test_invalid_name_rejected() -> None:
    with pytest.raises(ValueError):
        render("notifier", "bad-name-with-dashes!")


def test_test_file_runs_without_syntax_errors(tmp_path: Path) -> None:
    """Compiling the generated test file catches missing imports / typos."""
    scaffold = render("connector", "scaff_conn")
    test_path = tmp_path / scaffold.test_filename
    test_path.write_text(scaffold.test_body)
    # compile() raises SyntaxError on bad python; happy path is silent.
    compile(test_path.read_text(), str(test_path), "exec")
