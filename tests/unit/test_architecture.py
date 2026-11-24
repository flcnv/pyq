import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src"
BOOTSTRAP = "src.bootstrap"

# The composition root and the two processes' entry points. Everything else takes ports.
MAY_IMPORT_BOOTSTRAP = {
    SRC / "bootstrap.py",
    SRC / "main.py",
    SRC / "api" / "deps.py",
}
MAY_IMPORT_BOOTSTRAP_DIRS = {SRC / "worker"}


def imported_modules(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            yield node.module
            # `from src import bootstrap`
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def may_import_bootstrap(path: Path) -> bool:
    return path in MAY_IMPORT_BOOTSTRAP or any(
        directory in path.parents for directory in MAY_IMPORT_BOOTSTRAP_DIRS
    )


def restricted_modules() -> list[Path]:
    return sorted(path for path in SRC.rglob("*.py") if not may_import_bootstrap(path))


@pytest.mark.parametrize("path", restricted_modules(), ids=lambda path: str(path.relative_to(SRC)))
def test_only_the_composition_root_and_entry_points_import_bootstrap(path: Path) -> None:
    """Services take the ports they need, never `AppContext`: that keeps them testable with fakes
    and stops the context from turning into a service locator."""
    assert BOOTSTRAP not in set(imported_modules(path))


def test_the_check_covers_the_inner_layers() -> None:
    checked = {path.relative_to(SRC).parts[0] for path in restricted_modules()}
    assert {"domain", "application", "adapters", "api"} <= checked
