"""
Device Access plugin — lets JARVIS reach the user's OTHER paired computers, laptops
and phone. Pairing, permissions and revocation are managed on the dashboard's
Devices page (open the Remote Control QR, then tap "Devices").

Permissions are enforced by core.devices (scopes) and again by each device.
Sensitive scopes (files, screenshots, apps, phone location) cannot be granted from
here — only from the authenticated dashboard — so a voice command or prompt
injection can never widen its own access.
"""
from core.devices import get_hub, get_registry

PLUGIN = {
    "name": "device_access",
    "description": (
        "Reach the user's OTHER paired devices (their laptop, second PC, or phone). "
        "Use for: 'what's my laptop's battery', 'lock my laptop', 'send a message to my phone', "
        "'make my phone ring / find my phone', 'where is my phone', 'screenshot my laptop', "
        "'list files on my laptop', 'get <file> from my laptop', 'which devices are online'. "
        "Do NOT use this for the computer JARVIS itself is running on — use the normal "
        "computer/system tools for that. Only act on a device the user names or clearly implies."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: list, status, notify, lock, ring, locate, screenshot, "
                               "list_files, read_file, open_app",
            },
            "device": {"type": "STRING", "description": "Device name, e.g. 'laptop' or 'phone'. "
                                                          "Optional if only one device is paired."},
            "message": {"type": "STRING", "description": "Text to show (for notify)."},
            "path": {"type": "STRING", "description": "Folder or file path on the device "
                                                        "(for list_files / read_file)."},
            "app": {"type": "STRING", "description": "App name (for open_app)."},
        },
        "required": ["action"],
    },
}

_ACTIONS = {"list", "status", "notify", "lock", "ring", "locate", "screenshot",
            "list_files", "read_file", "open_app"}


def _resolve(query: str):
    reg = get_registry()
    matches = reg.find_by_name(query)
    if not matches:
        paired = reg.list()
        if not paired:
            return None, "No devices are paired yet. Open the Devices page on the dashboard to add one."
        names = ", ".join(d["name"] for d in paired)
        return None, f"I couldn't tell which device you meant. Paired devices: {names}."
    if len(matches) > 1:
        return None, "That matches more than one device: " + ", ".join(d["name"] for d in matches) + "."
    return matches[0], ""


def _describe_status(name: str, r: dict) -> str:
    bits = []
    if "battery_percent" in r:
        bits.append(f"battery at {r['battery_percent']} percent"
                    + (", charging" if r.get("charging") else ""))
    if "cpu_percent" in r:
        bits.append(f"CPU at {r['cpu_percent']} percent")
    if "memory_percent" in r:
        bits.append(f"memory at {r['memory_percent']} percent")
    if "disk_free_gb" in r:
        bits.append(f"{r['disk_free_gb']} gigabytes free")
    if not bits:
        return f"{name} is online. {r.get('note', '')}".strip()
    return f"{name}: " + ", ".join(bits) + "."


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        action = str(parameters.get("action", "")).strip().lower()
        if action not in _ACTIONS:
            return "Sir, I'm not sure what you'd like me to do with your devices."

        reg, hub = get_registry(), get_hub()

        if action == "list":
            devices = reg.list()
            if not devices:
                return "No devices are paired yet. Open the Devices page on the dashboard to add one."
            online = hub.online_ids()
            parts = [f"{d['name']} ({'online' if d['id'] in online else 'offline'})" for d in devices]
            return "Your devices: " + ", ".join(parts) + "."

        dev, err = _resolve(str(parameters.get("device", "")))
        if err:
            return err

        args: dict = {}
        if action == "notify":
            args["message"] = str(parameters.get("message", "")) or "JARVIS is trying to reach you."
        elif action in ("list_files", "read_file"):
            args["path"] = str(parameters.get("path", "")) or "."
        elif action == "open_app":
            args["name"] = str(parameters.get("app", ""))

        result = hub.request_sync(dev["id"], action, args, timeout=25, source="voice")
        if not result.get("ok"):
            text = f"Sir, {result.get('error', 'that did not work.')}"
        elif action == "status":
            text = _describe_status(dev["name"], result)
        elif action == "locate":
            lat, lon = result.get("lat"), result.get("lon")
            text = (f"{dev['name']} is at about {lat:.4f}, {lon:.4f}, "
                    f"accurate to roughly {int(result.get('accuracy_m', 0))} metres."
                    if lat is not None and lon is not None else f"{dev['name']} didn't share a location.")
        elif action == "list_files":
            entries = result.get("entries", [])
            shown = ", ".join(e["name"] + ("/" if e.get("dir") else "") for e in entries[:12])
            more = f" and {len(entries) - 12} more" if len(entries) > 12 else ""
            text = f"{len(entries)} items on {dev['name']}: {shown}{more}." if entries else "That folder is empty."
        elif action in ("read_file", "screenshot"):
            text = f"Done. I saved it to {result.get('saved_to', 'your uploads folder')}."
        elif action == "lock":
            text = f"{dev['name']} is locked."
        elif action == "ring":
            text = f"{dev['name']} is ringing."
        elif action == "notify":
            text = f"Message delivered to {dev['name']}."
        elif action == "open_app":
            text = f"Opened it on {dev['name']}."
        else:
            text = f"{dev['name']} is online."

        if player:
            try:
                player.write_log(f"JARVIS: {text}")
            except Exception:
                pass
        return text
    except Exception as e:
        return f"Sir, device access failed: {e}"
