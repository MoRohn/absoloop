"""Cross-platform helpers for Absoloop (Linux / macOS / Windows).

Keeps OS branching out of adapters and CLI so Codex Micro, provider spawns,
cancel, and report viewing behave the same everywhere.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
from typing import List, Optional, Sequence


def is_windows() -> bool:
    return os.name == "nt"


_stdio_ready = False


def ensure_stdio() -> None:
    """Keep styled output from crashing a legacy Windows console.

    GitHub's Windows runners, and many local consoles, still use a code
    page that cannot encode the infinity mark and arrows the CLI prints.
    ``print`` then raises ``UnicodeEncodeError`` and the process dies.
    Replace unencodable characters instead. UTF-8 consoles are unchanged.
    """
    global _stdio_ready
    if _stdio_ready:
        return
    _stdio_ready = True
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            continue


def tooling_home(env: Optional[dict] = None,
                 *,
                 anchor: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Resolve Absoloop's install root.

    Precedence:
      1. ABSOLOOP_HOME (env)
      2. Directory containing this package's parent (the checkout / install)
      3. ~/absoloop (legacy default)
    """
    environ = env if env is not None else os.environ
    raw = environ.get("ABSOLOOP_HOME")
    if raw:
        return pathlib.Path(raw).expanduser().resolve()
    if anchor is None:
        # absoloop_harness/platform_util.py → repo / install root
        anchor = pathlib.Path(__file__).resolve().parent.parent
    if (anchor / "absoloop_harness").is_dir():
        return anchor
    return (pathlib.Path.home() / "absoloop").resolve()


def ensure_home_on_path(home: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Insert tooling home on sys.path and export ABSOLOOP_HOME for children."""
    root = home or tooling_home()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.environ.setdefault("ABSOLOOP_HOME", str(root))
    return root


def prepare_spawn_argv(argv: Sequence[str]) -> List[str]:
    """Return argv CreateProcess can actually start.

    Unix runs a shebang script as argv[0]. Windows CreateProcess does not:
    a ``.py`` file or a Python shebang script raises WinError 193
    (``%1 is not a valid Win32 application``). Those are handed to the
    interpreter that is running Absoloop. ``.exe`` / ``.cmd`` / ``.bat``
    provider CLIs are left alone.
    """
    items = list(argv)
    if not items or not is_windows():
        return items
    program = items[0]
    path = pathlib.Path(program)
    suffix = path.suffix.lower()
    if suffix in {".exe", ".com", ".bat", ".cmd"}:
        return items
    if path.name.lower() in {"python.exe", "python3.exe", "py.exe"}:
        return items
    if not path.is_file():
        return items
    is_python = suffix in {".py", ".pyw"}
    if not is_python:
        try:
            with path.open("rb") as handle:
                head = handle.read(256)
        except OSError:
            return items
        if head.startswith(b"#!") and b"python" in head.split(b"\n", 1)[0].lower():
            is_python = True
    if not is_python:
        return items
    interpreter = sys.executable or "python"
    return [interpreter, str(path), *items[1:]]


def resolve_executable(name_or_path: str) -> Optional[str]:
    """Resolve a CLI for argv-array spawn (Windows .cmd/.exe aware)."""
    if not name_or_path:
        return None
    candidate = pathlib.Path(name_or_path).expanduser()
    if candidate.is_file():
        # Windows .cmd/.bat are not always os.X_OK; accept existing files.
        if is_windows() or os.access(candidate, os.X_OK):
            try:
                return str(candidate.resolve())
            except OSError:
                return str(candidate)
    found = shutil.which(name_or_path)
    if found:
        return found
    if is_windows() and not pathlib.Path(name_or_path).suffix:
        for suffix in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";"):
            suffix = suffix.strip()
            if not suffix:
                continue
            found = shutil.which(name_or_path + suffix)
            if found:
                return found
    return None


def kill_process_tree(pid: int) -> bool:
    """Best-effort terminate of a process and its descendants.

    Unix callers should prefer killpg when they own a session; this helper
    covers Windows trees and last-resort single-PID kills.
    """
    if pid <= 0:
        return False
    if is_windows():
        try:
            # taskkill is a console program. Attached to this console it can
            # deliver CTRL_C_EVENT to the parent (KeyboardInterrupt) when it
            # tears down a child tree. CREATE_NO_WINDOW keeps that event off
            # the CLI and the test runner.
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=20, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW)
            # 0 = killed, 128 = not found (already gone) — both OK for cancel.
            return result.returncode in (0, 128)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.kill(pid, 15)  # SIGTERM equivalent via Python on Windows
                return True
            except OSError:
                return False
    try:
        os.kill(pid, 15)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return False


def open_path(path: pathlib.Path) -> bool:
    """Open a file with the platform default handler (browser for .html)."""
    target = pathlib.Path(path).resolve()
    try:
        if is_windows():
            os.startfile(str(target))  # type: ignore[attr-defined]
            return True
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
            return True
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.run([opener, str(target)], check=False)
            return True
    except OSError:
        pass
    try:
        import webbrowser
        return bool(webbrowser.open(target.as_uri()))
    except Exception:
        return False


def python_gate_command(suffix: str = "-m unittest discover -s tests") -> str:
    """Gate command using the interpreter currently running Absoloop."""
    exe = sys.executable or "python"
    # Quote for shell=True on Windows paths with spaces.
    if " " in exe:
        return f'"{exe}" {suffix}'
    return f"{exe} {suffix}"


def rewrite_python_gate(command: str) -> str:
    """Rewrite leading python/python3 to the Absoloop interpreter.

    Leaves `py -3 …` alone (Windows launcher) when that is what the user
    configured; defaults use `python` which we rewrite to sys.executable.
    """
    stripped = command.strip()
    if not stripped:
        return command
    parts = stripped.split(None, 1)
    head = parts[0].strip("\"'")
    if pathlib.Path(head).name.lower() in ("python", "python3"):
        rest = parts[1] if len(parts) > 1 else ""
        return python_gate_command(rest) if rest else sys.executable
    return command


def tty_raw_listen_supported() -> bool:
    """True when termios/tty raw chord listen can work."""
    if is_windows():
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
        import select  # noqa: F401
    except ImportError:
        return False
    return True


def prerequisite_checks() -> List[str]:
    """Human-readable environment notes for `absoloop doctor`."""
    notes: List[str] = []
    home = tooling_home()
    notes.append(f"ABSOLOOP_HOME={home}")
    if not (home / "absoloop_harness").is_dir():
        notes.append(
            "fix: ABSOLOOP_HOME does not contain absoloop_harness/ — "
            "set ABSOLOOP_HOME to your Absoloop checkout")
    py = sys.executable or "?"
    notes.append(f"python={py} ({sys.version.split()[0]})")
    git = shutil.which("git")
    if git:
        notes.append(f"git={git}")
    else:
        notes.append(
            "fix: git not on PATH — harness worktrees and mission git "
            "delivery require git")
    if is_windows():
        notes.append(
            "platform=windows — use `absoloop do <action>` or line protocol "
            "for Codex Micro; TTY chord listen is Unix-only")
    else:
        notes.append(f"platform={sys.platform}")
    return notes
