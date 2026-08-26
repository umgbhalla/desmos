from __future__ import annotations

import inspect
from pathlib import Path

from desmos.kernel.const import ABI, HIDDEN_NS
from desmos.kernel.types import World


def package_root() -> Path:
    """The `desmos/` package dir — the facade surface, which is the public SDK."""
    return Path(__file__).resolve().parent.parent


def repo_root() -> Path:
    return package_root().parent


def skip_name(name: str) -> bool:
    return name in HIDDEN_NS or name.startswith("_")


def ns_names(world: World) -> list[str]:
    names = []
    for k, v in world.ns.items():
        if skip_name(k) or inspect.ismodule(v):
            continue
        names.append(k)
    return sorted(names)


def shape_of(value: object) -> str:
    if isinstance(value, str):
        return f"str, {len(value)} chars"
    if isinstance(value, (bytes, bytearray)):
        return f"{type(value).__name__}, {len(value)} bytes"
    if isinstance(value, (list, tuple, set, dict)):
        return f"{type(value).__name__}, len={len(value)}"
    shape = getattr(value, "shape", None)
    if shape is not None:
        return f"{type(value).__name__} shape={shape}"
    return type(value).__name__


def ns_index(world: World) -> str:
    names = ns_names(world)
    if not names:
        return "ns: (empty)"
    lines = ["ns:"]
    for name in names:
        lines.append(f"  {name}: {shape_of(world.ns.get(name))}")
    return "\n".join(lines)


# Notes that change many times inside one session. They live at the tail of the
# prompt, after every cache breakpoint, because a byte changed anywhere in the
# cached system blocks re-writes the whole prefix behind it -- measured at
# ~$0.85 per todo tick at 130k tokens of context.
VOLATILE_NOTES = ("todo", "anchors")
VOLATILE_MARKER = "# now"


def todo_digest(text: str) -> str:
    """Open items only, keeping their real numbers. Done work is not context."""
    rows = [r for r in text.splitlines() if r.strip()]
    live = [f"{i}. {r}" for i, r in enumerate(rows, 1) if not r.lstrip().startswith("[x]")]
    if not live:
        return ""
    done = len(rows) - len(live)
    return "\n".join(live) + (f"\n({done} done)" if done else "")


VOLATILE_VIEW = {"todo": todo_digest}


def inject(world: World, name: str, text: str, turns: int = 1) -> str:
    """Put a named block in the next turn's uncached tail.

    turns counts renders: 1 is the next request only, and 0 or less stays
    until retired. Editing the middle of a cached prefix destroys it, so a
    steering block belongs here and nowhere else.
    """
    world.injections[str(name)] = {"text": str(text), "turns": int(turns)}
    return str(name)


def retire(world: World, name: str) -> bool:
    """Drop a named block before its lifetime runs out."""
    return world.injections.pop(str(name), None) is not None


def expire(world: World) -> list[str]:
    """One turn spent. Returns the names that just fell out."""
    gone = []
    for name, item in list(world.injections.items()):
        turns = int(item.get("turns") or 0)
        if turns <= 0:
            continue
        if turns <= 1:
            world.injections.pop(name, None)
            gone.append(name)
        else:
            item["turns"] = turns - 1
    return gone


def steer(world: World, text: str) -> int:
    """Queue a line for the model to read at its next result."""
    line = str(text).strip()
    if line:
        world.steers.append(line)
    return len(world.steers)


def drain_steers(world: World) -> list[str]:
    """Take everything queued. The caller owns delivery."""
    queued = list(world.steers)
    world.steers.clear()
    return queued


def volatile(world: World, delta: str = "") -> str:
    """Per-turn mutable state, deliberately outside the cached prefix."""
    parts = []
    try:
        from desmos.state.persist import channel_notice

        notice = channel_notice(world)
    except Exception:  # noqa: BLE001 -- notification failure cannot block a turn
        notice = ""
    if notice:
        parts.append("[channel]\n" + notice)
    for key in VOLATILE_NOTES:
        raw = world.notes.get(key) or ""
        view = VOLATILE_VIEW.get(key, str)(raw)
        if view.strip():
            parts.append(f"[{key}]\n{view}")
    for name, item in world.injections.items():
        body = str(item.get("text") or "").strip()
        if body:
            parts.append(f"[{name}]\n{body}")
    if delta.strip():
        parts.append(CATALOG_DELTA_HEADER + "\n" + delta)
    return VOLATILE_MARKER + "\n" + "\n".join(parts) if parts else ""



def advertised_names(world: World) -> list[str]:
    """Canonical families plus truly custom tools; never compatibility aliases."""
    from desmos.kernel.const import CANONICAL, REMOVED_TAGS

    canonical = sorted(name for name in world.tools if name in CANONICAL)
    custom = sorted(
        name for name in world.tools
        if name not in CANONICAL and name not in REMOVED_TAGS
    )
    return [*canonical, *custom]

def catalog(world: World) -> str:
    lines = ["# tools"]
    for name in advertised_names(world):
        tool = world.tools[name]
        flag = " frozen" if tool.frozen else ""
        lines.append(f"<{name}>{flag} {tool.doc}")
    stable = [(k, v) for k, v in world.notes.items() if k not in VOLATILE_NOTES]
    if stable:
        lines.append("# your notes")
        for key, note in stable:
            lines.append(f"[{key}]\n{note}")
    if world.skills:
        from desmos.skills import format_skills_for_prompt

        block = format_skills_for_prompt(world.skills)
        if block:
            lines.append(block)
    mem = memory_block(world)
    if mem:
        lines.append(mem)
    lines.append(runtime_block(world))
    # Capabilities the code has and the catalog never said out loud, plus the
    # working style the driving model's family actually responds to. Last, so
    # it reads as instruction rather than reference.
    from desmos.transport.dialect import block as dialect_block

    lines.append(dialect_block(world))
    return "\n".join(lines)


def runtime_block(world: World) -> str:
    """Live facts so the agent can reload and unstick — Pi puts cwd/docs in system."""
    from desmos.state.persist import state_file

    cwd = str(world.cwd.resolve())
    root = repo_root()
    sdk = package_root()
    home = Path.home()
    return "\n".join(
        [
            "# runtime",
            f"cwd: {cwd}",
            f"generation: {world.generation} ({world.gen_reason})",
            f"model: {world.model}",
            f"thinking: {world.thinking}",
            f"harness_state: {state_file(world)}",
            f"generations_dir: {state_file(world).parent / 'generations'}",
            "identity: state is layered; docs/identity.md is the full inventory of what survives which reset.",
            "  repo-durable (.desmos/, beside harness_state): the harness db (transcript tail, notes, grown tools, generation, events), memories/records.jsonl, generations/, subagents/, pending/, plans/, decisions/, trajectory/ (self-pruning).",
            "  machine-global (~/.desmos/): settings.json (provider/model/effort — one file for every checkout), auth.json, user skills and extensions. rm -rf .desmos does not touch them.",
            "  in memory only: ns values, messages beyond the persisted tail, shells, pending monitors. reload_sdk keeps them; a process restart does not.",
            "  harness op=rollback restores notes/tools/prior only; it never touches messages, files, or memory records.",
            "  a fork (persist=False child) inherits none of the db and writes none back (plan/decide JSONL included). What future turns need goes in a memory record, a note, or a file — never only in speech or ns.",
            f"sdk: {sdk}",
            f"  ABI: {sdk / 'const.py'}",
            f"  catalog: {sdk / 'catalog.py'}",
            f"  loop: {sdk / 'loop.py'}",
            f"  dispatch: {sdk / 'dispatch.py'}",
            f"  edit: {sdk / 'edit.py'}",
            f"  complete: {sdk / 'complete.py'}",
            f"  generations: {sdk / 'generations.py'}",
            f"readme: {root / 'README.md'}",
            f"docs: {root / 'docs'}",
            f"  design: {root / 'docs' / 'design.md'}",
            f"  tags: {root / 'docs' / 'tags.md'}",
            f"  extensibility: {root / 'docs' / 'extensibility.md'}",
            f"  self-growth: {root / 'docs' / 'self-growth.md'}",
            f"  subagents: {root / 'docs' / 'subagents.md'}",
            f"  identity: {root / 'docs' / 'identity.md'}",
            f"project_skills: {world.cwd / '.desmos' / 'skills'}",
            f"user_skills: {home / '.desmos' / 'skills'}",
            f"shared_skills: {home / '.agents' / 'skills'}",
            f"project_extensions: {world.cwd / '.desmos' / 'extensions'}",
            f"user_extensions: {home / '.desmos' / 'extensions'}",
            # A human watches this session in a TUI, but the agent presses no
            # keys and reads no panes — every line of pane/keymap lore was ~1200
            # tokens the agent paid each POST and could never act on. The two
            # facts it needs (speak markdown; syscalls are visible on the wire,
            # so do not restate them in speech) already live in the ABI. The
            # full surface, for a human, is docs/design.md.
            "transcript: world.messages is append-only within a session — nothing already sent is rewritten or reordered. step() continues it. Syscall output arrives as user <result> blocks — not a restated task. Never write a result block in your own speech. Two explicit exceptions: what survives a process restart is the tail persist kept, not the whole chat; and reset() (the TUI reset op) drops the chat outright so a poisoned turn cannot train the next one.",
            "compaction: server-side (beta compact-2026-01-12, strategy compact_20260112) on adaptive models. Past the trigger the API folds earlier turns and returns a compaction block inside the assistant message; that block is replayed and replaces everything before it on the next POST. Nothing local is rewritten and the ABI/catalog cache blocks are untouched, so a fold never invalidates the cached prefix. A fold emits ev compacted and paints a FOLD card on the wire pane. Earlier turns you cannot see verbatim were folded, not lost — do not restate them.",
            "complete: Opus 5 is adaptive thinking + output_config.effort (default low). Older Claude 4 uses a token budget + interleaved thinking. Thinking/redacted blocks are replayed on the wire, not restated as speech. Live: POST in is emitted before the HTTP body; thinking and speech stream as the model writes (grok StreamingMarkdownRenderer / thinking_streaming). A syscall card opens when the tag starts; bash/python stdout streams into that Execute card; the user <result> is the finished output. The TUI paints those events as they arrive — a turn is not a single paint at the end.",
            "harness reload: op=reload rediscovers skills/extensions; op=skill loads one by name.",
            "harness reload-sdk: reimports desmos.*, reseeds builtins, and rebinds step without wiping state.",
            "workspace edit: op=edit, path=, and body old\\n---\\nnew replaces exactly one occurrence.",
            "python diagnostics: diag.error() returns the automatically recorded last uncaught exception; diag.symbol(obj, source=False) gives bounded location/signature/source metadata; diag.threads(pattern=None) gives bounded file/function/line stacks without locals.",
            "find: fff search over cwd. mode=path (default, fuzzy/typo-resistant paths + inline glob constraints), glob, grep, symbol (definitions first + usages), or multi (newline-separated patterns; optional constraints=). grep/symbol/multi accept match=plain|regex|fuzzy and context=N; limit=20. Your edits feed frecency. Absent build names scripts/build-fff-python.sh.",
            "knowledge recall: op=recall searches prior-session history; limit and mode are optional.",
            "grow: write a SKILL.md, then use harness op=reload or op=skill; knowledge op=system stores doctrine.",
            "unstick: read the error, fix attrs, retry; use harness op=register only for repeated operations.",
            "rollback: harness op=rollback with n=1. Read the docs before changing the harness.",
        ]
    )


# Past this much delta the frozen copy has drifted far enough that a reader
# would be reconstructing the catalog by hand. Refresh and pay the one rewrite.
CATALOG_DELTA_LIMIT = 4000
CATALOG_DELTA_HEADER = "# catalog changed since the block above -- these lines win"


def catalog_diff(frozen: str, live: str) -> str:
    import difflib

    rows = [
        line
        for line in difflib.unified_diff(
            frozen.splitlines(), live.splitlines(), n=0, lineterm=""
        )
        if not line.startswith(("---", "+++", "@@"))
    ]
    return "\n".join(rows)


def catalog_frozen(world: World) -> tuple[str, str]:
    """The catalog as first sent this run, plus what changed since.

    The cached prefix runs tools -> system -> messages, so one byte moved inside
    the catalog block re-writes every message token behind it. Holding the block
    still and shipping the difference at the tail costs a few hundred tokens
    instead of the whole prefix.
    """
    live = catalog(world)
    frozen = getattr(world, "catalog_frozen", "") or ""
    if not frozen or frozen == live:
        world.catalog_frozen = live
        return live, ""
    delta = catalog_diff(frozen, live)
    if len(delta) > CATALOG_DELTA_LIMIT:
        world.catalog_frozen = live
        return live, ""
    return frozen, delta


def system_prompt(world: World) -> str:
    body, delta = catalog_frozen(world)
    tail = volatile(world, delta)
    return ABI + "\n\n" + body + (("\n\n" + tail) if tail else "")


def header(world: World) -> str:
    """Dynamic per-step context not already present in the system runtime/history."""
    return ns_index(world)


def memory_block(world: World, budget: int = 2000) -> str:
    """Small routing summary; detailed durable memories stay tool-retrievable."""
    from desmos.state.memory import prompt_summary

    return prompt_summary(world, budget)
