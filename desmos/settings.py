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

from desmos import auth
from desmos.openai import EFFORTS as OPENAI_EFFORTS, MODELS as OPENAI_MODELS

ANTHROPIC_MODELS = ("claude-opus-5", "claude-sonnet-4-6")
ANTHROPIC_EFFORTS = ("low", "high", "xhigh")

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

    def valid(self) -> bool:
        entry = CATALOG.get(self.provider)
        return bool(entry) and self.model in entry["models"] and self.effort in entry["efforts"]


def provider_of(model: str) -> str:
    from desmos.openai import is_openai

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
    )
    return got if got.valid() else None


def save(settings: Settings) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2))
    os.replace(tmp, path)
    return path


def usable(provider: str) -> bool:
    try:
        auth.credential(provider, allow_refresh=False)
        return True
    except auth.NeedsAuth:
        return False


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
