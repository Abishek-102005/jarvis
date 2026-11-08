"""Tests for paired-device access: registry, pairing, hub, agent policy, voice plugin."""
import asyncio
import base64
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import devices as dv
from agent import jarvis_agent as agent


def make_registry(tmp):
    return dv.DeviceRegistry(Path(tmp) / "devices.json", Path(tmp) / "audit.log")


class FakeConn:
    """Stands in for a WebSocket. `responder(msg) -> result dict | None` (None = never answer)."""
    def __init__(self, hub=None, device_id="", responder=None):
        self.hub, self.device_id, self.responder = hub, device_id, responder
        self.sent, self.closed_with = [], None

    async def send_json(self, msg):
        self.sent.append(msg)
        if self.responder:
            result = self.responder(msg)
            if result is not None:
                self.hub.handle_message(self.device_id, {"type": "result", "id": msg["id"],
                                                         "ok": result.get("ok", True), "result": result})

    async def close(self, code=1000):
        self.closed_with = code


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = make_registry(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def pair(self, name="Laptop", kind="laptop", client="1.1.1.1"):
        return self.reg.pair(self.reg.create_pairing_code(), name, kind, "Windows 11", client)

    def test_pair_and_authenticate(self):
        dev_id, token = self.pair()
        dev = self.reg.authenticate(token)
        self.assertEqual(dev["id"], dev_id)
        self.assertNotIn("token_hash", dev)
        self.assertIsNone(self.reg.authenticate("wrong"))
        self.assertIsNone(self.reg.authenticate(""))

    def test_token_not_stored_in_plaintext(self):
        _, token = self.pair()
        self.assertNotIn(token, (Path(self.tmp.name) / "devices.json").read_text())

    def test_code_is_single_use(self):
        code = self.reg.create_pairing_code()
        self.reg.pair(code, "A", "laptop", "", "c")
        with self.assertRaises(dv.PairingError):
            self.reg.pair(code, "B", "laptop", "", "c")

    def test_expired_code_rejected(self):
        code = self.reg.create_pairing_code(ttl=-1)
        with self.assertRaises(dv.PairingError):
            self.reg.pair(code, "A", "laptop", "", "c")

    def test_bad_code_rate_limited(self):
        for _ in range(dv.MAX_FAILS):
            with self.assertRaises(dv.PairingError):
                self.reg.pair("NOPE", "A", "laptop", "", "attacker")
        good = self.reg.create_pairing_code()
        with self.assertRaises(dv.PairingError) as cm:
            self.reg.pair(good, "A", "laptop", "", "attacker")
        self.assertIn("Too many", str(cm.exception))
        # a different client is unaffected
        self.reg.pair(good, "A", "laptop", "", "someone-else")

    def test_duplicate_name_and_bad_kind(self):
        self.pair("Laptop")
        with self.assertRaises(dv.PairingError):
            self.pair("laptop")
        with self.assertRaises(dv.PairingError):
            self.reg.pair(self.reg.create_pairing_code(), "X", "toaster", "", "c")
        with self.assertRaises(dv.PairingError):
            self.reg.pair(self.reg.create_pairing_code(), "   ", "laptop", "", "c")

    def test_defaults_exclude_sensitive_scopes(self):
        for kind in dv.KINDS:
            self.assertFalse(set(dv.DEFAULT_SCOPES[kind]) & dv.SENSITIVE_SCOPES, kind)

    def test_set_scopes_drops_unknown_and_persists(self):
        dev_id, _ = self.pair()
        self.assertEqual(self.reg.set_scopes(dev_id, ["status", "files.read", "root.shell"]),
                         ["files.read", "status"])
        reloaded = make_registry(self.tmp.name)
        self.assertEqual(reloaded.get(dev_id)["scopes"], ["files.read", "status"])

    def test_revoke_invalidates_token(self):
        dev_id, token = self.pair()
        self.assertTrue(self.reg.revoke(dev_id))
        self.assertIsNone(self.reg.authenticate(token))
        self.assertFalse(self.reg.revoke(dev_id))

    def test_find_by_name(self):
        self.pair("Study Laptop")
        self.pair("Work Laptop")
        self.pair("Phone", "phone")
        self.assertEqual(len(self.reg.find_by_name("laptop")), 2)
        self.assertEqual(len(self.reg.find_by_name("study")), 1)
        self.assertEqual(self.reg.find_by_name("phone")[0]["kind"], "phone")
        self.assertEqual(self.reg.find_by_name("nothing"), [])

    def test_audit_never_logs_secrets(self):
        _, token = self.pair()
        dv.audit("x", Path(self.tmp.name) / "a2.log", token=token, data_b64="AAAA", cmd="read_file")
        text = (Path(self.tmp.name) / "a2.log").read_text()
        self.assertNotIn(token, text)
        self.assertNotIn("AAAA", text)
        self.assertIn("read_file", text)


class HubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = make_registry(self.tmp.name)
        self.hub = dv.DeviceHub(self.reg, save_root=Path(self.tmp.name) / "recv")
        self.dev_id, _ = self.reg.pair(self.reg.create_pairing_code(), "Laptop", "laptop", "", "c")

    def tearDown(self):
        self.tmp.cleanup()

    async def online(self, responder):
        conn = FakeConn(self.hub, self.dev_id, responder)
        await self.hub.attach(self.dev_id, conn)
        return conn

    async def test_command_roundtrip(self):
        conn = await self.online(lambda m: {"ok": True, "battery_percent": 80})
        r = await self.hub.request(self.dev_id, "status")
        self.assertTrue(r["ok"])
        self.assertEqual(r["battery_percent"], 80)
        self.assertEqual(conn.sent[0]["cmd"], "status")

    async def test_scope_enforced_on_hub(self):
        conn = await self.online(lambda m: {"ok": True})
        r = await self.hub.request(self.dev_id, "screenshot")
        self.assertFalse(r["ok"])
        self.assertIn("not enabled", r["error"])
        self.assertEqual(conn.sent, [])          # never even sent

    async def test_unknown_command_unknown_device_offline(self):
        await self.online(lambda m: {"ok": True})
        self.assertFalse((await self.hub.request(self.dev_id, "format_disk"))["ok"])
        self.assertFalse((await self.hub.request("nope", "ping"))["ok"])
        await self.hub.detach(self.dev_id, self.hub._conns[self.dev_id])
        r = await self.hub.request(self.dev_id, "ping")
        self.assertIn("offline", r["error"])

    async def test_timeout(self):
        await self.online(lambda m: None)
        r = await self.hub.request(self.dev_id, "ping", timeout=0.1)
        self.assertFalse(r["ok"])
        self.assertIn("respond", r["error"])
        self.assertEqual(self.hub._pending, {})

    async def test_device_reported_error_surfaces(self):
        await self.online(lambda m: {"ok": False, "error": "nope"})
        r = await self.hub.request(self.dev_id, "lock")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "nope")

    async def test_files_saved_to_disk_not_returned(self):
        self.reg.set_scopes(self.dev_id, ["files.read"])
        payload = base64.b64encode(b"hello").decode()
        await self.online(lambda m: {"ok": True, "filename": "../../evil name.txt", "data_b64": payload})
        r = await self.hub.request(self.dev_id, "read_file", {"path": "x"})
        self.assertNotIn("data_b64", r)
        saved = Path(r["saved_to"])
        self.assertEqual(saved.read_bytes(), b"hello")
        self.assertTrue(str(saved.resolve()).startswith(str((Path(self.tmp.name) / "recv").resolve())))

    async def test_disconnect_closes_connection(self):
        conn = await self.online(lambda m: {"ok": True})
        await self.hub.disconnect(self.dev_id)
        self.assertEqual(conn.closed_with, 4003)
        self.assertNotIn(self.dev_id, self.hub.online_ids())

    async def test_reconnect_replaces_old_connection(self):
        old = await self.online(lambda m: {"ok": True})
        new = await self.online(lambda m: {"ok": True})
        self.assertEqual(old.closed_with, 4000)
        self.assertIs(self.hub._conns[self.dev_id], new)

    async def test_command_is_audited(self):
        await self.online(lambda m: {"ok": True})
        await self.hub.request(self.dev_id, "ping", source="voice")
        await self.hub.request(self.dev_id, "screenshot", source="voice")      # denied by scope
        events = dv.read_audit(50, audit_file=Path(self.tmp.name) / "audit.log")
        kinds = [(e["event"], e.get("cmd"), e.get("source")) for e in events]
        self.assertIn(("command", "ping", "voice"), kinds)
        self.assertIn(("denied_scope", "screenshot", "voice"), kinds)


class ThreadedHubTests(unittest.TestCase):
    """request_sync is what the voice plugin uses from a worker thread."""
    def test_request_sync_from_thread(self):
        tmp = tempfile.TemporaryDirectory()
        reg = make_registry(tmp.name)
        hub = dv.DeviceHub(reg, save_root=Path(tmp.name))
        dev_id, _ = reg.pair(reg.create_pairing_code(), "Phone", "phone", "", "c")
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        hub.bind_loop(loop)
        conn = FakeConn(hub, dev_id, lambda m: {"ok": True})
        asyncio.run_coroutine_threadsafe(hub.attach(dev_id, conn), loop).result(2)
        self.assertTrue(hub.request_sync(dev_id, "ring", timeout=2)["ok"])      # phones get 'ring' by default
        self.assertFalse(hub.request_sync(dev_id, "locate", timeout=2)["ok"])   # 'locate' is sensitive: off
        loop.call_soon_threadsafe(loop.stop)
        loop.close() if not loop.is_running() else None
        tmp.cleanup()

    def test_request_sync_without_loop(self):
        tmp = tempfile.TemporaryDirectory()
        hub = dv.DeviceHub(make_registry(tmp.name), save_root=Path(tmp.name))
        r = hub.request_sync("x", "ping")
        self.assertFalse(r["ok"])
        tmp.cleanup()


class AgentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "shared"
        self.root.mkdir()
        (self.root / "a.txt").write_text("hi")
        (self.root / "sub").mkdir()
        self.outside = Path(self.tmp.name) / "secret.txt"
        self.outside.write_text("top secret")
        self.cfg = {"allow": {"files_roots": [str(self.root)], "screenshot": False, "apps": {}}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_allow_only_safe_commands(self):
        cfg = {"allow": {"files_roots": [], "screenshot": False, "apps": {}}}
        for ok in ("ping", "status", "notify", "lock"):
            self.assertTrue(agent._is_allowed(cfg, ok), ok)
        for no in ("list_files", "read_file", "screenshot", "open_app", "rm_rf"):
            self.assertFalse(agent._is_allowed(cfg, no), no)

    def test_path_traversal_blocked(self):
        for bad in ("../secret.txt", str(self.outside), "sub/../../secret.txt"):
            with self.assertRaises(PermissionError, msg=bad):
                agent.resolve_in_roots(bad, [str(self.root)])

    def test_symlink_escape_blocked(self):
        link = self.root / "link"
        try:
            link.symlink_to(self.outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(PermissionError):
            agent.resolve_in_roots("link", [str(self.root)])

    def test_inside_paths_allowed(self):
        self.assertEqual(agent.resolve_in_roots("a.txt", [str(self.root)]), (self.root / "a.txt").resolve())

    def test_no_roots_means_no_files(self):
        with self.assertRaises(PermissionError):
            agent.resolve_in_roots("a.txt", [])

    def test_execute_refuses_unallowed_and_unknown(self):
        self.assertFalse(agent.execute(self.cfg, "screenshot", {})["ok"])
        self.assertFalse(agent.execute(self.cfg, "open_app", {"name": "calc"})["ok"])
        self.assertFalse(agent.execute(self.cfg, "run_shell", {"cmd": "whoami"})["ok"])

    def test_list_and_read(self):
        r = agent.execute(self.cfg, "list_files", {"path": "."})
        self.assertTrue(r["ok"])
        self.assertEqual({e["name"] for e in r["entries"]}, {"a.txt", "sub"})
        r = agent.execute(self.cfg, "read_file", {"path": "a.txt"})
        self.assertEqual(base64.b64decode(r["data_b64"]), b"hi")
        self.assertFalse(agent.execute(self.cfg, "read_file", {"path": str(self.outside)})["ok"])

    def test_read_file_size_limit(self):
        big = self.root / "big.bin"
        big.write_bytes(b"0" * (agent.MAX_FILE_BYTES + 1))
        self.assertFalse(agent.execute(self.cfg, "read_file", {"path": "big.bin"})["ok"])

    def test_open_app_only_from_local_allowlist(self):
        cfg = {"allow": {"files_roots": [], "screenshot": False, "apps": {"calc": "/bin/true"}}}
        self.assertTrue(agent.execute(cfg, "open_app", {"name": "CALC"})["ok"])
        self.assertFalse(agent.execute(cfg, "open_app", {"name": "/bin/sh"})["ok"])


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = make_registry(self.tmp.name)
        self.hub = dv.DeviceHub(self.reg, save_root=Path(self.tmp.name) / "recv")
        dv._registry, dv._hub = self.reg, self.hub
        from plugins import device_access
        self.plugin = device_access
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.hub.bind_loop(self.loop)

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        dv._registry = dv._hub = None
        self.tmp.cleanup()

    def add(self, name, kind, responder=None):
        dev_id, _ = self.reg.pair(self.reg.create_pairing_code(), name, kind, "", "c")
        if responder:
            conn = FakeConn(self.hub, dev_id, responder)
            asyncio.run_coroutine_threadsafe(self.hub.attach(dev_id, conn), self.loop).result(2)
        return dev_id

    def test_plugin_metadata_valid(self):
        from core.plugin_loader import _validate
        self.assertTrue(_validate(self.plugin, "device_access.py").valid)

    def test_no_devices_paired(self):
        self.assertIn("No devices", self.plugin.run({"action": "list"}))
        self.assertIn("No devices", self.plugin.run({"action": "lock", "device": "laptop"}))

    def test_list_shows_online_state(self):
        self.add("Laptop", "laptop", lambda m: {"ok": True})
        self.add("Phone", "phone")
        text = self.plugin.run({"action": "list"})
        self.assertIn("Laptop (online)", text)
        self.assertIn("Phone (offline)", text)

    def test_lock_and_status(self):
        self.add("Laptop", "laptop", lambda m: {"ok": True, "battery_percent": 42, "charging": True})
        self.assertIn("locked", self.plugin.run({"action": "lock", "device": "laptop"}))
        text = self.plugin.run({"action": "status", "device": "laptop"})
        self.assertIn("42 percent", text)
        self.assertIn("charging", text)

    def test_ambiguous_device_asks(self):
        self.add("Study Laptop", "laptop", lambda m: {"ok": True})
        self.add("Work Laptop", "laptop", lambda m: {"ok": True})
        self.assertIn("more than one", self.plugin.run({"action": "lock", "device": "laptop"}))

    def test_sensitive_scope_cannot_be_used_without_dashboard_grant(self):
        self.add("Laptop", "laptop", lambda m: {"ok": True, "saved": True})
        text = self.plugin.run({"action": "screenshot", "device": "laptop"})
        self.assertIn("not enabled", text)

    def test_offline_device_message(self):
        self.add("Phone", "phone")
        self.assertIn("offline", self.plugin.run({"action": "ring", "device": "phone"}))

    def test_bad_action_never_raises(self):
        self.assertIn("not sure", self.plugin.run({"action": "format_c"}))
        self.assertIsInstance(self.plugin.run({}), str)


if __name__ == "__main__":
    unittest.main()
