from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from desmos.catalog import header, ns_names, system_prompt
from desmos.complete import (
    LAST,
    assistant_content,
    cached_payload,
    compaction_block,
    complete,
    redact_wire,
    text_of,
    thought_blocks,
    thinking_text,
)
from desmos.const import FROZEN, MAX_TOKENS, PRIOR_KEEP
from desmos.dispatch import dispatch
from desmos.dialect import family, tool_syscalls
from desmos.generations import ensure_gen1, evolve, rollback
from desmos.persist import load, save
from desmos import pending
from desmos.scan import clip, dropped_openers, scan, scan_spans, trailing_residue
from desmos.spill import spill
from desmos.types import Block, Tool, World


def format_results(results: list[tuple[Block, str]]) -> str:
    chunks = []
    for b, r in results:
        attr = " ".join(f'{k}="{v}"' for k, v in b.attrs.items())
        label = f"<{b.tag} {attr}>".strip() if attr else f"<{b.tag}>"
        chunks.append(f"{label} ->\n{r}")
    return "\n\n".join(chunks)


#: How much of a syscall's output reaches the model. dispatch clips a handler
#: at scan.RESULT_CAP first; this is the tighter cap that actually bounds the
#: transcript, and it is the number the prompt quotes -- check.py ties the two
#: together so the prompt cannot drift off it.
RESULT_CLIP = 6000


def format_result_message(results: list[tuple[Block, str]], cwd: Path | None = None) -> str:
    parts = []
    for b, r in results:
        body = spill(r, RESULT_CLIP, tag=b.tag, cwd=cwd)
        parts.append(f'<result tag="{b.tag}">{body}</result>')
    return "\n\n".join(parts)


def malformed_call_note(raw: str, stray: list[str]) -> str:
    """What the model reads when its syscall input did not parse.

    It has to say three things or the retry is blind: nothing ran, which text
    was outside the tags, and what a well-formed call looks like. The commonest
    cause is a closing tag inside a body, which truncates the call at that byte
    and dumps the remainder into the gap this reports.
    """
    if not stray:
        detail = "no complete syscall was found in it"
    else:
        shown = " … ".join(clip(s.strip(), 200) for s in stray[:3])
        detail = f"text outside any tag: {shown!r}"
    return (
        "[syscall input rejected — nothing ran. The input must be complete XML syscalls and"
        f" nothing else; {detail}. Every tag must be opened and closed, and a body must never"
        " contain its own closing tag (build it by concatenation instead). Send the call again.]"
    )


def normalize_syscall_input(value: Any) -> tuple[str, str | None]:
    """Normalize only unambiguous custom-tool input collections.

    Responses custom tools normally return one string. Some clients expose the
    same commands as an array of strings, and a model can also place that array
    in the custom string as JSON. Structural commas become newlines; prose or
    non-string entries remain an error so validation stays atomic.
    """
    if isinstance(value, list):
        if all(isinstance(part, str) for part in value):
            return "\n".join(value), None
        return repr(value), "syscall input arrays may contain only strings"
    if not isinstance(value, str):
        return repr(value), "syscall input must be a string or an array of strings"

    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return value, None
        if isinstance(parsed, list):
            if all(isinstance(part, str) for part in parsed):
                return "\n".join(parsed), None
            return value, "syscall input arrays may contain only strings"
    return value, None


#: The one assistant block a turn may carry that is a call rather than speech.
#: Responses calls it custom_tool_call and keys it by call_id; Anthropic calls
#: it tool_use and keys it by id. Same contract, two wires.
CALL_TYPES = {"custom_tool_call": "call_id", "tool_use": "id"}


def syscall_call(assistant: list[dict[str, Any]]) -> dict[str, Any] | None:
    calls = [
        b
        for b in assistant
        if b.get("type") in CALL_TYPES and (b.get("name") or "syscall") == "syscall"
    ]
    if len(calls) > 1:
        raise RuntimeError("the model returned more than one syscall call")
    if calls:
        key = CALL_TYPES[calls[0]["type"]]
        if not calls[0].get(key):
            raise RuntimeError(f"the model returned a syscall call without {key}")
    return calls[0] if calls else None


def syscall_body(call: dict[str, Any]) -> Any:
    """The raw XML a call carries. Anthropic wraps it in a JSON object."""
    value = call.get("input")
    if call.get("type") == "tool_use":
        return value.get("input") if isinstance(value, dict) else value
    return value


def set_syscall_body(call: dict[str, Any], raw: str) -> None:
    """Write the normalized body back, keeping each wire's own shape.

    The next request replays this item verbatim, so a shape the endpoint would
    reject here is a 400 on every later turn, not just this one.
    """
    if call.get("type") == "tool_use":
        value = call.get("input")
        call["input"] = {**value, "input": raw} if isinstance(value, dict) else {"input": raw}
    else:
        call["input"] = raw


def result_content(
    results: list[tuple[Block, str]], assistant: list[dict[str, Any]], cwd: Path
) -> str | list[dict[str, Any]]:
    output = format_result_message(results, cwd)
    call = syscall_call(assistant)
    if call is None:
        return output
    if call["type"] == "tool_use":
        return [{"type": "tool_result", "tool_use_id": call["id"], "content": output}]
    return [{"type": "custom_tool_call_output", "call_id": call["call_id"], "output": output}]


_BUILTIN_DOCS = (
    ("python", "exec Python in the persistent kernel"),
    ("bash", "isolated one-shot command in cwd — no state kept; use only when reset is useful"),
    ("shell", "preferred persistent pty: id= names the session, state/processes survive; long commands are monitored and resume you when they land; interrupt=1, close=1"),
    ("edit", "replace one occurrence: path= and body old\\n---\\nnew"),
    ("register", "install a tag: name= and doc=, body is def handle"),
    ("system", "write or delete a system note (name=, optional delete=1)"),
    ("tool", "rewrite a tool description: name= and doc="),
    ("skill", "load full SKILL.md: name="),
    ("reload", "rediscover skills and extensions now"),
    ("reload_sdk", "reimport desmos.* and rebind step; next complete() uses the new ABI"),
    ("evolve", "snapshot grown state as the next generation"),
    ("rollback", "restore generation n="),
    (
        "memory",
        "structured durable memory: body remembers; actions show/search/read/forget/verify/consolidate",
    ),
)


def seed_builtins(world: World) -> None:
    for name, doc in _BUILTIN_DOCS:
        existing = world.tools.get(name)
        if existing is None:
            world.tools[name] = Tool(name, doc, frozen=True)
        else:
            existing.frozen = True


def install_resources(world: World) -> None:
    from desmos.extensions import load_extensions
    from desmos.skills import bind_python_skill, discover_skills

    world.skills = discover_skills(world.cwd)
    for skill in world.skills:
        fn = bind_python_skill(world.ns, skill)
        if callable(fn) and skill.import_name and skill.import_name not in FROZEN:
            world.tools[skill.import_name] = Tool(
                name=skill.import_name,
                doc=skill.description or f"skill {skill.name}",
                handler=fn,
            )
    api = load_extensions(world.cwd)
    world.hooks = api.hooks
    for name, doc, handler in api.tools:
        if name not in FROZEN:
            world.tools[name] = Tool(name=name, doc=doc, handler=handler)


def reload(world: World) -> str:
    install_resources(world)
    return f"reloaded {len(world.skills)} skills, {len(world.tools)} tools"


def turn(
    world: World,
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    n: int = 1,
    emit: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[str, list[tuple[Block, str]], bool, list[dict[str, Any]]]:
    def fire(ev: dict[str, Any]) -> None:
        if emit is not None:
            emit(ev)

    def stopped() -> bool:
        return should_stop is not None and should_stop()

    install_resources(world)
    if world.ns.get("world") is not world:
        bind_step(world)  # ns lost its handles (cleanup, reload, stale exec globals)
    system = getattr(world, "system_override", "") or system_prompt(world)
    built = cached_payload(
        world.model, system, messages, max_tokens, thinking=world.thinking
    )
    req = {k: v for k, v in built.items() if k != "_betas"}
    fire(
        {
            "ev": "post",
            "n": n,
            "origin": "user" if n == 1 else "llm",
            "model": world.model,
            "request": redact_wire(req),
        }
    )
    streamed = False

    def on_delta(delta: dict[str, Any]) -> None:
        nonlocal streamed
        streamed = True
        kind = delta.get("kind")
        if kind == "thinking_delta":
            fire(
                {
                    "ev": "thinking",
                    "redacted": False,
                    "text": delta.get("text") or "",
                    "delta": True,
                }
            )
        elif kind == "thinking":
            fire(
                {
                    "ev": "thinking",
                    "redacted": bool(delta.get("redacted")),
                    "text": delta.get("text") or "",
                    "delta": False,
                }
            )
        elif kind == "text_delta":
            fire({"ev": "speech", "text": delta.get("text") or "", "delta": True})

    if world.complete_fn:
        resp = world.complete_fn(world.model, system, messages, max_tokens)
    else:
        resp = complete(
            world.model,
            system,
            messages,
            max_tokens,
            thinking=world.thinking,
            on_event=on_delta,
            should_stop=should_stop,
        )
        req = dict(LAST.get("payload") or {})
    # The card must show the payload this call actually sent. Re-reading the
    # complete.LAST global after the round trip raced the subagent pool: a child
    # POST landing between our POST and this line put the child's system prompt
    # and messages on the parent's log entry and complete card. The pop matters
    # too -- redact_wire(resp) goes into world.log["response"] and straight out
    # on the complete event, so leaving _request there would nest the whole
    # outgoing payload inside the response card.
    sent = resp.pop("_request", None)
    if sent:
        req = dict(sent)
    speech = text_of(resp)
    assistant = assistant_content(resp)
    world.log.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "usage": resp.get("usage") or {},
            "stop": resp.get("stop_reason"),
            "text": speech,
            "thinking": thinking_text(assistant),
            "request": redact_wire(req),
            "response": redact_wire(resp),
        }
    )
    if len(world.log) > 1:
        # Only the newest entry's wire bodies are ever read -- by the complete
        # event three lines below, and by nothing else in the process. Keeping
        # every payload made world.log quadratic: 60 turns over a 490KB
        # transcript held 16MB of duplicated request and response. The full wire
        # history lives in the trajectory files, which prune themselves.
        # Entries stay: spent_from indexes this list and check reads log[-2].
        world.log[-2].pop("request", None)
        world.log[-2].pop("response", None)
    # Durable before anything runs. This used to be appended by the caller
    # after the dispatch loop, so a crash or a kill during a <bash> lost the
    # assistant turn that ordered it: the side effect had happened and the
    # transcript never asked for it.
    messages.append({"role": "assistant", "content": assistant})
    parts = thought_blocks(assistant)
    if not streamed:
        for part in parts:
            fire(
                {
                    "ev": "thinking",
                    "redacted": part["redacted"],
                    "text": part["text"],
                }
            )
        fire({"ev": "speech", "text": speech})
    n_thoughts = sum(1 for p in parts if not p["redacted"])
    n_redacted = sum(1 for p in parts if p["redacted"])
    usage = (world.log[-1].get("usage") if world.log else {}) or {}
    # A fold rewrites what the model remembers. That is the largest thing the
    # harness does to itself in a run, and without this event the only trace is
    # the context bar dropping for no stated reason.
    fold = compaction_block(assistant)
    if fold is not None:
        # The block's summary field is the server's, not ours. Read whichever
        # string it carries rather than asserting a shape; the trajectory log
        # has the exact wire block if this ever needs pinning down.
        summary = next(
            (v for k, v in fold.items() if k != "type" and isinstance(v, str) and v.strip()),
            "",
        )
        fire({"ev": "compacted", "n": n, "kept": len(messages), "text": summary})
    # What the model wrote after its last closing tag. Never rewritten -- the
    # stored message must stay byte-exact for the cached prefix -- but recorded,
    # so a degenerate suffix is visible the first time instead of the fiftieth.
    residue = trailing_residue(speech)
    if residue and world.log:
        world.log[-1]["residue"] = residue
    fire(
        {
            "ev": "complete",
            "n": n,
            "origin": "user" if n == 1 else "llm",
            "model": world.model,
            "thinking": world.thinking,
            "thoughts": n_thoughts,
            "redacted": n_redacted,
            "usage": usage,
            "residue": residue,
            "request": (world.log[-1] or {}).get("request") or {},
            "response": (world.log[-1] or {}).get("response") or {},
        }
    )
    results: list[tuple[Block, str]] = []
    recoverable = False
    call = syscall_call(assistant)
    if call:
        raw, shape_error = normalize_syscall_input(syscall_body(call) or "")
        # The next Responses request replays the provider item verbatim. Keep
        # that replay schema-valid even when this client exposed input as an
        # array or the model encoded a JSON array inside the custom string.
        set_syscall_body(call, raw)
        provider_call = call.get("openai")
        if isinstance(provider_call, dict) and provider_call.get("type") == "custom_tool_call":
            provider_call["input"] = raw
        spans = scan_spans(raw)
        stray: list[str] = [shape_error] if shape_error else []
        cursor = 0
        for index, (_, start, end) in enumerate(spans):
            gap = raw[cursor:start]
            # Whitespace always separates calls. A comma is also a separator,
            # but only between two complete calls — never as leading/trailing
            # punctuation that could hide malformed input.
            invalid = gap.strip() if index == 0 else gap.strip(" \t\r\n,")
            if invalid:
                stray.append(gap)
            cursor = end
        if raw[cursor:].strip():
            stray.append(raw[cursor:])
        if not spans or stray:
            # The provider call itself is valid and therefore must receive a
            # typed output, even though its payload is not dispatchable. Raising
            # here left the custom_tool_call unanswered and ended the whole
            # step. Reject the payload atomically, pair the call with an error,
            # and let run_turns ask the model for a corrected call.
            problem = malformed_call_note(raw, stray)
            failed = Block("syscall", raw, {})
            results.append((failed, problem))
            recoverable = True
            fire(
                {
                    "ev": "result",
                    "phase": "start",
                    "tag": failed.tag,
                    "attrs": {},
                    "body": clip(raw),
                    "text": "",
                }
            )
            fire(
                {
                    "ev": "result",
                    "phase": "done",
                    "tag": failed.tag,
                    "attrs": {},
                    "body": clip(raw),
                    "text": clip(problem),
                }
            )
            blocks = []
        else:
            blocks = [block for block, _, _ in spans]
    elif tool_syscalls(world.model) and scan(speech):
        raise RuntimeError("the model emitted XML as speech instead of calling syscall")
    else:
        blocks = [] if tool_syscalls(world.model) else scan(speech)
    if not stopped():
        for b in blocks:
            if stopped():
                break
            fire(
                {
                    "ev": "result",
                    "phase": "start",
                    "tag": b.tag,
                    "attrs": dict(b.attrs),
                    "body": clip(b.body),
                    "text": "",
                }
            )

            def on_chunk(text: str, tag: str = b.tag) -> None:
                if text:
                    fire(
                        {
                            "ev": "result",
                            "phase": "delta",
                            "tag": tag,
                            "delta": True,
                            "text": text,
                        }
                    )

            try:
                r = dispatch(
                    world,
                    b,
                    on_chunk=on_chunk,
                    should_stop=should_stop,
                )
            except Exception:  # noqa: BLE001
                # A raising syscall unwound the whole turn and took the results
                # of the syscalls before it with it: `results` is local here, so
                # a <bash> that had already run lost its output and the
                # transcript was left ordering a tag with no outcome. An
                # ambiguous <edit> body did exactly this. A failure is this
                # tag's result, like every other failure in this system.
                r = traceback.format_exc()
            results.append((b, r))
            fire(
                {
                    "ev": "result",
                    "phase": "done",
                    "tag": b.tag,
                    "attrs": dict(b.attrs),
                    "body": clip(b.body),
                    "text": clip(r),
                }
            )
    # No syscalls usually means the model finished. It also looks exactly like
    # a reply that was cut off mid-tag: scan() drops an unterminated block, so
    # `<bash>ls` with no closing tag parses to nothing. stop_reason is the only
    # thing that tells the two apart, and it was written to world.log and read
    # by nobody. A cut-off turn is not a finished one.
    #
    # Every way a reply can end early belongs here, not just the endpoint's
    # two. A stop sequence firing means generation was guillotined the instant
    # it began impersonating the harness, and the degeneration guard means it
    # was cut mid-repetition -- both leave exactly the same silent, empty,
    # apparently-finished turn. They need opposite advice, though: a truncated
    # reply should be resumed, and a reply that was stopped for going wrong
    # must not be.
    # A cut reply is not a finished one even when some of its syscalls did
    # run: the ones after the cut never existed. And an opener that never
    # closed is dropped by scan in silence, which reads as a turn that simply
    # chose not to call anything -- the single most expensive silence in this
    # harness. Report both, and let the caller place the note after the
    # results so the transcript reads in the order things happened.
    reason = resp.get("stop_reason")
    parts: list[str] = []
    if reason in CUT_REASONS:
        parts.append(CUT_REASONS[reason])
    lost = dropped_openers(speech) if not call else []
    if lost:
        parts.append(
            "these tags opened and were never dispatched because no closing tag "
            "was found: " + ", ".join(lost) + ' — re-issue them, and declare '
            'end="TOKEN" if the body contains tag text'
        )
    note = f"[{'; '.join(parts)}]" if parts else None
    return speech, results, (not blocks and note is None and not recoverable), assistant, note


# Why a reply ended early, and what to tell the model about it. The first two
# were cut by the endpoint and should be resumed from where they stopped. The
# last two were cut by this harness precisely because the text was going
# wrong, so "continue" is the one instruction that must not be given.
CUT_REASONS = {
    "max_tokens": "reply was cut short: max_tokens — continue from where it stopped",
    "refusal": "reply was cut short: refusal — nothing was dispatched",
    "stop_sequence": (
        "generation was stopped: you began writing a result block or a user turn. "
        "Those are the harness speaking, never you. Emit the syscall and stop — the "
        "real result arrives on the next turn"
    ),
    "degenerate_repetition": (
        "generation was stopped: the output fell into a repetition loop and the "
        "stuck tail was discarded. Do not continue that text. Write the next step "
        "fresh and briefly"
    ),
}


def _commit_step(world: World, prompt: str, last: str) -> None:
    world.prior.append({"prompt": prompt, "speech": last})
    world.prior = world.prior[-PRIOR_KEEP:]
    save(world)


def _spent_tokens(world: World, since: int) -> int:
    """Tokens billed by this step so far, cached reads included.

    Both providers report the cached bulk of a prompt outside input_tokens, so
    counting only the fresh tokens made this a ceiling on cache misses: with a
    warm prefix a 50k budget ran 20 turns and 2.4M billed tokens without
    firing. The ceiling is in tokens, not dollars -- do not discount a cache
    read to its price here.
    """
    total = 0
    for entry in world.log[since:]:
        usage = entry.get("usage") or {}
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int):
                total += value
    return total


def run_turns(
    world: World,
    prompt: str,
    *,
    max_turns: int | None = None,
    max_tokens: int = MAX_TOKENS,
    quiet: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    max_total_tokens: int | None = None,
    has_input: Callable[[], bool] | None = None,
    on_continue: Callable[[int], str | None] | None = None,
    images: list[str] | None = None,
) -> str:
    """Run a step to its end, and always say how it ended.

    ``max_turns`` is off by default. A turn count is not a budget: it bounds
    nothing that costs money and it cut long tasks off mid-edit, which is the
    one failure the loop cannot recover from. A step ends when the model stops
    calling syscalls, when the user stops it, or when ``max_total_tokens`` --
    the ceiling in the unit that is actually spent -- is reached. Pass an int
    to cap turns anyway.

    Exactly one terminating event leaves here on every path, exception
    included: ``stopped`` if the cancel flag is up, ``done`` otherwise. The TUI
    clears ``running`` on that event and drains its queue from it, so a step
    that returns in silence leaves the pane stuck on "stopping" with the queued
    message never firing.

    There was one such path. A stop that landed during a turn the model
    finished on its own satisfied neither ``stopped() and not done`` in the
    loop nor ``not cancel.is_set()`` in the bridge, so nothing was emitted at
    all. Two emitters with complementary conditions is how a gap like that
    hides; now there is one.
    """
    if world.running:
        raise RuntimeError(
            "a step is already running on this world; call step() from a new "
            "turn, or spawn() a subagent, which gets its own world"
        )
    world.running = True
    hit: list[str] = []
    try:
        return _run_turns(
            world,
            prompt,
            max_turns=max_turns,
            max_tokens=max_tokens,
            quiet=quiet,
            on_event=on_event,
            should_stop=should_stop,
            max_total_tokens=max_total_tokens,
            has_input=has_input,
            on_continue=on_continue,
            images=images,
            budget_hit=hit,
        )
    finally:
        world.running = False
        if on_event is not None:
            if hit:
                on_event({"ev": "stopped", "text": f"{hit[0]}, saved"})
            elif should_stop is not None and should_stop():
                on_event({"ev": "stopped", "text": "stopped, saved"})
            else:
                on_event({"ev": "done"})


def _run_turns(
    world: World,
    prompt: str,
    *,
    max_turns: int | None = None,
    max_tokens: int = MAX_TOKENS,
    quiet: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    max_total_tokens: int | None = None,
    has_input: Callable[[], bool] | None = None,
    on_continue: Callable[[int], str | None] | None = None,
    images: list[str] | None = None,
    budget_hit: list[str] | None = None,
) -> str:
    def emit(ev: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(ev)

    # A child gets a token ceiling from its contract's budget. The root loop
    # only ever had max_turns, so a run that burned its whole context in four
    # enormous turns was unbounded in the one unit that costs money. Count from
    # where this step began, so a nested step() is charged for itself rather
    # than for the session in front of it.
    spent_from = len(world.log)
    hit = budget_hit if budget_hit is not None else []

    def stopped() -> bool:
        if max_total_tokens is not None and not hit:
            if _spent_tokens(world, spent_from) >= max_total_tokens:
                hit.append(f"token budget of {max_total_tokens} reached")
        return bool(hit) or (should_stop is not None and should_stop())

    def stop_note(n: int) -> str:
        if hit:
            return f"[stopped: {hit[0]} after turn {n}]"
        return f"[stopped by the user after turn {n}]"

    world.messages.append({"role": "user", "content": header(world) + "\n\n" + prompt})
    # Images the composer attached to this prompt. vision.attach appends its
    # blocks to the most recent user message, which is the one just pushed, so
    # the picture arrives with the sentence about it instead of one turn later.
    # A bad path is a note in that same message, never an exception: losing the
    # whole step because one screenshot moved is not a trade worth making.
    if images:
        from desmos import vision

        try:
            note = vision.attach(world, *images)
        except Exception as exc:  # noqa: BLE001 - any failure is a note
            note = f"[image attach failed: {exc}]"
            content = world.messages[-1]["content"]
            if isinstance(content, str):
                world.messages[-1]["content"] = [{"type": "text", "text": content}]
            world.messages[-1]["content"].append({"type": "text", "text": note})
            emit({"ev": "error", "text": note})
        else:
            emit({"ev": "attached", "text": note})
    last = ""
    n = 0
    while max_turns is None or n < max_turns:
        n += 1
        if stopped():
            if n > 1:
                world.messages.append({"role": "user", "content": stop_note(n - 1)})
            _commit_step(world, prompt, last)
            return last
        emit({"ev": "turn", "n": n})
        if not quiet:
            print(f"\n===== turn {n} =====")
        try:
            speech, results, done, assistant, cut_note = turn(
                world,
                world.messages,
                max_tokens,
                n=n,
                emit=emit,
                should_stop=stopped,
            )
        except Exception as exc:  # noqa: BLE001
            # A failure is a value, not an unwind. Letting it propagate left a
            # user message with no assistant reply -- so the next step appended
            # a second consecutive user turn -- while run_turns' finally still
            # emitted "done", telling the TUI the step succeeded next to an
            # unrelated error line. Write what happened where the model will
            # read it, say so once, and stop this step.
            note = f"[turn {n} failed: {type(exc).__name__}: {exc}]"
            world.messages.append({"role": "assistant", "content": [{"type": "text", "text": note}]})
            emit({"ev": "error", "n": n, "text": note})
            if not quiet:
                print(note)
            _commit_step(world, prompt, last)
            return last
        last = speech
        thoughts = thinking_text(assistant)
        if thoughts and not quiet:
            print("--- thinking ---")
            print(thoughts)
            print("--------------")
        if not quiet:
            print(speech)
        last_results = format_results(results) if results else ""
        if last_results and not quiet:
            print("\n--- results ---")
            print(last_results)
        # Whatever ran, its output goes back. The stop path used to return
        # before this, so a Ctrl+C landing after the first of three syscalls
        # threw away the results of the ones that had already finished -- the
        # model's next context showed its own tags with no outcome and no
        # marker that they had been executed.
        call = syscall_call(assistant)
        if results or call:
            world.messages.append(
                {"role": "user", "content": result_content(results, assistant, world.cwd)}
            )
        # After the results, never before: the note explains what did not run,
        # and reads as nonsense ahead of the output of what did.
        if cut_note:
            world.messages.append({"role": "user", "content": cut_note})
            emit({"ev": "error", "n": n, "text": cut_note})
            if not quiet:
                print(cut_note)
        if done or stopped():
            if stopped():
                # A stop left no trace in the transcript, so the next step read
                # the model's own tags, whichever results happened to run, and
                # nothing saying it had been interrupted -- which reads as work
                # that finished.
                world.messages.append({"role": "user", "content": stop_note(n)})
                _commit_step(world, prompt, speech)
                return speech
            # The model stopped calling syscalls, but background work it started
            # is still running. Nothing waits on it while it runs: the turn is
            # already over, a stop is still heard, and a queued follow-up still
            # wins. When a task lands, the step resumes with its output as an
            # ordinary user turn -- the same shape a syscall result arrives in.
            if pending.count(world):
                emit({"ev": "pending", "n": pending.count(world)})
                landed = pending.wait_next(world, stop=stopped, interrupt=has_input)
                if landed:
                    text = pending.notice(landed)
                    world.messages.append({"role": "user", "content": text})
                    emit({"ev": "resumed", "n": n, "text": text})
                    continue
            _commit_step(world, prompt, speech)
            return speech
        if on_continue is not None:
            reminder = on_continue(n)
            if reminder:
                world.messages.append({"role": "user", "content": reminder})
                emit({"ev": "guidance", "n": n, "text": reminder})
    # Only reachable when a caller asked for a turn cap. Same for it as for the
    # rest: it was printed, and the bridge runs quiet=True, so the only signal
    # was a `done` event identical to a clean finish.
    note = f"[hit max_turns={max_turns} — the task was not finished]"
    world.messages.append({"role": "user", "content": note})
    emit({"ev": "error", "text": note})
    if not quiet:
        print(f"\n{note}")
    _commit_step(world, prompt, last)
    return last


def new_world(
    cwd: Path,
    state_path: Path | None = None,
    *,
    ns: dict[str, Any] | None = None,
    persist: bool = True,
) -> World:
    world = World(cwd=cwd, state_path=state_path, persist=persist)
    if ns is not None:
        world.ns = ns
    world.ns.setdefault("CWD", str(cwd))
    seed_builtins(world)
    install_resources(world)
    if persist:
        load(world)
        ensure_gen1(world)
    return world


def bind_step(world: World) -> Callable[..., str]:
    def step(
        prompt: str,
        *,
        max_turns: int | None = None,
        max_tokens: int = MAX_TOKENS,
        max_total_tokens: int | None = None,
    ) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise TypeError("step(prompt) needs a non-empty string")
        from desmos.loop import run_turns as _run

        return _run(
            world,
            prompt,
            max_turns=max_turns,
            max_tokens=max_tokens,
            max_total_tokens=max_total_tokens,
        )

    world.ns["step"] = step
    world.ns["world"] = world
    world.ns["reload"] = lambda: reload(world)
    world.ns["reload_sdk"] = lambda: reload_sdk(world)
    world.ns["reset"] = lambda: reset_transcript(world)
    world.ns["evolve"] = lambda reason="": evolve(world, str(reason))
    world.ns["rollback"] = lambda n=1: rollback(world, int(n))
    world.ns["switch"] = lambda model, effort=None: _switch(world, model, effort)
    return step


def _switch(world: World, model: str, effort: str | None = None) -> str:
    """`switch(...)` in the kernel. Same call the TUI picker makes."""
    from desmos.settings import switch as _do

    return _do(world, str(model), str(effort) if effort else None)


def reset_transcript(world: World) -> str:
    """Drop the append-only chat so a poisoned turn cannot train the next one."""
    if world.running:
        raise RuntimeError("cannot reset the transcript from inside a running step")
    n = len(world.messages)
    world.messages.clear()
    world.prior.clear()
    save(world)
    return f"transcript cleared ({n} messages)"


#: Modules whose globals ARE live state, so re-executing the file destroys it.
_RELOAD_SKIP = {
    # _BY_WORLD is every in-flight async task. Reload drops them and
    # pending.count(world) then reports 0 in the middle of a step.
    "desmos.pending",
    # _WIRE is bound to the real stdout at import on purpose. Rebinding it
    # during a <python> syscall -- where sys.stdout is exec's chunk writer --
    # feeds every event back into itself.
    "desmos.bridge",
    # Its module body is `raise SystemExit(main())`.
    "desmos.__main__",
}


def reload_sdk(world: World | None = None) -> str:
    """Reimport desmos.* then rebind. Safe from the kernel or <reload_sdk/> after editing the SDK."""
    import importlib
    import sys

    importlib.invalidate_caches()
    old_subagent = sys.modules.get("desmos.subagent")
    old_subagent_emit = getattr(old_subagent, "_EMIT", None)
    if old_subagent_emit is None and world is not None:
        # During a live turn the active bridge route is also installed here.
        old_subagent_emit = getattr(world, "emit", None)
    if old_subagent_emit is None:
        # Import-based bridge launches keep the same module-level wire route.
        old_subagent_emit = getattr(sys.modules.get("desmos.bridge"), "_emit", None)
    old_subagent_runs = dict(getattr(old_subagent, "RUNS", {}))
    for name in list(sys.modules):
        if name == "edit" or name.startswith("desmos_skill_"):
            del sys.modules[name]
    # This list only exists to put a module ahead of the ones that bind its
    # names at import time (`from X import Y`). Everything else in the package
    # is derived below, because as a hand list it fell behind: dialect, spill,
    # shell, vision, acp and subagent_tool were never reloaded, and a stale
    # desmos.dialect leaves the system prompt on the old source until the
    # process restarts -- the one failure this tag exists to prevent.
    order = [
        "desmos.const",
        "desmos.types",
        "desmos.scan",
        "desmos.dialect",
        "desmos.spill",
        "desmos.edit",
        "desmos.exec",
        "desmos.persist",
        "desmos.memory",
        "desmos.subagent_contracts",
        "desmos.subagent_prompt",
        "desmos.subagent",
        "desmos.generations",
        "desmos.auth",
        # Ahead of desmos.openai, which binds names out of it at import time.
        # Left to the derived pass, `from desmos.complete import ...` ran
        # against the stale module and reload_sdk died on the new name.
        "desmos.complete",
        "desmos.openai",
        "desmos.settings",
        "desmos.complete",
        "desmos.catalog",
        "desmos.dispatch",
        "desmos.skills",
        "desmos.extensions",
        "desmos.loop",
        "desmos.ext",
        "desmos.cli",
    ]
    ordered = set(order)
    names = [n for n in order if n in sys.modules]
    names += sorted(
        n
        for n in list(sys.modules)
        if n.startswith("desmos.") and n not in ordered and n not in _RELOAD_SKIP
    )
    names += [n for n in ("desmos", "inverted") if n in sys.modules]
    reloaded = []
    for name in names:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        importlib.reload(mod)
        reloaded.append(name)
    if world is not None:
        from desmos.loop import bind_step as _bind
        from desmos.loop import reload as _reload
        from desmos.loop import seed_builtins as _seed

        _seed(world)
        _reload(world)
        _bind(world)
        # importlib.reload resets module globals in-place. Without restoring
        # this binding, the next spawn silently creates an orphan World, so its
        # pending completion can never resume the live TUI loop.
        import desmos.subagent as _subagents

        _subagents.bind(world)
        _subagents.RUNS.update(old_subagent_runs)
        if old_subagent_emit is not None:
            _subagents.set_emitter(old_subagent_emit)
    return "sdk reloaded: " + ", ".join(reloaded)


def attach(shell: Any = None, *, cwd: str | Path | None = None, model: str | None = None) -> World:
    if shell is None:
        try:
            from IPython import get_ipython
        except ImportError as exc:
            raise RuntimeError("IPython is not installed") from exc
        shell = get_ipython()
    if shell is None:
        raise RuntimeError("no IPython shell — use python -m desmos console")
    path = Path(cwd or Path.cwd()).resolve()
    world = new_world(path, ns=shell.user_ns)
    world.shell = shell
    if model:
        world.model = model
    bind_step(world)
    return world


def run(args: Any) -> int:
    import json
    import os

    cwd = Path(args.cwd).resolve()
    os.chdir(cwd)
    world = new_world(cwd)
    run_dir = Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)
    world.model = args.model
    cap = args.max_turns if args.max_turns is not None else "unbounded"
    print(f"model={world.model} thinking={world.thinking} max_turns={cap} cwd={cwd}")
    print(system_prompt(world))
    print("--------------")
    run_turns(
        world,
        args.task,
        max_turns=args.max_turns,
        max_tokens=args.max_tokens,
        max_total_tokens=getattr(args, "max_total_tokens", None),
    )
    summary = {
        "task": args.task,
        "ns": ns_names(world),
        "tools": {n: t.doc for n, t in world.tools.items()},
        "notes": world.notes,
        "turns": len(world.log),
        "usage": [e.get("usage") for e in world.log],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("\n===== summary =====")
    print(json.dumps(summary, indent=2))
    return 0
