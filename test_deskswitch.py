#!/usr/bin/env python3
"""Offline tests for deskswitch (no Mac, monitor or Logitech device needed)."""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ["DESKSWITCH_HOME"] = TMP
sys.path.insert(0, str(Path(__file__).parent))
import deskswitch as ds  # noqa: E402


def cfg_with(**sections):
    cfg = ds.load_config(Path("/nonexistent"))
    for sec, kv in sections.items():
        for k, v in kv.items():
            cfg.set(sec, k, str(v))
    return cfg


class FakeMonitor:
    def __init__(self, current=None):
        self.current = current
        self.sets = []
        self.dry_run = False

    def get_input(self):
        return self.current

    def set_input(self, v):
        self.sets.append(v)
        self.current = None  # after switching away we can no longer read it
        return True


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_follower(seq, idle=1.0, paused=False, current=None, recent=None, **follow):
    cfg = cfg_with(monitor={"this_input": 27, "peer_input": 15},
                   follow=dict({"debounce": 2, "cooldown": 6}, **follow))
    clock = Clock()
    it = iter(seq)
    mon = FakeMonitor(current)
    fol = ds.Follower(cfg, mon, presence_fn=lambda: next(it), idle_fn=lambda: idle,
                      clock=clock, paused_fn=lambda: paused, recent_fn=lambda w: recent)
    return fol, mon, clock


def drive(fol, clock, n, dt=0.7):
    out = []
    for _ in range(n):
        out.append(fol.step())
        clock.t += dt
    return [a for a in out if a]


K = {"keyboard": True, "mouse": False}
M = {"keyboard": False, "mouse": True}
KM = {"keyboard": True, "mouse": True}
NONE = {"keyboard": False, "mouse": False}


class FollowerTests(unittest.TestCase):
    def test_flow_arrival_pulls_once(self):
        seq = [NONE, NONE, M, M, KM, KM, KM, KM]
        fol, mon, clock = make_follower(seq)
        self.assertEqual(drive(fol, clock, len(seq)), ["pull"])
        self.assertEqual(mon.sets, [27])

    def test_mouse_only_peek_does_not_pull(self):
        seq = [NONE, M, M, M, M, M]
        fol, mon, clock = make_follower(seq)
        self.assertEqual(drive(fol, clock, len(seq)), [])
        self.assertEqual(mon.sets, [])

    def test_departure_while_active_pushes(self):
        seq = [KM, KM, K, NONE, NONE, NONE]
        fol, mon, clock = make_follower(seq, idle=1.5, push="yes")
        self.assertEqual(drive(fol, clock, len(seq)), ["push"])
        self.assertEqual(mon.sets, [15])

    def test_departure_after_idle_is_sleep(self):
        seq = [KM, NONE, NONE, NONE]
        fol, mon, clock = make_follower(seq, idle=600, push="yes")
        self.assertEqual(drive(fol, clock, len(seq)), ["skip:idle"])
        self.assertEqual(mon.sets, [])

    def test_keyboard_asleep_mouse_active_keeps_here(self):
        seq = [KM, M, M, M, M]
        fol, mon, clock = make_follower(seq, push="yes")
        self.assertEqual(drive(fol, clock, len(seq)), [])

    def test_push_disabled_by_default(self):
        seq = [KM, NONE, NONE, NONE]
        fol, mon, clock = make_follower(seq)
        self.assertEqual(drive(fol, clock, len(seq)), ["skip:push-off"])

    def test_single_glitch_is_debounced(self):
        seq = [NONE, KM, NONE, NONE, NONE]
        fol, mon, clock = make_follower(seq)
        self.assertEqual(drive(fol, clock, len(seq)), [])

    def test_paused(self):
        seq = [NONE, KM, KM, KM]
        fol, mon, clock = make_follower(seq, paused=True)
        self.assertEqual(drive(fol, clock, len(seq)), ["skip:paused"])
        self.assertEqual(mon.sets, [])

    def test_already_on_this_input(self):
        seq = [NONE, KM, KM]
        fol, mon, clock = make_follower(seq, current=27)
        self.assertEqual(drive(fol, clock, len(seq)), ["skip:already"])

    def test_recent_manual_switch_suppresses_push(self):
        seq = [KM, NONE, NONE]
        fol, mon, clock = make_follower(seq, push="yes", recent=15)
        self.assertEqual(drive(fol, clock, len(seq)), ["skip:recent"])

    def test_cooldown_defers_then_acts_on_final_state(self):
        # arrive (pull), leave + come back inside the cooldown -> no extra action,
        # leave and stay away -> push only after the cooldown expires
        seq = [NONE, KM, KM, NONE, NONE] + [NONE] * 12
        fol, mon, clock = make_follower(seq, push="yes", idle=0.5)
        actions = drive(fol, clock, len(seq))
        self.assertEqual(actions, ["pull", "push"])
        self.assertEqual(mon.sets, [27, 15])

    def test_presence_error_is_ignored(self):
        def boom():
            raise RuntimeError("ioreg hiccup")
        cfg = cfg_with(monitor={"this_input": 27})
        fol = ds.Follower(cfg, FakeMonitor(), presence_fn=boom, idle_fn=lambda: 0,
                          clock=Clock(), paused_fn=lambda: False, recent_fn=lambda w: None)
        self.assertIsNone(fol.step())

    def test_arrive_on_both(self):
        seq = [NONE, K, K, K, KM, KM]
        fol, mon, clock = make_follower(seq, arrive_on="both")
        self.assertEqual(drive(fol, clock, len(seq)), ["pull"])


class MonitorTests(unittest.TestCase):
    def test_betterdisplay_cmd(self):
        m = ds.Monitor(cfg_with())
        cmd = m.set_cmd(17)
        self.assertTrue(cmd[0].endswith("betterdisplaycli") or cmd[0].endswith("BetterDisplay"))
        self.assertEqual(cmd[1:], ["set", "-nameLike=ZQE-CAA", "-feature=ddc", "-vcp=0x60", "-value=17"])
        self.assertIn("-value", m.get_cmd())

    def test_m1ddc_cmd(self):
        m = ds.Monitor(cfg_with(monitor={"backend": "m1ddc", "m1ddc_display": "2"}))
        self.assertEqual(m.set_cmd(15)[1:], ["display", "2", "set", "input", "15"])
        self.assertIsNone(m.get_cmd())

    def test_custom_cmd(self):
        m = ds.Monitor(cfg_with(commands={"set": "mytool --mon '{name}' --in {value}"}))
        self.assertEqual(m.set_cmd(18), ["mytool", "--mon", "ZQE-CAA", "--in", "18"])

    def test_parse_int(self):
        self.assertEqual(ds.parse_int("17\n"), 17)
        self.assertEqual(ds.parse_int("value: 0x1b"), 27)
        self.assertIsNone(ds.parse_int("Failed."))

    def test_set_failure_detected(self):
        m = ds.Monitor(cfg_with())
        with mock.patch.object(ds, "run", return_value=(0, "Failed.", "")):
            self.assertFalse(m.set_input(17))
        with mock.patch.object(ds, "run", return_value=(0, "", "")):
            self.assertTrue(m.set_input(17))


class PresenceTests(unittest.TestCase):
    def test_ioreg_parsing_and_match(self):
        fake = ('  | {\n  |   "Product" = "MX Master 3S"\n  |   "Transport" = "Bluetooth Low Energy"\n'
                '  | {\n  |   "Product" = "Apple Internal Keyboard / Trackpad"\n')
        with mock.patch.object(ds, "run", return_value=(0, fake, "")):
            p = ds.Presence(cfg_with()).read()
        self.assertEqual(p, {"keyboard": False, "mouse": True})

    def test_empty_name_ignores_role(self):
        with mock.patch.object(ds, "ioreg_products", return_value=["MX Keys S"]):
            p = ds.Presence(cfg_with(devices={"mouse": ""})).read()
        self.assertEqual(p, {"keyboard": True})

    def test_hid_idle(self):
        out = '    | |   "HIDIdleTime" = 2500000000\n'
        with mock.patch.object(ds, "run", return_value=(0, out, "")):
            self.assertAlmostEqual(ds.hid_idle_seconds(), 2.5)


class FakeAgent(threading.Thread):
    """Unix-socket server speaking the Options+ agent framing."""

    def __init__(self, path, close_immediately=False):
        super().__init__(daemon=True)
        self.path = path
        self.close_immediately = close_immediately
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(1)
        self.requests = []

    @staticmethod
    def frame(proto, payload):
        inner = struct.pack(">I", len(proto)) + proto + struct.pack(">I", len(payload)) + payload
        return struct.pack("<I", len(inner)) + inner

    def run(self):
        conn, _ = self.srv.accept()
        if self.close_immediately:
            conn.close()
            return
        conn.sendall(self.frame(b"bin", b"\x00\x01greeting"))
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                fr = ds.LogiAgent.decode_one(buf)
                if not fr:
                    break
                _, raw, buf = fr
                req = json.loads(raw)
                self.requests.append(req)
                if req["path"] == "/devices/list":
                    payload = {"deviceInfos": [
                        {"id": "dev00000000", "deviceType": "MOUSE", "connected": True,
                         "connectionType": "BLUETOOTH", "displayName": "MX Master 3S"},
                        {"id": "dev00000001", "deviceType": "KEYBOARD", "connected": True,
                         "connectionType": "BLUETOOTH", "displayName": "MX Keys S"},
                        {"id": "virt", "deviceType": "KEYBOARD", "connected": True,
                         "connectionType": "VIRTUAL", "displayName": "Virtual"},
                    ]}
                    resp = {"msgId": req["msg_id"], "result": {"code": "SUCCESS"}, "payload": payload}
                else:
                    resp = {"msgId": req["msg_id"], "path": req["path"], "result": {"code": "SUCCESS"}}
                # unrelated broadcast first, then the answer, split across two sends
                data = self.frame(b"json", json.dumps({"msgId": "999"}).encode()) + \
                    self.frame(b"json", json.dumps(resp).encode())
                conn.sendall(data[:7])
                conn.sendall(data[7:])


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.path = os.path.join(self.dir, "logitech_kiros_agent-test")
        self.glob = mock.patch.object(ds.LogiAgent, "SOCKET_GLOB", os.path.join(self.dir, "logitech_kiros_agent-*"))
        self.glob.start()

    def tearDown(self):
        self.glob.stop()

    def test_roundtrip_framing(self):
        msg = {"msg_id": "1", "verb": "GET", "path": "/devices/list"}
        enc = ds.LogiAgent.encode(msg)
        proto, raw, rest = ds.LogiAgent.decode_one(enc + b"\x01")
        self.assertEqual((proto, json.loads(raw), rest), (b"json", msg, b"\x01"))
        self.assertIsNone(ds.LogiAgent.decode_one(enc[:-1]))

    def test_devices_and_change_host(self):
        srv = FakeAgent(self.path)
        srv.start()
        a = ds.LogiAgent(timeout=3)
        devs = a.devices()
        self.assertEqual([d["id"] for d in devs], ["dev00000000", "dev00000001"])
        self.assertEqual(a.change_host("dev00000000", 1), "SUCCESS")
        a.close()
        req = srv.requests[-1]
        self.assertEqual(req["verb"], "SET")
        self.assertEqual(req["payload"], {"@type": "type.googleapis.com/logi.protocol.devices.ChangeHost", "host": 1})

    def test_agent_refusal(self):
        srv = FakeAgent(self.path, close_immediately=True)
        srv.start()
        a = ds.LogiAgent(timeout=2)
        with self.assertRaises(ds.AgentError):
            a.devices()

    def test_no_socket(self):
        with mock.patch.object(ds.LogiAgent, "SOCKET_GLOB", "/nonexistent/x-*"):
            with self.assertRaises(ds.AgentError):
                ds.LogiAgent().devices()


class ConfigTests(unittest.TestCase):
    def test_example_config_parses(self):
        cfg = ds.load_config(Path(__file__).with_name("config.example.ini"))
        self.assertIsNone(ds.opt_int(cfg, "monitor", "this_input"))
        self.assertTrue(cfg.getboolean("follow", "pull"))
        self.assertEqual(cfg.getfloat("follow", "active_window"), 10.0)

    def test_hex_inputs(self):
        cfg = cfg_with(monitor={"this_input": "0x1b"})
        self.assertEqual(ds.opt_int(cfg, "monitor", "this_input"), 27)

    def test_cli_dry_run_switch_without_devices(self):
        cfgfile = Path(TMP) / "config.ini"
        cfgfile.write_text("[monitor]\nthis_input = 27\npeer_input = 15\n")
        with mock.patch.object(ds, "CONFIG_PATH", cfgfile):
            ds.main(["switch", "--no-devices", "--dry-run"])
            ds.main(["toggle"])
            self.assertTrue(ds.is_paused())
            ds.main(["resume"])
            self.assertFalse(ds.is_paused())


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def time(self):
        self.t += 0.5
        return self.t

    monotonic = time

    def sleep(self, s):
        self.t += s


class CalibrateTests(unittest.TestCase):
    def setUp(self):
        self.cfgfile = Path(tempfile.mkdtemp()) / "config.ini"
        self.patches = [
            mock.patch.object(ds, "CONFIG_PATH", self.cfgfile),
            mock.patch.object(ds, "time", FakeTime()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_calibrate(self, answers, own=27, presence_seq=(), agent_ok=False):
        mon = FakeMonitor(current=own)
        mon.name, mon.backend = "ZQE-CAA", "betterdisplay"
        it = iter(presence_seq)
        pres = mock.Mock()
        pres.names = {"keyboard": "MX Keys", "mouse": "MX Master"}
        pres.read.side_effect = lambda: next(it)
        agent = mock.Mock()
        if agent_ok:
            agent.devices.return_value = [{"displayName": "MX KEYS S", "connected": True}]
        else:
            agent.devices.side_effect = ds.AgentError("refused")
        with mock.patch.object(ds, "Monitor", return_value=mon), \
                mock.patch.object(ds, "Presence", return_value=pres), \
                mock.patch.object(ds, "LogiAgent", return_value=agent), \
                mock.patch("builtins.input", side_effect=answers), \
                mock.patch("sys.stdout"):
            ds.main(["calibrate"])
        return mon, ds.load_config(self.cfgfile)

    def test_happy_path_single_mac_mode(self):
        answers = ["", "1", "y", "y", "", ""]
        mon, cfg = self.run_calibrate(answers, presence_seq=[NONE, KM])
        self.assertEqual(mon.sets, [17, 27])
        self.assertEqual(cfg.get("monitor", "this_input"), "27")
        self.assertEqual(cfg.get("monitor", "peer_input"), "17")
        self.assertEqual(cfg.get("follow", "pull"), "yes")
        self.assertEqual(cfg.get("follow", "push"), "yes")
        self.assertEqual(cfg.get("devices", "presence"), "ioreg")
        # comments from the example config survive
        self.assertIn("# pull: when the devices arrive", self.cfgfile.read_text())

    def test_wrong_code_then_hidden_input_ignored(self):
        # 17 is wrong, 18 works; monitor ignores the hidden-input command;
        # ioreg keeps showing the devices, so presence falls back to the agent
        answers = ["", "1", "n", "", "2", "y", "n", "", "", ""]
        stuck = [KM] * 100
        mon, cfg = self.run_calibrate(answers, presence_seq=stuck, agent_ok=True)
        self.assertEqual(mon.sets, [17, 18, 27])
        self.assertEqual(cfg.get("monitor", "peer_input"), "18")
        self.assertEqual(cfg.get("follow", "pull"), "no")
        self.assertEqual(cfg.get("devices", "presence"), "optionsplus")

    def test_cannot_read_own_input_asks(self):
        answers = ["", "4", "1", "y", "y", "", ""]
        mon, cfg = self.run_calibrate(answers, own=None, presence_seq=[NONE, KM])
        self.assertEqual(cfg.get("monitor", "this_input"), "27")


class InstallTests(unittest.TestCase):
    def test_install_runs_from_private_copy(self):
        root = Path(tempfile.mkdtemp())
        cfgfile = root / "config.ini"
        cfgfile.write_text("[monitor]\nthis_input = 27\npeer_input = 17\n")
        calls = []
        with mock.patch.object(ds, "CONFIG_PATH", cfgfile), \
                mock.patch.object(ds, "INSTALL_DIR", root / "share"), \
                mock.patch.object(ds, "PLIST_PATH", root / "LaunchAgents" / "x.plist"), \
                mock.patch.object(ds, "LOG_PATH", root / "Logs" / "d.log"), \
                mock.patch.object(ds, "run", side_effect=lambda c, **k: calls.append(c) or (0, "", "")), \
                mock.patch("sys.stdout"):
            ds.main(["install"])
            import plistlib
            with (root / "LaunchAgents" / "x.plist").open("rb") as fh:
                pl = plistlib.load(fh)
            copy = root / "share" / "deskswitch.py"
            self.assertEqual(pl["ProgramArguments"][1:], [str(copy), "follow"])
            self.assertEqual(copy.read_text(), Path(ds.__file__).read_text())
            self.assertEqual(calls[-1][:2], ["/bin/launchctl", "bootstrap"])
            ds.main(["uninstall"])
            self.assertFalse(copy.exists())


class UpdateConfigTests(unittest.TestCase):
    def test_in_place_edit(self):
        f = Path(tempfile.mkdtemp()) / "c.ini"
        f.write_text("[monitor]\n# this_input = 1\nthis_input =\nname = X\n\n[follow]\npull = yes\n")
        ds.update_config_file(f, {"monitor": {"this_input": 27, "peer_input": 17},
                                  "follow": {"pull": "no"}, "hooks": {"after_here": "say hi"}})
        txt = f.read_text()
        self.assertIn("# this_input = 1\nthis_input = 27\nname = X\npeer_input = 17\n", txt)
        self.assertIn("[follow]\npull = no", txt)
        self.assertIn("[hooks]\nafter_here = say hi", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
