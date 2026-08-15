---
name: trajectory-retrace
description: Audit a long or compacted Desmos trajectory by content rather than API role, recover genuine human requests, exclude tool results and harness-generated nudges, and identify skipped implementation or operational work with evidence.
---

# Trajectory retrace

Use this when the user asks what remains, whether anything was skipped, or for
an audit of everything they requested across a long session.

## Principle

API role is not authorship. Tool outputs, subagent contracts, guidance
reminders, bridge notices, and background completion events can all occupy a
user-shaped slot. Classify by content-block type and provenance before deciding
that a message came from the human.

## Procedure

1. **Freeze the current state.**
   - Read the persistent todo list.
   - Check working-tree status, upstream ahead/behind count, current commit, and
     subagent status.
   - If visual changes are involved, compare source and executable freshness.
   - Respect an explicit instruction not to reload, rebuild, push, or restart.

2. **Recover human requests.**
   - Load this skill, then call `trajectory_retrace.run(world)` in Python.
   - The helper reads both `world.messages` and persisted request bodies in
     `world.log`.
   - It understands OpenAI and Anthropic content shapes, chained `prior steps`
     summaries, direct text/image prompts, and server-compacted trajectories.
   - Never substitute a count of `role == user` for this extraction.

3. **Build a request ledger.**
   Give each recovered request one verdict:
   - `DONE`: implemented and independently verified.
   - `ANSWERED`: informational question with no remaining action.
   - `SUPERSEDED`: the user explicitly changed direction or closed it.
   - `OPEN`: requested deliverable is absent or verification failed.
   - `OPERATIONAL`: implementation is done but push, build, restart, or another
     deployment step remains.
   - `UNCERTAIN`: folded or summarized evidence is insufficient.

4. **Verify, do not infer.**
   - For code work, require a committed blob, relevant test, or real entry-point
     probe.
   - For pushes, compare the upstream and local commit graph.
   - For runtime visibility, compare source and binary/process freshness.
   - For subagent work, inspect the integrated files and parent-run checks; a
     child verdict is only a lead.
   - A checked todo is evidence of intent, not proof by itself.

5. **Reconcile duplicates and changed direction.**
   - Prompt envelopes often repeat the current prompt and carry truncated prior
     summaries. Merge exact duplicates and obvious truncated copies.
   - Do not resurrect a request the user explicitly superseded.
   - Do flag a claim such as “all done” when the branch is still ahead,
     the binary is stale, tests failed, or the working tree is dirty.

6. **Report in decision order.**
   - First sentence: whether implementation work remains.
   - Then list genuinely open work.
   - Separate operational steps from implementation gaps.
   - State the audit scope and excluded generated-message counts.
   - Name uncertainties instead of silently treating them as complete.

## Python helper

```python
import trajectory_retrace

audit = trajectory_retrace.run(world)
audit["prompts"]          # ordered genuine human requests
audit["excluded"]         # tool results, contracts, guidance, background notices
audit["limitations"]      # what compaction or missing logs prevent proving
```

The helper extracts requests; semantic reconciliation against commits, tests,
todos, and deployment state remains the agent's responsibility.
