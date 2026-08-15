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

from typing import Any

# Every id the codex CLI offers today starts with "gpt" — gpt-5.6-sol /
# -terra / -luna, gpt-daybreak-blue-latest, gpt-5.5, gpt-5.4(-mini),
# gpt-5.3-codex-spark. The bare family names are here too because
# DESMOS_MODEL takes any string and people write the alias, not the id.
# No o1/o3/o4: those were a guess, and a two-character substring is the
# wrong thing to route a whole prompt dialect on.
OPENAI_MARKERS = ("gpt", "sol", "terra", "luna", "daybreak", "codex")


def family(model: str) -> str:
    """Which prompt dialect a model wants. Anthropic is the default."""
    name = (model or "").lower()
    return "openai" if any(t in name for t in OPENAI_MARKERS) else "anthropic"


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
            "results are clipped at 6000 chars per call, with the dropped count marked. Filter in"
            " the call -- grep, head, a slice -- rather than dumping and losing the tail.",
            "subagents: spawn(task, agent=\"general\") returns an id and does not block."
            " agent is general (edit, 500 turns), explore (researcher, read-only, 500), or review"
            " (critic, read-only, 300). fanout(tasks) spawns many and defaults to explore, not"
            " general. wait(*ids, timeout=600) blocks; gather(ids) waits and joins their output;"
            " status() lists running; result(id) reads one; spawn(resume=id) continues a finished"
            " run. A child is an isolated World with its own transcript and no persist.",
            "subagent contracts: spawn also takes a TaskContract instead of a string --"
            " objective, non_goals, deliverable_schema, required_evidence, acceptance_checks,"
            " allowed_tools, allowed_paths, write_paths, dependencies, require_tool_use, and a budget:"
            " Budget(max_turns, max_tokens, wall_seconds, max_retries). Under a contract"
            " structured_result(id) returns a typed RunResult and judgment(id) returns"
            " accepted/rejected with reasons. The judge scores the declarations against what the"
            " parent observed at runtime, so a child cannot pass by asserting it passed; a"
            " dependency holds a child until that run is accepted, and a rejected dependency"
            " stops it with an explicit reason. A string task skips all of it and gives you"
            " prose you have to take on trust.",
            "step(prompt, max_turns=32, max_total_tokens=None) runs a nested turn loop on this"
            " same world. max_total_tokens is a prompt+completion ceiling counted from the start"
            " of that step; hitting it stops the loop, says so in the transcript, and reports"
            " `stopped` rather than `done`.",
            "reset() clears the transcript when a poisoned turn would otherwise train the next one."
            " ns, notes, tools, and skills survive it.",
            "extensions: a .py under .desmos/extensions gets api.tool(name, doc, handler) to add a"
            " tag and api.hook(\"before_dispatch\", fn) to inspect or veto one -- returning a string"
            " from that hook replaces the syscall result and the call never runs.",
            "<edit> also accepts old_str= and new_str= as attributes when the body form is awkward.",
            "state: <python> calls share one kernel -- a name bound in one is there in the next,"
            " this turn and later turns. <bash> calls do not: each is a fresh subprocess in cwd, so"
            " a cd or an export in one is gone by the next. Chain shell state inside a single"
            " <bash>, not across several.",
            "a failing call does not stop the ones after it. Every tag in the turn runs, and you get"
            " every result. If a later call only makes sense when an earlier one succeeded, put it"
            " in the next turn instead.",
            "a tag you never close is dropped in silence -- no result, no error, and the turn looks"
            " like you called nothing. Close every tag you open.",
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
        "You already check your own work, so do not add a verification turn and do not spawn a"
        " subagent to review what you just did. Verifying with a call in the same reply is not a"
        " separate pass -- that is just doing the work.",
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
        "Decide reversible, low-impact things yourself. Ask first when an action is hard to undo,"
        " or when it is costly, public, or exposes data.",
        "When you spawn, give the child a TaskContract with acceptance_checks and write_paths."
        " A string task returns prose you then have to trust.",
        "Close with the outcome in the first sentence. Then separate what you verified by running"
        " it from what you are inferring, and name anything still unproven.",
    ]
)


def dialect(model: str) -> str:
    """Family-specific working style. Deliberately short for openai."""
    return _OPENAI if family(model) == "openai" else _ANTHROPIC


def block(world: Any) -> str:
    model = getattr(world, "model", "") or ""
    return "\n\n".join([capabilities(), growth(), dialect(model)])
