from __future__ import annotations

import inspect
from pathlib import Path

from desmos.const import ABI, HIDDEN_NS, PRIOR_KEEP
from desmos.persist import state_file
from desmos.scan import clip
from desmos.types import World


def package_root() -> Path:
    return Path(__file__).resolve().parent


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


def catalog(world: World) -> str:
    from desmos.const import FROZEN

    lines = ["# tools"]
    for name in (*sorted(t for t in world.tools if t in FROZEN), *sorted(t for t in world.tools if t not in FROZEN)):
        tool = world.tools[name]
        flag = " frozen" if tool.frozen else ""
        lines.append(f"<{name}>{flag} {tool.doc}")
    if world.notes:
        lines.append("# your notes")
        for key, note in world.notes.items():
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
    return "\n".join(lines)


def runtime_block(world: World) -> str:
    """Live facts so the agent can reload and unstick — Pi puts cwd/docs in system."""
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
            f"  extensibility: {root / 'docs' / 'extensibility.md'}",
            f"  self-growth: {root / 'docs' / 'self-growth.md'}",
            f"examples: {root / 'examples'}",
            f"project_skills: {world.cwd / '.desmos' / 'skills'}",
            f"user_skills: {home / '.desmos' / 'skills'}",
            f"shared_skills: {home / '.agents' / 'skills'}",
            f"project_extensions: {world.cwd / '.desmos' / 'extensions'}",
            f"user_extensions: {home / '.desmos' / 'extensions'}",
            "tui: python -m desmos tui",
            "  middle: turn story (you / think / speech). speech is markdown.",
            "  right: wire only — complete() cards and XML syscalls. USER = your enter started the POST. LLM = the model called again. Do not narrate the same sentences into both panes.",
            "  bottom: input. Tab/Shift-Tab cycles story → calls → input. j/k or arrows highlight a block. h/l or ←/→ or Enter folds (Collapsed / Truncated / Expanded). r = raw markdown. click selects, double-click folds, wheel scrolls the pane under the cursor.",
            "  blocks: you = UserPrompt, think = Thinking (starts collapsed), speech = AgentMessage (grok markdown), wire = ToolCall. Never stamp everything 'out'.",
            "  markdown: grok-build AgentMessageBlock via xai_grok_markdown pretty + syntect tokyo-night. Speak markdown. Never emit angle-bracket tags in prose.",
            "  acp: python -m desmos tui --grok attaches grok-build pager via NDJSON JSON-RPC 2.0 (not Content-Length).",
            "transcript: world.messages is append-only. step() continues it. Syscall output arrives as user <result> blocks — not a restated task. Never write a result block in your own speech.",
            "complete: Opus 5 is adaptive thinking + output_config.effort (default low). Older Claude 4 uses a token budget + interleaved thinking. Thinking/redacted blocks are replayed on the wire, not restated as speech.",
            "reload: every turn rediscovers skills/extensions. After writing SKILL.md this turn, emit <reload/> then <skill name=\"…\"/>, or wait one turn.",
            "reload_sdk: <reload_sdk/> reimports desmos.*, reseeds missing builtins, rebinds step. Does not wipe ns, notes, or messages. New ABI/loop apply on the next complete().",
            "edit: <edit path=\"file\">old\\n---\\nnew</edit> — exactly one occurrence. Relative paths are cwd-relative. Or edit.run(path, old, new).",
            "grow: write .desmos/skills/<name>/SKILL.md (optional handle/run). <system name> for doctrine. <tool> to rewrite a description. Then <evolve>why</evolve>.",
            "unstick: read the error, fix attrs, retry. Unknown tag → register it first. Nameless <system> writes note \"note\".",
            "rollback: <rollback n=\"1\"/>. Read the docs above before changing the harness.",
        ]
    )


def system_prompt(world: World) -> str:
    return ABI + "\n\n" + catalog(world)


def header(world: World, task: str) -> str:
    lines = [f"generation: {world.generation} ({world.gen_reason})", f"cwd: {world.cwd}", ns_index(world)]
    if world.prior:
        lines.append("prior steps:")
        for i, item in enumerate(world.prior[-PRIOR_KEEP:], 1):
            lines.append(f"  {i}. user: {clip(item['prompt'], 240)}")
            lines.append(f"     you: {clip(item['speech'], 400)}")
    lines.append(f"prompt: {task}")
    return "\n".join(lines)


def memory_block(world: World, budget: int = 2000) -> str:
    """Tail of .desmos/MEMORY.md — durable episodes, not doctrine.

    On disk it is theoretically durable; in the prompt it is actually
    consulted. Tail-only and capped so an append-only log can never
    crowd out the transcript.
    """
    path = state_file(world).parent / "MEMORY.md"
    try:
        text = path.read_text().strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) > budget:
        text = "...\n" + text[-budget:]
    return "# memory (durable, newest last)\n" + text
