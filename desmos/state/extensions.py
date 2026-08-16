"""Pi-shaped extensions: load(api) from ~/.desmos/extensions and .desmos/extensions."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable


class ExtAPI:
    def __init__(self) -> None:
        self.hooks: dict[str, list[Callable[..., Any]]] = {}
        self.tools: list[tuple[str, str, Callable[..., Any]]] = []
        # A file that raises on import used to disappear without a word, which
        # looks exactly like a hook that registered and never fired.
        self.errors: list[str] = []

    def on(self, event: str, fn: Callable[..., Any]) -> None:
        self.hooks.setdefault(event, []).append(fn)

    def register_tool(self, name: str, doc: str, handler: Callable[..., Any]) -> None:
        self.tools.append((name, doc, handler))

    # The ABI has always advertised api.hook and api.tool. Neither existed, so
    # an extension written against the prompt failed on its first line. Both
    # spellings are the interface now; the pi-shaped ones stay primary.
    hook = on
    tool = register_tool


def extension_roots(cwd: Path) -> list[Path]:
    roots = [Path.home() / ".desmos" / "extensions"]
    cur = cwd.resolve()
    chain: list[Path] = []
    while True:
        chain.append(cur)
        if (cur / ".git").exists() or cur.parent == cur:
            break
        cur = cur.parent
    for path in reversed(chain):
        roots.append(path / ".desmos" / "extensions")
    return roots


def _load_file(path: Path, api: ExtAPI) -> None:
    spec = importlib.util.spec_from_file_location(f"desmos_ext_{path.stem}", path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    load = getattr(module, "load", None)
    if callable(load):
        load(api)


def load_extensions(cwd: Path) -> ExtAPI:
    api = ExtAPI()
    for root in extension_roots(cwd):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.py")) + sorted(root.glob("*/index.py")):
            try:
                _load_file(path, api)
            except Exception as exc:
                api.errors.append(f"{path}: {type(exc).__name__}: {exc}")
    return api
