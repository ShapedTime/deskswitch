#!/usr/bin/env python3
"""
deskswitch - make one monitor follow your Logitech keyboard/mouse between Macs.

Written for: 2x MacBook + Huawei MateView GT 34 (ZQE-CAA) + MX Keys S + MX Master 4
(Logi Flow / Enhanced Easy-Switch). Only the defaults are specific to that hardware.

How it works
  monitor   DDC/CI, VCP 0x60 (Input Select), sent through BetterDisplay's CLI or m1ddc.
  presence  macOS drops a Bluetooth HID device from the IORegistry when the device
            switches to another host, so `ioreg` tells this Mac whether the keyboard
            and mouse are here (no permissions needed). Alternative: ask the
            Logi Options+ agent (also works with a Bolt receiver).
  devices   `switch` moves the keyboard and mouse through the Logi Options+ agent's
            local socket (SET /change_host/<id>/host). Undocumented API, see README.

Commands
  init                     write ~/.config/deskswitch/config.ini (if missing)
  calibrate                guided test: input codes, hidden-input DDC, presence; writes config
  status                   what this Mac sees right now
  ddc get | ddc set N      read / write the monitor input (VCP 0x60)
  switch [--no-devices]    push monitor + keyboard + mouse to the other Mac
  follow [--dry-run]       daemon: the monitor follows the keyboard/mouse
  pause | resume | toggle  pin the monitor where it is (daemon stops acting)
  agent list               devices known to the Logi Options+ agent
  install | uninstall      LaunchAgent that runs `follow` at login

Python 3.9+ standard library only (the stock /usr/bin/python3 is fine).
"""
from __future__ import annotations

import argparse
import configparser
import glob
import json
import logging
import os
import plistlib
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("DESKSWITCH_HOME", str(HOME / ".config" / "deskswitch")))
CONFIG_PATH = CONFIG_DIR / "config.ini"
PAUSE_FLAG = CONFIG_DIR / "paused"
LAST_ACTION = CONFIG_DIR / "last_action.json"
LABEL = "local.deskswitch"
PLIST_PATH = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_PATH = HOME / "Library" / "Logs" / "deskswitch.log"
INSTALL_DIR = HOME / ".local" / "share" / "deskswitch"   # not TCC-protected, unlike ~/Downloads

# Homebrew lives in /opt/homebrew on Apple Silicon, /usr/local on Intel, and some
# people (and LaunchAgents with a bare PATH) need ~/.homebrew too.
TOOL_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", str(HOME / ".homebrew" / "bin")]
BETTERDISPLAY_APP_BINS = [
    "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay",
    str(HOME / "Applications" / "BetterDisplay.app" / "Contents" / "MacOS" / "BetterDisplay"),
]
IOREG = "/usr/sbin/ioreg"

HERE, AWAY = "HERE", "AWAY"

DEFAULTS = {
    "monitor": {
        "name": "ZQE-CAA",        # substring of the display name (BetterDisplay -nameLike)
        "this_input": "",         # VCP 0x60 value of the input THIS Mac is plugged into
        "peer_input": "",         # VCP 0x60 value of the other Mac's input
        "backend": "betterdisplay",  # betterdisplay | m1ddc | custom
        "m1ddc_display": "",      # optional m1ddc display number/UUID
        "settle": "0",            # seconds to wait after an input switch before hooks
    },
    "commands": {
        # Override the DDC commands. {name} and {value} are substituted.
        "set": "",
        "get": "",
    },
    "devices": {
        "presence": "ioreg",      # ioreg | optionsplus
        "keyboard": "MX Keys",    # case-insensitive substring of the HID product name
        "mouse": "MX Master",     # leave empty to ignore that device
    },
    "follow": {
        "pull": "yes",            # devices arrive here  -> point the monitor here
        "push": "no",             # devices leave while in use -> point it at the peer
        "arrive_on": "keyboard",  # keyboard | mouse | both | any
        "leave_on": "all",        # all | keyboard | mouse | any
        "poll": "0.7",
        "debounce": "2",          # consecutive identical readings to accept a change
        "cooldown": "6",          # seconds between monitor switches
        "active_window": "10",    # push only if you touched an input device this recently
    },
    "logi": {
        "peer_host": "",          # 0-based Easy-Switch channel the OTHER Mac is paired on
    },
    "hooks": {
        "after_here": "",         # shell command after the monitor was pointed here
        "after_away": "",         # shell command after the monitor was pointed away
    },
}

log = logging.getLogger("deskswitch")


# --------------------------------------------------------------------------- utils

def load_config(path: Path = CONFIG_PATH) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
    cfg.read_dict(DEFAULTS)
    if path.exists():
        cfg.read(path)
    return cfg


def opt_int(cfg, section, key):
    raw = cfg.get(section, key, fallback="").strip()
    if not raw:
        return None
    return int(raw, 0)


def find_tool(name: str, extra=()) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in list(TOOL_DIRS):
        cand = os.path.join(d, name)
        if os.access(cand, os.X_OK):
            return cand
    for cand in extra:
        if os.access(cand, os.X_OK):
            return cand
    return None


def run(cmd: list, timeout: float = 8.0):
    """Run a command, never raise. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {' '.join(map(str, cmd))}"


def parse_int(text: str) -> int | None:
    m = re.search(r"\b(0x[0-9a-fA-F]+|\d+)\b", text or "")
    if not m:
        return None
    return int(m.group(1), 0)


def run_hook(cmd: str, dry_run: bool = False):
    if not cmd.strip():
        return
    if dry_run:
        log.info("[dry-run] would run hook: %s", cmd)
        return
    rc, out, err = run(["/bin/sh", "-c", cmd], timeout=20)
    log.info("hook rc=%s %s", rc, (out + err).strip()[:300])


def is_paused() -> bool:
    return PAUSE_FLAG.exists()


def record_action(input_value: int):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LAST_ACTION.write_text(json.dumps({"t": time.time(), "input": input_value}))
    except OSError:
        pass


def recent_action(window: float):
    """Input value set by any deskswitch command within `window` seconds, else None."""
    try:
        d = json.loads(LAST_ACTION.read_text())
        if time.time() - float(d["t"]) <= window:
            return int(d["input"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


# ------------------------------------------------------------------------- monitor

class Monitor:
    """DDC/CI input select (VCP 0x60) through an external tool."""

    def __init__(self, cfg, dry_run: bool = False):
        self.name = cfg.get("monitor", "name")
        self.backend = cfg.get("monitor", "backend").strip().lower()
        self.m1ddc_display = cfg.get("monitor", "m1ddc_display").strip()
        self.custom_set = cfg.get("commands", "set").strip()
        self.custom_get = cfg.get("commands", "get").strip()
        self.dry_run = dry_run

    # -- command construction (kept separate so it can be tested without a Mac)
    def set_cmd(self, value: int) -> list:
        if self.custom_set:
            return shlex.split(self.custom_set.format(name=self.name, value=value))
        if self.backend == "m1ddc":
            tool = find_tool("m1ddc") or "m1ddc"
            disp = ["display", self.m1ddc_display] if self.m1ddc_display else []
            return [tool] + disp + ["set", "input", str(value)]
        tool = find_tool("betterdisplaycli", BETTERDISPLAY_APP_BINS) or "betterdisplaycli"
        return [tool, "set", f"-nameLike={self.name}", "-feature=ddc", "-vcp=0x60", f"-value={value}"]

    def get_cmd(self) -> list | None:
        if self.custom_get:
            return shlex.split(self.custom_get.format(name=self.name, value=""))
        if self.backend == "m1ddc":
            return None  # m1ddc cannot read the input source
        tool = find_tool("betterdisplaycli", BETTERDISPLAY_APP_BINS) or "betterdisplaycli"
        return [tool, "get", f"-nameLike={self.name}", "-feature=ddc", "-vcp=0x60", "-value"]

    def set_input(self, value: int) -> bool:
        cmd = self.set_cmd(value)
        if self.dry_run:
            log.info("[dry-run] would run: %s", " ".join(cmd))
            return True
        rc, out, err = run(cmd)
        msg = (out + err).strip()
        if rc != 0 or re.search(r"fail|error|not found|invalid", msg, re.I):
            log.warning("DDC set %s failed (rc=%s): %s", value, rc, msg[:300])
            return False
        log.info("monitor -> input %s", value)
        record_action(value)
        return True

    def get_input(self) -> int | None:
        cmd = self.get_cmd()
        if not cmd:
            return None
        rc, out, err = run(cmd)
        if rc != 0 or re.search(r"fail|error|invalid", out + err, re.I):
            return None
        return parse_int(out)


# ------------------------------------------------------------- Logi Options+ agent

class AgentError(ConnectionError):
    pass


class LogiAgent:
    """Client for the Logi Options+ agent socket.

    Wire format (reverse engineered by github.com/saimanish1/logitech-ipc-protocol):
      LE32(total_len) + BE32(len("json")) + b"json" + BE32(msg_len) + JSON
    Requests carry msg_id/verb/path[/payload]; responses carry msgId and result.code.
    Undocumented: Logitech can change or lock it down (2.7 did on Windows).
    """

    SOCKET_GLOB = "/tmp/logitech_kiros_agent-*"

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self.sock = None
        self.buf = b""
        self.msg_id = 0

    @classmethod
    def find_socket(cls) -> str | None:
        socks = sorted(s for s in glob.glob(cls.SOCKET_GLOB) if not s.endswith(".real"))
        return socks[0] if socks else None

    @staticmethod
    def encode(msg: dict) -> bytes:
        data = json.dumps(msg, separators=(",", ":")).encode()
        inner = struct.pack(">I", 4) + b"json" + struct.pack(">I", len(data)) + data
        return struct.pack("<I", len(inner)) + inner

    @staticmethod
    def decode_one(buf: bytes):
        """Return (proto, payload_bytes, rest) or None if buf holds no full frame."""
        if len(buf) < 4:
            return None
        total = struct.unpack_from("<I", buf, 0)[0]
        if len(buf) < 4 + total:
            return None
        inner, rest = buf[4:4 + total], buf[4 + total:]
        plen = struct.unpack_from(">I", inner, 0)[0]
        proto = inner[4:4 + plen]
        ipos = 4 + plen
        mlen = struct.unpack_from(">I", inner, ipos)[0]
        return proto, inner[ipos + 4:ipos + 4 + mlen], rest

    def connect(self):
        path = self.find_socket()
        if not path:
            raise AgentError("Logi Options+ agent socket not found (is Options+ running?)")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(path)
        except OSError as e:
            s.close()
            raise AgentError(f"cannot connect to {path}: {e}")
        self.sock, self.buf = s, b""

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock, self.buf = None, b""

    def _recv(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise AgentError("agent closed the connection (newer Options+ builds may "
                             "refuse third-party clients)")
        self.buf += chunk

    def request(self, verb: str, path: str, payload: dict | None = None) -> dict:
        if self.sock is None:
            self.connect()
        self.msg_id += 1
        mid = str(self.msg_id)
        msg = {"msg_id": mid, "verb": verb, "path": path}
        if payload is not None:
            msg["payload"] = payload
        deadline = time.time() + self.timeout
        try:
            self.sock.sendall(self.encode(msg))
            while time.time() < deadline:
                frame = self.decode_one(self.buf)
                if frame is None:
                    self._recv()
                    continue
                proto, raw, self.buf = frame
                if proto != b"json":
                    continue
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("msgId") == mid:
                    return r
        except (OSError, struct.error) as e:
            self.close()
            raise AgentError(f"agent connection lost: {e}")
        self.close()
        raise AgentError(f"no reply to {verb} {path}")

    def devices(self) -> list:
        r = self.request("GET", "/devices/list")
        infos = r.get("payload", {}).get("deviceInfos", []) or []
        return [d for d in infos if d.get("connectionType") != "VIRTUAL"]

    def change_host(self, device_id: str, host: int) -> str:
        r = self.request("SET", f"/change_host/{device_id}/host", {
            "@type": "type.googleapis.com/logi.protocol.devices.ChangeHost",
            "host": host,
        })
        return r.get("result", {}).get("code", "NO_CODE")


# ------------------------------------------------------------------------ presence

def ioreg_products() -> list:
    rc, out, err = run([IOREG, "-r", "-c", "IOHIDDevice", "-d", "1", "-l", "-w", "0"])
    if rc != 0:
        raise RuntimeError(f"ioreg failed: {err.strip()}")
    return re.findall(r'"Product" = "([^"]*)"', out)


def match(name: str, products) -> bool:
    n = name.strip().lower()
    return bool(n) and any(n in (p or "").lower() for p in products)


class Presence:
    """Answers: are the keyboard / mouse connected to THIS Mac right now?"""

    def __init__(self, cfg):
        self.method = cfg.get("devices", "presence").strip().lower()
        self.names = {
            "keyboard": cfg.get("devices", "keyboard").strip(),
            "mouse": cfg.get("devices", "mouse").strip(),
        }
        self.agent = LogiAgent() if self.method == "optionsplus" else None

    @property
    def roles(self):
        return [r for r, n in self.names.items() if n]

    def read(self) -> dict:
        if self.method == "optionsplus":
            devs = self.agent.devices()
            products = [d.get("displayName", "") for d in devs if d.get("connected")]
        else:
            products = ioreg_products()
        return {r: match(self.names[r], products) for r in self.roles}


def hid_idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse/trackpad event on this Mac."""
    rc, out, _ = run([IOREG, "-c", "IOHIDSystem", "-d", "4", "-w", "0"])
    m = re.search(r'"HIDIdleTime" = (\d+)', out)
    return int(m.group(1)) / 1e9 if m else None


# -------------------------------------------------------------------------- follow

def arrived(p: dict, mode: str) -> bool:
    vals = list(p.values())
    if mode == "both":
        return all(vals)
    if mode == "any":
        return any(vals)
    return p.get(mode, False)


def left(p: dict, mode: str) -> bool:
    vals = list(p.values())
    if mode == "keyboard" or mode == "mouse":
        return not p.get(mode, True)
    if mode == "any":
        return not all(vals)
    return not any(vals)  # "all": every configured device is gone


class Follower:
    """Debounced HERE/AWAY state machine. One `step()` per poll.

    HERE  = the device(s) named by arrive_on are connected to this Mac
    AWAY  = the device(s) named by leave_on are gone
    anything in between keeps the previous state (e.g. keyboard asleep, mouse here).
    """

    def __init__(self, cfg, monitor, presence_fn, idle_fn=hid_idle_seconds,
                 clock=time.monotonic, paused_fn=is_paused, recent_fn=recent_action):
        f = "follow"
        self.pull = cfg.getboolean(f, "pull")
        self.push = cfg.getboolean(f, "push")
        self.arrive_on = cfg.get(f, "arrive_on").strip().lower()
        self.leave_on = cfg.get(f, "leave_on").strip().lower()
        self.debounce = max(1, cfg.getint(f, "debounce"))
        self.cooldown = cfg.getfloat(f, "cooldown")
        self.active_window = cfg.getfloat(f, "active_window")
        self.this_input = opt_int(cfg, "monitor", "this_input")
        self.peer_input = opt_int(cfg, "monitor", "peer_input")
        self.settle = cfg.getfloat("monitor", "settle")
        self.hook_here = cfg.get("hooks", "after_here")
        self.hook_away = cfg.get("hooks", "after_away")
        self.monitor = monitor
        self.presence_fn = presence_fn
        self.idle_fn = idle_fn
        self.clock = clock
        self.paused_fn = paused_fn
        self.recent_fn = recent_fn

        self.state = None          # debounced HERE / AWAY
        self.applied = None        # state we last acted on (or deliberately ignored)
        self.pending = None
        self.pending_n = 0
        self.idle_at_change = None
        self.cooldown_until = 0.0

    def observe(self, p: dict):
        if arrived(p, self.arrive_on):
            return HERE
        if left(p, self.leave_on):
            return AWAY
        return self.state  # in-between: keep what we had

    def step(self):
        """Poll once. Returns the action taken: 'pull', 'push', 'skip:<why>' or None."""
        now = self.clock()
        try:
            p = self.presence_fn()
        except Exception as e:  # presence source hiccup: try again next poll
            log.warning("presence read failed: %s", e)
            return None
        obs = self.observe(p)
        if obs is None:
            return None
        if self.state is None:
            self.state = self.applied = obs
            log.info("initial state: devices %s %s", obs, p)
            return None
        if obs != self.state:
            if obs == self.pending:
                self.pending_n += 1
            else:
                self.pending, self.pending_n = obs, 1
            if self.pending_n >= self.debounce:
                self.state, self.pending, self.pending_n = obs, None, 0
                self.idle_at_change = self.idle_fn() if obs == AWAY else None
                log.info("devices now %s %s", obs, p)
        else:
            self.pending, self.pending_n = None, 0

        if self.state != self.applied and now >= self.cooldown_until:
            return self.act(now)
        return None

    def act(self, now):
        target = self.state
        self.applied = target
        if self.paused_fn():
            log.info("paused: not following (%s)", target)
            return "skip:paused"
        if target == HERE:
            if not self.pull or self.this_input is None:
                return "skip:pull-off"
            cur = self.monitor.get_input()
            if cur == self.this_input:
                log.info("monitor already on this Mac (input %s)", cur)
                return "skip:already"
            ok = self.monitor.set_input(self.this_input)
            self.cooldown_until = now + self.cooldown
            if ok:
                if self.settle:
                    time.sleep(self.settle)
                run_hook(self.hook_here, self.monitor.dry_run)
            return "pull" if ok else "skip:ddc-failed"
        # AWAY
        if not self.push or self.peer_input is None:
            return "skip:push-off"
        idle = self.idle_at_change
        if idle is not None and idle > self.active_window:
            log.info("devices left after %.0fs idle: looks like sleep, not a switch", idle)
            return "skip:idle"
        if self.recent_fn(self.cooldown) == self.peer_input:
            log.info("peer input was just set by `switch`; nothing to do")
            return "skip:recent"
        ok = self.monitor.set_input(self.peer_input)
        self.cooldown_until = now + self.cooldown
        if ok:
            run_hook(self.hook_away, self.monitor.dry_run)
        return "push" if ok else "skip:ddc-failed"


# ------------------------------------------------------------------------ commands

def need(cfg, key):
    v = opt_int(cfg, "monitor", key)
    if v is None:
        sys.exit(f"Set [monitor] {key} in {CONFIG_PATH} first (see README: 'Find your input codes').")
    return v


def cmd_init(cfg, args):
    if CONFIG_PATH.exists() and not args.force:
        print(f"{CONFIG_PATH} already exists (use --force to overwrite)")
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    example = Path(__file__).with_name("config.example.ini")
    if example.exists():
        CONFIG_PATH.write_text(example.read_text())
    else:
        out = configparser.ConfigParser(interpolation=None)
        out.read_dict(DEFAULTS)
        with CONFIG_PATH.open("w") as fh:
            out.write(fh)
    print(f"wrote {CONFIG_PATH}")


def cmd_status(cfg, args):
    mon = Monitor(cfg)
    print(f"config        {CONFIG_PATH} ({'found' if CONFIG_PATH.exists() else 'missing: run init'})")
    print(f"paused        {is_paused()}")
    print(f"ddc backend   {mon.backend}: set -> {' '.join(mon.set_cmd(0))}")
    cur = mon.get_input()
    print(f"monitor input {cur if cur is not None else 'unknown (read failed: normal if this Mac is not the one shown)'}")
    print(f"this/peer     {cfg.get('monitor', 'this_input') or '?'} / {cfg.get('monitor', 'peer_input') or '?'}")
    try:
        prods = ioreg_products()
        logi = [p for p in prods if re.search(r"MX|Logi|Bolt|Receiver", p, re.I)]
        print(f"HID (ioreg)   {', '.join(sorted(set(logi))) or 'no Logitech HID devices'}")
    except RuntimeError as e:
        print(f"HID (ioreg)   {e}")
    try:
        pres = Presence(cfg).read()
        print(f"presence      {pres}  (method: {cfg.get('devices', 'presence')})")
    except Exception as e:
        print(f"presence      error: {e}")
    idle = hid_idle_seconds()
    print(f"HID idle      {idle:.1f}s" if idle is not None else "HID idle      unknown")
    sock = LogiAgent.find_socket()
    print(f"Options+ sock {sock or 'not found'}")


def cmd_ddc(cfg, args):
    mon = Monitor(cfg, dry_run=args.dry_run)
    if args.op == "get":
        v = mon.get_input()
        if v is None:
            print("read failed (normal on the input that is not being shown; also check the name)")
            sys.exit(1)
        print(v)
    else:
        if args.value is None:
            sys.exit("usage: ddc set N")
        sys.exit(0 if mon.set_input(int(args.value, 0)) else 1)


def cmd_agent(cfg, args):
    agent = LogiAgent()
    try:
        for d in agent.devices():
            print(f"{d.get('id', '?'):16} {d.get('deviceType', '?'):9} "
                  f"{'connected' if d.get('connected') else 'away':9} "
                  f"{d.get('connectionType', '?'):10} {d.get('displayName', '?')}")
    except AgentError as e:
        sys.exit(f"Options+ agent: {e}")
    finally:
        agent.close()


def cmd_switch(cfg, args):
    """Push: monitor -> peer, then keyboard + mouse -> peer host."""
    peer_input = need(cfg, "peer_input")
    mon = Monitor(cfg, dry_run=args.dry_run)
    # This Mac is the one on screen, so DDC from here is the reliable direction.
    mon.set_input(peer_input)
    run_hook(cfg.get("hooks", "after_away"), args.dry_run)
    if args.no_devices:
        return
    peer_host = opt_int(cfg, "logi", "peer_host")
    if peer_host is None:
        print("[logi] peer_host not set: press the keyboard's Easy-Switch key for the other Mac "
              "(with Enhanced Easy-Switch linked, the mouse follows).")
        return
    names = [cfg.get("devices", k).strip().lower() for k in ("keyboard", "mouse")]
    agent = LogiAgent()
    try:
        devs = [d for d in agent.devices() if d.get("connected")
                and d.get("deviceType") in ("KEYBOARD", "MOUSE")]
        named = [d for d in devs if any(n and n in d.get("displayName", "").lower() for n in names)]
        # mouse first, keyboard last: the keyboard is what the peer's daemon waits for
        for d in sorted(named or devs, key=lambda d: d.get("deviceType") != "MOUSE"):
            if args.dry_run:
                print(f"[dry-run] would move {d.get('displayName')} ({d.get('id')}) to host {peer_host}")
                continue
            code = agent.change_host(d["id"], peer_host)
            print(f"{d.get('displayName')}: {code}")
    except AgentError as e:
        print(f"Options+ agent unavailable ({e}). Press the keyboard's Easy-Switch key instead.")
    finally:
        agent.close()


def cmd_follow(cfg, args):
    if opt_int(cfg, "monitor", "this_input") is None:
        sys.exit(f"Set [monitor] this_input (and peer_input if push=yes) in {CONFIG_PATH} first.")
    mon = Monitor(cfg, dry_run=args.dry_run)
    presence = Presence(cfg)
    fol = Follower(cfg, mon, presence.read)
    poll = cfg.getfloat("follow", "poll")
    log.info("following %s (arrive_on=%s leave_on=%s pull=%s push=%s%s)",
             presence.names, fol.arrive_on, fol.leave_on, fol.pull, fol.push,
             " DRY-RUN" if args.dry_run else "")
    while True:
        try:
            fol.step()
        except Exception as e:  # keep the daemon alive no matter what
            log.exception("step failed: %s", e)
        time.sleep(poll)


def cmd_pause(cfg, args):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if args.cmd == "pause" or (args.cmd == "toggle" and not is_paused()):
        PAUSE_FLAG.touch()
        state = "paused: the monitor stays where it is"
    else:
        PAUSE_FLAG.unlink(missing_ok=True)
        state = "following"
    print(state)
    if args.notify:
        run(["/usr/bin/osascript", "-e", f'display notification "{state}" with title "deskswitch"'])


def update_config_file(path: Path, values: dict):
    """Set {section: {key: value}} in an INI file in place, keeping comments."""
    lines = path.read_text().splitlines() if path.exists() else []
    for section, kv in values.items():
        start = next((i for i, l in enumerate(lines) if l.strip().lower() == f"[{section}]"), None)
        if start is None:
            lines += ["", f"[{section}]"]
            start = len(lines) - 1
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")),
                   len(lines))
        for key, val in kv.items():
            pat = re.compile(rf"^\s*{re.escape(key)}\s*[=:]")
            idx = next((i for i in range(start + 1, end) if pat.match(lines[i])), None)
            if idx is None:
                # insert after the section's last non-blank line
                ins = end
                while ins - 1 > start and not lines[ins - 1].strip():
                    ins -= 1
                lines.insert(ins, f"{key} = {val}")
                end += 1
            else:
                lines[idx] = f"{key} = {val}"
    path.write_text("\n".join(lines) + "\n")


# MCCS 2.2 input codes; Huawei may deviate, which is why calibrate checks each one.
INPUT_MENU = [("HDMI-1", 17), ("HDMI-2", 18), ("DP", 15), ("USB-C", 27)]


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def ask_yes(prompt: str) -> bool:
    return ask(f"{prompt} [y/N] ").strip().lower().startswith("y")


def pick_input(prompt: str, exclude=None) -> int | None:
    opts = [(n, c) for n, c in INPUT_MENU if c != exclude]
    menu = "  ".join(f"{i + 1}) {n}" for i, (n, _) in enumerate(opts))
    raw = ask(f"{prompt}\n     {menu}  or type a code: ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(opts):
        return opts[int(raw) - 1][1]
    try:
        return int(raw, 0) if raw else None
    except ValueError:
        return None


def wait_presence(presence, want_here: bool, timeout: float = 8.0) -> dict:
    """Poll until the devices are all here / all gone, or the timeout passes."""
    deadline = time.time() + timeout
    p = {}
    while True:
        try:
            p = presence.read()
        except Exception as e:
            print(f"     presence read failed: {e}")
            return {}
        if p and (all(p.values()) if want_here else not any(p.values())):
            return p
        if time.time() >= deadline:
            return p
        time.sleep(0.5)


def cmd_calibrate(cfg, args):
    """Interactive: find input codes, test DDC on the hidden input, test presence."""
    if args.name:
        cfg.set("monitor", "name", args.name)
    mon = Monitor(cfg)
    settle = 2.5
    print(f"""
deskswitch calibrate - monitor '{mon.name}', DDC backend '{mon.backend}'

Put this Terminal on the MacBook's BUILT-IN screen and answer with the BUILT-IN
keyboard: the ultrawide will change inputs, and the MX keyboard will leave this Mac.
Start with THIS Mac showing on the ultrawide, the MX keyboard + mouse on this Mac,
and the OTHER Mac awake (screen on).
""")
    ask("Press Enter when ready... ")

    # 1. this Mac's input: the monitor reports the input it is showing
    own = mon.get_input()
    if own is not None:
        print(f"1/5  Monitor reports input {own}: that is this Mac's code.")
    else:
        print("1/5  Could not read the input over DDC (BetterDisplay running? name right?).")
        own = pick_input("     Which input is THIS Mac plugged into?")
        if own is None:
            sys.exit("     Aborted.")

    # 2. the other Mac's input: switch from here, where DDC is certain to be heard
    peer = None
    candidate = pick_input("2/5  Which input is the OTHER Mac plugged into?", exclude=own)
    for _ in range(4):
        if candidate is None:
            break
        print(f"     Switching the ultrawide to input {candidate}...")
        mon.set_input(candidate)
        time.sleep(settle)
        if ask_yes("     Is the OTHER Mac's desktop showing on the ultrawide now?"):
            peer = candidate
            break
        ask("     If the ultrawide left this Mac (e.g. 'no signal'), switch back to this Mac\n"
            "     with the joystick. Press Enter to continue... ")
        candidate = pick_input("     Not that one. Try another code (Huawei may not follow MCCS),"
                               " blank to stop:", exclude=own)
    if peer is None:
        print("     Could not find the other Mac's code. Run probe.sh and look at the "
              "capabilities string (60(...)), then edit the config by hand.")
        return

    # 3. the decisive test: does the monitor obey a Mac it is not showing?
    print(f"3/5  Asking for input {own} from THIS Mac, which is not on screen now...")
    mon.set_input(own)
    time.sleep(settle)
    pull_ok = ask_yes("     Is this Mac back on the ultrawide?")
    if not pull_ok:
        ask("     Switch back to this Mac with the joystick, then press Enter... ")

    # 4. presence: do the MX devices disappear from this Mac when they switch away?
    pres = Presence(cfg)
    print(f"4/5  Watching for keyboard '{pres.names['keyboard']}' and mouse '{pres.names['mouse']}'.")
    ask("     Move the MX keyboard AND mouse to the other Mac (Flow or Easy-Switch),\n"
        "     then press Enter on the built-in keyboard... ")
    away = wait_presence(pres, want_here=False)
    ask("     Bring them back to this Mac, then press Enter... ")
    back = wait_presence(pres, want_here=True)
    presence_ok = bool(away) and not any(away.values()) and bool(back) and all(back.values())
    print(f"     away: {away}  back: {back}  -> {'OK' if presence_ok else 'NOT reliable'}")

    # 5. Logi Options+ agent (needed for `switch` and for presence = optionsplus)
    agent = LogiAgent()
    try:
        devs = agent.devices()
        agent_ok = True
        print("5/5  Options+ agent answers: " + ", ".join(
            f"{d.get('displayName')} ({'here' if d.get('connected') else 'away'})" for d in devs))
    except AgentError as e:
        agent_ok = False
        print(f"5/5  Options+ agent: {e}")
    finally:
        agent.close()

    presence = cfg.get("devices", "presence")
    if not presence_ok and agent_ok:
        presence = "optionsplus"
    values = {
        "monitor": {"name": mon.name, "this_input": own, "peer_input": peer},
        "devices": {"presence": presence},
        # push from this Mac always works: when you leave it, it is the one on screen
        "follow": {"pull": "yes" if pull_ok else "no", "push": "yes"},
    }
    if not CONFIG_PATH.exists():
        cmd_init(cfg, argparse.Namespace(force=False))
    update_config_file(CONFIG_PATH, values)
    print(f"\nWrote {CONFIG_PATH}: this_input={own} peer_input={peer} "
          f"pull={'yes' if pull_ok else 'no'} push=yes presence={presence}")
    if pull_ok:
        print("RESULT: the monitor obeys this Mac even when it shows the other one, so THIS Mac\n"
              "can run both directions alone. The other Mac needs nothing installed.")
    else:
        print("RESULT: the monitor only obeys the Mac it is showing. This Mac hands the screen\n"
              "over when you leave it; the other Mac must do the same (install deskswitch there\n"
              f"with this_input={peer}, peer_input={own}, pull=no, push=yes).")
    if not presence_ok and not agent_ok:
        print("WARNING: neither ioreg nor the Options+ agent tracked the devices; following won't work.")
    print("Next: python3 deskswitch.py follow --dry-run   then   python3 deskswitch.py install")


def cmd_install(cfg, args):
    uid = os.getuid()
    run(["/bin/launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    installed = INSTALL_DIR / "deskswitch.py"
    if args.cmd == "uninstall":
        PLIST_PATH.unlink(missing_ok=True)
        installed.unlink(missing_ok=True)
        print("LaunchAgent removed")
        return
    if opt_int(cfg, "monitor", "this_input") is None:
        sys.exit(f"Configure {CONFIG_PATH} (this_input/peer_input) before installing.")
    # macOS privacy protection (TCC) stops launchd jobs from reading ~/Downloads,
    # ~/Desktop, ~/Documents and iCloud Drive, even though Terminal can. So the
    # daemon always runs from a private copy in an unprotected folder.
    src = Path(__file__).resolve()
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if src != installed.resolve():
        shutil.copy2(src, installed)
        print(f"copied {src} -> {installed}")
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(installed), "follow"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "EnvironmentVariables": {"PATH": ":".join(TOOL_DIRS + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])},
    }
    with PLIST_PATH.open("wb") as fh:
        plistlib.dump(plist, fh)
    rc, out, err = run(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)])
    print(f"installed {PLIST_PATH} (launchctl rc={rc}) - log: {LOG_PATH}")
    if rc != 0:
        print((out + err).strip())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deskswitch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--force", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("calibrate", help="interactive: input codes, hidden-input DDC test, presence test")
    p.add_argument("--name", help="display name substring (default from config)")
    p = sub.add_parser("ddc")
    p.add_argument("op", choices=["get", "set"])
    p.add_argument("value", nargs="?")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("switch")
    p.add_argument("--no-devices", action="store_true", help="only switch the monitor")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("follow")
    p.add_argument("--dry-run", action="store_true")
    for name in ("pause", "resume", "toggle"):
        p = sub.add_parser(name)
        p.add_argument("--notify", action="store_true", help="show a macOS notification")
    p = sub.add_parser("agent")
    p.add_argument("op", choices=["list"])
    sub.add_parser("install")
    sub.add_parser("uninstall")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config()
    handlers = {
        "init": cmd_init, "status": cmd_status, "calibrate": cmd_calibrate,
        "ddc": cmd_ddc, "switch": cmd_switch,
        "follow": cmd_follow, "pause": cmd_pause, "resume": cmd_pause, "toggle": cmd_pause,
        "agent": cmd_agent, "install": cmd_install, "uninstall": cmd_install,
    }
    handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
