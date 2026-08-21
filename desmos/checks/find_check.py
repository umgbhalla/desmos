"""find checks: the workspace op=find syscall driven through real dispatch over a real
fff engine.

The whole file silent-skips when the fff extension module is not built (it is
built out-of-band by the orchestrator). Each check drives the real dispatch
path in a fresh temp tree and tears the engine down after, so no check holds a
handle on a deleted tempdir.

(a) typo round-trip: <workspace op="find">mian.py</workspace> ranks src/main.py first.
(b) watcher liveness: a file created after the scan settles is findable within
    5s (proves watch=True).
(c) absent-module refusal names the build script.
(d) frecency ordering proves edit touches reach fff.
(e) the real dispatch path exposes glob, constrained/context grep,
    definition-first symbol search, and constrained multi-pattern grep.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path


def _world(cwd: Path):
    from desmos.loop import new_world

    return new_world(cwd, state_path=None, persist=False, ns={})


def _find(world, query: str, **attrs) -> str:
    from desmos.dispatch import dispatch
    from desmos.types import Block

    return dispatch(world, Block("workspace", query, {**{k: str(v) for k, v in attrs.items()}, "op": "find"}))


def _first_path(out: str) -> str:
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("("):  # skip a "(still scanning...)" note
            continue
        return line.split("\t", 1)[0]
    return ""


def _typo_round_trip() -> None:
    from desmos.state import find as find_mod

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "docs").mkdir()
        (root / "src" / "main.py").write_text("x = 1\n")
        (root / "src" / "other.py").write_text("y = 2\n")
        (root / "docs" / "readme.md").write_text("hi\n")
        try:
            out = _find(_world(root), "mian.py")
            top = _first_path(out)
            assert top.endswith("main.py"), f"typo query ranked {top!r} first, not main.py:\n{out}"
        finally:
            find_mod.reset()


def _watcher_liveness() -> None:
    from desmos.state import find as find_mod

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "seed.py").write_text("s = 0\n")
        world = _world(root)
        try:
            _find(world, "seed")  # builds engine, waits for the initial scan
            (root / "zebrafish_marker.py").write_text("z = 1\n")
            deadline = time.time() + 5.0
            found = ""
            while time.time() < deadline:
                out = _find(world, "zebrafish_marker")
                if "zebrafish_marker.py" in out:
                    found = out
                    break
                time.sleep(0.2)
            assert found, "a file created after the initial scan was not findable within 5s (watch off?)"
        finally:
            find_mod.reset()


def _absent_module_refusal() -> None:
    from desmos.state import find as find_mod

    with tempfile.TemporaryDirectory() as tmp:
        world = _world(Path(tmp))
        saved = sys.modules.get("fff")
        sys.modules["fff"] = None  # forces `import fff` to raise ImportError
        try:
            out = _find(world, "anything")
            assert "find unavailable" in out and "build-fff-python.sh" in out, (
                f"absent fff must refuse and name the build script, got:\n{out}"
            )
        finally:
            if saved is None:
                sys.modules.pop("fff", None)
            else:
                sys.modules["fff"] = saved
            find_mod.reset()


def _frecency_ordering() -> None:
    from desmos.dispatch import dispatch
    from desmos.state import find as find_mod
    from desmos.types import Block

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "alpha_one.py").write_text("v0\n")
        (root / "alpha_two.py").write_text("v0\n")
        world = _world(root)
        try:
            # Each successful workspace op=edit on alpha_two feeds frecency via the dispatch
            # edit choke point (touch -> track_access) before any engine exists.
            # A single access does not clear fff's boost threshold; a short chain
            # of real edits does — and every one of them is a genuine touch, so
            # reverting the vendored track_access patch drops the boost to zero
            # and this ordering assertion fails.
            for i in range(6):
                msg = dispatch(
                    world,
                    Block("workspace", f"v{i}\n---\nv{i + 1}", {"op": "edit", "path": "alpha_two.py"}),
                )
                assert f"v{i + 1}" in (root / "alpha_two.py").read_text(), f"edit {i} did not apply: {msg}"
            out = _find(world, "alpha")
            paths = [
                ln.split("\t", 1)[0].strip()
                for ln in out.splitlines()
                if "alpha_" in ln
            ]
            assert paths and paths[0].endswith("alpha_two.py"), (
                f"edited alpha_two.py should rank first by frecency, got order {paths}:\n{out}"
            )
        finally:
            find_mod.reset()


def _content_modes() -> None:
    """The real dispatch path exposes fff's glob, grep, symbol, and multi modes."""
    from desmos.state import find as find_mod

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "docs").mkdir()
        (root / "src" / "alpha.py").write_text(
            "def AlphaSymbol():\n"
            "    return 'needle'\n"
            "\n"
            "value = AlphaSymbol()\n"
        )
        (root / "src" / "beta.ts").write_text("const BetaSymbol = 1;\n")
        (root / "docs" / "note.txt").write_text("needle outside code\n")
        world = _world(root)
        try:
            globbed = _find(world, "*.py", mode="glob")
            assert "src/alpha.py" in globbed and "beta.ts" not in globbed, globbed

            grep = _find(world, "src/ needle", mode="grep", context=1)
            assert "src/alpha.py:2:" in grep and "| def AlphaSymbol():" in grep, grep
            assert "docs/note.txt" not in grep, grep

            symbols = _find(world, "AlphaSymbol", mode="symbol")
            lines = [line for line in symbols.splitlines() if line and not line.startswith("(")]
            assert lines and "[def]" in lines[0] and "def AlphaSymbol" in lines[0], symbols
            assert any("value = AlphaSymbol()" in line for line in lines[1:]), symbols

            multi = _find(
                world,
                "AlphaSymbol\nBetaSymbol",
                mode="multi",
                constraints="src/",
            )
            assert "alpha.py" in multi and "beta.ts" in multi, multi

            invalid = _find(world, "x", mode="unknown")
            assert "invalid mode" in invalid, invalid
        finally:
            find_mod.reset()


def check() -> None:
    try:
        import fff  # noqa: F401
    except Exception:
        print("[find] fff extension not built; skipping find checks")
        return

    _typo_round_trip()
    _watcher_liveness()
    _absent_module_refusal()
    _frecency_ordering()
    _content_modes()
    print("find check ok")


if __name__ == "__main__":
    check()
