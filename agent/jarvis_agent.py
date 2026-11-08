#!/usr/bin/env python3
"""
jarvis_agent.py — run this on your OTHER computers/laptops to let JARVIS reach them.

Setup (once per machine):
    pip install websockets psutil mss pillow        # psutil/mss/pillow are optional
    python jarvis_agent.py pair --hub https://192.168.1.20:8000 --code ABCD2345 --name "Study Laptop"

Then keep it running:
    python jarvis_agent.py run

By default the agent only allows: status, notify, lock.
Everything else must be switched on LOCALLY, on this machine, in addition to the
permission you grant on the JARVIS dashboard:
    python jarvis_agent.py allow files  ~/Documents/Shared      # list/read files under that folder only
    python jarvis_agent.py allow screenshot
    python jarvis_agent.py allow app notepad --cmd notepad.exe
    python jarvis_agent.py deny  screenshot
    python jarvis_agent.py unpair

Every command received is printed here and appended to ~/.jarvis_agent/audit.log.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

HOME_DIR    = Path.home() / ".jarvis_agent"
CONFIG_FILE = HOME_DIR / "config.json"
AUDIT_FILE  = HOME_DIR / "audit.log"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_LIST = 200

DEFAULT_CONFIG = {
    "hub": "", "token": "", "fingerprint": "", "name": "",
    "allow": {"files_roots": [], "screenshot": False, "apps": {}},
}

# command -> local permission needed ("" = always allowed once paired)
LOCAL_PERMISSION = {
    "ping": "", "status": "", "notify": "", "lock": "",
    "list_files": "files", "read_file": "files",
    "screenshot": "screenshot", "open_app": "apps",
}


# ── config / audit ───────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update({k: v for k, v in cfg.items() if k != "allow"})
    merged["allow"].update(cfg.get("allow", {}))
    return merged


def save_config(cfg: dict) -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass


def log(event: str, **fields) -> None:
    line = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields}
    print(f"[agent] {event} " + " ".join(f"{k}={v}" for k, v in fields.items()))
    try:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── local permission helpers ─────────────────────────────────────────────────

def _is_allowed(cfg: dict, cmd: str) -> bool:
    need = LOCAL_PERMISSION.get(cmd)
    if need is None:
        return False
    allow = cfg["allow"]
    if need == "":
        return True
    if need == "files":
        return bool(allow.get("files_roots"))
    if need == "screenshot":
        return bool(allow.get("screenshot"))
    if need == "apps":
        return bool(allow.get("apps"))
    return False


def resolve_in_roots(path_str: str, roots: list[str]) -> Path:
    """Resolve `path_str` and make sure it is inside one of `roots`.
    Resolving first defeats '..' tricks and symlinks that point outside."""
    if not roots:
        raise PermissionError("No folders are shared on this device.")
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = Path(roots[0]).expanduser() / p
    p = p.resolve()
    for r in roots:
        root = Path(r).expanduser().resolve()
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    raise PermissionError("That path is outside the folders shared on this device.")


# ── command handlers ─────────────────────────────────────────────────────────

def h_ping(cfg, args):
    return {"ok": True, "pong": True}


def h_status(cfg, args):
    out = {"ok": True, "host": platform.node(), "os": f"{platform.system()} {platform.release()}"}
    try:
        import psutil
        out["cpu_percent"] = psutil.cpu_percent(interval=0.3)
        vm = psutil.virtual_memory()
        out["memory_percent"] = vm.percent
        du = shutil.disk_usage(Path.home().anchor or "/")
        out["disk_free_gb"] = round(du.free / 1e9, 1)
        out["uptime_hours"] = round((time.time() - psutil.boot_time()) / 3600, 1)
        bat = psutil.sensors_battery()
        if bat is not None:
            out["battery_percent"] = round(bat.percent)
            out["charging"] = bool(bat.power_plugged)
    except ImportError:
        out["note"] = "Install psutil for battery, CPU and memory details."
    except Exception as e:
        out["note"] = f"Partial status: {e}"
    return out


def h_notify(cfg, args):
    msg = str(args.get("message", "")).strip()[:300] or "JARVIS says hello."
    title = "JARVIS"
    sysname = platform.system()
    try:
        if sysname == "Windows":
            import ctypes
            import threading
            threading.Thread(
                target=lambda: ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40 | 0x40000),
                daemon=True).start()
        elif sysname == "Darwin":
            esc = msg.replace('"', "'")
            subprocess.Popen(["osascript", "-e", f'display notification "{esc}" with title "{title}"'])
        else:
            subprocess.Popen(["notify-send", title, msg])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"Couldn't show the notification: {e}"}


def h_lock(cfg, args):
    sysname = platform.system()
    try:
        if sysname == "Windows":
            import ctypes
            ctypes.windll.user32.LockWorkStation()
        elif sysname == "Darwin":
            subprocess.Popen(["pmset", "displaysleepnow"])
        else:
            subprocess.Popen(["loginctl", "lock-session"])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"Couldn't lock the screen: {e}"}


def h_list_files(cfg, args):
    try:
        folder = resolve_in_roots(str(args.get("path") or "."), cfg["allow"]["files_roots"])
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    if not folder.is_dir():
        return {"ok": False, "error": "That folder doesn't exist."}
    entries = []
    for child in sorted(folder.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))[:MAX_LIST]:
        try:
            entries.append({"name": child.name, "dir": child.is_dir(),
                            "size": child.stat().st_size if child.is_file() else None})
        except OSError:
            continue
    return {"ok": True, "path": str(folder), "entries": entries}


def h_read_file(cfg, args):
    try:
        p = resolve_in_roots(str(args.get("path", "")), cfg["allow"]["files_roots"])
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    if not p.is_file():
        return {"ok": False, "error": "That file doesn't exist."}
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        return {"ok": False, "error": f"File is {size // 1024} KB; the limit is {MAX_FILE_BYTES // 1024} KB."}
    return {"ok": True, "filename": p.name, "data_b64": base64.b64encode(p.read_bytes()).decode("ascii")}


def h_screenshot(cfg, args):
    # Visible to whoever is at the machine: show a notice first.
    h_notify(cfg, {"message": "JARVIS is taking a screenshot of this screen."})
    time.sleep(1.5)
    try:
        import mss
        import mss.tools
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[1])
            png = mss.tools.to_png(shot.rgb, shot.size)
    except ImportError:
        try:
            from PIL import ImageGrab
            buf = io.BytesIO()
            ImageGrab.grab().save(buf, format="PNG")
            png = buf.getvalue()
        except Exception as e:
            return {"ok": False, "error": f"Install 'mss' or 'pillow' for screenshots ({e})."}
    except Exception as e:
        return {"ok": False, "error": f"Screenshot failed: {e}"}
    if len(png) > MAX_FILE_BYTES:
        return {"ok": False, "error": "Screenshot is too large to send."}
    return {"ok": True, "filename": "screenshot.png", "data_b64": base64.b64encode(png).decode("ascii")}


def h_open_app(cfg, args):
    name = str(args.get("name", "")).strip().lower()
    apps = {k.lower(): v for k, v in cfg["allow"]["apps"].items()}
    cmd = apps.get(name)
    if not cmd:
        return {"ok": False, "error": f"'{name}' isn't on this device's allowed-apps list."}
    try:
        subprocess.Popen([cmd])        # fixed command from LOCAL config; never from the hub
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"Couldn't open it: {e}"}


HANDLERS = {
    "ping": h_ping, "status": h_status, "notify": h_notify, "lock": h_lock,
    "list_files": h_list_files, "read_file": h_read_file,
    "screenshot": h_screenshot, "open_app": h_open_app,
}


def execute(cfg: dict, cmd: str, args: dict) -> dict:
    """Apply local policy, then run the handler. Never raises."""
    if cmd not in HANDLERS or not _is_allowed(cfg, cmd):
        log("refused", cmd=cmd)
        return {"ok": False, "error": f"'{cmd}' isn't enabled on this device. "
                                      f"Enable it here with: jarvis_agent.py allow ..."}
    log("command", cmd=cmd, args=json.dumps({k: str(v)[:80] for k, v in args.items()}))
    try:
        return HANDLERS[cmd](cfg, args)
    except Exception as e:
        return {"ok": False, "error": f"{cmd} failed: {e}"}


# ── networking ───────────────────────────────────────────────────────────────

def _insecure_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def server_fingerprint(hub_url: str) -> str:
    u = urlparse(hub_url)
    if u.scheme != "https":
        return ""
    pem = ssl.get_server_certificate((u.hostname, u.port or 443))
    h = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def _norm_fp(fp: str) -> str:
    return fp.replace(":", "").replace(" ", "").upper()


def do_pair(args) -> int:
    hub = args.hub.rstrip("/")
    fp = ""
    try:
        fp = server_fingerprint(hub)
    except Exception as e:
        print(f"Could not reach {hub}: {e}")
        return 1
    if hub.startswith("http://"):
        print("WARNING: the hub is using plain HTTP. Pair only on a network you trust.")
    elif args.fingerprint and _norm_fp(args.fingerprint) != _norm_fp(fp):
        print("Fingerprint mismatch! The server is not the one you expected. Aborting.")
        print(f"  expected: {args.fingerprint}\n  got:      {fp}")
        return 1
    else:
        print(f"Hub certificate fingerprint:\n  {fp}\n"
              "Check this matches the one shown on the JARVIS dashboard before continuing.")
    body = json.dumps({"code": args.code, "name": args.name, "kind": args.kind,
                       "platform": f"{platform.system()} {platform.release()}"}).encode()
    req = urllib.request.Request(hub + "/api/devices/pair", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15, context=_insecure_ctx()) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", e.reason)
        except Exception:
            msg = e.reason
        print(f"Pairing failed: {msg}")
        return 1
    except Exception as e:
        print(f"Pairing failed: {e}")
        return 1
    cfg = load_config()
    cfg.update({"hub": hub, "token": data["token"], "fingerprint": fp, "name": args.name})
    save_config(cfg)
    print(f"Paired as '{args.name}'. Default access: status, notify, lock.")
    print("Start the agent with:  python jarvis_agent.py run")
    return 0


async def run_agent() -> int:
    try:
        import websockets
    except ImportError:
        print("Please install the 'websockets' package:  pip install websockets")
        return 1
    cfg = load_config()
    if not cfg["token"] or not cfg["hub"]:
        print("This device isn't paired yet. Run: jarvis_agent.py pair --hub URL --code CODE --name NAME")
        return 1
    hub = cfg["hub"]
    ws_url = ("wss" if hub.startswith("https") else "ws") + "://" + hub.split("://", 1)[1]
    ws_url += f"/ws/agent?token={cfg['token']}"
    ctx = _insecure_ctx() if hub.startswith("https") else None
    backoff = 2
    print(f"[agent] '{cfg['name']}' connecting to {hub} (Ctrl+C to stop)")
    while True:
        try:
            if cfg["fingerprint"]:                      # pin the hub certificate
                current = await asyncio.get_running_loop().run_in_executor(None, server_fingerprint, hub)
                if _norm_fp(current) != _norm_fp(cfg["fingerprint"]):
                    log("fingerprint_mismatch")
                    print("Hub certificate changed — refusing to connect. Re-pair if this is expected.")
                    return 1
            async with websockets.connect(ws_url, ssl=ctx, max_size=20 * 1024 * 1024,
                                          ping_interval=20) as ws:
                log("connected")
                backoff = 2
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "cmd":
                        continue
                    cfg = load_config()                 # pick up allow/deny changes live
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, execute, cfg, str(msg.get("cmd", "")), msg.get("args") or {})
                    await ws.send(json.dumps({"type": "result", "id": msg.get("id"),
                                              "ok": bool(result.get("ok")), "result": result}))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            code = getattr(getattr(e, "rcvd", None), "code", None)
            if code == 4001 or code == 4003:
                print("The hub rejected this device (revoked or invalid token). Run 'unpair' and pair again.")
                return 1
            log("disconnected", reason=str(e)[:100])
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ── CLI ──────────────────────────────────────────────────────────────────────

def cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="JARVIS device agent")
    sub = ap.add_subparsers(dest="action", required=True)

    p = sub.add_parser("pair", help="pair this device with JARVIS")
    p.add_argument("--hub", required=True, help="e.g. https://192.168.1.20:8000")
    p.add_argument("--code", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--kind", choices=["pc", "laptop"], default="laptop")
    p.add_argument("--fingerprint", default="", help="certificate fingerprint shown on the dashboard")

    sub.add_parser("run", help="connect and wait for commands")
    sub.add_parser("status", help="show local settings")
    sub.add_parser("unpair", help="forget the pairing on this device")

    for verb in ("allow", "deny"):
        a = sub.add_parser(verb, help=f"{verb} a capability on this device")
        a.add_argument("what", choices=["files", "screenshot", "app"])
        a.add_argument("value", nargs="?", help="folder (files) or app name (app)")
        a.add_argument("--cmd", help="executable for 'allow app'")

    args = ap.parse_args(argv)

    if args.action == "pair":
        return do_pair(args)
    if args.action == "run":
        try:
            return asyncio.run(run_agent())
        except KeyboardInterrupt:
            return 0
    cfg = load_config()
    if args.action == "status":
        shown = {**cfg, "token": "(hidden)" if cfg["token"] else ""}
        print(json.dumps(shown, indent=2))
        return 0
    if args.action == "unpair":
        cfg.update({"hub": "", "token": "", "fingerprint": ""})
        save_config(cfg)
        print("Unpaired. Also remove this device on the JARVIS dashboard.")
        return 0

    allow = cfg["allow"]
    on = args.action == "allow"
    if args.what == "screenshot":
        allow["screenshot"] = on
    elif args.what == "files":
        if not args.value:
            print("Give a folder, e.g.  allow files ~/Documents/Shared")
            return 1
        folder = str(Path(args.value).expanduser().resolve())
        roots = [r for r in allow["files_roots"] if r != folder]
        allow["files_roots"] = roots + [folder] if on else roots
    elif args.what == "app":
        if not args.value:
            print("Give an app name, e.g.  allow app notepad --cmd notepad.exe")
            return 1
        if on:
            if not args.cmd:
                print("Use --cmd to say which program to launch.")
                return 1
            allow["apps"][args.value] = args.cmd
        else:
            allow["apps"].pop(args.value, None)
    save_config(cfg)
    print("Saved.")
    return 0


if __name__ == "__main__":
    sys.exit(cli())
