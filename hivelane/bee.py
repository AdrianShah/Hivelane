"""The desktop bee: a small always-on-top badge that shows the room's state
next to whatever app the agent runs in (a terminal, Cursor, VS Code, Claude
desktop), for people who never open the dashboard.

Design constraints:

- Zero tokens. Nothing here reaches an agent's context; the bee is a separate
  process that reads the local daemon.
- Lightweight. No new dependencies and no browser engine: macOS draws it with
  AppKit through `osascript -l JavaScript`, Windows with WinForms through
  PowerShell, Linux with tkinter when it is installed. It polls the daemon's
  GET /bee every BEE_POLL_SECONDS and only redraws on change (plus a short
  pulse while there is a warning).
- Never in the way of an agent. `hivelane up` may be run by an agent in a tool
  call: the bee is started detached with every stdio stream on /dev/null, so
  the agent's command returns at once.

HIVELANE_BEE=0 (or `hivelane bee off`) turns it off; it also stays off on a
headless machine, over SSH and in CI.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

ENV_SWITCH = "HIVELANE_BEE"
_OFF = {"0", "false", "no", "off"}
BEE_POLL_SECONDS = 2.0
# The bee quits after the daemon has been unreachable this long.
BEE_GIVE_UP_SECONDS = 300
# Daemon-side: how a state ranks when several repos are connected.
_RANK = {"off": 0, "connecting": 1, "live": 2, "warn": 3, "block": 4, "error": 5}


# --- state (daemon side, pure) --------------------------------------------------


def bee_state(status: dict[str, Any]) -> dict[str, Any]:
    """What the bee shows, from the daemon's status(): the worst state across
    its repos. Keys: state (off|connecting|live|warn|block|error), label (short
    text beside the bee, "" for none), tip (hover text), root (the repo a
    click opens, or None)."""
    raw = status.get("repos")
    repos: list[Any] = raw if isinstance(raw, list) else []
    best: dict[str, Any] = {"state": "off", "label": "", "tip": "Hivelane: no room on this machine", "root": None}
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        view = _repo_view(repo)
        if _RANK[view["state"]] > _RANK[best["state"]]:
            best = view
    return best


def _repo_view(repo: dict[str, Any]) -> dict[str, Any]:
    room = str(repo.get("room") or "?")
    root = repo.get("root")
    peers = repo.get("peers")
    agents = (len(peers) if isinstance(peers, list) else 0) + 1
    head = f"Hivelane · {room} · {agents} agent{'s' if agents != 1 else ''}"
    if repo.get("fatal"):
        return {"state": "error", "label": "stopped", "tip": f"Hivelane · {room}: stopped ({repo.get('fatal')})", "root": root}
    if not repo.get("connected"):
        return {"state": "connecting", "label": "", "tip": f"Hivelane · {room}: connecting", "root": root}
    held = repo.get("held") if isinstance(repo.get("held"), int) else 0
    raw_alert = repo.get("alert")
    alert: Optional[dict[str, Any]] = raw_alert if isinstance(raw_alert, dict) else None
    kind = alert.get("kind") if alert else None
    if held or kind == "block":
        where = _where(alert) if kind == "block" else ""
        label = f"{held} held" if held else "edit held"
        return {"state": "block", "label": label, "tip": f"{head}\nAn edit is held{where}. Click for the dashboard.", "root": root}
    if kind == "warn":
        return {"state": "warn", "label": "overlap", "tip": f"{head}\nSomeone is nearby{_where(alert)}.", "root": root}
    if kind == "nudge" and alert is not None:
        text = str(alert.get("text") or "you were pinged")
        return {"state": "warn", "label": "pinged", "tip": f"{head}\n{text[:1].upper()}{text[1:]}", "root": root}
    return {"state": "live", "label": "", "tip": f"{head}\nAll clear.", "root": root}


def _where(alert: Optional[dict[str, Any]]) -> str:
    if not alert:
        return ""
    path = str(alert.get("file_path") or "")
    others = [str(o) for o in alert.get("others") or []][:3]
    out = f" on {path}" if path else ""
    if others:
        out += f" ({', '.join(others)})"
    return out


# --- process management (CLI side) ----------------------------------------------


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "1").strip().lower() not in _OFF


def headless_reason() -> Optional[str]:
    """Why there is no desktop to draw on, or None when there is one."""
    if os.environ.get("CI"):
        return "CI"
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return "an SSH session"
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "no display"
    return None


def _home() -> Path:
    from hivelane.config import hivelane_home

    return hivelane_home()


def pid_path() -> Path:
    return _home() / "bee.pid"


def running_pid() -> Optional[int]:
    try:
        pid = int(pid_path().read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except PermissionError:
        return pid
    except OSError:
        return None
    return pid


def command(port: int) -> Optional[list[str]]:
    """The argv that runs the bee on this platform, or None when it has no
    way to draw one."""
    from hivelane.auth import token_path

    hivelane = shutil.which("hivelane") or ""
    args = [str(port), str(token_path()), hivelane, str(_home() / "bee-position.txt")]
    if sys.platform == "darwin":
        if shutil.which("osascript") is None:
            return None
        script = _home() / "bee.js"
        _write_if_changed(script, MAC_SCRIPT)
        return ["osascript", "-l", "JavaScript", str(script), *args]
    if sys.platform == "win32":
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell is None:
            return None
        script = _home() / "bee.ps1"
        _write_if_changed(script, WINDOWS_SCRIPT)
        return [shell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "hivelane.bee", *args]


def _write_if_changed(path: Path, text: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def start(port: int) -> str:
    """Start the bee unless it runs already. Returns one short line for the
    CLI. Detached and silent: it must never hold an agent's tool call open."""
    if not enabled():
        return f"off ({ENV_SWITCH}=0)"
    reason = headless_reason()
    if reason:
        return f"off ({reason})"
    if running_pid() is not None:
        return "on"
    argv = command(port)
    if argv is None:
        return "off (this system has no way to draw it; tkinter missing?)"
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)  # noqa: S603 - fixed argv
    except OSError as exc:
        return f"off (could not start: {exc.__class__.__name__})"
    pid_path().parent.mkdir(parents=True, exist_ok=True)
    pid_path().write_text(str(proc.pid))
    return "on"


def stop() -> bool:
    pid = running_pid()
    try:
        pid_path().unlink()
    except OSError:
        pass
    if pid is None:
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)  # noqa: S603,S607
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


# --- Linux renderer (tkinter) ----------------------------------------------------

COLORS = {
    "live": "#2FA36B",
    "warn": "#F5A524",
    "block": "#E5484D",
    "error": "#E5484D",
    "connecting": "#8A8D93",
    "off": "#5B5E66",
}


def _ellipse(cx: float, cy: float, rx: float, ry: float, deg: float, n: int = 24) -> list[float]:
    import math

    a = math.radians(deg)
    pts: list[float] = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts += [cx + x * math.cos(a) - y * math.sin(a), cy + x * math.sin(a) + y * math.cos(a)]
    return pts


def run_tk(port: int, token_file: str, hivelane: str, position_file: str) -> None:  # pragma: no cover - needs a display
    import json
    import time
    import tkinter as tk
    import urllib.request

    size = 44
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#0B0B0C")
    x, y = root.winfo_screenwidth() - size - 16, 12
    try:
        px, py = (int(v) for v in Path(position_file).read_text().split())
        x, y = px, py
    except (OSError, ValueError):
        pass
    root.geometry(f"{size}x{size}+{x}+{y}")
    canvas = tk.Canvas(root, width=size, height=size, bg="#0B0B0C", highlightthickness=0)
    canvas.pack()
    state: dict[str, Any] = {"view": {"state": "off", "root": None}, "fails_since": None, "drag": None, "moved": False}

    def draw() -> None:
        canvas.delete("all")
        view = state["view"]
        ring = COLORS.get(view["state"], COLORS["off"])
        canvas.create_oval(3, 3, size - 3, size - 3, outline=ring, width=2)
        s, ox, oy = 26 / 152, (size - 26) / 2 - 4 * 26 / 152, (size - 26 * 128 / 152) / 2 - 18 * 26 / 152 + 1

        def p(xx: float, yy: float) -> tuple[float, float]:
            return ox + xx * s, oy + yy * s

        for cx, deg in ((34, -32), (126, 32)):
            pts = _ellipse(*p(cx, 68), 27 * s, 12 * s, deg)
            canvas.create_polygon(pts, fill="#EEF1F5", outline="#18181B", width=1)
        hexagon = [p(80, 52), p(118.1, 74), p(118.1, 118), p(80, 140), p(41.9, 118), p(41.9, 74)]
        canvas.create_polygon([c for xy in hexagon for c in xy], fill="#FFFFFF", outline="#18181B", width=1.5)
        canvas.create_rectangle(*p(48, 103), *p(112, 114), fill="#0062D1", width=0)
        canvas.create_rectangle(*p(52, 119), *p(108, 127), fill="#18181B", width=0)
        for ex in (66, 94):
            canvas.create_oval(*p(ex - 6, 77), *p(ex + 6, 89), fill="#18181B", width=0)

    def poll() -> None:
        view = None
        try:
            token = Path(token_file).read_text().strip()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/bee", headers={"X-Hivelane-Token": token})
            with urllib.request.urlopen(req, timeout=1) as resp:  # noqa: S310 - loopback only
                view = json.loads(resp.read().decode())
            state["fails_since"] = None
        except (OSError, ValueError):
            state["fails_since"] = state["fails_since"] or time.time()
            if time.time() - state["fails_since"] > BEE_GIVE_UP_SECONDS:
                root.destroy()
                return
            view = {"state": "off", "label": "", "tip": "Hivelane is not running", "root": None}
        if view != state["view"]:
            state["view"] = view
            draw()
        root.after(int(BEE_POLL_SECONDS * 1000), poll)

    def press(event: Any) -> None:
        state["drag"] = (event.x_root - root.winfo_x(), event.y_root - root.winfo_y())
        state["moved"] = False

    def motion(event: Any) -> None:
        dx, dy = state["drag"]
        root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")
        state["moved"] = True

    def release(_event: Any) -> None:
        if state["moved"]:
            try:
                Path(position_file).write_text(f"{root.winfo_x()} {root.winfo_y()}")
            except OSError:
                pass
        elif hivelane and state["view"].get("root"):
            subprocess.Popen([hivelane, "open", "--root", state["view"]["root"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", motion)
    canvas.bind("<ButtonRelease-1>", release)
    draw()
    poll()
    root.mainloop()


# --- macOS renderer (AppKit through JavaScript for Automation) ---------------------

MAC_SCRIPT = r"""
// Hivelane desktop bee (macOS). Written by hivelane/bee.py; run by
// `osascript -l JavaScript bee.js PORT TOKEN_FILE HIVELANE POSITION_FILE`.
ObjC.import('Cocoa');

const POLL = 2.0, GIVE_UP = 300;
const C = (hex, a) => {
  const n = parseInt(hex.slice(1), 16);
  return $.NSColor.colorWithSRGBRedGreenBlueAlpha(((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, a === undefined ? 1 : a);
};
const RING = { live: '#2FA36B', warn: '#F5A524', block: '#E5484D', error: '#E5484D', connecting: '#8A8D93', off: '#5B5E66' };
const LABEL = { warn: '#F5C26B', block: '#FF8A8D', error: '#FF8A8D' };
const SIZE = 40;
let view = { state: 'off', label: '', tip: 'Hivelane is starting', root: null };
let phase = 0, failsSince = null, panel = null, beeView = null, pulseTimer = null;
let args = [];

function readText(path) {
  const s = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null);
  return s.isNil() ? null : s.js;
}

function fetchView() {
  const token = (readText(args[1]) || '').trim();
  const req = $.NSMutableURLRequest.requestWithURLCachePolicyTimeoutInterval(
    $.NSURL.URLWithString('http://127.0.0.1:' + args[0] + '/bee'), 1, 1.0);
  req.setValueForHTTPHeaderField(token, 'X-Hivelane-Token');
  const data = $.NSURLConnection.sendSynchronousRequestReturningResponseError(req, null, null);
  if (data.isNil()) return null;
  try { return JSON.parse($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js); } catch (e) { return null; }
}

function poly(points) {
  const p = $.NSBezierPath.bezierPath;
  points.forEach(([x, y], i) => i ? p.lineToPoint($.NSMakePoint(x, y)) : p.moveToPoint($.NSMakePoint(x, y)));
  p.closePath;
  return p;
}

function line(d, w) {
  const p = $.NSBezierPath.bezierPath;
  p.setLineWidth(w); p.setLineCapStyle(1);
  d(p);
  C('#18181B').setStroke; p.stroke;
}

// The guard bee from brand/mascot.ts, in its 160x160 space (flipped view).
function drawBee(state) {
  const HEX = [[80, 52], [118.1, 74], [118.1, 118], [80, 140], [41.9, 118], [41.9, 74]];
  [[34, -32], [126, 32]].forEach(([cx, deg]) => {
    $.NSGraphicsContext.saveGraphicsState;
    const t = $.NSAffineTransform.transform;
    t.translateXByYBy(cx, 68); t.rotateByDegrees(deg); t.concat;
    const w = $.NSBezierPath.bezierPathWithOvalInRect($.NSMakeRect(-27, -12, 54, 24));
    C('#EEF1F5').setFill; w.fill; w.setLineWidth(6); C('#18181B').setStroke; w.stroke;
    $.NSGraphicsContext.restoreGraphicsState;
  });
  line((p) => { p.moveToPoint($.NSMakePoint(72, 57)); p.curveToPointControlPoint1ControlPoint2($.NSMakePoint(56, 30), $.NSMakePoint(69, 44), $.NSMakePoint(62, 37));
               p.moveToPoint($.NSMakePoint(88, 57)); p.curveToPointControlPoint1ControlPoint2($.NSMakePoint(104, 30), $.NSMakePoint(91, 44), $.NSMakePoint(98, 37)); }, 6);
  C('#18181B').setFill;
  $.NSBezierPath.bezierPathWithOvalInRect($.NSMakeRect(49, 22, 12, 12)).fill;
  $.NSBezierPath.bezierPathWithOvalInRect($.NSMakeRect(99, 22, 12, 12)).fill;
  const body = poly(HEX);
  C('#FFFFFF').setFill; body.fill;
  $.NSGraphicsContext.saveGraphicsState;
  body.addClip;
  C('#0062D1').setFill; $.NSBezierPath.fillRect($.NSMakeRect(30, 103, 100, 11));
  C('#18181B').setFill; $.NSBezierPath.fillRect($.NSMakeRect(30, 119, 100, 8));
  $.NSGraphicsContext.restoreGraphicsState;
  body.setLineWidth(7); body.setLineJoinStyle(1); C('#18181B').setStroke; body.stroke;
  // The face carries the state, like the dashboard's avatar.
  if (state === 'live') {
    line((p) => { p.moveToPoint($.NSMakePoint(60, 84)); p.curveToPointControlPoint1ControlPoint2($.NSMakePoint(72, 84), $.NSMakePoint(63, 76), $.NSMakePoint(69, 76));
                 p.moveToPoint($.NSMakePoint(88, 84)); p.curveToPointControlPoint1ControlPoint2($.NSMakePoint(100, 84), $.NSMakePoint(91, 76), $.NSMakePoint(97, 76)); }, 6);
    line((p) => { p.moveToPoint($.NSMakePoint(73, 93)); p.curveToPointControlPoint1ControlPoint2($.NSMakePoint(87, 93), $.NSMakePoint(76, 99), $.NSMakePoint(84, 99)); }, 5);
  } else if (state === 'off' || state === 'connecting') {
    line((p) => { p.moveToPoint($.NSMakePoint(59, 84)); p.lineToPoint($.NSMakePoint(73, 84)); p.moveToPoint($.NSMakePoint(87, 84)); p.lineToPoint($.NSMakePoint(101, 84)); }, 6);
  } else {
    if (state !== 'warn') line((p) => { p.moveToPoint($.NSMakePoint(58, 71)); p.lineToPoint($.NSMakePoint(72, 75)); p.moveToPoint($.NSMakePoint(102, 71)); p.lineToPoint($.NSMakePoint(88, 75)); }, 5);
    C('#18181B').setFill;
    $.NSBezierPath.bezierPathWithOvalInRect($.NSMakeRect(59, 76, 14, 14)).fill;
    $.NSBezierPath.bezierPathWithOvalInRect($.NSMakeRect(87, 76, 14, 14)).fill;
    line((p) => { p.moveToPoint($.NSMakePoint(73, 96)); p.lineToPoint($.NSMakePoint(87, 96)); }, 5);
  }
}

function labelWidth() {
  if (!view.label) return 0;
  const attrs = $.NSDictionary.dictionaryWithObjectForKey($.NSFont.systemFontOfSizeWeight(12, 0.23), $.NSFontAttributeName);
  return Math.ceil($(view.label).sizeWithAttributes(attrs).width) + 14;
}

// Drawn into an image (drawRect: cannot be overridden from JXA), shown in
// the view's layer; the view itself only handles the mouse.
function render() {
  const w = SIZE + labelWidth();
  const img = $.NSImage.alloc.initWithSize($.NSMakeSize(w, SIZE));
  img.lockFocusFlipped(true);
  const bg = $.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius($.NSMakeRect(1, 1, w - 2, SIZE - 2), SIZE / 2 - 1, SIZE / 2 - 1);
  C('#0B0B0C').setFill; bg.fill;
  const pulse = (view.state === 'warn' || view.state === 'block') ? 0.45 + 0.55 * Math.abs(Math.sin(phase)) : 1;
  const ring = $.NSBezierPath.bezierPathWithOvalInRect($.NSMakeRect(3, 3, SIZE - 6, SIZE - 6));
  ring.setLineWidth(2); C(RING[view.state] || RING.off, pulse).setStroke; ring.stroke;
  $.NSGraphicsContext.saveGraphicsState;
  const t = $.NSAffineTransform.transform;
  const s = 24 / 152;
  t.translateXByYBy((SIZE - 24) / 2 - 4 * s, (SIZE - 24 * 128 / 152) / 2 - 18 * s);
  t.scaleBy(s); t.concat;
  drawBee(view.state);
  $.NSGraphicsContext.restoreGraphicsState;
  if (view.label) {
    const keys = $([$.NSFontAttributeName, $.NSForegroundColorAttributeName]);
    const vals = $([$.NSFont.systemFontOfSizeWeight(12, 0.23), C(LABEL[view.state] || '#F5F5F7')]);
    $(view.label).drawAtPointWithAttributes($.NSMakePoint(SIZE - 2, 12), $.NSDictionary.dictionaryWithObjectsForKeys(vals, keys));
  }
  img.unlockFocus;
  beeView.layer.setContents(img);
}

ObjC.registerSubclass({
  name: 'HivelaneBeeView',
  superclass: 'NSView',
  methods: {
    'acceptsFirstMouse:': { types: ['bool', ['id']], implementation: function () { return true; } },
    'mouseDown:': {
      types: ['void', ['id']],
      implementation: function (event) {
        const before = panel.frame.origin;
        panel.performWindowDragWithEvent(event);
        const after = panel.frame.origin;
        if (Math.abs(after.x - before.x) + Math.abs(after.y - before.y) > 2) {
          const top = $.NSScreen.mainScreen.frame.size.height - (after.y + SIZE);
          $(String(Math.round(after.x + panel.frame.size.width)) + ' ' + String(Math.round(top)))
            .writeToFileAtomicallyEncodingError(args[3], true, $.NSUTF8StringEncoding, null);
        } else if (args[2] && view.root) {
          $.NSTask.launchedTaskWithLaunchPathArguments(args[2], $(['open', '--root', view.root]));
        }
      },
    },
  },
});

ObjC.registerSubclass({
  name: 'HivelaneBeeTicker',
  superclass: 'NSObject',
  methods: {
    'poll:': { types: ['void', ['id']], implementation: function () { tick(); } },
    'pulse:': { types: ['void', ['id']], implementation: function () { phase += 0.35; render(); } },
  },
});

let ticker = null;
function layout() {
  const w = SIZE + labelWidth();
  const screen = $.NSScreen.mainScreen.visibleFrame;
  let right = screen.origin.x + screen.size.width - 14, top = 10;
  const saved = (readText(args[3]) || '').trim().split(/\s+/).map(Number);
  if (saved.length === 2 && saved.every(Number.isFinite)) { right = saved[0]; top = saved[1]; }
  const full = $.NSScreen.mainScreen.frame;
  const y = full.size.height - top - SIZE;
  panel.setFrameDisplay($.NSMakeRect(right - w, y, w, SIZE), true);
  beeView.setFrame($.NSMakeRect(0, 0, w, SIZE));
}

function tick() {
  const next = fetchView();
  if (next === null) {
    failsSince = failsSince || Date.now();
    if (Date.now() - failsSince > GIVE_UP * 1000) $.NSApplication.sharedApplication.terminate(null);
  } else {
    failsSince = null;
  }
  const fresh = next || { state: 'off', label: '', tip: 'Hivelane is not running', root: null };
  if (JSON.stringify(fresh) === JSON.stringify(view)) return;
  const resized = fresh.label !== view.label;
  view = fresh;
  beeView.setToolTip(view.tip || '');
  if (resized) layout();
  const alarm = view.state === 'warn' || view.state === 'block';
  if (alarm && pulseTimer === null) {
    pulseTimer = $.NSTimer.scheduledTimerWithTimeIntervalTargetSelectorUserInfoRepeats(0.08, ticker, 'pulse:', null, true);
  } else if (!alarm && pulseTimer !== null) {
    pulseTimer.invalidate; pulseTimer = null; phase = 0;
  }
  render();
}

function run(argv) {
  args = argv;
  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(1); // accessory: no Dock icon, no menu bar
  panel = $.NSPanel.alloc.initWithContentRectStyleMaskBackingDefer($.NSMakeRect(0, 0, SIZE, SIZE), 0 | 128, 2, false);
  panel.setLevel(25);
  panel.setOpaque(false);
  panel.setBackgroundColor($.NSColor.clearColor);
  panel.setHasShadow(true);
  panel.setHidesOnDeactivate(false);
  panel.setCollectionBehavior(1 | 16 | 256);
  beeView = $.HivelaneBeeView.alloc.initWithFrame($.NSMakeRect(0, 0, SIZE, SIZE));
  beeView.setWantsLayer(true);
  beeView.layer.setContentsGravity($.kCAGravityResize);
  panel.setContentView(beeView);
  ticker = $.HivelaneBeeTicker.alloc.init;
  layout();
  panel.orderFrontRegardless;
  tick();
  $.NSTimer.scheduledTimerWithTimeIntervalTargetSelectorUserInfoRepeats(POLL, ticker, 'poll:', null, true);
  app.run;
}
"""


# --- Windows renderer (WinForms through PowerShell) --------------------------------

WINDOWS_SCRIPT = r"""
# Hivelane desktop bee (Windows). Written by hivelane/bee.py; run by
# powershell -File bee.ps1 PORT TOKEN_FILE HIVELANE POSITION_FILE
param([string]$Port, [string]$TokenFile, [string]$Hivelane, [string]$PositionFile)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$ring = @{ live = '#2FA36B'; warn = '#F5A524'; block = '#E5484D'; error = '#E5484D'; connecting = '#8A8D93'; off = '#5B5E66' }
$script:view = @{ state = 'off'; label = ''; tip = 'Hivelane is starting'; root = $null }
$script:failsSince = $null
$size = 40

$form = New-Object Windows.Forms.Form
$form.FormBorderStyle = 'None'; $form.TopMost = $true; $form.ShowInTaskbar = $false
$form.StartPosition = 'Manual'; $form.BackColor = [Drawing.ColorTranslator]::FromHtml('#0B0B0C')
$tip = New-Object Windows.Forms.ToolTip

function Place {
  $w = $size; if ($script:view.label) { $w += 14 + [Windows.Forms.TextRenderer]::MeasureText($script:view.label, $form.Font).Width }
  $area = [Windows.Forms.Screen]::PrimaryScreen.WorkingArea
  $right = $area.Right - 14; $top = $area.Top + 10
  if (Test-Path $PositionFile) { $p = (Get-Content $PositionFile -Raw).Trim() -split '\s+'; if ($p.Count -eq 2) { $right = [int]$p[0]; $top = [int]$p[1] } }
  $form.Bounds = New-Object Drawing.Rectangle(($right - $w), $top, $w, $size)
  $path = New-Object Drawing.Drawing2D.GraphicsPath
  $path.AddArc(0, 0, $size, $size, 90, 180); $path.AddArc($w - $size, 0, $size, $size, 270, 180); $path.CloseFigure()
  $form.Region = New-Object Drawing.Region($path)
}

$form.Add_Paint({
  param($s, $e)
  $g = $e.Graphics; $g.SmoothingMode = 'AntiAlias'
  $pen = New-Object Drawing.Pen([Drawing.ColorTranslator]::FromHtml($ring[$script:view.state]), 2)
  $g.DrawEllipse($pen, 3, 3, $size - 6, $size - 6)
  $k = 24 / 152; $g.TranslateTransform(($size - 24) / 2 - 4 * $k, ($size - 24 * 128 / 152) / 2 - 18 * $k); $g.ScaleTransform($k, $k)
  $ink = [Drawing.ColorTranslator]::FromHtml('#18181B'); $inkPen = New-Object Drawing.Pen($ink, 6)
  foreach ($w in @(@(34, -32), @(126, 32))) {
    $st = $g.Save(); $g.TranslateTransform($w[0], 68); $g.RotateTransform($w[1])
    $g.FillEllipse((New-Object Drawing.SolidBrush([Drawing.ColorTranslator]::FromHtml('#EEF1F5'))), -27, -12, 54, 24)
    $g.DrawEllipse($inkPen, -27, -12, 54, 24); $g.Restore($st)
  }
  $g.FillEllipse((New-Object Drawing.SolidBrush($ink)), 49, 22, 12, 12); $g.FillEllipse((New-Object Drawing.SolidBrush($ink)), 99, 22, 12, 12)
  $g.DrawLine($inkPen, 72, 57, 56, 30); $g.DrawLine($inkPen, 88, 57, 104, 30)
  $hex = [Drawing.PointF[]]@((New-Object Drawing.PointF(80, 52)), (New-Object Drawing.PointF(118.1, 74)), (New-Object Drawing.PointF(118.1, 118)), (New-Object Drawing.PointF(80, 140)), (New-Object Drawing.PointF(41.9, 118)), (New-Object Drawing.PointF(41.9, 74)))
  $g.FillPolygon([Drawing.Brushes]::White, $hex)
  $g.FillRectangle((New-Object Drawing.SolidBrush([Drawing.ColorTranslator]::FromHtml('#0062D1'))), 46, 103, 68, 11)
  $g.FillRectangle((New-Object Drawing.SolidBrush($ink)), 50, 119, 60, 8)
  $inkPen.Width = 7; $g.DrawPolygon($inkPen, $hex); $inkPen.Width = 5
  $b = New-Object Drawing.SolidBrush($ink)
  switch ($script:view.state) {
    'live' { $g.DrawArc($inkPen, 60, 78, 12, 10, 180, 180); $g.DrawArc($inkPen, 88, 78, 12, 10, 180, 180); $g.DrawArc($inkPen, 73, 88, 14, 10, 0, 180) }
    { $_ -in 'off', 'connecting' } { $g.DrawLine($inkPen, 59, 84, 73, 84); $g.DrawLine($inkPen, 87, 84, 101, 84) }
    default {
      if ($script:view.state -ne 'warn') { $g.DrawLine($inkPen, 58, 71, 72, 75); $g.DrawLine($inkPen, 102, 71, 88, 75) }
      $g.FillEllipse($b, 59, 76, 14, 14); $g.FillEllipse($b, 87, 76, 14, 14); $g.DrawLine($inkPen, 73, 96, 87, 96)
    }
  }
  $g.ResetTransform()
  if ($script:view.label) {
    $color = if ($script:view.state -eq 'warn') { '#F5C26B' } else { '#FF8A8D' }
    [Windows.Forms.TextRenderer]::DrawText($g, $script:view.label, $form.Font, (New-Object Drawing.Point(($size - 2), 12)), [Drawing.ColorTranslator]::FromHtml($color))
  }
})

$script:down = $null; $script:moved = $false
$form.Add_MouseDown({ param($s, $e) $script:down = $e.Location; $script:moved = $false })
$form.Add_MouseMove({ param($s, $e)
  if ($e.Button -eq 'Left' -and $script:down) {
    $form.Left += $e.X - $script:down.X; $form.Top += $e.Y - $script:down.Y; $script:moved = $true } })
$form.Add_MouseUp({
  if ($script:moved) { Set-Content -Path $PositionFile -Value "$($form.Right) $($form.Top)" }
  elseif ($Hivelane -and $script:view.root) { Start-Process -WindowStyle Hidden -FilePath $Hivelane -ArgumentList @('open', '--root', $script:view.root) }
  $script:down = $null })

$timer = New-Object Windows.Forms.Timer; $timer.Interval = 2000
$timer.Add_Tick({
  try {
    $token = (Get-Content $TokenFile -Raw).Trim()
    $next = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/bee" -Headers @{ 'X-Hivelane-Token' = $token } -TimeoutSec 1
    $script:failsSince = $null
  } catch {
    if (-not $script:failsSince) { $script:failsSince = Get-Date }
    if (((Get-Date) - $script:failsSince).TotalSeconds -gt 300) { $form.Close(); return }
    $next = @{ state = 'off'; label = ''; tip = 'Hivelane is not running'; root = $null }
  }
  $changed = ($next.state -ne $script:view.state) -or ($next.label -ne $script:view.label) -or ($next.tip -ne $script:view.tip)
  if ($changed) {
    $relabel = $next.label -ne $script:view.label
    $script:view = @{ state = $next.state; label = $next.label; tip = $next.tip; root = $next.root }
    $tip.SetToolTip($form, $next.tip)
    if ($relabel) { Place }
    $form.Invalidate()
  }
})

Place
$timer.Start()
[Windows.Forms.Application]::Run($form)
"""


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - needs a display
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4:
        print("usage: python -m hivelane.bee PORT TOKEN_FILE HIVELANE POSITION_FILE", file=sys.stderr)
        return 2
    run_tk(int(args[0]), args[1], args[2], args[3])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
