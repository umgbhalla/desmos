"""The D1 sink: the only part of sync that touches a network.

The outbox holds facts and knows nothing about transport. This module is the
other half: a callable that takes one batch and posts it to a Cloudflare
Worker sitting in front of a D1 database. It is deliberately small and
deliberately boring -- stdlib urllib, one POST, no retry loop of its own,
because retry already lives in the outbox and two retry policies is one too
many.

Push-only. Nothing here reads from D1, and nothing here deletes. The Worker
is expected to INSERT OR IGNORE on ``fingerprint``, which makes a redelivered
batch a no-op on the far side as well as on this one: the same fingerprint
that dedupes the local queue is the primary key up there.

Configuration is environment, not a file: ``DESMOS_D1_URL`` and
``DESMOS_D1_TOKEN``. Absent either, ``push`` reports itself unconfigured and
does nothing -- a harness with no cloud is the ordinary case, not an error.

UNVERIFIED against Cloudflare's own limits: D1 row size, request size and
statement caps are not read from their docs here, so a batch that is legal
locally may be refused up there. The failure path is a non-2xx, which leaves
every row of the batch pending. That is the whole reason the drain marks a
batch sent only after the sink returns.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from desmos.kernel.types import World
from desmos.state import outbox

URL_ENV = "DESMOS_D1_URL"
TOKEN_ENV = "DESMOS_D1_TOKEN"
TIMEOUT = 15.0


class SinkError(RuntimeError):
    """The far side refused or could not be reached. The batch stays queued."""


def configured() -> tuple[str, str]:
    return os.environ.get(URL_ENV, "").strip(), os.environ.get(TOKEN_ENV, "").strip()


def _wire(batch: list[dict[str, Any]]) -> dict[str, Any]:
    """What goes on the wire: the fact, its fingerprint, and nothing local.

    Row ids, attempt counts and error strings are this machine's bookkeeping
    and are not the far side's business.
    """
    return {
        "rows": [
            {
                "fingerprint": str(row["fingerprint"]),
                "kind": str(row["kind"]),
                "created_at": str(row["created_at"]),
                "workspace_id": str(row["workspace_id"]),
                "payload": row.get("payload"),
            }
            for row in batch
        ]
    }


def sink(
    url: str, token: str = "", timeout: float = TIMEOUT
) -> Callable[[list[dict[str, Any]]], dict[str, Any]]:
    """Build the callable the outbox drain expects."""

    def send(batch: list[dict[str, Any]]) -> dict[str, Any]:
        body = json.dumps(_wire(batch)).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "desmos-outbox/1",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 0) or 0)
                text = response.read().decode("utf-8", "replace")[:400]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise SinkError(f"d1 sink refused {exc.code}: {detail}") from exc
        except Exception as exc:  # connection refused, DNS, timeout
            raise SinkError(f"d1 sink unreachable: {exc}") from exc
        if not 200 <= status < 300:
            raise SinkError(f"d1 sink returned {status}: {text}")
        return {"status": status, "body": text}

    return send


def push(world: World, limit: int = outbox.BATCH) -> dict[str, Any]:
    """Drain one batch to the configured Worker, or say why not.

    Called by a caller who does not care whether a cloud exists: an
    unconfigured harness gets an explicit report and an untouched queue.
    """
    url, token = configured()
    depth = len(outbox.pending(world, limit))
    if not url:
        return {"configured": False, "sent": 0, "pending": depth, "error": ""}
    result = outbox.drain(world, sink(url, token), limit)
    return {
        "configured": True,
        "sent": int(result["sent"]),
        "failed": int(result["failed"]),
        "error": str(result["error"]),
        "pending": len(outbox.pending(world, limit)),
    }
