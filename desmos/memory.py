from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from desmos.persist import state_file
from desmos.types import World

RECORDS_SUBDIR = "memories"
RECORDS_FILENAME = "records.jsonl"
SUMMARY_FILENAME = "memory_summary.md"
HANDBOOK_FILENAME = "MEMORY.md"
LEGACY_FILENAME = "legacy_MEMORY.md"
SUMMARY_BUDGET = 2000
MAX_SEARCH_RESULTS = 12
MAX_READ_CHARS = 5000

_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk|ghp|github_pat)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}", re.I),
    re.compile(
        r"(?i)\b(api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*"
        r"([\"']?)[^\s,\"']{8,}\2"
    ),
)


def memory_root(world: World) -> Path:
    return state_file(world).parent


def records_path(root: Path) -> Path:
    return root / RECORDS_SUBDIR / RECORDS_FILENAME


def summary_path(root: Path) -> Path:
    return root / SUMMARY_FILENAME


def handbook_path(root: Path) -> Path:
    return root / HANDBOOK_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _clean(value: str) -> str:
    return " ".join(value.split())


def _redact(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED_SECRET]", redacted)
        else:
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def _stable_id(scope: str, kind: str, content: str) -> str:
    digest = hashlib.sha256(f"{scope}\0{kind}\0{_clean(content).lower()}".encode()).hexdigest()[:12]
    return f"{scope}.{kind}.{digest}"


def _load_records(root: Path) -> list[dict[str, Any]]:
    path = records_path(root)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("content"), str):
            records.append(item)
    return records


def _write_records(root: Path, records: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records)
    _atomic_write(records_path(root), text)


def _legacy_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current_date = date.today().isoformat()
    chunks: list[tuple[str, str]] = []
    active: list[str] = []

    def flush() -> None:
        if active:
            content = "\n".join(active).strip()
            if content:
                chunks.append((current_date, content))
            active.clear()

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", heading):
                current_date = heading
        elif line.startswith("- "):
            flush()
            active.append(line[2:])
        elif active and (line.startswith("  ") or not line.strip()):
            active.append(line.strip())
    flush()

    now = _now()
    for created, content in chunks:
        safe = _redact(content)
        scope = "user" if re.search(r"\b(user|name|prefers?|asked|Umang)\b", safe, re.I) else "workflow"
        kind = "preference" if scope == "user" else "episode"
        records.append(
            {
                "id": _stable_id(scope, kind, safe),
                "scope": scope,
                "kind": kind,
                "content": safe,
                "status": "active",
                "confidence": "legacy",
                "created": created,
                "updated": now,
                "last_verified": None,
                "sources": ["legacy:MEMORY.md"],
            }
        )
    return records


def _ensure_records(world: World) -> tuple[Path, list[dict[str, Any]]]:
    root = memory_root(world)
    path = records_path(root)
    if path.exists():
        return root, _load_records(root)

    legacy = handbook_path(root)
    records = _legacy_records(legacy.read_text(encoding="utf-8")) if legacy.exists() else []
    if not world.persist:
        return root, records

    root.mkdir(parents=True, exist_ok=True)
    if legacy.exists():
        backup = root / RECORDS_SUBDIR / LEGACY_FILENAME
        if not backup.exists():
            _atomic_write(backup, legacy.read_text(encoding="utf-8"))
    _write_records(root, records)
    _rebuild(root, records)
    return root, records


def _priority(record: dict[str, Any]) -> tuple[int, str, str]:
    scope_rank = {"user": 0, "workflow": 1, "repo": 2, "global": 3, "episode": 4, "volatile": 5}
    return (
        scope_rank.get(str(record.get("scope")), 6),
        str(record.get("kind", "")),
        str(record.get("id", "")),
    )


def _summary(records: list[dict[str, Any]], budget: int = SUMMARY_BUDGET) -> str:
    lines = ["v1", "Durable memory index. Search the memory tool by ID or keywords for details."]
    active = [r for r in records if r.get("status", "active") == "active"]
    for record in sorted(active, key=_priority):
        content = _clean(str(record["content"]))
        if len(content) > 240:
            content = content[:237].rstrip() + "..."
        line = f"- [{record.get('scope', 'repo')}/{record.get('kind', 'note')}] {content} (id: {record['id']})"
        candidate = "\n".join((*lines, line))
        if len(candidate) > budget:
            continue
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def _handbook(records: list[dict[str, Any]]) -> str:
    lines = ["# MEMORY", "", "Generated from structured records. Use the memory tool to modify entries.", ""]
    for record in sorted(records, key=_priority):
        lines.extend(
            [
                f"## {record['id']}",
                (
                    f"- scope: {record.get('scope', 'repo')}; kind: {record.get('kind', 'note')}; "
                    f"status: {record.get('status', 'active')}; confidence: {record.get('confidence', 'explicit')}; "
                    f"updated: {record.get('updated', '')}; verified: {record.get('last_verified') or 'never'}"
                ),
                f"- {record['content']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _rebuild(root: Path, records: list[dict[str, Any]]) -> None:
    _atomic_write(summary_path(root), _summary(records))
    _atomic_write(handbook_path(root), _handbook(records))


def prompt_summary(world: World, budget: int = SUMMARY_BUDGET) -> str:
    root = memory_root(world)
    path = summary_path(root)
    if not path.exists() and world.persist:
        _ensure_records(world)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) > budget:
        text = text[: budget - 4].rstrip() + "\n..."
    return "# memory summary (durable; retrieve details with memory)\n" + text


def _save(root: Path, records: list[dict[str, Any]]) -> None:
    _write_records(root, records)
    _rebuild(root, records)


def _find(records: list[dict[str, Any]], record_id: str) -> dict[str, Any] | None:
    return next((r for r in records if r.get("id") == record_id), None)


def remember(
    world: World,
    content: str,
    *,
    record_id: str = "",
    scope: str = "repo",
    kind: str = "note",
    confidence: str = "explicit",
    source: str = "user",
) -> str:
    if not world.persist:
        return "memory disabled for this non-persistent world"
    content = _redact(content.strip())
    if not content:
        return "memory failed: content required"
    scope = _clean(scope).lower() or "repo"
    kind = _clean(kind).lower() or "note"
    record_id = _clean(record_id) or _stable_id(scope, kind, content)
    root, records = _ensure_records(world)
    now = _now()
    existing = _find(records, record_id)
    duplicate = next(
        (
            r
            for r in records
            if _clean(str(r.get("content", ""))).lower() == _clean(content).lower()
            and r.get("scope") == scope
            and r.get("kind") == kind
        ),
        None,
    )
    target = existing or duplicate
    if target is None:
        target = {
            "id": record_id,
            "scope": scope,
            "kind": kind,
            "content": content,
            "status": "active",
            "confidence": confidence,
            "created": now,
            "updated": now,
            "last_verified": now if confidence in {"verified", "explicit"} else None,
            "sources": [source],
        }
        records.append(target)
        action = "remembered"
    else:
        target.update(
            {
                "scope": scope,
                "kind": kind,
                "content": content,
                "status": "active",
                "confidence": confidence,
                "updated": now,
            }
        )
        sources = target.setdefault("sources", [])
        if source not in sources:
            sources.append(source)
        action = "updated"
    _save(root, records)
    return f"{action} {target['id']} ({len([r for r in records if r.get('status') == 'active'])} active)"


def search(world: World, query: str, *, max_results: int = MAX_SEARCH_RESULTS, mode: str = "all") -> str:
    _, records = _ensure_records(world)
    terms = [term.lower() for term in query.split() if term]
    if not terms:
        return "memory search failed: query required"
    hits = []
    for record in records:
        if record.get("status", "active") != "active":
            continue
        blob = " ".join(
            str(record.get(key, "")) for key in ("id", "scope", "kind", "content", "confidence")
        ).lower()
        matched = all(term in blob for term in terms) if mode != "any" else any(term in blob for term in terms)
        if matched:
            hits.append(record)
    if not hits:
        return "no match"
    lines = []
    for record in sorted(hits, key=_priority)[: max(1, min(max_results, MAX_SEARCH_RESULTS))]:
        content = _clean(str(record["content"]))
        if len(content) > 320:
            content = content[:317].rstrip() + "..."
        lines.append(f"{record['id']} [{record.get('scope')}/{record.get('kind')}] {content}")
    if len(hits) > len(lines):
        lines.append(f"... {len(hits) - len(lines)} more")
    return "\n".join(lines)


def read(world: World, record_id: str) -> str:
    _, records = _ensure_records(world)
    record = _find(records, record_id.strip())
    if record is None:
        return f"memory not found: {record_id.strip()}"
    text = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
    return text[:MAX_READ_CHARS] + ("\n..." if len(text) > MAX_READ_CHARS else "")


def forget(world: World, record_id: str) -> str:
    if not world.persist:
        return "memory disabled for this non-persistent world"
    root, records = _ensure_records(world)
    record = _find(records, record_id.strip())
    if record is None:
        return f"memory not found: {record_id.strip()}"
    record["status"] = "forgotten"
    record["updated"] = _now()
    _save(root, records)
    return f"forgot {record['id']}"


def verify(world: World, record_id: str) -> str:
    if not world.persist:
        return "memory disabled for this non-persistent world"
    root, records = _ensure_records(world)
    record = _find(records, record_id.strip())
    if record is None:
        return f"memory not found: {record_id.strip()}"
    now = _now()
    record["last_verified"] = now
    record["updated"] = now
    record["confidence"] = "verified"
    _save(root, records)
    return f"verified {record['id']} at {now}"


def consolidate(world: World) -> str:
    if not world.persist:
        return "memory disabled for this non-persistent world"
    root, records = _ensure_records(world)
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    merged: list[dict[str, Any]] = []
    duplicates = 0
    for record in records:
        key = (
            str(record.get("scope", "repo")),
            str(record.get("kind", "note")),
            _clean(str(record.get("content", ""))).lower(),
        )
        existing = seen.get(key)
        if existing is None:
            seen[key] = record
            merged.append(record)
            continue
        duplicates += 1
        existing["updated"] = max(str(existing.get("updated", "")), str(record.get("updated", "")))
        existing["sources"] = sorted(set(existing.get("sources", [])) | set(record.get("sources", [])))
        if record.get("last_verified"):
            existing["last_verified"] = max(
                str(existing.get("last_verified") or ""), str(record["last_verified"])
            )
    _save(root, merged)
    active = sum(r.get("status", "active") == "active" for r in merged)
    return f"consolidated {active} active memories ({duplicates} duplicates merged)"


def show(world: World) -> str:
    root, records = _ensure_records(world)
    active = sum(r.get("status", "active") == "active" for r in records)
    forgotten = sum(r.get("status") == "forgotten" for r in records)
    try:
        summary = summary_path(root).read_text(encoding="utf-8").strip()
    except OSError:
        summary = ""
    return f"{active} active, {forgotten} forgotten\n\n{summary}".strip()


def handle_memory(world: World, body: str, attrs: dict[str, str] | None = None) -> str:
    attrs = attrs or {}
    raw = body.strip()
    action = (attrs.get("action") or "").strip().lower()
    payload = raw

    if not action:
        for command in ("show", "consolidate"):
            if raw == command:
                action, payload = command, ""
                break
        else:
            for command in ("search", "grep", "read", "forget", "verify", "remember"):
                prefix = command + " "
                if raw.startswith(prefix):
                    action, payload = command, raw[len(prefix) :].strip()
                    break
    action = action or "remember"

    if action == "show":
        return show(world)
    if action in {"search", "grep"}:
        try:
            limit = int(attrs.get("max", MAX_SEARCH_RESULTS))
        except ValueError:
            return "memory search failed: max must be an integer"
        return search(world, payload, max_results=limit, mode=attrs.get("mode", "all"))
    if action == "read":
        return read(world, attrs.get("id") or payload)
    if action == "forget":
        return forget(world, attrs.get("id") or payload)
    if action == "verify":
        return verify(world, attrs.get("id") or payload)
    if action == "consolidate":
        return consolidate(world)
    if action != "remember":
        return f"memory failed: unknown action {action!r}"
    return remember(
        world,
        payload,
        record_id=attrs.get("id", ""),
        scope=attrs.get("scope", "repo"),
        kind=attrs.get("kind", "note"),
        confidence=attrs.get("confidence", "explicit"),
        source=attrs.get("source", "user"),
    )
