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
import threading
import time
from contextlib import contextmanager
from typing import Iterator, List, Optional, Sequence


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
# numeric values are the contract the taskkill tests assert on every OS.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)

# Console control events. Close / logoff / shutdown must still reach Python.
_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1
_CHILD_TEARDOWN_QUIET_SECONDS = 5.0
_child_teardown_depth = 0
_child_teardown_until = 0.0
_ctrl_handler_installed = False
_ctrl_handler_ref = None  # keep the WINFUNCTYPE alive; Windows calls it later


def ctrl_event_should_swallow(ctrl_type: int) -> bool:
    """True only for Ctrl+C / Ctrl+Break raised by a child-tree kill.

    A real operator Ctrl+C still becomes KeyboardInterrupt once the kill
    and its short quiet period are over. Closing the console is never
    swallowed.
    """
    if int(ctrl_type) not in (_CTRL_C_EVENT, _CTRL_BREAK_EVENT):
        return False
    if _child_teardown_depth > 0:
        return True
    return time.monotonic() < _child_teardown_until


def windows_child_creationflags() -> int:
    """CreateProcess flags that keep a child off this console.

    ``CREATE_NEW_PROCESS_GROUP`` still attaches the child. When that child
    is destroyed, Windows delivers ``CTRL_C_EVENT`` to the parent and the
    next ``Thread.join`` raises ``KeyboardInterrupt``. ``DETACHED_PROCESS``
    does not inherit the console, so a real Ctrl+C still aborts the CLI
    and the child's death does not.
    """
    return _DETACHED_PROCESS | _CREATE_NO_WINDOW


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    kernel32.ResetEvent.argtypes = [ctypes.c_void_p]
    kernel32.ResetEvent.restype = wintypes.BOOL
    return kernel32


def _install_console_ctrl_handler() -> None:
    """Install one handler that runs ahead of Python's.

    Last registered is called first. Returning TRUE stops the chain, so
    Python does not turn the event into KeyboardInterrupt. Returning FALSE
    leaves the CLI's Ctrl+C abort alone. ``os.name`` is checked on purpose:
    unit tests patch ``is_windows`` on Linux and must not load kernel32.
    """
    global _ctrl_handler_installed, _ctrl_handler_ref
    if _ctrl_handler_installed or os.name != "nt":
        return
    import ctypes

    # ctypes.WINFUNCTYPE, not a CFUNCTYPE argtype. Setting argtypes to the
    # prototype and then passing the callback makes SetConsoleCtrlHandler
    # fail, and the handler never runs.
    routine = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def _on_console_ctrl(ctrl_type: int) -> int:
        try:
            return 1 if ctrl_event_should_swallow(int(ctrl_type)) else 0
        except Exception:
            return 0

    callback = routine(_on_console_ctrl)
    kernel32 = _kernel32()
    # Pass the callback as a raw pointer so the NULL-handler argtypes
    # (c_void_p) do not try to build a second prototype around it.
    pointer = ctypes.cast(callback, ctypes.c_void_p)
    if not kernel32.SetConsoleCtrlHandler(pointer, True):
        return
    _ctrl_handler_ref = callback
    _ctrl_handler_installed = True


def _set_ignore_ctrl_c(ignore: bool) -> None:
    """Ignore or restore console Ctrl+C for this process.

    ``SetConsoleCtrlHandler(NULL, TRUE)`` is the Win32 switch for this.
    It does not ignore Ctrl+Break, and it does not have to stay on: a
    real Ctrl+C must still abort the CLI once child teardown is over.
    Safe to call from any thread. No-op off Windows.
    """
    if os.name != "nt":
        return
    kernel32 = _kernel32()
    kernel32.SetConsoleCtrlHandler(None, bool(ignore))


_ctrl_c_restore_gen = 0


def _schedule_restore_ctrl_c(delay_seconds: float) -> None:
    """Re-enable Ctrl+C after ``delay_seconds`` on a daemon timer.

    ``signal.signal`` is main-thread only. This Win32 call is not, so the
    watchdog thread that killed the child can arm the restore too.
    A newer teardown invalidates the previous timer.
    """
    global _ctrl_c_restore_gen
    if os.name != "nt":
        return
    _ctrl_c_restore_gen += 1
    generation = _ctrl_c_restore_gen

    def _restore(expected: int = generation) -> None:
        if expected != _ctrl_c_restore_gen or _child_teardown_depth > 0:
            return
        clear_spurious_keyboard_interrupt()
        _set_ignore_ctrl_c(False)

    timer = threading.Timer(delay_seconds, _restore)
    timer.daemon = True
    timer.start()


# SIG_IGN as a C function pointer. PyOS_setsig takes the same value the
# C runtime uses; 1 is SIG_IGN on Windows and on POSIX.
_SIG_IGN_PTR = 1
_sigint_ignore_until = 0.0
_c_sigint_prev = None
_c_sigbreak_prev = None
_sigint_py_saved = None
_sigbreak_py_saved = None
_pending_restore_ref = None
_restore_timer = None


def _pyos_setsig(signum: int, handler):
    """Set the C signal disposition. Works from any thread.

    ``signal.signal`` refuses non-main threads, but the spurious SIGINT
    arrives while the main thread is blocked in ``Thread.join``.
    """
    import ctypes

    py = ctypes.pythonapi
    py.PyOS_setsig.argtypes = [ctypes.c_int, ctypes.c_void_p]
    py.PyOS_setsig.restype = ctypes.c_void_p
    pointer = handler if isinstance(handler, ctypes.c_void_p) else ctypes.c_void_p(handler)
    return py.PyOS_setsig(int(signum), pointer)


def _install_sigint_ignore() -> None:
    """Point SIGINT/SIGBREAK at SIG_IGN until :func:`_restore_sigint_ignore`."""
    global _c_sigint_prev, _c_sigbreak_prev, _sigint_py_saved, _sigbreak_py_saved
    if _c_sigint_prev is None:
        try:
            _c_sigint_prev = _pyos_setsig(signal.SIGINT, _SIG_IGN_PTR)
        except (AttributeError, OSError, TypeError):
            _c_sigint_prev = None
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            try:
                _c_sigbreak_prev = _pyos_setsig(sigbreak, _SIG_IGN_PTR)
            except (AttributeError, OSError, TypeError):
                _c_sigbreak_prev = None
    if threading.current_thread() is not threading.main_thread():
        return
    if _sigint_py_saved is None:
        try:
            _sigint_py_saved = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (OSError, ValueError, TypeError):
            _sigint_py_saved = None
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None and _sigbreak_py_saved is None:
            try:
                _sigbreak_py_saved = signal.getsignal(sigbreak)
                signal.signal(sigbreak, signal.SIG_IGN)
            except (OSError, ValueError, TypeError):
                _sigbreak_py_saved = None


def _drain_pending_keyboard_interrupt() -> None:
    """Drop a SIGINT that was already tripped, without aborting the caller."""
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        import ctypes

        py = ctypes.pythonapi
        py.PyErr_CheckSignals.argtypes = []
        py.PyErr_CheckSignals.restype = ctypes.c_int
        py.PyErr_ExceptionMatches.argtypes = [ctypes.py_object]
        py.PyErr_ExceptionMatches.restype = ctypes.c_int
        py.PyErr_Clear.argtypes = []
        py.PyErr_Clear.restype = None
        if py.PyErr_CheckSignals() < 0 and py.PyErr_ExceptionMatches(KeyboardInterrupt):
            py.PyErr_Clear()
    except Exception:
        return


def _restore_python_sigint_handlers() -> None:
    global _sigint_py_saved, _sigbreak_py_saved, _c_sigint_prev, _c_sigbreak_prev
    saved_int = _sigint_py_saved
    saved_break = _sigbreak_py_saved
    c_int = _c_sigint_prev
    c_break = _c_sigbreak_prev
    _sigint_py_saved = None
    _sigbreak_py_saved = None
    _c_sigint_prev = None
    _c_sigbreak_prev = None
    if threading.current_thread() is threading.main_thread():
        try:
            if saved_int is not None:
                signal.signal(signal.SIGINT, saved_int)
            sigbreak = getattr(signal, "SIGBREAK", None)
            if sigbreak is not None and saved_break is not None:
                signal.signal(sigbreak, saved_break)
            return
        except (OSError, ValueError, TypeError):
            pass
    if c_int is not None:
        try:
            _pyos_setsig(signal.SIGINT, c_int)
        except (AttributeError, OSError, TypeError):
            pass
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None and c_break is not None:
        try:
            _pyos_setsig(sigbreak, c_break)
        except (AttributeError, OSError, TypeError):
            pass


def _queue_sigint_restore() -> None:
    """Run the restore on the main thread once it is back in the eval loop.

    ``Thread.join`` holds the GIL released inside ``lock.acquire``. A
    pending call is delivered when that acquire finishes, which is after
    the window where the spurious SIGINT would have been raised.
    """
    global _pending_restore_ref
    try:
        import ctypes

        routine = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)

        def _on_pending(_arg) -> int:
            try:
                _restore_python_sigint_handlers()
            except Exception:
                return 0
            return 0

        callback = routine(_on_pending)
        _pending_restore_ref = callback
        py = ctypes.pythonapi
        py.Py_AddPendingCall.argtypes = [routine, ctypes.c_void_p]
        py.Py_AddPendingCall.restype = ctypes.c_int
        if py.Py_AddPendingCall(callback, None) == 0:
            return
    except Exception:
        pass
    if threading.current_thread() is threading.main_thread():
        _restore_python_sigint_handlers()


def _restore_sigint_if_due() -> None:
    if time.monotonic() < _sigint_ignore_until:
        _arm_sigint_restore_timer()
        return
    if threading.current_thread() is threading.main_thread():
        _restore_python_sigint_handlers()
    else:
        _queue_sigint_restore()


def _arm_sigint_restore_timer() -> None:
    global _restore_timer
    delay = max(0.05, _sigint_ignore_until - time.monotonic())
    timer = threading.Timer(delay, _restore_sigint_if_due)
    timer.daemon = True
    timer.start()
    _restore_timer = timer


def shield_console_interrupt(seconds: float = 8.0) -> None:
    """Ignore SIGINT for ``seconds`` so a dying child cannot abort us.

    On Windows, ``Thread.join`` acquires its lock with the GIL released.
    That acquire is not itself interruptible, but taking the GIL back
    runs the pending SIGINT handler and raises ``KeyboardInterrupt``.
    The handler has to be ``SIG_IGN`` for the whole wait, not only for
    the ``taskkill`` call. A real Ctrl+C works again once the window
    ends. No-op off Windows.
    """
    global _sigint_ignore_until
    if os.name != "nt":
        return
    _sigint_ignore_until = max(_sigint_ignore_until, time.monotonic() + max(0.0, seconds))
    _install_sigint_ignore()
    _drain_pending_keyboard_interrupt()
    _arm_sigint_restore_timer()


def clear_spurious_keyboard_interrupt() -> None:
    """Reset Python's manual-reset sigint event if a child already tripped it.

    ``Thread.join`` waits on that event. Once set, the next join raises
    ``KeyboardInterrupt`` even though the operator did not press Ctrl+C.
    No-op off Windows, and no-op if this interpreter does not export the
    event handle.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = _kernel32()
        handle = None
        try:
            py = ctypes.pythonapi
            py._PyOS_SigintEvent.restype = ctypes.c_void_p
            py._PyOS_SigintEvent.argtypes = []
            handle = py._PyOS_SigintEvent()
        except (AttributeError, OSError):
            handle = None
        if not handle:
            get_proc = kernel32.GetProcAddress
            get_proc.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            get_proc.restype = ctypes.c_void_p
            addr = get_proc(ctypes.c_void_p(sys.dllhandle), b"_PyOS_SigintEvent")
            if not addr:
                return
            getter = ctypes.CFUNCTYPE(ctypes.c_void_p)(addr)
            handle = getter()
        if handle:
            kernel32.ResetEvent(ctypes.c_void_p(handle))
    except Exception:
        return


@contextmanager
def child_teardown(quiet_seconds: float = _CHILD_TEARDOWN_QUIET_SECONDS) -> Iterator[None]:
    """Hold off spurious console Ctrl+C while a child tree is destroyed.

    On Windows, killing a console child delivers CTRL_C_EVENT to every
    process that shares the console. Python raises KeyboardInterrupt on
    the main thread inside ``Thread.join``, after ``taskkill`` has already
    returned. CREATE_NO_WINDOW does not stop that. Ctrl+C is ignored only
    for this block plus a few seconds, then re-enabled so a real Ctrl+C
    still aborts the CLI.
    """
    global _child_teardown_depth, _child_teardown_until
    _install_console_ctrl_handler()
    _set_ignore_ctrl_c(True)
    # SIG_IGN has to outlast this block: the main thread raises
    # KeyboardInterrupt when it next reacquires the GIL, often in join.
    shield_console_interrupt(quiet_seconds)
    _child_teardown_depth += 1
    try:
        yield
    finally:
        _child_teardown_depth = max(0, _child_teardown_depth - 1)
        if _child_teardown_depth == 0:
            _child_teardown_until = time.monotonic() + quiet_seconds
            clear_spurious_keyboard_interrupt()
            _schedule_restore_ctrl_c(quiet_seconds)


def kill_process_tree(pid: int) -> bool:
    """Best-effort terminate of a process and its descendants.

    Unix callers should prefer killpg when they own a session; this helper
    covers Windows trees and last-resort single-PID kills.
    """
    if pid <= 0:
        return False
    if is_windows():
        # Detach taskkill so it does not join this console, and arm the
        # ctrl handler for the event Windows still delivers on child death.
        with child_teardown():
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=20, check=False,
                    creationflags=_CREATE_NO_WINDOW | _DETACHED_PROCESS)
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
