"""Report desmos lifecycle state to the Herdr pane sidebar.

Desmos is not a registered Herdr agent kind, so its pane reads "unknown".
Herdr's pane.report_agent accepts an unregistered agent label, so this module
maps the existing wire-event stream onto Herdr's four pane states and pushes
each transition over the Herdr control socket. Pure stdlib; inert (zero
sockets, zero threads) unless HERDR_ENV=1, HERDR_SOCKET_PATH and HERDR_PANE_ID
are all set; every exception is swallowed so a sidebar hiccup can never reach
the turn loop.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import time
from typing import Any

#: ev field -> Herdr pane state. Every other ev is ignored on purpose:
#: only lifecycle edges belong in a sidebar, not deltas or telemetry.
_STATE_BY_EV = {
    "prompt": "working",
    "turn": "working",
    "done": "idle",
    "stopped": "idle",
    "decision": "blocked",
    "error": "idle",
}

_uniq = itertools.count(1)
_last_state: str | None = None


def _target() -> tuple[str, str] | None:
    """The (socket_path, pane_id) pair, or None when reporting is inert."""
    if os.environ.get("HERDR_ENV") != "1":
        return None
    path = os.environ.get("HERDR_SOCKET_PATH")
    pane = os.environ.get("HERDR_PANE_ID")
    if not path or not pane:
        return None
    return path, pane


def observe(event: dict[str, Any]) -> None:
    """Map one wire event to a pane state and report the transition.

    Duplicate consecutive states are suppressed; the sidebar only needs
    edges. Never raises.
    """
    global _last_state
    try:
        target = _target()
        if target is None:
            return
        state = _STATE_BY_EV.get(str(event.get("ev")))
        if state is None or state == _last_state:
            return
        _last_state = state
        path, pane = target
        params: dict[str, Any] = {
            "pane_id": pane,
            "source": "herdr:desmos",
            "agent": "desmos",
            "state": state,
            "seq": time.time_ns(),
        }
        text = event.get("text")
        if isinstance(text, str) and text:
            params["message"] = text[:120]
        frame = {
            "id": next(_uniq),
            "method": "pane.report_agent",
            "params": params,
        }
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(0.5)
            conn.connect(path)
            line = json.dumps(frame, separators=(",", ":")) + "\n"
            conn.sendall(line.encode("utf-8"))
            conn.recv(4096)  # read and discard the reply
        finally:
            conn.close()
    except Exception:
        pass
