"""Cross-platform helpers for Absoloop (Linux / macOS / Windows).

Keeps OS branching out of adapters and CLI so Codex Micro, provider spawns,
cancel, and report viewing behave the same everywhere.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
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


# Win32 CreateProcess flags. CPython only defines these on Windows; the
# numeric values are the contract the spawn tests assert on every OS.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
# CTRL_BREAK_EVENT is 1. signal.CTRL_BREAK_EVENT exists only on Windows;
# Linux tests that force the Windows kill branch must still have a number.
_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", 1)
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_WINDOWS_KILL_WAIT_SECONDS = 2.0


def windows_child_creationflags() -> int:
    """CreateProcess flags for a provider CLI.

    ``CREATE_NEW_PROCESS_GROUP`` makes the child a group leader and turns
    off Ctrl+C for that group, matching ``start_new_session`` on Unix: the
    operator's Ctrl+C still aborts the CLI, and cancel delivers
    ``CTRL_BREAK_EVENT`` to this group only. ``DETACHED_PROCESS`` is not
    used here; a detached child has no console, so a break cannot reach
    its process group.
    """
    return _CREATE_NEW_PROCESS_GROUP


def windows_detached_creationflags() -> int:
    """CreateProcess flags for tools that must not share this console.

    ``git`` failing under ``Temp`` used to deliver a console event to the
    test process. A detached, windowless git cannot do that, and it does
    not need to receive Ctrl+Break.
    """
    return _DETACHED_PROCESS | _CREATE_NO_WINDOW


def windows_pid_alive(pid: int) -> bool:
    """True when ``pid`` is still running.

    ``os.kill(pid, 0)`` is not a liveness probe on Windows. Signal 0 is
    ``CTRL_C_EVENT``, and ``GenerateConsoleCtrlEvent`` of Ctrl+C is
    delivered to every process on this console. Python then raises
    ``KeyboardInterrupt`` the next time the main thread checks signals,
    which is often inside ``Thread.join``. Query the process instead.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        return False
    return _windows_pid_alive(pid)


def _windows_pid_alive(pid: int) -> bool:
    """OpenProcess + GetExitCodeProcess. Only called when ``os.name == 'nt'``."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _terminate_pid(pid: int) -> bool:
    """TerminateProcess on one pid. Not a console event, and not taskkill.exe."""
    if os.name != "nt":
        try:
            os.kill(pid, 15)
            return True
        except ProcessLookupError:
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return not _windows_pid_alive(pid)
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _wait_windows_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not _windows_pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _kill_windows_tree(pid: int) -> bool:
    """Break the child's process group, then TerminateProcess if it survives.

    The provider was started with ``CREATE_NEW_PROCESS_GROUP``, so
    ``CTRL_BREAK_EVENT`` reaches that group and not the parent. Ctrl+C is
    never used: it cannot be limited to one group. ``taskkill.exe`` is a
    console program; spawning it was itself enough to trip the parent's
    signal handler. Descendants that share the child's group die with the
    break. A child that ignores break is then terminated directly.
    """
    try:
        os.kill(pid, _CTRL_BREAK_EVENT)
    except OSError:
        pass
    if _wait_windows_dead(pid, _WINDOWS_KILL_WAIT_SECONDS):
        return True
    return _terminate_pid(pid)


def kill_process_tree(pid: int) -> bool:
    """Best-effort terminate of a process and its descendants.

    Unix callers should prefer killpg when they own a session; this helper
    covers Windows trees and last-resort single-PID kills.
    """
    if pid <= 0:
        return False
    if is_windows():
        return _kill_windows_tree(pid)
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
