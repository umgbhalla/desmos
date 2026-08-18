"""What this session is spending, and the ceiling it must not cross.

`calls` has priced every model round since the ledger landed, but nothing read
it back while a step was running. So a runaway loop was bounded by tokens --
which are a proxy -- and never by money, which is the thing that actually runs
out. This module is the missing half: a rolling window over the workspace's
own ledger, a limit from the environment, one warning before the ceiling and a
hard stop at it.

Three deliberate choices:

- **The window rolls, it does not reset.** A calendar budget is spent in the
  first hour of the day and idle for twenty-three; a rolling window bounds the
  rate instead of the date.
- **Aggregation is cross-session.** Two sibling sessions in one workspace spend
  from one card, so they are counted together. The alternative -- per-session
  budgets -- is a limit that doubles every time you open a second front.
- **Account identity is the provider, not the credential.** Reading a token to
  fingerprint it would put a credential-shaped value in the state layer to
  distinguish accounts almost nobody has. `DESMOS_ACCOUNT` overrides it for
  anyone who does.

The stop is the loop's existing budget rail: `over()` is consulted where the
token ceiling is, so exceeding it ends the step with a stated reason rather
than by raising into the middle of a turn.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from desmos.kernel import catalog
from desmos.kernel.types import World
from desmos.state.persist import _open, _workspace_id, state_file

#: Injection name for the warning block. Idempotent by name.
BLOCK = "budget"

DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_SOFT = 0.8


def _number(name: str, default: float, *, low: float = 0.0) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > low else default


def limit_usd() -> float:
    """The ceiling in dollars. Zero -- the default -- means no ceiling."""
    raw = os.environ.get("DESMOS_BUDGET_USD")
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value if value > 0 else 0.0


def window_hours() -> float:
    return _number("DESMOS_BUDGET_WINDOW_HOURS", DEFAULT_WINDOW_HOURS)


def soft_share() -> float:
    value = _number("DESMOS_BUDGET_SOFT", DEFAULT_SOFT)
    return value if 0.0 < value <= 1.0 else DEFAULT_SOFT


def account(world: World) -> str:
    """Which purse this spend comes out of."""
    override = os.environ.get("DESMOS_ACCOUNT", "").strip()
    if override:
        return override
    model = str(getattr(world, "model", "") or "").lower()
    if model.startswith("gpt") or model.startswith("o1") or model.startswith("o3"):
        return "openai"
    if model.startswith("claude"):
        return "anthropic"
    return "unknown"


def spend(
    world: World, hours: float | None = None, purse: str = ""
) -> dict[str, Any]:
    """Sum the workspace's priced calls inside the rolling window."""
    span = window_hours() if hours is None else float(hours)
    since = (datetime.now(timezone.utc) - timedelta(hours=span)).isoformat()
    who = purse or account(world)
    empty = {
        "usd": 0.0, "calls": 0, "input": 0, "output": 0,
        "account": who, "hours": span, "since": since,
    }
    if not getattr(world, "persist", False):
        return empty
    try:
        conn = _open(state_file(world))
    except Exception:  # noqa: BLE001 - a missing ledger is not a budget breach
        return empty
    try:
        workspace = _workspace_id(conn, world, create=False)
        if workspace is None:
            return empty
        row = conn.execute(
            """
            SELECT coalesce(sum(c.cost_usd), 0.0) AS usd,
                   count(*) AS calls,
                   coalesce(sum(c.input_tokens + c.cache_read_input_tokens
                                + c.cache_creation_input_tokens), 0) AS input,
                   coalesce(sum(c.output_tokens), 0) AS output
            FROM calls AS c JOIN sessions AS s ON s.id = c.session_id
            WHERE s.workspace_id = ? AND c.ts >= ?
              AND (c.account = ? OR c.account = '')
            """,
            (workspace, since, who),
        ).fetchone()
        return {
            "usd": float(row["usd"]),
            "calls": int(row["calls"]),
            "input": int(row["input"]),
            "output": int(row["output"]),
            "account": who,
            "hours": span,
            "since": since,
        }
    finally:
        conn.close()


def status(world: World) -> dict[str, Any]:
    ceiling = limit_usd()
    used = spend(world)
    share = (used["usd"] / ceiling) if ceiling > 0 else 0.0
    return {
        **used,
        "limit": ceiling,
        "share": share,
        "over": bool(ceiling > 0 and used["usd"] >= ceiling),
        "soft": bool(ceiling > 0 and share >= soft_share()),
    }


def text(state: dict[str, Any]) -> str:
    return (
        f"Budget: ${state['usd']:.2f} of ${state['limit']:.2f} spent in the "
        f"last {state['hours']:.0f}h on {state['account']} "
        f"({state['share'] * 100:.0f}%), across {state['calls']} model calls "
        "in this workspace -- siblings included. At the ceiling the step stops "
        "itself, so finish or hand off the current thread before then rather "
        "than being cut mid-edit. Raise it with DESMOS_BUDGET_USD if the work "
        "is worth it; that is the user's call, not yours."
    )


def watch(world: World) -> dict[str, Any]:
    """Install or retire the warning block for the spend right now."""
    state = status(world)
    if state["limit"] <= 0:
        catalog.retire(world, BLOCK)
        return state
    if state["soft"]:
        catalog.inject(world, BLOCK, text(state), turns=0)
    else:
        catalog.retire(world, BLOCK)
    return state


#: id(world) -> (log length when measured, whether it was over). The loop asks
#: on every stopped() call, which is far more often than a call is priced.
_SEEN: dict[int, tuple[int, bool]] = {}


def over(world: World) -> bool:
    """Has this workspace passed its ceiling? Cached until a call is priced."""
    if limit_usd() <= 0:
        return False
    mark = len(getattr(world, "log", []) or [])
    seen = _SEEN.get(id(world))
    if seen is not None and seen[0] == mark:
        return seen[1]
    hit = bool(status(world)["over"])
    _SEEN[id(world)] = (mark, hit)
    return hit


def render(world: World) -> str:
    state = status(world)
    if state["limit"] <= 0:
        return (
            f"${state['usd']:.2f} on {state['account']} in the last "
            f"{state['hours']:.0f}h over {state['calls']} calls; no ceiling set "
            "(DESMOS_BUDGET_USD)"
        )
    return text(state)
