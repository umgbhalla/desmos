"""What this install has been told to use, and whether it can be used.

Three fields, one file at ~/.desmos/settings.json: provider, model, effort.
The file existing is also the answer to "has this user been onboarded" -- a
fresh machine has no file, so the TUI opens the picker instead of guessing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from desmos.transport import auth
from desmos.transport.openai import EFFORTS as OPENAI_EFFORTS, MODELS as OPENAI_MODELS

ANTHROPIC_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-4-6")
# Probed against the API, which answers "Input should be 'low', 'medium',
# 'high', 'xhigh' or 'max'" -- the same five rungs OpenAI offers. The old
# three-value list was a guess that hid medium and max from the picker.
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")

CATALOG: dict[str, dict[str, Any]] = {
    "anthropic": {"models": list(ANTHROPIC_MODELS), "efforts": list(ANTHROPIC_EFFORTS)},
    "openai": {"models": list(OPENAI_MODELS), "efforts": list(OPENAI_EFFORTS)},
}


def settings_path() -> Path:
    return Path(os.environ.get("DESMOS_SETTINGS") or (Path.home() / ".desmos" / "settings.json"))


@dataclass
class Settings:
    provider: str = "anthropic"
    model: str = "claude-opus-5"
    effort: str = "low"
    # What a resident agent elsewhere should call the human at this seat.
    # Empty means nobody said, and callers fall back to the seat name.
    user: str = ""

    def valid(self) -> bool:
        entry = CATALOG.get(self.provider)
        return bool(entry) and self.model in entry["models"] and self.effort in entry["efforts"]


# Weakest to strongest. Only used to find the nearest rung on another ladder.
_EFFORT_ORDER = ("none", "low", "medium", "high", "xhigh", "max")


def clamp_effort(provider: str, effort: str) -> str:
    """The nearest effort this provider actually offers.

    The two ladders are different lengths -- OpenAI has medium and max,
    Anthropic does not -- so an effort that is perfectly valid on one is
    unknown on the other. Rejecting the switch on that basis meant a session
    running sol at medium simply could not move to Opus: the bridge answered
    "unknown model/effort" and stayed where it was. The effort is not what the
    user asked to change, so it should bend rather than block.
    """
    have = CATALOG.get(provider, {}).get("efforts") or []
    if not have or effort in have:
        return effort if effort in have else (have[0] if have else effort)
    if effort not in _EFFORT_ORDER:
        return have[0]
    want = _EFFORT_ORDER.index(effort)
    # Nearest by intensity; a tie goes to the stronger rung, because quietly
    # thinking less is the worse surprise.
    return min(have, key=lambda e: (abs(_EFFORT_ORDER.index(e) - want), -_EFFORT_ORDER.index(e)))


def provider_of(model: str) -> str:
    from desmos.transport.openai import is_openai

    return "openai" if is_openai(model) else "anthropic"


def load() -> Settings | None:
    """None means never configured. A corrupt file is treated as never configured."""
    try:
        raw = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    got = Settings(
        provider=str(raw.get("provider") or "anthropic"),
        model=str(raw.get("model") or ""),
        effort=str(raw.get("effort") or "low"),
        user=str(raw.get("user") or ""),
    )
    return got if got.valid() else None


def save(settings: Settings) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    body = {k: v for k, v in asdict(settings).items() if v != ""}
    tmp.write_text(json.dumps(body, indent=2))
    os.replace(tmp, path)
    return path


def usable(provider: str) -> bool:
    try:
        auth.credential(provider, allow_refresh=False)
        return True
    except auth.NeedsAuth:
        return False


def switch(world: Any, model: str, effort: str | None = None) -> str:
    """Point this world at another model, and say what it cost.

    The bridge's `op: model` and a `<python>switch(...)` call are the same
    operation, so they are the same function. Assigning `world.model` by hand
    reaches the next `complete()` too -- `turn` reads it fresh every time --
    but skips validation, the credential check, and the settings write, so it
    can hand the next turn a model this machine cannot call.

    Returns the line to show the human. Raises ValueError when the choice is
    not a real one, because a switch that silently did not happen is worse
    than a refusal: every later turn reports a model the wire is not using.
    """
    choice = Settings(provider=provider_of(model), model=model, effort=effort or world.thinking)
    if not choice.valid():
        raise ValueError(f"unknown model/effort: {choice.model} {choice.effort}")
    if not usable(choice.provider):
        raise ValueError(f"{choice.provider} has no usable credential")

    was = provider_of(str(world.model or ""))
    world.model, world.thinking = choice.model, choice.effort
    save(choice)
    if was and was != choice.provider:
        # wire_content fences blocks across providers: the other provider's
        # thinking and its signatures cannot be replayed, so they are dropped
        # from every later request. That is a real change to what the model can
        # see, and silence about it reads as the harness losing reasoning for
        # no reason.
        return (
            f"provider switched {was} → {choice.provider}. Speech and syscall results "
            f"replay in full, but {was} thinking blocks cannot cross providers and are "
            f"dropped from later requests — the new model reads the conversation without "
            f"the old model's reasoning."
        )
    return f"model {choice.model} effort {choice.effort} — in effect from the next turn."


def picker() -> dict[str, Any]:
    """Everything the onboarding screen needs, so the TUI hardcodes nothing."""
    current = load()
    rows = {r["provider"]: r for r in auth.status()}
    providers = []
    for name, entry in CATALOG.items():
        row = rows.get(name) or {"ok": False, "detail": "unknown provider"}
        providers.append(
            {
                "provider": name,
                "ok": bool(row.get("ok")),
                "detail": row.get("detail") or "",
                "account": row.get("account") or "",
                "plan": row.get("plan") or "",
                "source": row.get("source") or "",
                "can_login": name == "openai",
                "models": entry["models"],
                "efforts": entry["efforts"],
            }
        )
    return {
        "onboarding": current is None,
        "current": asdict(current) if current else None,
        "providers": providers,
    }


def resolve() -> Settings:
    """What a session should start with. Saved choice first, then whatever works."""
    got = load()
    if got and usable(got.provider):
        return got
    env = os.environ.get("DESMOS_MODEL")
    if env:
        return Settings(provider=provider_of(env), model=env, effort="low")
    for name, entry in CATALOG.items():
        if usable(name):
            return Settings(provider=name, model=entry["models"][0], effort="low")
    return got or Settings()
