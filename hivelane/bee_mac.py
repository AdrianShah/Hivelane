"""The desktop bee on macOS: draw the logo bee, then hand over to the menu bar.

The drawing is the Windows bee's (hivelane/bee_win.py: the logo bee from
frontend/public/favicon.svg, its faces, the amber or red corner dot and the
wing beat), so both look the same. This file renders those frames to PNG once,
into HIVELANE_HOME/bee-art/<hash of the drawing code>/, then replaces itself
with the AppKit menu bar item (MAC_SCRIPT in hivelane/bee.py, run by
`osascript -l JavaScript`), which loads the PNGs and flips through them. The
exec keeps the pid that `hivelane bee off` reads from bee.pid.

hivelane/bee.py runs it by path with `python -I -S` (standard library only):

    python -I -S bee_mac.py BEE_JS PORT TOKEN_FILE HIVELANE HOME
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
# -I leaves this file's folder off sys.path; bee_win.py is a plain stdlib
# module beside it.
sys.path.insert(0, str(HERE))
import bee_win  # noqa: E402

# Menu bar icons are 18 pt; drawn at 2x for Retina (macOS scales it on 1x).
POINTS = 18
SCALE = 2
# The poses a beat passes through: frame k and FRAMES - k look the same.
POSES = bee_win.FRAMES // 2 + 1
ANIMATED = tuple(bee_win.FRAME_MS)
STATES = ("live", "connecting", "warn", "block", "error", "off")
PALETTES = {"light": bee_win.LIGHT, "dark": bee_win.DARK}
RENDER_VERSION = "1"


def png(size: int, bgra: bytes) -> bytes:
    """A size x size RGBA PNG from bee_win.pixels()' top-down BGRA."""
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter: none
        line = bgra[y * size * 4 : (y + 1) * size * 4]
        for i in range(0, len(line), 4):
            b, g, r, a = line[i : i + 4]
            rows.extend((r, g, b, a))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    head = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", head)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def art_key() -> str:
    code = (HERE / "bee_win.py").read_bytes() + Path(__file__).read_bytes()
    return hashlib.sha256(code + f"{POINTS}x{SCALE}:{RENDER_VERSION}".encode()).hexdigest()[:16]


def frame_name(state: str, palette: str, pose: int) -> str:
    return f"{state}-{palette}-{pose}.png"


def render(home: Path) -> Path:
    """Every frame the menu bar needs, drawn once per version of the drawing
    code. Returns the folder; older versions are removed."""
    root = home / "bee-art"
    out = root / art_key()
    done = out / "done"
    if done.exists():
        return out
    tmp = root / (out.name + f".tmp{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    size = POINTS * SCALE
    for state in STATES:
        poses = range(POSES) if state in ANIMATED else range(1)
        for pose in poses:
            cover = bee_win.coverage(size, pose, state)
            for name, palette in PALETTES.items():
                (tmp / frame_name(state, name, pose)).write_bytes(png(size, bee_win.pixels(cover, state, palette)))
    (tmp / "done").write_text(f"{POSES}\n")
    for old in root.iterdir():
        if old.is_dir() and old.name != tmp.name:
            shutil.rmtree(old, ignore_errors=True)
    try:
        tmp.rename(out)
    except OSError:
        # Another bee finished the same render first; its copy is identical.
        shutil.rmtree(tmp, ignore_errors=True)
        if not done.exists():
            raise
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("usage: python -I -S bee_mac.py BEE_JS PORT TOKEN_FILE HIVELANE HOME", file=sys.stderr)
        return 2
    script, port, token_file, hivelane, home = argv
    try:
        art = str(render(Path(home)))
    except OSError:
        art = ""  # the menu bar item falls back to a plain dot
    os.execvp(
        "osascript",
        [
            "osascript",
            "-l",
            "JavaScript",
            script,
            port,
            token_file,
            hivelane,
            home,
            art,
            str(bee_win.FRAMES),
            str(POSES),
        ],
    )
    return 0  # pragma: no cover - exec does not return


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
