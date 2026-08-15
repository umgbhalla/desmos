"""Where a provider's credentials live, whether they are usable, and how to get them.

Two providers, three ways in:

  anthropic   ANTHROPIC_API_KEY in the environment.
  openai      OPENAI_API_KEY in the environment, or a ChatGPT (Codex) OAuth
              token on disk -- ours at ~/.desmos/auth.json, or the one the
              Codex CLI already wrote at ~/.codex/auth.json.

Nothing here talks to a model. It answers one question -- can this provider be
used right now, and with what -- so the onboarding screen can show the answer
instead of a stack trace on the first request.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# The Codex CLI's public client. Same id pi uses, and the only one the device
# endpoints below will answer to.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE = "https://auth.openai.com"
TOKEN_URL = f"{AUTH_BASE}/oauth/token"
DEVICE_CODE_URL = f"{AUTH_BASE}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE}/api/accounts/deviceauth/token"
DEVICE_VERIFY_URL = f"{AUTH_BASE}/codex/device"
DEVICE_REDIRECT_URI = f"{AUTH_BASE}/deviceauth/callback"
DEVICE_TIMEOUT_S = 15 * 60

# The browser flow's redirect is registered against this exact origin and path,
# so the port is not ours to choose.
SCOPE = "openid profile email offline_access"
LOCAL_PORT = 1455
CALLBACK_PATH = "/auth/callback"
LOCAL_REDIRECT_URI = f"http://localhost:{LOCAL_PORT}{CALLBACK_PATH}"
BROWSER_TIMEOUT_S = 5 * 60

# The access token is a JWT and carries the account id the Codex backend wants
# in a header. Read it from the token rather than storing it twice.
JWT_CLAIM = "https://api.openai.com/auth"

PROVIDERS = ("anthropic", "openai")


class NeedsAuth(RuntimeError):
    """No usable credential. The message is what to do about it."""


@dataclass
class Credential:
    provider: str
    kind: str  # "env" | "oauth"
    token: str
    account_id: str | None = None
    expires: int | None = None  # epoch seconds, from the JWT
    source: str = ""
    plan: str | None = None

    def expired(self, leeway: int = 300) -> bool:
        return self.expires is not None and self.expires - leeway <= time.time()

    def masked(self) -> str:
        t = self.token
        return f"{t[:6]}…{t[-4:]}" if len(t) > 14 else "set"


def _jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT body. No signature check -- we are reading our own token."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        out = json.loads(base64.urlsafe_b64decode(part))
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def account_id(access_token: str) -> str | None:
    claim = _jwt_payload(access_token).get(JWT_CLAIM)
    acct = claim.get("chatgpt_account_id") if isinstance(claim, dict) else None
    return acct or None


def plan_type(access_token: str) -> str | None:
    claim = _jwt_payload(access_token).get(JWT_CLAIM)
    return claim.get("chatgpt_plan_type") if isinstance(claim, dict) else None


def token_expiry(access_token: str) -> int | None:
    exp = _jwt_payload(access_token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def desmos_auth_path() -> Path:
    return Path(os.environ.get("DESMOS_AUTH") or (Path.home() / ".desmos" / "auth.json"))


def codex_auth_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    return Path(home) / "auth.json" if home else Path.home() / ".codex" / "auth.json"


def auth_files() -> list[Path]:
    """Ours first: a login we performed should win over one we borrowed."""
    return [desmos_auth_path(), codex_auth_path()]


def read_auth_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("tokens"), dict) else None


def write_auth_file(path: Path, tokens: dict[str, Any], *, keep: dict[str, Any] | None = None) -> None:
    """Rewrite one auth.json in the Codex CLI's own shape, 0600, atomically.

    Same shape on purpose: refreshing rotates the refresh token, so whichever
    file we read from has to be the file we write back to, or the CLI that owns
    it is left holding a token the server has already retired.
    """
    body: dict[str, Any] = dict(keep or {})
    body.setdefault("auth_mode", "chatgpt")
    body.setdefault("OPENAI_API_KEY", None)
    body["tokens"] = tokens
    body["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp, same discipline as the trajectory writer. A fixed
    # ".json.tmp" meant two concurrent writers renamed each other's file out
    # from under themselves, and the losers died with FileNotFoundError out of
    # os.replace -- which killed the subagent turns that were only refreshing a
    # token. mkstemp already creates the file 0600.
    fd, tmp = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(body, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _post_json(url: str, body: dict[str, Any], timeout: int = 30) -> tuple[int, dict[str, Any] | str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def _post_form(url: str, form: dict[str, str], timeout: int = 30) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise NeedsAuth(f"OpenAI token endpoint {e.code}: {body[:400]}") from e


# ponytail: in-process lock only; a Codex CLI refreshing the same auth.json
# concurrently still races. Per-file flock if that turns up.
_REFRESH_LOCK = threading.Lock()


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    """Trade a refresh token for a new pair. The refresh token rotates."""
    out = _post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
    )
    if not out.get("access_token"):
        raise NeedsAuth(f"refresh response missing access_token: {sorted(out)}")
    return out


def _credential_from_file(path: Path, *, allow_refresh: bool = True) -> Credential | None:
    data = read_auth_file(path)
    if not data:
        return None
    tokens = dict(data.get("tokens") or {})
    access = tokens.get("access_token") or ""
    if not access:
        return None
    cred = Credential(
        provider="openai",
        kind="oauth",
        token=access,
        account_id=account_id(access) or tokens.get("account_id"),
        expires=token_expiry(access),
        source=str(path),
        plan=plan_type(access),
    )
    if cred.expired() and allow_refresh and tokens.get("refresh_token"):
        with _REFRESH_LOCK:
            # Four subagent threads reach here at once holding the same refresh
            # token, and it rotates: the losers POSTed a token the server had
            # already retired and then stamped their dead pair over the
            # winner's file. Re-read under the lock and take the winner's.
            data = read_auth_file(path) or data
            tokens = dict(data.get("tokens") or tokens)
            exp = token_expiry(tokens.get("access_token") or "")
            if (exp is None or exp - 300 <= time.time()) and tokens.get("refresh_token"):
                fresh = refresh_tokens(tokens["refresh_token"])
                tokens["access_token"] = fresh["access_token"]
                tokens["refresh_token"] = fresh.get("refresh_token") or tokens["refresh_token"]
                if fresh.get("id_token"):
                    tokens["id_token"] = fresh["id_token"]
                tokens["account_id"] = account_id(fresh["access_token"]) or tokens.get("account_id")
                write_auth_file(path, tokens, keep={k: v for k, v in data.items() if k != "tokens"})
        # The tokens above came off disk a second time, so the access token the
        # first read validated is no longer guaranteed to be there: a writer
        # racing us can leave a tokens dict that is truthy but has no
        # access_token, and there is then nothing to hand back.
        access = tokens.get("access_token") or ""
        if not access:
            return None
        cred = Credential(
            provider="openai",
            kind="oauth",
            token=access,
            account_id=account_id(access) or tokens.get("account_id"),
            expires=token_expiry(access),
            source=str(path),
            plan=plan_type(access),
        )
    return cred


def openai_credential(*, allow_refresh: bool = True) -> Credential | None:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return Credential(provider="openai", kind="env", token=key, source="OPENAI_API_KEY")
    for path in auth_files():
        cred = _credential_from_file(path, allow_refresh=allow_refresh)
        if cred:
            return cred
    return None


def anthropic_credential() -> Credential | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return Credential(provider="anthropic", kind="env", token=key, source="ANTHROPIC_API_KEY")


def credential(provider: str, *, allow_refresh: bool = True) -> Credential:
    if provider == "anthropic":
        cred = anthropic_credential()
        if cred:
            return cred
        raise NeedsAuth("ANTHROPIC_API_KEY is not set")
    if provider == "openai":
        cred = openai_credential(allow_refresh=allow_refresh)
        if cred:
            return cred
        raise NeedsAuth(
            "no OpenAI credential: set OPENAI_API_KEY, or run `python -m desmos auth login openai`"
        )
    raise NeedsAuth(f"unknown provider {provider!r}")


def status(*, allow_refresh: bool = False) -> list[dict[str, Any]]:
    """One row per provider, for the onboarding screen and `desmos auth`."""
    rows: list[dict[str, Any]] = []
    for name in PROVIDERS:
        try:
            cred = credential(name, allow_refresh=allow_refresh)
        except NeedsAuth as e:
            rows.append({"provider": name, "ok": False, "detail": str(e)})
            continue
        row: dict[str, Any] = {
            "provider": name,
            "ok": True,
            "kind": cred.kind,
            "source": cred.source,
            "token": cred.masked(),
        }
        if cred.account_id:
            row["account"] = cred.account_id
        if cred.plan:
            row["plan"] = cred.plan
        if cred.expires:
            row["expires_in"] = max(0, int(cred.expires - time.time()))
            row["stale"] = cred.expired()
        rows.append(row)
    return rows


# ---------------------------------------------------------------- device login


@dataclass
class DeviceCode:
    device_auth_id: str
    user_code: str
    interval: int
    verify_url: str = DEVICE_VERIFY_URL
    expires_in: int = DEVICE_TIMEOUT_S


def start_device_login() -> DeviceCode:
    code, body = _post_json(DEVICE_CODE_URL, {"client_id": CLIENT_ID})
    if code == 404:
        raise NeedsAuth("device login is not enabled for this account; use OPENAI_API_KEY")
    if code >= 400 or not isinstance(body, dict):
        raise NeedsAuth(f"device code request failed ({code}): {str(body)[:300]}")
    try:
        interval = int(float(body.get("interval", 5)))
    except (TypeError, ValueError):
        interval = 5
    if not body.get("device_auth_id") or not body.get("user_code"):
        raise NeedsAuth(f"malformed device code response: {sorted(body)}")
    return DeviceCode(str(body["device_auth_id"]), str(body["user_code"]), max(1, interval))


_sleep = time.sleep  # named so a test can drive the poll loop without waiting


def poll_device_login(device: DeviceCode, *, on_wait: Callable[[int], None] | None = None) -> dict[str, str]:
    """Block until the browser half finishes. Returns the authorization code."""
    deadline = time.time() + device.expires_in
    wait = device.interval
    while time.time() < deadline:
        code, body = _post_json(
            DEVICE_TOKEN_URL,
            {"device_auth_id": device.device_auth_id, "user_code": device.user_code},
        )
        if code < 400 and isinstance(body, dict) and body.get("authorization_code"):
            return {
                "code": str(body["authorization_code"]),
                "verifier": str(body.get("code_verifier") or ""),
            }
        err = body.get("error") if isinstance(body, dict) else None
        err = err.get("code") if isinstance(err, dict) else err
        if code in (403, 404) or err == "deviceauth_authorization_pending":
            pass
        elif err == "slow_down":
            wait += 2
        else:
            raise NeedsAuth(f"device auth failed ({code}): {str(body)[:300]}")
        if on_wait is not None:
            on_wait(wait)
        _sleep(wait)
    raise NeedsAuth("device login timed out")


def exchange_code(code: str, verifier: str, redirect_uri: str = DEVICE_REDIRECT_URI) -> dict[str, Any]:
    return _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
    )


# ------------------------------------------------------------- browser login


def _pkce() -> tuple[str, str]:
    """Verifier and its S256 challenge, both base64url without padding."""
    import hashlib
    import secrets

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def authorize_url(challenge: str, state: str, redirect_uri: str = LOCAL_REDIRECT_URI) -> str:
    q = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
        }
    )
    return f"{AUTH_BASE}/oauth/authorize?{q}"


_DONE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>desmos</title>"
    "<body style=\"font:16px/1.6 ui-monospace,monospace;background:#16161e;color:#c8c8c8;"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0\">"
    "<div><div style=\"color:#9ece6a\">signed in</div>"
    "<div style=\"color:#787878\">you can close this tab and go back to the terminal</div></div>"
)


def wait_for_callback(state: str, timeout: int = BROWSER_TIMEOUT_S) -> str:
    """Serve exactly one request on the registered port and return its ?code."""
    import http.server
    import socket

    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib's spelling
            parts = urllib.parse.urlsplit(self.path)
            params = dict(urllib.parse.parse_qsl(parts.query))
            if parts.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            if params.get("state") != state:
                got["error"] = "state mismatch (a different login is in flight?)"
            elif params.get("error"):
                got["error"] = params.get("error_description") or params["error"]
            elif params.get("code"):
                got["code"] = params["code"]
            else:
                got["error"] = "callback carried neither code nor error"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_PAGE.encode())

        def log_message(self, *_a: Any) -> None:
            pass  # the terminal is ours, not the http server's

    try:
        server = http.server.HTTPServer(("127.0.0.1", LOCAL_PORT), Handler)
    except OSError as e:
        raise NeedsAuth(
            f"port {LOCAL_PORT} is busy ({e}); OpenAI only accepts that exact "
            f"redirect, so close the other login (Codex CLI?) or use device login"
        ) from e
    server.timeout = timeout
    with server:
        deadline = time.time() + timeout
        while not got and time.time() < deadline:
            server.handle_request()
    if got.get("error"):
        raise NeedsAuth(got["error"])
    if not got.get("code"):
        raise NeedsAuth("browser login timed out")
    return got["code"]


def browser_login(*, notify: Callable[[str], None] = print, open_browser: bool = True) -> dict[str, Any]:
    """Full round trip: open the consent page, catch the redirect, exchange the code."""
    import secrets
    import webbrowser

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    url = authorize_url(challenge, state)
    opened = bool(open_browser) and webbrowser.open(url)
    notify(f"waiting for the browser at {LOCAL_REDIRECT_URI}" if opened else f"open this to sign in:\n{url}")
    code = wait_for_callback(state)
    return exchange_code(code, verifier, redirect_uri=LOCAL_REDIRECT_URI)


def login_openai(*, notify: Callable[[str], None] = print, method: str = "browser") -> Credential:
    """Sign in and leave a usable credential on disk.

    method="browser" is the whole cycle -- consent page, localhost callback,
    code exchange. method="device" skips straight to a code the user types on
    another machine. method="auto" tries the browser and drops to the device
    code if this box has no browser, no display, or no socket to bind.
    """
    tokens_out: dict[str, Any] | None = None
    if method in {"browser", "auto"}:
        try:
            tokens_out = browser_login(notify=notify)
        except Exception as e:
            if method == "browser":
                raise
            notify(f"browser login unavailable ({e}); falling back to a device code")
    if tokens_out is None:
        device = start_device_login()
        url = f"{device.verify_url}?user_code={urllib.parse.quote(device.user_code)}"
        notify(f"code {device.user_code}\nopen {url}")
        got = poll_device_login(device, on_wait=lambda _s: None)
        tokens_out = exchange_code(got["code"], got["verifier"])
    access = tokens_out.get("access_token")
    if not access:
        raise NeedsAuth(f"token exchange returned no access_token: {sorted(tokens_out)}")
    tokens = {
        "id_token": tokens_out.get("id_token") or "",
        "access_token": access,
        "refresh_token": tokens_out.get("refresh_token") or "",
        "account_id": account_id(access) or "",
    }
    path = desmos_auth_path()
    write_auth_file(path, tokens)
    notify(f"saved {path}")
    return Credential(
        provider="openai",
        kind="oauth",
        token=access,
        account_id=tokens["account_id"],
        expires=token_expiry(access),
        source=str(path),
        plan=plan_type(access),
    )


def logout_openai() -> list[str]:
    """Only ever removes our own file. The Codex CLI's login is not ours to end."""
    removed = []
    path = desmos_auth_path()
    if path.exists():
        path.unlink()
        removed.append(str(path))
    return removed
