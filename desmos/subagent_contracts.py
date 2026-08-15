from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# Evidence kinds that assert something was observed at runtime. A child that
# declares one of these without ever calling a tool is narrating, not working.
OBSERVABLE_EVIDENCE = frozenset(
    {"command", "output", "file", "path", "test", "log", "artifact", "diff", "run"}
)


@dataclass(frozen=True)
class TaskContract:
    objective: str
    non_goals: tuple[str, ...] = ()
    deliverable_schema: str = "A concise final summary."
    required_evidence: tuple[str, ...] = ()
    acceptance_checks: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    require_tool_use: bool = True
    compact: bool = False

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("task objective is required")
        if not self.deliverable_schema.strip():
            raise ValueError("deliverable_schema is required")
        if self.write_paths and not self.allowed_paths:
            raise ValueError("write_paths require allowed_paths")
        outside = [path for path in self.write_paths if path not in self.allowed_paths]
        if outside:
            raise ValueError(f"write paths are outside allowed_paths: {outside}")

    @classmethod
    def legacy(cls, task: str) -> TaskContract:
        return cls(objective=task)

    @classmethod
    def simple(
        cls,
        objective: str,
        *,
        paths: tuple[str, ...] | list[str] = (),
        write: tuple[str, ...] | list[str] = (),
        checks: tuple[str, ...] | list[str] = (),
        tools: tuple[str, ...] | list[str] = (),
        depends: tuple[str, ...] | list[str] = (),
        evidence: tuple[str, ...] | list[str] = (),
    ) -> TaskContract:
        """Build a judged contract without repeating the full schema vocabulary."""

        return cls(
            objective=objective,
            allowed_paths=tuple(paths),
            write_paths=tuple(write),
            acceptance_checks=tuple(checks),
            allowed_tools=tuple(tools),
            dependencies=tuple(depends),
            required_evidence=tuple(evidence),
            compact=True,
        )

    def prompt(self) -> str:
        payload = {
            "objective": self.objective,
            "non_goals": list(self.non_goals),
            "deliverable_schema": self.deliverable_schema,
            "required_evidence": list(self.required_evidence),
            "acceptance_checks": list(self.acceptance_checks),
            "allowed_tools": list(self.allowed_tools),
            "allowed_paths": list(self.allowed_paths),
            "write_paths": list(self.write_paths),
            "dependencies": list(self.dependencies),
            "require_tool_use": self.require_tool_use,
        }
        if self.compact:
            payload = {
                key: value
                for key, value in payload.items()
                if value not in ([], (), "", False)
            }
            return (
                "Execute this compact judged task. Stay inside its scope.\n"
                + json.dumps(payload, separators=(",", ":"), sort_keys=True)
                + "\nReturn only JSON with keys summary, claims, artifacts, changed_paths, "
                "checks, failures, unresolved. Each claim is {text,evidence}; each check is "
                "{name,passed,evidence}; each evidence item is {kind,reference,detail}. "
                "Use empty arrays where needed. Every claim and passed check needs observed evidence."
            )

        result_shape = {
            "summary": "human-readable summary",
            "claims": [
                {
                    "text": "claim",
                    "evidence": [
                        {
                            "kind": "file_line|command|artifact|observation",
                            "reference": "stable reference",
                            "detail": "what it proves",
                        }
                    ],
                }
            ],
            "artifacts": ["path or artifact identifier"],
            "changed_paths": ["path"],
            "checks": [
                {
                    "name": "must exactly match one declared acceptance check",
                    "passed": True,
                    "evidence": [
                        {
                            "kind": "command|file_line|observation",
                            "reference": "stable reference",
                            "detail": "observed result",
                        }
                    ],
                }
            ],
            "failures": [],
            "unresolved": [],
        }
        return (
            "Execute this typed task contract. Treat the contract as authoritative.\n\n"
            + json.dumps(payload, indent=2, sort_keys=True)
            + "\n\nYour final answer must be only one JSON object with this shape:\n"
            + json.dumps(result_shape, indent=2)
            + "\nEvery claim and passed acceptance check needs concrete evidence. "
            "Do not report a check as passed merely because you intended to run it."
        )


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    reference: str
    detail: str = ""

    def valid(self) -> bool:
        return bool(self.kind.strip() and self.reference.strip())


@dataclass(frozen=True)
class Claim:
    text: str
    evidence: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    evidence: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class RunResult:
    terminal_state: str
    stop_reason: str
    summary: str
    claims: tuple[Claim, ...] = ()
    artifacts: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    checks: tuple[CheckResult, ...] = ()
    failures: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    duration: float = 0.0
    retries: int = 0
    raw: str = ""


@dataclass(frozen=True)
class Judgment:
    accepted: bool
    reasons: tuple[str, ...] = ()


def _evidence(raw: Any) -> tuple[EvidenceRef, ...]:
    if not isinstance(raw, list):
        return ()
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        evidence = EvidenceRef(
            kind=str(item.get("kind") or ""),
            reference=str(item.get("reference") or ""),
            detail=str(item.get("detail") or ""),
        )
        if evidence.valid():
            out.append(evidence)
    return tuple(out)


def _json_object(text: str) -> dict[str, Any]:
    body = text.strip()
    if body.startswith("```"):
        lines = body.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    try:
        value = json.loads(body)
    except ValueError:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(body[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("structured result must be a JSON object")
    return value


def parse_run_result(
    text: str,
    *,
    terminal_state: str,
    stop_reason: str,
    usage: dict[str, int],
    duration: float,
    retries: int = 0,
) -> RunResult:
    try:
        raw = _json_object(text)
    except (TypeError, ValueError) as exc:
        return RunResult(
            terminal_state=terminal_state,
            stop_reason="invalid_result",
            summary=text.strip(),
            failures=(f"invalid structured result: {exc}",),
            usage=dict(usage),
            duration=duration,
            retries=retries,
            raw=text,
        )

    claims = []
    for item in raw.get("claims", []):
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            claims.append(Claim(str(item["text"]).strip(), _evidence(item.get("evidence"))))
    checks = []
    for item in raw.get("checks", []):
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            checks.append(
                CheckResult(
                    name=str(item["name"]).strip(),
                    passed=item.get("passed") is True,
                    evidence=_evidence(item.get("evidence")),
                )
            )

    def strings(name: str) -> tuple[str, ...]:
        value = raw.get(name, [])
        return tuple(str(item) for item in value if isinstance(item, str) and item.strip()) if isinstance(value, list) else ()

    return RunResult(
        terminal_state=terminal_state,
        stop_reason=stop_reason,
        summary=str(raw.get("summary") or "").strip(),
        claims=tuple(claims),
        artifacts=strings("artifacts"),
        changed_paths=strings("changed_paths"),
        checks=tuple(checks),
        failures=strings("failures"),
        unresolved=strings("unresolved"),
        usage=dict(usage),
        duration=duration,
        retries=retries,
        raw=text,
    )


def judge(
    contract: TaskContract,
    result: RunResult,
    observed_tools: tuple[str, ...] = (),
) -> Judgment:
    """Judge declarations against the parent's observed runtime events."""
    reasons: list[str] = []
    if contract.require_tool_use and not observed_tools:
        reasons.append("no tool use observed")
    if result.terminal_state != "done":
        reasons.append(f"terminal state is {result.terminal_state}")
    if result.stop_reason not in {"completed", "end_turn"}:
        reasons.append(f"stop reason is {result.stop_reason}")
    if not result.summary:
        reasons.append("summary is empty")
    for claim in result.claims:
        if not claim.evidence:
            reasons.append(f"claim lacks evidence: {claim.text}")
    checks = {check.name: check for check in result.checks}
    for required in contract.acceptance_checks:
        check = checks.get(required)
        if check is None:
            reasons.append(f"missing acceptance check: {required}")
        elif not check.passed:
            reasons.append(f"acceptance check failed: {required}")
        elif not check.evidence:
            reasons.append(f"acceptance check lacks evidence: {required}")
    declared_kinds = {
        evidence.kind
        for claim in result.claims
        for evidence in claim.evidence
    } | {
        evidence.kind
        for check in result.checks
        for evidence in check.evidence
    }
    if declared_kinds & OBSERVABLE_EVIDENCE and not observed_tools:
        reasons.append("observable evidence was declared but no tool use was observed")
    if contract.required_evidence:
        kinds = {
            evidence.kind
            for claim in result.claims
            for evidence in claim.evidence
        } | {
            evidence.kind
            for check in result.checks
            for evidence in check.evidence
        }
        for required in contract.required_evidence:
            if required not in kinds:
                reasons.append(f"missing required evidence kind: {required}")
    if result.changed_paths:
        if not contract.write_paths:
            reasons.append("result reports changed paths for a read-only contract")
        for changed in result.changed_paths:
            if changed not in contract.write_paths:
                reasons.append(f"changed path is outside write scope: {changed}")
    if result.failures:
        reasons.extend(f"reported failure: {failure}" for failure in result.failures)
    return Judgment(accepted=not reasons, reasons=tuple(reasons))
