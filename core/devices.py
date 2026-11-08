"""
core/devices.py — Paired-device registry and command hub for JARVIS.

Lets JARVIS reach YOUR other computers, laptops and phone — but only devices
you explicitly paired, only for the permissions (scopes) you granted, and
every command is written to an audit log.

Security model (defence in depth):
  1. Pairing needs a one-time code generated from the authenticated dashboard.
     Codes expire in 5 minutes, are single-use, and wrong guesses are rate-limited.
  2. Each device gets its own random token. Only a SHA-256 hash is stored here,
     so a leaked devices.json cannot be used to impersonate a device.
  3. Scopes are checked on the hub AND again on the device (the agent has its own
     local allow-list). Sensitive scopes are OFF by default.
  4. Revoking a device drops its live connection immediately.
  5. Sensitive scopes can only be granted from the authenticated dashboard —
     never from a voice command or LLM tool call.

This module is pure stdlib so it can be unit-tested without FastAPI.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

BASE_DIR     = Path(__file__).resolve().parent.parent
DEVICES_FILE = BASE_DIR / "config" / "devices.json"
AUDIT_FILE   = BASE_DIR / "config" / "device_audit.log"

KINDS = ("pc", "laptop", "phone")

# command -> scope required (None = always allowed for a paired device)
COMMAND_SCOPE: dict[str, Optional[str]] = {
    "ping":        None,
    "status":      "status",
    "notify":      "notify",
    "lock":        "lock",
    "ring":        "ring",
    "locate":      "locate",
    "list_files":  "files.list",
    "read_file":   "files.read",
    "screenshot":  "screenshot",
    "open_app":    "apps.open",
}
ALL_SCOPES = tuple(sorted({s for s in COMMAND_SCOPE.values() if s}))

# Never granted by default; must be switched on from the dashboard.
SENSITIVE_SCOPES = frozenset({"files.list", "files.read", "screenshot", "apps.open", "locate"})

DEFAULT_SCOPES = {
    "pc":     ["status", "notify", "lock"],
    "laptop": ["status", "notify", "lock"],
    "phone":  ["status", "notify", "ring"],
}

_PAIR_CHARS = [c for c in "ABCDEFGHJKMNPQRSTUVWXYZ23456789"]  # no look-alikes
PAIR_TTL_SECS     = 300
MAX_FAILS         = 5
FAIL_WINDOW_SECS  = 300
MAX_FILE_BYTES    = 5 * 1024 * 1024


class PairingError(Exception):
    """Raised for bad/expired codes, rate limiting, or invalid input."""


class DeviceError(Exception):
    """Raised when a command cannot be delivered (unknown, offline, no scope)."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def cert_fingerprint(cert_path: Path | None = None) -> str:
    """SHA-256 fingerprint (colon hex) of the dashboard TLS certificate, or ''."""
    path = cert_path or (BASE_DIR / "config" / "certs" / "jarvis.crt")
    try:
        der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding="ascii"))
        h = hashlib.sha256(der).hexdigest().upper()
        return ":".join(h[i:i + 2] for i in range(0, len(h), 2))
    except Exception:
        return ""


# ── Audit log ────────────────────────────────────────────────────────────────

_audit_lock = threading.Lock()


def audit(event: str, audit_file: Path | None = None, **fields: Any) -> None:
    """Append one JSON line. Never raises, never logs file contents or tokens."""
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
    rec.update({k: v for k, v in fields.items() if k not in ("token", "data_b64")})
    path = audit_file or AUDIT_FILE
    try:
        with _audit_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_audit(limit: int = 100, audit_file: Path | None = None) -> list[dict]:
    path = audit_file or AUDIT_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


# ── Registry ─────────────────────────────────────────────────────────────────

class DeviceRegistry:
    def __init__(self, path: Path | None = None, audit_file: Path | None = None):
        self._path = path or DEVICES_FILE
        self._audit_file = audit_file
        self._lock = threading.RLock()
        self._devices: dict[str, dict] = {}
        self._codes: dict[str, float] = {}            # code -> expiry
        self._fails: dict[str, list[float]] = {}      # client -> fail timestamps
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._devices = {d["id"]: d for d in data.get("devices", [])}
        except Exception:
            self._devices = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"devices": list(self._devices.values())}, indent=2),
                           encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            os.replace(tmp, self._path)
        except Exception:
            pass

    @staticmethod
    def _public(dev: dict) -> dict:
        return {k: v for k, v in dev.items() if k != "token_hash"}

    # -- pairing -------------------------------------------------------------
    def create_pairing_code(self, ttl: int = PAIR_TTL_SECS) -> str:
        with self._lock:
            now = time.time()
            self._codes = {c: e for c, e in self._codes.items() if e > now}
            code = "".join(secrets.choice(_PAIR_CHARS) for _ in range(8))
            self._codes[code] = now + ttl
        audit("pair_code_created", self._audit_file)
        return code

    def _check_rate(self, client: str) -> None:
        now = time.time()
        recent = [t for t in self._fails.get(client, []) if now - t < FAIL_WINDOW_SECS]
        self._fails[client] = recent
        if len(recent) >= MAX_FAILS:
            raise PairingError("Too many failed attempts. Wait a few minutes and try again.")

    def pair(self, code: str, name: str, kind: str = "laptop",
             platform_name: str = "", client: str = "unknown") -> tuple[str, str]:
        """Consume a code and register a device. Returns (device_id, token)."""
        name = re.sub(r"\s+", " ", (name or "").strip())[:40]
        if not name:
            raise PairingError("A device name is required.")
        if kind not in KINDS:
            raise PairingError(f"Device kind must be one of: {', '.join(KINDS)}.")
        with self._lock:
            self._check_rate(client)
            code = (code or "").strip().upper()
            now = time.time()
            expiry = self._codes.pop(code, 0)
            if not code or expiry < now:
                self._fails.setdefault(client, []).append(now)
                audit("pair_failed", self._audit_file, client=client)
                raise PairingError("That pairing code is invalid or has expired.")
            if any(d["name"].lower() == name.lower() for d in self._devices.values()):
                raise PairingError(f"A device named '{name}' is already paired.")
            device_id = uuid.uuid4().hex[:10]
            token = secrets.token_urlsafe(32)
            self._devices[device_id] = {
                "id": device_id,
                "name": name,
                "kind": kind,
                "platform": (platform_name or "")[:60],
                "scopes": list(DEFAULT_SCOPES[kind]),
                "token_hash": _hash_token(token),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_seen": None,
            }
            self._save()
        audit("paired", self._audit_file, device=name, kind=kind, client=client)
        return device_id, token

    # -- lookup / auth -------------------------------------------------------
    def authenticate(self, token: str) -> Optional[dict]:
        if not token:
            return None
        h = _hash_token(token.strip())
        with self._lock:
            for dev in self._devices.values():
                if secrets.compare_digest(dev["token_hash"], h):
                    dev["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    return self._public(dev)
        return None

    def get(self, device_id: str) -> Optional[dict]:
        with self._lock:
            d = self._devices.get(device_id)
            return self._public(d) if d else None

    def list(self) -> list[dict]:
        with self._lock:
            return [self._public(d) for d in self._devices.values()]

    def find_by_name(self, query: str) -> list[dict]:
        """Exact (case-insensitive) match wins; otherwise substring matches."""
        q = (query or "").strip().lower()
        devs = self.list()
        if not q:
            return devs if len(devs) == 1 else []
        exact = [d for d in devs if d["name"].lower() == q]
        return exact or [d for d in devs if q in d["name"].lower()]

    # -- mutation ------------------------------------------------------------
    def set_scopes(self, device_id: str, scopes: list[str]) -> list[str]:
        clean = sorted({s for s in scopes if s in ALL_SCOPES})
        with self._lock:
            dev = self._devices.get(device_id)
            if not dev:
                raise DeviceError("Unknown device.")
            dev["scopes"] = clean
            self._save()
            name = dev["name"]
        audit("scopes_changed", self._audit_file, device=name, scopes=clean)
        return clean

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            dev = self._devices.pop(device_id, None)
            if dev:
                self._save()
        if dev:
            audit("revoked", self._audit_file, device=dev["name"])
        return bool(dev)


# ── Hub (live connections + request/response) ────────────────────────────────

class DeviceHub:
    """Tracks live agent connections and delivers commands to them.

    A 'connection' is any object with `async send_json(dict)` and `async close(code=...)`
    (FastAPI WebSocket satisfies this).
    """

    def __init__(self, registry: DeviceRegistry, save_root: Path | None = None):
        self.registry = registry
        self.save_root = save_root or (BASE_DIR / "uploads" / "devices")
        self._conns: dict[str, Any] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _audit(self, event: str, **fields: Any) -> None:
        audit(event, self.registry._audit_file, **fields)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def online_ids(self) -> set[str]:
        return set(self._conns)

    async def attach(self, device_id: str, conn: Any) -> None:
        old = self._conns.get(device_id)
        self._conns[device_id] = conn
        dev = self.registry.get(device_id)
        self._audit("connected", device=dev["name"] if dev else device_id)
        if old is not None and old is not conn:
            try:
                await old.close(code=4000)
            except Exception:
                pass

    async def detach(self, device_id: str, conn: Any) -> None:
        if self._conns.get(device_id) is conn:
            self._conns.pop(device_id, None)
            dev = self.registry.get(device_id)
            self._audit("disconnected", device=dev["name"] if dev else device_id)

    async def disconnect(self, device_id: str) -> None:
        conn = self._conns.pop(device_id, None)
        if conn is not None:
            try:
                await conn.close(code=4003)
            except Exception:
                pass

    def handle_message(self, device_id: str, msg: dict) -> None:
        if not isinstance(msg, dict) or msg.get("type") != "result":
            return
        fut = self._pending.pop(str(msg.get("id", "")), None)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    async def request(self, device_id: str, cmd: str, args: dict | None = None,
                      timeout: float = 20.0, source: str = "dashboard") -> dict:
        """Send a command and wait for the device's reply. Always returns a dict."""
        dev = self.registry.get(device_id)
        if not dev:
            return {"ok": False, "error": "Unknown device."}
        if cmd not in COMMAND_SCOPE:
            return {"ok": False, "error": f"Unsupported command '{cmd}'."}
        need = COMMAND_SCOPE[cmd]
        if need and need not in dev["scopes"]:
            self._audit("denied_scope", device=dev["name"], cmd=cmd, source=source)
            return {"ok": False,
                    "error": f"'{need}' access is not enabled for {dev['name']}. "
                             f"You can turn it on from the dashboard's Devices page."}
        conn = self._conns.get(device_id)
        if conn is None:
            return {"ok": False, "error": f"{dev['name']} is offline."}

        req_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        safe_args = {k: v for k, v in (args or {}).items()}
        self._audit("command", device=dev["name"], cmd=cmd, source=source,
              args={k: (str(v)[:120]) for k, v in safe_args.items()})
        try:
            await conn.send_json({"type": "cmd", "id": req_id, "cmd": cmd, "args": safe_args})
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"{dev['name']} didn't respond in time."}
        except Exception as e:
            return {"ok": False, "error": f"Could not reach {dev['name']}: {e}"}
        finally:
            self._pending.pop(req_id, None)

        result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
        result.setdefault("ok", bool(msg.get("ok", True)))
        if not msg.get("ok", True) and "error" not in result:
            result["error"] = str(msg.get("error", "The device reported an error."))
        return self._post_process(dev, cmd, result)

    # -- sync wrapper for plugins running in worker threads -------------------
    def request_sync(self, device_id: str, cmd: str, args: dict | None = None,
                     timeout: float = 20.0, source: str = "voice") -> dict:
        loop = self._loop
        if loop is None or not loop.is_running():
            return {"ok": False,
                    "error": "The dashboard service isn't running, so I can't reach your devices."}
        fut = asyncio.run_coroutine_threadsafe(
            self.request(device_id, cmd, args, timeout, source), loop)
        try:
            return fut.result(timeout + 5)
        except Exception as e:
            return {"ok": False, "error": f"Request failed: {e}"}

    # -- file results are saved to disk, never pushed through the LLM ---------
    def _post_process(self, dev: dict, cmd: str, result: dict) -> dict:
        b64 = result.pop("data_b64", None)
        if b64 and result.get("ok"):
            try:
                import base64
                raw = base64.b64decode(b64)
                if len(raw) > MAX_FILE_BYTES + 1024:
                    return {"ok": False, "error": "File is too large."}
                fname = re.sub(r"[^\w.\- ]", "_", str(result.get("filename") or cmd))[:80] or cmd
                folder = self.save_root / re.sub(r"[^\w\- ]", "_", dev["name"])
                folder.mkdir(parents=True, exist_ok=True)
                dest = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{fname}"
                dest.write_bytes(raw)
                result["saved_to"] = str(dest)
                result["bytes"] = len(raw)
            except Exception as e:
                return {"ok": False, "error": f"Couldn't save the received file: {e}"}
        return result


# ── Module singletons (the plugin and the dashboard share these) ─────────────

_registry: Optional[DeviceRegistry] = None
_hub: Optional[DeviceHub] = None
_singleton_lock = threading.RLock()


def get_registry() -> DeviceRegistry:
    global _registry
    with _singleton_lock:
        if _registry is None:
            _registry = DeviceRegistry()
        return _registry


def get_hub() -> DeviceHub:
    global _hub
    with _singleton_lock:
        if _hub is None:
            _hub = DeviceHub(get_registry())
        return _hub
