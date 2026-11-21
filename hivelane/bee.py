"""The desktop bee: the room's state where people already look, for those
who never open the dashboard.

- macOS: an item in the menu bar, beside the other background apps.
- Windows: the logo bee in the system tray, wings beating; an amber or red
  dot in its corner instead of a coloured body (with a quiet heads-up when it
  turns amber or red).
- Linux: a thin colour tab tucked against the right edge of the screen that
  slides out on hover.

The bee's body takes the state's colour: green when the room is clear, amber
when someone is nearby or you were pinged, red when an edit is held.

Design constraints:

- Zero tokens. Nothing here reaches an agent's context; the bee is a separate
  process that reads the local daemon.
- Lightweight. No new dependencies and no browser engine: AppKit through
  `osascript -l JavaScript` on macOS, the Win32 tray through ctypes on Windows
  (hivelane/bee_win.py, a few MB), tkinter on Linux. It polls the daemon's
  GET /bee every BEE_POLL_SECONDS and redraws only on change (on Windows the
  wings also flap, from frames drawn once and cached).
- Never in the way of an agent. `hivelane up` may be run by an agent in a tool
  call: the bee is started detached with every stdio stream on /dev/null, so
  the agent's command returns at once.

HIVELANE_BEE=0, its "Hide the bee" menu item or `hivelane bee off` keep it
off; it also stays off on a headless machine, over SSH and in CI.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
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
    click opens, or None), room (its code, or None) and url (its dashboard,
    or None)."""
    raw = status.get("repos")
    repos: list[Any] = raw if isinstance(raw, list) else []
    best: dict[str, Any] = {"state": "off", "label": "", "tip": "Hivelane: no room on this machine", "root": None, "room": None, "url": None}
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        view = _repo_view(repo)
        if _RANK[view["state"]] > _RANK[best["state"]]:
            best = view
    return best


def _repo_view(repo: dict[str, Any]) -> dict[str, Any]:
    view = _repo_state(repo)
    # What a click on the bee needs: the room's code and its dashboard link.
    room, relay = repo.get("room"), repo.get("relay")
    view["room"] = str(room) if room else None
    view["url"] = None
    if room and isinstance(relay, str):
        from hivelane.cli import room_url

        view["url"] = room_url(relay, str(room))
    return view


def _repo_state(repo: dict[str, Any]) -> dict[str, Any]:
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


def hidden_path() -> Path:
    """Present while the person hid the bee (its "Hide" menu item or
    `hivelane bee off`): `hivelane up` then leaves it off."""
    return _home() / "bee-hidden"


def running_pid() -> Optional[int]:
    try:
        pid = int(pid_path().read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    return pid if _alive(pid) else None


def _alive(pid: int) -> bool:
    """Whether process `pid` is still running."""
    if sys.platform == "win32":
        # os.kill(pid, 0) is no liveness probe on Windows: signal 0 is
        # CTRL_C_EVENT there, and it fails (WinError 87) for a live process.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists, just not ours
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def style() -> str:
    """Where the bee lives here: "menubar" (macOS), "tray" (Windows) or
    "edge" (Linux: a tab on the right edge of the screen)."""
    if sys.platform == "darwin":
        return "menubar"
    if sys.platform == "win32":
        return "tray"
    return "edge"


def command(port: int) -> Optional[list[str]]:
    """The argv that runs the bee on this platform, or None when it has no
    way to draw one."""
    from hivelane.auth import token_path

    hivelane = shutil.which("hivelane") or _beside_python("hivelane.exe") or ""
    args = [str(port), str(token_path()), hivelane, str(_home())]
    if sys.platform == "darwin":
        if shutil.which("osascript") is None:
            return None
        script = _home() / "bee.js"
        _write_if_changed(script, MAC_SCRIPT)
        return ["osascript", "-l", "JavaScript", str(script), *args]
    if sys.platform == "win32":
        return [_windowless_python(), "-I", "-S", str(Path(__file__).with_name("bee_win.py")), *args]
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "hivelane.bee", *args]


def _beside_python(name: str) -> Optional[str]:
    """`name` in this interpreter's Scripts folder (a venv install whose
    Scripts folder is not on PATH), or None."""
    path = Path(sys.executable).with_name(name)
    return str(path) if path.is_file() else None


def _windowless_python() -> str:
    """pythonw.exe of the base interpreter. Not the venv's: that one is a
    launcher that starts the real interpreter as a second process, which
    doubles the bee's memory and leaves the pid file pointing at the launcher.
    The bee only needs the standard library, so the base one will do."""
    base = Path(getattr(sys, "_base_executable", "") or sys.executable)
    for candidate in (base.with_name("pythonw.exe"), Path(sys.executable).with_name("pythonw.exe")):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _write_if_changed(path: Path, text: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        # No copy yet (or an unreadable one): write it below.
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


WHERE = {"menubar": "in the menu bar", "tray": "in the system tray", "edge": "on the right edge of the screen"}


def start(port: int, *, force: bool = False) -> str:
    """Start the bee unless it runs already. Returns one short line for the
    CLI. Detached and silent: it must never hold an agent's tool call open.
    force (`hivelane bee on`) clears an earlier "Hide"."""
    if not enabled():
        return f"off ({ENV_SWITCH}=0)"
    reason = headless_reason()
    if reason:
        return f"off ({reason})"
    if force:
        hidden_path().unlink(missing_ok=True)
    elif hidden_path().exists():
        return "off (you hid it; `hivelane bee on` brings it back)"
    where = WHERE[style()]
    if running_pid() is not None:
        return f"on, {where}"
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
    return f"on, {where}"


def stop(*, hide: bool = False) -> bool:
    """Stop the bee; hide also keeps `hivelane up` from starting it again."""
    if hide:
        hidden_path().parent.mkdir(parents=True, exist_ok=True)
        hidden_path().touch()
    pid = running_pid()
    # The pid file goes either way; a missing one just means nothing ran.
    pid_path().unlink(missing_ok=True)
    if pid is None:
        return False
    try:
        if sys.platform == "win32":
            # Ask first (WM_CLOSE), so the bee removes its own tray icon; a
            # forced kill leaves a dead icon in the tray until the mouse
            # passes over it.
            subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, check=False)  # noqa: S603,S607
            for _ in range(20):
                if not _alive(pid):
                    return True
                time.sleep(0.1)
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)  # noqa: S603,S607
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


# --- Linux: a tab on the screen edge (tkinter) -------------------------------------

COLORS = {
    "live": "#2FA36B",
    "warn": "#F5A524",
    "block": "#E5484D",
    "error": "#E5484D",
    "connecting": "#8A8D93",
    "off": "#5B5E66",
}
EDGE_PEEK = 8  # px of the tab left on screen while it is tucked away
EDGE_OPEN = 240
EDGE_HEIGHT = 64


def run_edge(port: int, token_file: str, hivelane: str, home: str) -> None:  # pragma: no cover - needs a display
    """A colour tab tucked against the right edge of the screen: only a
    EDGE_PEEK-px strip in the state's colour shows; hovering slides it out to
    the room's status; clicking opens the dashboard; dragging moves it up or
    down (remembered)."""
    import json
    import time
    import tkinter as tk
    import urllib.request

    spot = Path(home) / "bee-edge.txt"
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    try:
        y = int(spot.read_text().strip())
    except (OSError, ValueError):
        y = screen_h // 3
    canvas = tk.Canvas(root, width=EDGE_OPEN, height=EDGE_HEIGHT, bg="#0B0B0C", highlightthickness=0)
    canvas.pack()
    state: dict[str, Any] = {"view": {"state": "off", "label": "", "tip": "", "root": None}, "open": False, "fails_since": None, "drag": None, "moved": False, "y": y}

    def place() -> None:
        width = EDGE_OPEN if state["open"] else EDGE_PEEK
        root.geometry(f"{width}x{EDGE_HEIGHT}+{screen_w - width}+{state['y']}")

    def draw() -> None:
        canvas.delete("all")
        view = state["view"]
        tone = COLORS.get(view["state"], COLORS["off"])
        canvas.create_rectangle(0, 0, EDGE_PEEK, EDGE_HEIGHT, fill=tone, width=0)
        if not state["open"]:
            return
        lines = str(view.get("tip") or "").split("\n")
        canvas.create_text(EDGE_PEEK + 14, 18, anchor="w", fill="#F5F5F7", font=("TkDefaultFont", 10, "bold"), text=lines[0][:34])
        canvas.create_text(EDGE_PEEK + 14, 40, anchor="w", fill=tone, font=("TkDefaultFont", 10), text=(view.get("label") or (lines[1] if len(lines) > 1 else ""))[:34])

    def poll() -> None:
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

    def enter(_event: Any) -> None:
        state["open"] = True
        place()
        draw()

    def leave(_event: Any) -> None:
        if state["drag"] is None:
            state["open"] = False
            place()
            draw()

    def press(event: Any) -> None:
        state["drag"] = event.y_root - state["y"]
        state["moved"] = False

    def motion(event: Any) -> None:
        state["y"] = max(0, min(screen_h - EDGE_HEIGHT, event.y_root - state["drag"]))
        state["moved"] = True
        place()

    def release(_event: Any) -> None:
        state["drag"] = None
        if state["moved"]:
            try:
                spot.write_text(str(state["y"]))
            except OSError:
                # Not remembered across restarts; the tab still moved.
                pass
        elif hivelane and state["view"].get("root"):
            subprocess.Popen([hivelane, "open", "--root", state["view"]["root"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603

    canvas.bind("<Enter>", enter)
    canvas.bind("<Leave>", leave)
    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", motion)
    canvas.bind("<ButtonRelease-1>", release)
    place()
    draw()
    poll()
    root.mainloop()


# --- macOS: a menu bar item (AppKit through JavaScript for Automation) -------------

MAC_SCRIPT = r"""
// Hivelane bee (macOS): a menu bar item beside the other background apps.
// Written by hivelane/bee.py; run by
// `osascript -l JavaScript bee.js PORT TOKEN_FILE HIVELANE HOME`.
ObjC.import('Cocoa');

const POLL = 2.0, GIVE_UP = 300;
const TONE = { live: '#2FA36B', warn: '#F5A524', block: '#E5484D', error: '#E5484D', connecting: '#8A8D93', off: '#8A8D93' };
let view = { state: 'off', label: '', tip: 'Hivelane is starting', root: null };
let shown = '', failsSince = null, item = null, ticker = null, args = [];

const C = (hex, a) => {
  const n = parseInt(hex.slice(1), 16);
  return $.NSColor.colorWithSRGBRedGreenBlueAlpha(((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, a === undefined ? 1 : a);
};

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

function dark() {
  try { return item.button.effectiveAppearance.name.js.indexOf('Dark') >= 0; } catch (e) { return false; }
}

// The guard bee from brand/mascot.ts, menu-bar sized: the body is filled
// with the state's colour, the outline follows the menu bar (light or dark).
function icon(state) {
  const W = 22, H = 18, s = 16.5 / 128;
  const ink = dark() ? '#F5F5F7' : '#18181B';
  const img = $.NSImage.alloc.initWithSize($.NSMakeSize(W, H));
  img.lockFocusFlipped(true);
  const t = $.NSAffineTransform.transform;
  t.translateXByYBy((W - 152 * s) / 2 - 4 * s, (H - 128 * s) / 2 - 18 * s);
  t.scaleBy(s); t.concat;
  [[34, -32], [126, 32]].forEach(([cx, deg]) => {
    $.NSGraphicsContext.saveGraphicsState;
    const r = $.NSAffineTransform.transform;
    r.translateXByYBy(cx, 68); r.rotateByDegrees(deg); r.concat;
    const w = $.NSBezierPath.bezierPathWithOvalInRect($.NSMakeRect(-27, -12, 54, 24));
    C(ink, 0.18).setFill; w.fill; w.setLineWidth(10); C(ink).setStroke; w.stroke;
    $.NSGraphicsContext.restoreGraphicsState;
  });
  const ant = $.NSBezierPath.bezierPath;
  ant.setLineWidth(10); ant.setLineCapStyle(1);
  ant.moveToPoint($.NSMakePoint(72, 57)); ant.lineToPoint($.NSMakePoint(58, 30));
  ant.moveToPoint($.NSMakePoint(88, 57)); ant.lineToPoint($.NSMakePoint(102, 30));
  C(ink).setStroke; ant.stroke;
  const hex = [[80, 50], [119, 73], [119, 119], [80, 142], [41, 119], [41, 73]];
  const body = $.NSBezierPath.bezierPath;
  hex.forEach(([x, y], i) => i ? body.lineToPoint($.NSMakePoint(x, y)) : body.moveToPoint($.NSMakePoint(x, y)));
  body.closePath;
  C(TONE[state] || TONE.off).setFill; body.fill;
  body.setLineWidth(11); body.setLineJoinStyle(1); C(ink).setStroke; body.stroke;
  C('#18181B').setFill;
  $.NSBezierPath.bezierPathWithOvalInRect($.NSMakeRect(58, 78, 15, 15)).fill;
  $.NSBezierPath.bezierPathWithOvalInRect($.NSMakeRect(87, 78, 15, 15)).fill;
  img.unlockFocus;
  img.setTemplate(false);
  return img;
}

function menuItem(title, action) {
  const m = $.NSMenuItem.alloc.initWithTitleActionKeyEquivalent(title, action || null, '');
  if (action) m.setTarget(ticker);
  return m;
}

function render() {
  const key = JSON.stringify([view, dark()]);
  if (key === shown) return;
  shown = key;
  const button = item.button;
  button.setImage(icon(view.state));
  button.setImagePosition(view.label ? 2 : 1); // image left of the title, or image only
  if (view.label) {
    const font = $.NSFont.monospacedDigitSystemFontOfSizeWeight(12, 0.23);
    const attrs = $.NSDictionary.dictionaryWithObjectsForKeys($([font, C(TONE[view.state] || TONE.off)]), $([$.NSFontAttributeName, $.NSForegroundColorAttributeName]));
    button.setAttributedTitle($.NSAttributedString.alloc.initWithStringAttributes(' ' + view.label, attrs));
  } else {
    button.setTitle('');
  }
  button.setToolTip(view.tip || '');
  const menu = $.NSMenu.alloc.init;
  (view.tip || 'Hivelane').split('\n').forEach((line) => menu.addItem(menuItem(line, null)));
  menu.addItem($.NSMenuItem.separatorItem);
  const open = menuItem('Open the dashboard', 'openDashboard:');
  if (!view.root) open.setTarget(null);
  menu.addItem(open);
  menu.addItem(menuItem('Hide the bee', 'hideBee:'));
  item.setMenu(menu);
}

ObjC.registerSubclass({
  name: 'HivelaneBeeTicker',
  superclass: 'NSObject',
  methods: {
    'poll:': { types: ['void', ['id']], implementation: function () { tick(); } },
    'openDashboard:': {
      types: ['void', ['id']],
      implementation: function () {
        if (args[2] && view.root) $.NSTask.launchedTaskWithLaunchPathArguments(args[2], $(['open', '--root', view.root]));
      },
    },
    'hideBee:': {
      types: ['void', ['id']],
      implementation: function () {
        $('').writeToFileAtomicallyEncodingError(args[3] + '/bee-hidden', true, $.NSUTF8StringEncoding, null);
        $.NSApplication.sharedApplication.terminate(null);
      },
    },
  },
});

function tick() {
  const next = fetchView();
  if (next === null) {
    failsSince = failsSince || Date.now();
    if (Date.now() - failsSince > GIVE_UP * 1000) $.NSApplication.sharedApplication.terminate(null);
  } else {
    failsSince = null;
  }
  view = next || { state: 'off', label: '', tip: 'Hivelane is not running', root: null };
  render();
}

function run(argv) {
  args = argv;
  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(1); // accessory: no Dock icon
  ticker = $.HivelaneBeeTicker.alloc.init;
  item = $.NSStatusBar.systemStatusBar.statusItemWithLength(-1); // variable length
  tick();
  $.NSTimer.scheduledTimerWithTimeIntervalTargetSelectorUserInfoRepeats(POLL, ticker, 'poll:', null, true);
  app.run;
}
"""


# --- Windows: a system tray icon (hivelane/bee_win.py, ctypes) -------------------
#
# Kept in its own file: it runs as `pythonw -I -S bee_win.py ...`, standard
# library only, so it stays a few MB.


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - needs a display
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4:
        print("usage: python -m hivelane.bee PORT TOKEN_FILE HIVELANE HOME", file=sys.stderr)
        return 2
    run_edge(int(args[0]), args[1], args[2], args[3])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
