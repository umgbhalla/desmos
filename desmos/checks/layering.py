"""Layering check: the import direction from docs/upgrade-paths.md is law.

kernel imports nothing above it; transport imports kernel only; state imports
kernel; agents import kernel+transport+state; front imports the layers below
it; checks import anything.

Top-level modules are the public SDK: facades (star re-exports of one
subpackage impl) plus a frozen handful of real top-level files. Facades
import upward by design. Internal code (anything inside a subpackage)
importing a facade is the decay mode this check exists to catch.

Function-level imports that cross layers upward are load-order seams the
Phase-1 move made deliberately (late binding so kernel does not hard-import
transport/state/agents at module scope). They are frozen in ALLOWED_FN_EDGES:
a new one fails, a removed one fails until it is deleted from the list.

Pure stdlib + ast over the source tree; imports nothing from desmos, so it
can never be broken by the breakage it reports.
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent  # .../desmos

LAYERS = ("kernel", "transport", "state", "agents", "front")

# Which internal layers each internal layer may import (module scope or not).
ALLOWED = {
    "kernel": {"kernel"},
    "transport": {"kernel", "transport"},
    "state": {"kernel", "state"},
    "agents": {"kernel", "transport", "state", "agents"},
    "front": {"kernel", "transport", "state", "agents", "front"},
}

# Real top-level modules that are not facades and not checks. A new
# top-level impl module is a facade-contract violation, not a convenience.
TOP_LEVEL_REAL = {"desmos", "desmos.__main__", "desmos.ext", "desmos.vision"}

# Frozen: every function-level import that crosses layers upward, as of the
# Phase-1 move. These are load-order seams (late binding), commented at their
# sites. Adding one is a design decision — it fails here until frozen.
ALLOWED_FN_EDGES = {
    ("desmos.kernel.catalog", "desmos.skills"),
    ("desmos.kernel.catalog", "desmos.state.memory"),
    ("desmos.kernel.catalog", "desmos.state.persist"),
    ("desmos.kernel.catalog", "desmos.transport.dialect"),
    ("desmos.kernel.dispatch", "desmos.state.refine"),
    ("desmos.kernel.dispatch", "desmos.state.persist"),
    ("desmos.kernel.canonical", "desmos.skills"),  # <harness op=skill> loader
    ("desmos.kernel.canonical", "desmos.state.find"),  # find engine + edit frecency touch
    ("desmos.kernel.canonical", "desmos.state.recall"),  # recall shells the memex-desmos fork
    ("desmos.kernel.canonical", "desmos.state.generations"),
    ("desmos.kernel.canonical", "desmos.state.memory"),
    ("desmos.kernel.canonical", "desmos.state.refine"),
    ("desmos.kernel.canonical", "desmos.agents.subagent"),
    ("desmos.kernel.canonical", "desmos.state.compact"),
    ("desmos.kernel.canonical", "desmos.state.persist"),
    ("desmos.kernel.canonical", "desmos.state.plan"),  # <knowledge op=plan> store
    ("desmos.kernel.canonical", "desmos.state.decisions"),  # <knowledge op=decide> store
    ("desmos.kernel.loop", "desmos.state.decisions"),  # decide: answer ingestion seam
    ("desmos.kernel.canonical", "desmos.transport.complete"),
    ("desmos.kernel.exec", "desmos.state.persist"),
    ("desmos.state.persist", "desmos.front.herdr"),  # Herdr pane sidebar seam
    ("desmos.kernel.loop", "desmos.agents.pending"),  # resume seam
    ("desmos.kernel.loop", "desmos.state.plan"),  # plan rail answers a stop
    ("desmos.kernel.loop", "desmos.state.budget"),  # money ceiling answers a stop
    ("desmos.kernel.loop", "desmos.state.persist"),  # per-call usage ledger
    ("desmos.kernel.shell", "desmos.agents.pending"),  # monitor hand-off
    ("desmos.kernel.loop", "desmos.agents.subagent"),  # reload_sdk emitter/RUNS rebind
    ("desmos.kernel.loop", "desmos.skills"),
    ("desmos.kernel.loop", "desmos.state.extensions"),
    ("desmos.kernel.loop", "desmos.state.generations"),
    ("desmos.kernel.loop", "desmos.state.persist"),
    ("desmos.kernel.loop", "desmos.transport.complete"),
    ("desmos.kernel.loop", "desmos.transport.dialect"),
    ("desmos.kernel.loop", "desmos.transport.settings"),
    ("desmos.transport.complete", "desmos.skills"),  # filter_skill_dialects, pure text
    ("desmos.transport.complete", "desmos.state.persist"),  # attach cache identity
    ("desmos.transport.openai", "desmos.skills"),  # filter_skill_dialects, pure text
    ("desmos.transport.openai", "desmos.state.persist"),  # provider session identity
    ("desmos.front.cli", "desmos.check"),  # the `check` subcommand runs the floor
}


def _modules() -> dict[str, Path]:
    mods: dict[str, Path] = {}
    for p in sorted(PKG_ROOT.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = ("desmos",) + p.relative_to(PKG_ROOT).with_suffix("").parts
        if rel[-1] == "__init__":
            rel = rel[:-1]
        mods[".".join(rel)] = p
    return mods


def _edges(mod: str, path: Path, known: set[str]) -> set[tuple[str, bool]]:
    """(target, fn_level) desmos-edges of one module, from its AST."""
    is_pkg = path.name == "__init__.py"
    out: set[tuple[str, bool]] = set()

    def resolve_from(node: ast.ImportFrom) -> list[str]:
        m = node.module or ""
        if node.level:
            parts = mod.split(".") if is_pkg else mod.split(".")[:-1]
            parts = parts[: len(parts) - node.level + 1]
            m = ".".join(parts + m.split(".")) if m else ".".join(parts)
        if m.split(".")[0] != "desmos":
            return []
        # `from desmos.x import y` imports desmos.x.y when y is a module;
        # the bare package edge only exists for names that are not modules
        hits = [f"{m}.{a.name}" for a in node.names if f"{m}.{a.name}" in known]
        if len(hits) < len(node.names):
            hits.append(m)
        return hits

    def walk(node: ast.AST, fn: bool) -> None:
        for child in ast.iter_child_nodes(node):
            in_fn = fn or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            if isinstance(child, ast.Import):
                for a in child.names:
                    if a.name.split(".")[0] == "desmos":
                        out.add((a.name, fn))
            elif isinstance(child, ast.ImportFrom):
                for t in resolve_from(child):
                    out.add((t, fn))
            walk(child, in_fn)

    walk(ast.parse(path.read_text()), False)
    return out


def _classify(mod: str, path: Path, known: set[str]) -> str:
    """kernel/transport/state/agents/front | checks | facade | sdk | skill-data."""
    parts = mod.split(".")
    if len(parts) == 1:
        return "sdk"
    sub = parts[1]
    if sub == "skills":
        # desmos.skills itself is state code; deeper files are skill payloads,
        # stored-state-like consumers of the public SDK (facades) by design.
        return "state" if len(parts) == 2 else "skill-data"
    if sub in LAYERS:
        return sub
    if sub == "checks" or sub == "check" or sub.endswith("_check"):
        return "checks"
    # facade: module-scope import of exactly desmos.<layer>.<own name>
    for tgt, fn in _edges(mod, path, known):
        t = tgt.split(".")
        if not fn and len(t) == 3 and t[1] in LAYERS and t[2] == sub:
            return "facade"
    if mod in TOP_LEVEL_REAL:
        return "sdk"
    return "unknown"


def _target_layer(tgt: str, classes: dict[str, str]) -> str:
    if tgt in classes:
        return classes[tgt]
    # dotted target not itself a module we walked (e.g. desmos.skills.x)
    parts = tgt.split(".")
    if len(parts) >= 2 and parts[1] == "skills":
        return "state"
    if len(parts) >= 2 and parts[1] in LAYERS:
        return parts[1]
    return classes.get(".".join(parts[:2]), "unknown")


def self_check() -> None:
    mods = _modules()
    known = set(mods)
    classes = {m: _classify(m, p, known) for m, p in mods.items()}

    bad: list[str] = []
    for m, cls in sorted(classes.items()):
        if cls == "unknown":
            bad.append(
                f"{m}: top-level module that is neither a facade, a check, nor "
                f"one of {sorted(TOP_LEVEL_REAL)} — the top level is the SDK facade, "
                f"implementation lives in a subpackage"
            )

    seen_fn_upward: set[tuple[str, str]] = set()
    for m, p in sorted(mods.items()):
        cls = classes[m]
        if cls not in ALLOWED:  # facades, sdk, checks, skill payloads: exempt
            continue
        for tgt, fn in sorted(_edges(m, p, known)):
            tl = _target_layer(tgt, classes)
            if tl in ("facade", "sdk"):
                bad.append(
                    f"{m} ({cls}) imports {tgt} ({tl}): internal code imports "
                    f"real subpackage paths, never a facade"
                )
            elif tl in ALLOWED[cls]:
                continue
            elif fn:
                seen_fn_upward.add((m, tgt))
                if (m, tgt) not in ALLOWED_FN_EDGES:
                    bad.append(
                        f"{m} ({cls}) -> {tgt} ({tl}): new function-level upward "
                        f"import; layering is {cls} -> {sorted(ALLOWED[cls])} only. "
                        f"If this is a deliberate load-order seam, freeze it in "
                        f"desmos/checks/layering.py:ALLOWED_FN_EDGES"
                    )
            else:
                bad.append(
                    f"{m} ({cls}) -> {tgt} ({tl}) at module scope: "
                    f"{cls} may import {sorted(ALLOWED[cls])} only"
                )

    for edge in sorted(ALLOWED_FN_EDGES - seen_fn_upward):
        bad.append(
            f"stale allowlist entry {edge[0]} -> {edge[1]}: edge no longer in "
            f"the tree, delete it from ALLOWED_FN_EDGES"
        )

    if bad:
        raise AssertionError("layering violations:\n  " + "\n  ".join(bad))

    n_fac = sum(1 for c in classes.values() if c == "facade")
    print(
        f"layering check ok ({len(mods)} modules, {n_fac} facades, "
        f"{len(ALLOWED_FN_EDGES)} acknowledged fn-level upward edges)"
    )
    for src, tgt in sorted(ALLOWED_FN_EDGES):
        print(f"  acknowledged: {src} -> {tgt}")


if __name__ == "__main__":
    self_check()
