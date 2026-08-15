from __future__ import annotations

"""XML-facing controller for typed and batched subagent launches."""

import json
import shlex
from typing import Any

import desmos.agents.subagent as S
from desmos.agents.subagent_contracts import TaskContract


def _table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no runs"
    return "\n".join(
        f"{r['id']} {r.get('agent', ''):<9} {r.get('state', ''):<8} "
        f"{r.get('secs', 0):>6}s turns={r.get('turns', 0)} "
        f"{r.get('task', '')[:44]}"
        for r in rows
    )


def _contract(raw: dict[str, Any]) -> TaskContract:
    data = dict(raw)
    for name in (
        "non_goals",
        "required_evidence",
        "acceptance_checks",
        "allowed_tools",
        "allowed_paths",
        "write_paths",
        "dependencies",
    ):
        if name in data:
            data[name] = tuple(data[name])
    return TaskContract(**data)


def _spec(raw: dict[str, Any]) -> dict[str, Any]:
    item = dict(raw)
    if "contract" in item:
        if "task" in item:
            raise ValueError("a launch item takes task or contract, not both")
        contract = item.pop("contract")
        if not isinstance(contract, dict):
            raise TypeError("contract must be an object")
        item["task"] = _contract(contract)
    return item


def _watch(ids: list[str]) -> str:
    """Report, do not block.

    A blocking wait spends the model's turn asleep and puts a queued follow-up
    behind a call that is only counting seconds. Every spawn is already
    registered as a background task, so the step ends and the loop resumes it
    with the child's outcome when the child actually settles.
    """
    runs = S.status()
    if ids:
        runs = [r for r in runs if r["id"] in set(ids)]
    live = [r for r in runs if r["state"] in ("pending", "running")]
    head = (
        f"{len(live)} still running; this session resumes on its own when one settles."
        if live
        else "nothing left running."
    )
    return head + ("\n" + _table(runs) if runs else "")


def _json_command(data: dict[str, Any]) -> str:
    op = str(data.get("op", "spawn_many"))
    if op in {"spawn_many", "fanout"}:
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("spawn_many requires a non-empty tasks array")
        ids = S.spawn_many([_spec(item) for item in tasks])
        return json.dumps({"ids": ids, "count": len(ids)})
    if op == "spawn":
        item = _spec(dict(data.get("launch") or data))
        item.pop("op", None)
        ids = S.spawn_many([item])
        return json.dumps({"ids": ids, "count": 1})
    if op == "wait":
        ids = [str(x) for x in data.get("ids", ())]
        if data.get("block"):
            timeout = float(data.get("timeout", 600))
            return _table(S.wait(*ids, timeout=timeout))
        return _watch(ids)
    if op == "status":
        return _table(S.status())
    if op == "result":
        return S.result(str(data["id"]))
    raise ValueError(f"unknown agents operation {op!r}")


def handle(body: str, **_attrs: str) -> str:
    text = (body or "").strip()
    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError("agents JSON command must be an object")
        return _json_command(data)
    if text.startswith("spawn "):
        head, sep, task = text[6:].partition(":")
        if not sep:
            return "usage: spawn <agent> [model=<model>] [thinking=<effort>]: task"
        words = shlex.split(head)
        agent = words.pop(0) if words else "general"
        over: dict[str, Any] = {}
        for word in words:
            key, mark, value = word.partition("=")
            if not mark:
                raise ValueError(f"bad launch override {word!r}; expected key=value")
            over[key] = value
        rid = S.spawn(task.strip(), agent=agent, **over)
        return f"spawned {rid} ({agent})"
    if text.startswith("wait"):
        return _watch(text.split()[1:])
    if text.startswith("result "):
        return S.result(text.split()[1])
    if text in {"status", ""}:
        return _table(S.status())
    return (
        "usage: spawn <agent> [model=<model>]: task | JSON spawn_many/status/wait/result"
    )
