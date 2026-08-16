"""What a model call costs, from one table both languages read.

Two implementations of a price list is two answers to "what did this session
spend". The Python one hardcoded sonnet rates and ignored `world.model`, so an
opus session under-reported by 40%; the Rust meter had a real per-model table
and never wrote it down. The rates now live in `prices.json` beside this file:
Python loads it at import, the TUI `include_str!`s it at build time, and a
check asserts the two agree on a fixed fixture.

Prefix match, in file order -- longest-lived vendor names first. An unknown
model takes the `default` entry rather than free, because a silent zero reads
as "this cost nothing".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TABLE_PATH = Path(__file__).resolve().parent / "prices.json"

#: The four token counters both wires report, mapped to their rate multiplier.
#: `input_tokens` is fresh prompt, billed at list price; the cache counters are
#: the same tokens at a discount or a premium.
USAGE_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)

_TABLE: dict[str, Any] | None = None


def table() -> dict[str, Any]:
    """The parsed price table. Read once; the file does not change at runtime."""
    global _TABLE
    if _TABLE is None:
        _TABLE = json.loads(TABLE_PATH.read_text())
    return _TABLE


def rates(model: str | None) -> tuple[float, float]:
    """(input, output) USD per million tokens for a model name."""
    name = model or ""
    data = table()
    for entry in data["models"]:
        if name.startswith(entry["prefix"]):
            return float(entry["input"]), float(entry["output"])
    fallback = data["default"]
    return float(fallback["input"]), float(fallback["output"])


def cost(usage: dict[str, Any] | None, model: str | None) -> float:
    """USD for one call's usage dict, cache tiers included.

    `cache_creation.ephemeral_1h_input_tokens` splits the write counter: the
    hour cache is a premium over the five-minute one, and a long session that
    parks its prefix in the 1h tier is billed differently for the same number
    of tokens.
    """
    if not usage:
        return 0.0
    in_rate, out_rate = rates(model)
    mult = table()["multipliers"]

    def n(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    write = n("cache_creation_input_tokens")
    creation = usage.get("cache_creation")
    hour = 0
    if isinstance(creation, dict):
        value = creation.get("ephemeral_1h_input_tokens")
        hour = int(value) if isinstance(value, (int, float)) else 0
    hour = min(hour, write)

    return (
        n("input_tokens") * in_rate
        + n("cache_read_input_tokens") * in_rate * float(mult["cache_read"])
        + (write - hour) * in_rate * float(mult["cache_write_5m"])
        + hour * in_rate * float(mult["cache_write_1h"])
        + n("output_tokens") * out_rate
    ) / 1_000_000.0


def saved(usage: dict[str, Any] | None, model: str | None) -> float:
    """What the cache read would have cost as fresh input, minus what it did."""
    if not usage:
        return 0.0
    in_rate, _ = rates(model)
    read = usage.get("cache_read_input_tokens") or 0
    read = int(read) if isinstance(read, (int, float)) else 0
    return read * in_rate * (1.0 - float(table()["multipliers"]["cache_read"])) / 1_000_000.0


def totals(usages: list[dict[str, Any]]) -> dict[str, int]:
    """Sum the four counters across calls, ignoring anything non-numeric."""
    out = {key: 0 for key in USAGE_KEYS}
    for usage in usages:
        for key in USAGE_KEYS:
            value = (usage or {}).get(key)
            if isinstance(value, (int, float)):
                out[key] += int(value)
    return out
