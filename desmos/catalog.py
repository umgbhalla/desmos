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
    # Capabilities the code has and the catalog never said out loud, plus the
    # working style the driving model's family actually responds to. Last, so
    # it reads as instruction rather than reference.
    from desmos.dialect import block as dialect_block

    lines.append(dialect_block(world))
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
            f"  subagents: {root / 'docs' / 'subagents.md'}",
            f"project_skills: {world.cwd / '.desmos' / 'skills'}",
            f"user_skills: {home / '.desmos' / 'skills'}",
            f"shared_skills: {home / '.agents' / 'skills'}",
            f"project_extensions: {world.cwd / '.desmos' / 'extensions'}",
            f"user_extensions: {home / '.desmos' / 'extensions'}",
            "tui: python -m desmos tui",
            "  middle: story only — you typed, Thought (collapsed), redacted Thought (no ciphertext), speech markdown. XML stripped from speech.",
            "  right: wire only — syscall <tag> cards with the actual body plus result. Cards start Expanded so the payload is visible; h folds. The YOU/MODEL POST #n rows (usage + thinking/redacted counts, not the thought text) are hidden by default; the border title carries a [+posts]/[-posts] chip — click it, or press p with the calls pane focused, to fold them back into place where they happened. Do not copy speech onto this pane.",
            "  bottom split: last complete() POST in / POST out as a foldable JSON tree (keys, typed values, ▸/▾ objects and arrays). Long strings stay collapsed until you open that row. Redacted thinking ciphertext is [redacted]. Keys never appear. j/k select, h/l fold. Ctrl-F / e / double-click expands that side into a mid popup (grok modal, in/out tabs). Tree is the same foldable tree at the popup width; r swaps in grok BlockViewerPane over pretty-printed raw JSON (search, wrap, copy). Esc / q / Ctrl-F close. y copies the raw body of the focused tab.",
            "  queue: while a step is running, Enter stacks a follow-up (grok pending_prompts). After the step settles the front runs. Empty Enter (or Enter on a queue row) is send-now: stop the current step and fire the front. [ ] reorder, d deletes, e lifts a row into the composer to edit (Enter returns it to its slot, empty Enter drops it). Tab reaches the queue pane only when it is non-empty — an empty queue has height 0 and is skipped.",
            "  meta: bottom-right pane. Row 1 is the turn — braille spinner, thinking / responding / run <tag> / waiting / stopping, phase elapsed / turn elapsed, live subagent count; it is drawn idle too so the meters never shift. Then ctx (a sequence bar coloured by kind, in trajectory order), cache read-vs-write, cost, then model / effort / gen, then the theme name with swatches in the accents blocks are painted in (Oscura Midnight at launch unless GROK_THEME or the grok config names another; /theme switches). A queued model switch takes the swatch row while pending. Squeezed to one row the pane shows ctx only. There is no separate turn-status band and no key legend; the wire column runs to the last row and the calls pane took that space. ctrl+g / ctrl+b open and close the git and file panes from anywhere, composer included.",
            "  input: under the POST split, bare-bordered — [stop] rides its top edge while a step runs (click cancels, same as Ctrl-C), and identity moved to meta. It grows with what you type, measured at the story column width, up to half the column; wrapping breaks between words. Tab/Shift-Tab cycles story → calls → meta → git → files → POST in → POST out → queue → input; a pane collapsed to zero rows is skipped, and an empty queue always is. `?` in any pane but the composer floats that pane's key cheatsheet; the next key closes it. While a step is running, typing a follow-up stays in the composer until Enter (status says enter queues); empty Enter is send now. j/k or arrows highlight a block. h/l or ←/→ folds (Collapsed / Truncated / Expanded). Enter / Ctrl-F zooms the selected block into grok BlockViewerPane (ListPane wrap at the popup width: / search, f filter, v select, w wrap, r raw). Esc / q / Ctrl-F close. Spawn still wins first: Enter on a SubagentBlock opens that spawn session. Terminal font zoom is a resize — viewer and scrollback re-wrap via grok prepare_layout, not a second renderer. click selects a block. Drag selects text (grok overlay, copies on release). Double-click on text copies the word; double-click on chrome folds (or opens a spawn). Esc clears a text selection. Wheel scrolls the pane under the cursor.",
            "  spawn: parent story gets grok SubagentBlock. spawn is async so Started stays and a Completed/Failed row is appended. Enter opens the child session (its own story + calls). Esc returns. Child thinking/speech never land on the parent story.",
            "  blocks: you = UserPrompt, think = Thinking (starts collapsed), speech = AgentMessage (grok markdown), wire = ToolCall, spawn = SubagentBlock. Never stamp everything 'out'.",
            "  markdown: grok-build AgentMessageBlock via xai_grok_markdown pretty + syntect tokyo-night. Speak markdown. Never emit angle-bracket tags in prose.",
            "  acp: python -m desmos tui --grok attaches grok-build pager via NDJSON JSON-RPC 2.0 (not Content-Length).",
            "transcript: world.messages is append-only within a session — nothing already sent is rewritten or reordered. step() continues it. Syscall output arrives as user <result> blocks — not a restated task. Never write a result block in your own speech. Two explicit exceptions: what survives a process restart is the tail persist kept, not the whole chat; and reset() (the TUI reset op) drops the chat outright so a poisoned turn cannot train the next one.",
            "compaction: server-side (beta compact-2026-01-12, strategy compact_20260112) on adaptive models. Past the trigger the API folds earlier turns and returns a compaction block inside the assistant message; that block is replayed and replaces everything before it on the next POST. Nothing local is rewritten and the ABI/catalog cache blocks are untouched, so a fold never invalidates the cached prefix. A fold emits ev compacted and paints a FOLD card on the wire pane. Earlier turns you cannot see verbatim were folded, not lost — do not restate them.",
            "complete: Opus 5 is adaptive thinking + output_config.effort (default low). Older Claude 4 uses a token budget + interleaved thinking. Thinking/redacted blocks are replayed on the wire, not restated as speech. Live: POST in is emitted before the HTTP body; thinking and speech stream as the model writes (grok StreamingMarkdownRenderer / thinking_streaming). A syscall card opens when the tag starts; bash/python stdout streams into that Execute card; the user <result> is the finished output. The TUI paints those events as they arrive — a turn is not a single paint at the end.",
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
    """Small routing summary; detailed durable memories stay tool-retrievable."""
    from desmos.memory import prompt_summary

    return prompt_summary(world, budget)
