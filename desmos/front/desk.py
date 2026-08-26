"""First-party desktop/web UI for Desmos.

The kernel stays the ACP agent. This module is paint plus a transport:
HTTP serves the SPA, one WebSocket carries the same JSON-RPC objects
`python -m desmos acp` writes as NDJSON on stdio. There is no second loop,
no fake handshake, and no demo engine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import socket
import struct
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from desmos.front.acp import AcpServer, rpc_error

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
STATIC_DIR = Path(__file__).resolve().parent / "desk_static"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def _accept_key(key: str) -> str:
    digest = hashlib.sha1((key.strip() + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(payload: bytes, *, opcode: int = 1, fin: bool = True) -> bytes:
    """Server-to-client WebSocket frame. Unmasked, as the RFC requires."""
    header = bytes([(0x80 if fin else 0x00) | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack("!H", n)
    else:
        header += bytes([127]) + struct.pack("!Q", n)
    return header + payload


def _mask(payload: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def decode_frames(buf: bytearray) -> Iterator[tuple[int, bytes]]:
    """Yield complete (opcode, payload) frames. Leaves a partial frame in buf."""
    while True:
        if len(buf) < 2:
            return
        b0, b1 = buf[0], buf[1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        n = b1 & 0x7F
        offset = 2
        if n == 126:
            if len(buf) < 4:
                return
            n = struct.unpack("!H", buf[2:4])[0]
            offset = 4
        elif n == 127:
            if len(buf) < 10:
                return
            n = struct.unpack("!Q", buf[2:10])[0]
            offset = 10
        if masked:
            if len(buf) < offset + 4:
                return
            key = bytes(buf[offset : offset + 4])
            offset += 4
        else:
            key = b""
        if len(buf) < offset + n:
            return
        payload = bytes(buf[offset : offset + n])
        del buf[: offset + n]
        if masked:
            payload = _mask(payload, key)
        yield opcode, payload


class WsClient:
    """One browser connection. send() is the only writer of the socket."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.dead = False
        self.lock = threading.Lock()

    def send(self, obj: dict[str, Any]) -> None:
        data = encode_frame(json.dumps(obj, default=str).encode("utf-8"))
        with self.lock:
            if self.dead:
                raise OSError("ws dead")
            try:
                self.sock.sendall(data)
            except OSError:
                self.dead = True
                raise

    def close(self, code: int = 1000) -> None:
        with self.lock:
            self.dead = True
            try:
                self.sock.sendall(encode_frame(struct.pack("!H", code), opcode=8))
            except OSError:
                pass
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass


class DeskHub:
    """One AcpServer, fan-out of session/update, per-client RPC replies."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = Path(cwd).resolve()
        self.lock = threading.Lock()
        self.clients: list[WsClient] = []
        self.server = AcpServer(self._on_agent, default_cwd=self.cwd)

    def _on_agent(self, obj: dict[str, Any]) -> None:
        dead: list[WsClient] = []
        with self.lock:
            live = list(self.clients)
        for client in live:
            try:
                client.send(obj)
            except OSError:
                dead.append(client)
        if dead:
            with self.lock:
                for client in dead:
                    if client in self.clients:
                        self.clients.remove(client)
                    client.close()

    def attach(self, client: WsClient) -> None:
        with self.lock:
            self.clients.append(client)

    def detach(self, client: WsClient) -> None:
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)

    def dispatch(self, client: WsClient, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "session/prompt" and "id" in msg:
            threading.Thread(
                target=self._rpc, args=(client, msg), name="desk-prompt", daemon=True
            ).start()
            return
        self._rpc(client, msg)

    def _rpc(self, client: WsClient, msg: dict[str, Any]) -> None:
        try:
            resp = self.server.handle(msg)
        except Exception as exc:  # noqa: BLE001 — keep the UI alive
            if "id" in msg:
                resp = rpc_error(msg.get("id"), -32603, f"{type(exc).__name__}: {exc}")
            else:
                resp = None
        if resp is not None:
            try:
                client.send(resp)
            except OSError:
                pass


def _static_path(rel: str) -> Path | None:
    raw = rel.lstrip("/") or "index.html"
    if raw.startswith("assets/"):
        raw = raw[len("assets/") :]
    candidate = (STATIC_DIR / raw).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    if raw == "index.html":
        return None
    index = STATIC_DIR / "index.html"
    return index if index.is_file() else None


class DeskHandler(BaseHTTPRequestHandler):
    hub: DeskHub
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/acp", "/ws"}:
            self._upgrade_ws()
            return
        if parsed.path == "/health":
            body = json.dumps(
                {"ok": True, "cwd": str(self.hub.cwd), "clients": len(self.hub.clients)}
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        path = _static_path(parsed.path)
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/md":
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(max(0, length)) if length else b""
        try:
            from desmos.front.mdhtml import render as _md

            html = _md(raw.decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001 — the UI still has escaped source
            body = f"markdown renderer failed: {type(exc).__name__}: {exc}".encode("utf-8")
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = _static_path(parsed.path)
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()

    def _upgrade_ws(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if not key or upgrade != "websocket":
            self.send_error(HTTPStatus.BAD_REQUEST, "expected websocket upgrade")
            return
        accept = _accept_key(key)
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        sock = self.connection
        sock.settimeout(None)
        client = WsClient(sock)
        self.hub.attach(client)
        buf = bytearray()
        try:
            while not client.dead:
                ready, _, _ = select.select([sock], [], [], 30.0)
                if not ready:
                    try:
                        client.send({"jsonrpc": "2.0", "method": "desk/ping", "params": {}})
                    except OSError:
                        break
                    continue
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                for opcode, payload in decode_frames(buf):
                    if opcode in (8,):  # close
                        return
                    if opcode == 9:  # ping
                        with client.lock:
                            if not client.dead:
                                try:
                                    sock.sendall(encode_frame(payload, opcode=10))
                                except OSError:
                                    return
                        continue
                    if opcode == 10:
                        continue
                    if opcode not in (1, 2):
                        continue
                    try:
                        msg = json.loads(payload.decode("utf-8"))
                    except ValueError as exc:
                        client.send(rpc_error(None, -32700, f"Parse error: {exc}"))
                        continue
                    if not isinstance(msg, dict):
                        client.send(rpc_error(None, -32600, "Invalid Request"))
                        continue
                    self.hub.dispatch(client, msg)
        finally:
            self.hub.detach(client)
            client.close()
            # The HTTP server would try to close a socket we already shut.
            self.close_connection = True

    def finish(self) -> None:
        if getattr(self, "close_connection", True) and hasattr(self, "connection"):
            try:
                super().finish()
            except OSError:
                pass


def _handler_for(hub: DeskHub) -> type[DeskHandler]:
    class Bound(DeskHandler):
        pass

    Bound.hub = hub
    return Bound


def serve(
    cwd: Path | str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 7734,
    open_browser: bool = True,
) -> int:
    """Block serving the UI until the process is killed."""
    try:
        from desmos.front.mdhtml import ensure as _ensure_md

        _ensure_md(release=False)
    except Exception as exc:  # noqa: BLE001 — Desk still serves; /md will 503
        print(f"desmos-md-html: {exc}", file=sys.stderr, flush=True)
    hub = DeskHub(Path(cwd or Path.cwd()).resolve())
    httpd = ThreadingHTTPServer((host, int(port)), _handler_for(hub))
    actual = httpd.server_address[1]
    url = f"http://{host}:{actual}/"
    print(f"desmos desk {url}  cwd={hub.cwd}", flush=True)
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def serve_thread(
    cwd: Path | str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[int, Callable[[], None], DeskHub]:
    """Background server for checks. port=0 binds an ephemeral port."""
    hub = DeskHub(Path(cwd or Path.cwd()).resolve())
    httpd = ThreadingHTTPServer((host, int(port)), _handler_for(hub))
    actual = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, name="desk-http", daemon=True)
    thread.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        probe = socket.socket()
        try:
            probe.settimeout(0.05)
            probe.connect((host, actual))
            break
        except OSError:
            time.sleep(0.01)
        finally:
            probe.close()

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    return actual, stop, hub


class RpcClient:
    """Stdlib WebSocket JSON-RPC client used by checks and the round-trip proof."""

    def __init__(self, host: str, port: int, path: str = "/acp") -> None:
        self.host = host
        self.port = port
        self.path = path
        self.sock = socket.create_connection((host, port), timeout=8)
        self.buf = bytearray()
        self._handshake()
        self.sock.settimeout(8)

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("desk websocket handshake closed")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise OSError(f"desk websocket handshake refused: {status!r}")
        expect = _accept_key(key)
        if expect.encode("ascii") not in head:
            raise OSError("desk websocket accept mismatch")
        self.buf.extend(rest)

    def send(self, obj: dict[str, Any]) -> None:
        payload = json.dumps(obj, default=str).encode("utf-8")
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x81])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        self.sock.sendall(header + mask + _mask(payload, mask))

    def recv(self, timeout: float = 8.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            for opcode, payload in decode_frames(self.buf):
                if opcode == 8:
                    raise OSError("desk websocket closed")
                if opcode in (9, 10):
                    continue
                if opcode in (1, 2):
                    msg = json.loads(payload.decode("utf-8"))
                    if isinstance(msg, dict) and msg.get("method") == "desk/ping":
                        continue
                    if isinstance(msg, dict):
                        return msg
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("desk websocket recv timed out")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("desk websocket closed")
            self.buf.extend(chunk)

    def call(self, method: str, params: dict[str, Any] | None = None, *, req_id: int = 1, timeout: float = 30.0) -> dict[str, Any]:
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        notes: list[dict[str, Any]] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} timed out")
            msg = self.recv(timeout=remaining)
            if msg.get("id") == req_id:
                msg["_notes"] = notes
                return msg
            notes.append(msg)

    def close(self) -> None:
        try:
            self.sock.sendall(encode_frame(struct.pack("!H", 1000), opcode=8))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
