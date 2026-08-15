from __future__ import annotations

"""Per-family prompt dialect.

The ABI is frozen and the catalog is factual. This is the third layer: the
same harness described in the register the driving model actually responds to.

Two findings drove the split, and they point opposite ways:

  - GPT-5.6 degrades on long prompts (OpenAI measured removing redundant
    instruction improving quality 10-15% while cutting 41-66% of tokens), and
    it reads "be concise" as permission to return a shorter artifact rather
    than a shorter explanation. So: say each thing once, never ask for brevity.
  - Claude Opus 5 runs long by default and a conciseness instruction measurably
    cuts response length ~20%; it also self-verifies unprompted, so telling it
    to verify produces over-verification, and it reaches for subagents freely
    enough to need a ceiling rather than encouragement.

An instruction that helps one costs the other. Hence two blocks, not one
averaged block that is wrong for both.

`capabilities()` is shared and factual: things this harness can do that the
catalog never said out loud. Both families need it; neither infers it.
"""

import os
from typing import Any


def family(model: str) -> str:
    """Which prompt dialect a model wants. Anthropic is the default.

    Routing and dialect answer from one predicate. This file used to keep its
    own marker list, and the two disagreed both ways: DESMOS_MODEL=sol got
    OpenAI dialect prose on a body POSTed to api.anthropic.com, and o3-mini got
    Anthropic prose on a Responses request.
    """
    from desmos.transport.openai import is_openai  # function-level, matching settings.provider_of

    return "openai" if is_openai(model) else "anthropic"


def tool_syscalls(model: str) -> bool:
    """Whether this model receives syscalls as a typed tool call.

    OpenAI has always had one. The Anthropic path used to parse tags back out
    of assistant prose, which is the same channel the model writes its own
    narration on -- so it could run past its own call and invent the result,
    and a reader saw raw XML in the story pane whenever the stripper and the
    scanner disagreed about where a body ended. Both families now hand the
    harness a typed call instead.

    DESMOS_TOOL_SYSCALLS=0 puts the Anthropic side back on prose parsing. It
    exists so a session that cannot issue a call has a way back in.
    """
    if family(model) == "openai":
        return True
    return (os.environ.get("DESMOS_TOOL_SYSCALLS") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def growth() -> str:
    """When to build a tool, not just how.

    The prompt already said "grow what you need as you go" and then only ever
    explained mechanics -- where a SKILL.md lives, what attrs <register>
    takes. Exhortation plus a manual is not a decision procedure: it never
    said at what point in a task the answer stops being another <python> and
    starts being a tag. So the model wrote the same block three times and
    called that working.

    This is the trigger, the anti-trigger, and the price.
    """
    return "\n".join(
        [
            "# building your own tools",
            "The frozen tags are not your toolset. They are what a toolset gets built from:"
            " <register> installs a tag that is live on the very next dispatch and survives into"
            " later sessions, a skill is a file the catalog lists by name and loads only when"
            " asked for, a note is doctrine that rides in every prompt from then on.",
            "Build one when the work says so. The signals are concrete: you have written close to"
            " the same <python> a third time; the task has many units differing only by an"
            " argument -- forty files, twenty endpoints, every row of a table; or you worked"
            " something out that was not obvious and a later turn would have to work it out"
            " again from nothing.",
            "Do not build one for something you will do once. A tag used twice costs more to"
            " write than it saves, and the harness is not improved by a drawer of them.",
            "The price is one line of catalog, forever, in every request after this one. A tag"
            " that earns it replaces a block you would otherwise rewrite from scratch each time."
            " Weigh it against that, not against the effort of writing it.",
            "Choose by how long it needs to live: a note for doctrine that should shape every"
            " turn, a tag for an operation you will call again, a skill for a procedure with"
            " enough detail that it should stay out of the prompt until it is needed.",
            "Scale it to the task. A question answered in one call needs nothing. A task that"
            " will run for many turns, or that you can already see repeating, is worth a few"
            " minutes of tool-building before the repetition starts rather than after it.",
        ]
    )


def capabilities() -> str:
    """What the harness supports and the catalog never stated.

    Every line here is a capability the code already has. They are written
    once, factually, with no family-specific steering -- steering lives in
    dialect().
    """
    return "\n".join(
        [
            "# what you can actually do",
            "batching: several tags in one turn all run, in the order you wrote them, and every"
            " result comes back in the same reply. Batch independent calls instead of spending a"
            " turn each. A call that needs an earlier result waits for the next turn.",
            "results are capped at 6000 chars per call, but nothing is thrown away: over the cap"
            " the whole output is written to .desmos/out/NNNN-<tag>.txt and the result opens with"
            " a pointer line naming that file. Read the part you need out of it -- grep, sed,"
            " head, a line range -- instead of paging it back into the transcript. Filtering in"
            " the call itself is still cheaper than spilling and reading back.",
            "subagents: spawn(task, agent=\"general\") returns an id and does not block."
            " general/worker use gpt-5.6-sol with edit capability; explore/scout use"
            " gpt-5.6-luna for read-only reconnaissance; review/reviewer and the read-only"
            " security and planner roles use sol for advanced judgment; sniffer uses luna for"
            " quick reproduction/localization. model and thinking are launch overrides."
            " system_prompt, system_append, user_input, and task_template customize each child."
            " Subagent launches have no turn, token, usage, or wall-time stop budget."
            " guidance_every_turns defaults to 8, is configurable per launch, and re-anchors"
            " long runs without stopping them; guidance_reminder overrides its text."
            " fanout(tasks) defaults to explore, not general;"
            " spawn_many(specs) validates a whole batch before concurrent launch and registers one"
            " parent-loop completion callback after every child settles."
            " wait(*ids, timeout=600) blocks; gather(ids) waits and joins their output; status()"
            " lists running; result(id) reads one; spawn(resume=id) continues a finished run."
            " A child is an isolated World with its own transcript and no persist.",
            "subagent contracts: for routine bounded work, pass task text plus"
            " simple={paths, write, checks, tools, depends, evidence}; it expands to a compact judged"
            " contract. Use TaskContract directly for high-risk or unusual work -- objective,"
            " non_goals, deliverable_schema, required_evidence, acceptance_checks, allowed_tools,"
            " allowed_paths, write_paths, dependencies, and require_tool_use. Under either contract"
            " structured_result(id) returns a typed RunResult and judgment(id) returns"
            " accepted/rejected with reasons. The judge scores the declarations against what the"
            " parent observed at runtime, so a child cannot pass by asserting it passed; a"
            " dependency holds a child until that run is accepted, and a rejected dependency"
            " stops it with an explicit reason. A string task skips all of it and gives you"
            " prose you have to take on trust.",
            "step(prompt, max_turns=None, max_total_tokens=None) runs a nested turn loop on this"
            " same world. There is no turn cap unless you ask for one — a step ends when the"
            " model stops calling syscalls, when the user stops it, or on the token ceiling."
            " max_total_tokens is a prompt+completion ceiling counted from the start"
            " of that step; hitting it stops the loop, says so in the transcript, and reports"
            " `stopped` rather than `done`.",
            "reset() clears the transcript when a poisoned turn would otherwise train the next one."
            " ns, notes, tools, and skills survive it.",
            "extensions: a .py under .desmos/extensions gets api.tool(name, doc, handler) to add a"
            " tag and api.hook(\"before_dispatch\", fn) to inspect or veto one -- returning a string"
            " from that hook replaces the syscall result and the call never runs.",
            "<edit> also accepts old_str= and new_str= as attributes when the body form is awkward.",
            "state: <python> calls share one kernel -- a name bound in one is there in the next,"
            " this turn and later turns. Prefer <shell id=\"main\"> for command work: its cwd,"
            " environment, interactive process, and unfinished build survive across calls."
            " There are no read windows to choose and nothing to poll: a command that outlives"
            " the first look is taken over by a monitor that owns the terminal, and the step is"
            " resumed with its output when it actually finishes. A result saying it is monitored"
            " means the work is still going -- go do something else, or interrupt it. A program"
            " that asks a question comes back saying so; answer it with another <shell> on the"
            " same id. Use <bash> only for a quick hermetic one-shot where a fresh subprocess is"
            " the point.",
            "a failing call does not stop the ones after it. Every tag in the turn runs, and you get"
            " every result. If a later call only makes sense when an earlier one succeeded, put it"
            " in the next turn instead.",
            "a tag you never close is dropped in silence -- no result, no error, and the turn looks"
            " like you called nothing. Close every tag you open.",
            "a body ends at the first closing tag, so a body that contains its own closer is"
            " cut there and the rest leaks out as speech -- which is what makes editing this"
            " codebase hazardous, since its sources are full of literal tag text. Declare an"
            " end token instead: <python end=\"X\"> runs to </python:X> and any bare"
            " </python> inside it is ordinary text. It works on every tag, the token is any word,"
            " and the attribute never reaches the handler. Use it for edits to harness code,"
            " for tests that quote calls, and any time you would otherwise build a closing tag"
            " by string concatenation.",
            "results arrive after your whole message, never mid-sentence. So do not write prose"
            " that predicts what a call will return, and do not branch in this reply on a value"
            " this reply is still computing. Read the results next turn.",
            "switching model or effort mid-session: switch(\"claude-opus-5\") from <python>, or"
            " switch(\"gpt-5.6-sol\", \"high\"). It validates the choice, checks this machine has a"
            " credential for that provider, saves it, and returns a line describing what changed."
            " The new model drives the next turn -- not the rest of this reply, which is already"
            " being written by the current one. A provider change drops the previous provider's"
            " thinking blocks from later requests; speech and results replay in full.",
        ]
    )


# Opus 5 reviewed this block and found two lines that fight the XML design.
# "Lead with the outcome" was unscoped, but a reply that carries tags is
# written *before* any result exists -- so it forced either a prediction or a
# dead preamble; it now applies to the closing message only. And "no separate
# verification pass" read as "do not verify" when verifying here IS a call.
# Its other note -- that the scope and corrections lines restate defaults --
# is left in: Anthropic's own Opus 5 guidance says to add both because the
# model expands scope and over-narrates corrections, and a model's report
# that it does not need an instruction is not a measurement of whether it does.
_ANTHROPIC = "\n".join(
    [
        "# how to work here",
        "Keep responses focused and brief; put the weight on the answer, not the preamble or the"
        " caveats. In the message that closes the task -- after the last result is in -- lead with"
        " the outcome: one sentence on what happened or what you found.",
        "The transcript already carries every call, its body and its result, and the reader can"
        " see them. Do not restate them in prose: no pasted commands, no grep counts, no"
        " insertion counts, no bookkeeping about which check ran. Do the checks; report the"
        " conclusion and the one number that changes a decision.",
        "Deliver what was asked, at the scope intended. Make routine judgment calls yourself and"
        " check in only when readings differ enough to change the work. If you think the ask is"
        " wrong, say so in a sentence and continue with it as asked. Finish the whole task; report"
        " completion only when it is actually done, and if something is blocked, do the rest and"
        " say plainly what is missing.",
        "Before reporting a task complete, run the relevant verification; if it cannot run, say"
        " so. Do not repeat a passing check or spawn a reviewer for work you can verify directly.",
        "For non-trivial implementation, explore existing patterns and settle the approach before"
        " editing; skip a separate planning phase for simple or fully specified work.",
        "After compaction, restore the user's request, decisions, files touched, errors, pending"
        " work, and the exact next step; do not revive completed or tangential work.",
        "A child's report is a claim about its own work. Under a contract, judgment(id) is the"
        " harness's verdict on that claim -- read the verdict, not the prose.",
        "Subagents cost a full context each. Use them for genuinely independent, sizeable tracks;"
        " do not use them for work you could finish in a handful of calls, and keep spawn counts"
        " low.",
        "Correct an earlier statement only when the error changes what the reader would do. State"
        " it plainly and move on -- no tally, no apology, no re-audit of work that was right.",
    ]
)


# gpt-5.6-sol reviewed this block and pushed back on three lines; all three
# edits are its wording. "Pick one reading" let it silently narrow the
# deliverable, "decide reversible things" read as license for anything
# undoable-but-costly, and "plan before the first call" fought discovery on
# any task where you cannot plan until you have looked.
_OPENAI = "\n".join(
    [
        "# how to work here",
        "Form a brief initial plan, then revise it after discovery. Parallelize discovery: batch"
        " the independent reads you already know you need rather than one call per turn.",
        "Say what you are about to do in a sentence before a run of calls, and again when you"
        " change direction. Not every call.",
        "Deliver exactly what was asked. If you spot adjacent work, name it as optional rather"
        " than doing it. On low-impact ambiguity pick the likelier reading and say which; if the"
        " readings would change the deliverable, ask.",
        "When the user asks for a change, continue through implementation and verification."
        " Inspection and a plan are discovery, not completion.",
        "Decide reversible, low-impact things yourself. Ask first when an action is hard to undo,"
        " or when it is costly, public, or exposes data.",
        "When you spawn routine bounded work, use the compact simple contract with paths, write"
        " scope, and checks. Reserve the full TaskContract schema for high-risk or unusual work."
        " A bare string task returns prose you then have to trust.",
        "Close with the outcome in the first sentence. Then separate what you verified by running"
        " it from what you are inferring, and name anything still unproven.",
        "A response that only calls `syscall` is complete and correct. Write no assistant text"
        " after that call -- the sentence of intent goes before it, and the outcome goes in the"
        " next turn after the results arrive. If you have nothing to say, say nothing.",
    ]
)


# The Anthropic half of what openai.CONTRACT says. Shorter on purpose: Opus 5
# does not need the "this is not a chat interface with a tool API" paragraph
# that gpt-5.6 does, and every line here costs a cached-prefix token forever.
_ANTHROPIC_TOOL_CONTRACT = "\n".join(
    [
        "# how you act here",
        "You have one tool, `syscall`. Its `input` is raw XML: one or more complete tags from"
        " the register above and nothing else -- no prose around them, no fence, no JSON. The"
        " harness runs them in order and hands back their result blocks as that tool's output.",
        "XML written in an assistant message is not dispatched. It is text; the reader sees the"
        " raw tag and nothing runs. Every call goes in the tool.",
        "The call ends your response. Write nothing after it, and never write a result block"
        " yourself -- the real output arrives on the next turn.",
        "A reply with no `syscall` call ends the step. Saying you are blocked does not pause"
        " anything, it hands control back; find out with a tag first.",
    ]
)


def dialect(model: str) -> str:
    """Family-specific working style. Deliberately short for openai."""
    if family(model) == "openai":
        return _OPENAI
    if tool_syscalls(model):
        return _ANTHROPIC + "\n\n" + _ANTHROPIC_TOOL_CONTRACT
    return _ANTHROPIC


def block(world: Any) -> str:
    model = getattr(world, "model", "") or ""
    return "\n\n".join([capabilities(), growth(), dialect(model)])
