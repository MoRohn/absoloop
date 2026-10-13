"""absoloop-process: safe child-process supervision for provider CLIs.

- argv arrays only — no shell, no interpolation of task text into commands
- stdout (machine-readable) kept separate from stderr (diagnostics)
- incremental JSONL decoding tolerant of partial/garbage lines
- wall-clock timeout and cooperative cancellation that kill the entire
  process group, so provider-spawned children die with the provider
- minimal, allowlisted environment; secrets never leak into artifacts
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

# Baseline env vars any CLI reasonably needs; everything else must be
# explicitly allowlisted per provider in absoloop.toml.
BASE_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM",
    "LANG", "LC_ALL", "LC_CTYPE", "NO_COLOR",
    "SystemRoot", "ComSpec", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
)


def build_child_env(extra_allowlist: Optional[List[str]] = None,
                    extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Minimal environment for a provider process. Provider CLIs find their
    own credential stores via HOME; Absoloop passes through nothing else
    unless allowlisted (e.g. XAI_API_KEY for CI) and never persists any of it."""
    env: Dict[str, str] = {}
    for key in list(BASE_ENV_ALLOWLIST) + list(extra_allowlist or []):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(extra_env or {})
    return env


class CommandConstructionError(Exception):
    """A command failed safety validation before spawn."""


def validate_argv(argv: List[str]) -> List[str]:
    """Reject anything that is not a plain argv array of strings. The task
    prompt may contain arbitrary hostile text; as a discrete argv element it
    is inert. What is never acceptable is routing through a shell."""
    if not argv or not isinstance(argv, list):
        raise CommandConstructionError("argv must be a non-empty list")
    for item in argv:
        if not isinstance(item, str):
            raise CommandConstructionError(f"argv element {item!r} is not a string")
    program = argv[0]
    if program.endswith(("sh", "bash", "zsh", "cmd.exe", "powershell.exe")) and "-c" in argv[1:3]:
        raise CommandConstructionError("shell -c invocation is forbidden")
    return argv


@dataclass
class ProcessOutcome:
    exit_code: Optional[int]
    timed_out: bool
    cancelled: bool
    stderr: str
    duration_seconds: float


@dataclass
class SupervisedProcess:
    """A running provider CLI. Iterate `jsonl_events()` for parsed stdout
    lines; call `cancel()` from any thread to kill the whole process group.

    When `cancel_flag` points at a file, the watchdog treats its appearance
    as an external cancel request (`absoloop cancel <run-id>` from another
    terminal) and marks the outcome cancelled before killing the group."""
    argv: List[str]
    cwd: str
    env: Dict[str, str]
    timeout_seconds: float = 1800.0
    stdin_text: Optional[str] = None
    cancel_flag: Optional[pathlib.Path] = None

    _proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    _cancelled: bool = field(default=False, repr=False)
    _timed_out: bool = field(default=False, repr=False)
    _stderr_chunks: List[str] = field(default_factory=list, repr=False)
    _started_at: float = 0.0

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def pgid(self) -> Optional[int]:
        if self._proc is None:
            return None
        if os.name == "nt":
            return self._proc.pid
        try:
            return os.getpgid(self._proc.pid)
        except OSError:
            return self._proc.pid

    def start(self) -> "SupervisedProcess":
        validate_argv(self.argv)
        popen_kwargs: Dict[str, Any] = {}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True   # own process group
        else:
            # New group so Ctrl+C in the CLI is not delivered to the provider.
            # No window so killing that provider cannot signal this console.
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        self._started_at = time.time()
        from .platform_util import prepare_spawn_argv
        spawn_argv = prepare_spawn_argv(self.argv)
        self._proc = subprocess.Popen(
            spawn_argv, cwd=self.cwd, env=self.env,
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if self.stdin_text is not None else subprocess.DEVNULL,
            **popen_kwargs)
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        if self.stdin_text is not None:
            threading.Thread(target=self._feed_stdin, daemon=True).start()
        return self

    def _feed_stdin(self) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(self.stdin_text or "")
            self._proc.stdin.close()
        except OSError:
            pass

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            for line in self._proc.stderr:
                self._stderr_chunks.append(line)
        except (OSError, ValueError):
            pass

    def raw_lines(self) -> Iterator[str]:
        """Stdout lines as they arrive, honoring the wall-clock timeout."""
        assert self._proc is not None, "start() first"
        deadline = self._started_at + self.timeout_seconds
        watchdog = threading.Thread(target=self._watchdog, args=(deadline,), daemon=True)
        watchdog.start()
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            yield line.rstrip("\n")
        self._proc.wait()

    def jsonl_events(self) -> Iterator[Dict[str, Any]]:
        """Parsed JSON objects from stdout. Non-JSON and partial lines are
        surfaced as {'_absoloop_unparsed': <line>} so nothing is silently
        dropped and callers can log or ignore them."""
        for line in self.raw_lines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                yield {"_absoloop_unparsed": stripped}
                continue
            if isinstance(value, dict):
                yield value
            else:
                yield {"_absoloop_unparsed": stripped}

    def _watchdog(self, deadline: float) -> None:
        assert self._proc is not None
        while self._proc.poll() is None:
            if self._cancelled:
                return
            if self.cancel_flag is not None and self.cancel_flag.is_file():
                self._cancelled = True
                self._kill_group()
                return
            if time.time() >= deadline:
                self._timed_out = True
                self._kill_group()
                return
            time.sleep(0.2)

    def cancel(self) -> None:
        """Terminate the full child-process group; safe from any thread."""
        self._cancelled = True
        self._kill_group()

    def _kill_group(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                for _ in range(20):
                    if proc.poll() is not None:
                        return
                    time.sleep(0.1)
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                # Provider CLIs spawn grandchildren; terminate the whole tree.
                # Stay inside child_teardown until the process is actually
                # gone: Windows can deliver CTRL_C_EVENT after taskkill
                # returns, and proc.kill() can deliver another.
                from .platform_util import child_teardown, kill_process_tree
                with child_teardown():
                    if not kill_process_tree(proc.pid):
                        proc.terminate()
                    for _ in range(20):
                        if proc.poll() is not None:
                            return
                        time.sleep(0.1)
                    try:
                        proc.kill()
                    except OSError:
                        pass
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    def outcome(self) -> ProcessOutcome:
        assert self._proc is not None
        exit_code = self._proc.poll()
        return ProcessOutcome(
            exit_code=exit_code,
            timed_out=self._timed_out,
            cancelled=self._cancelled,
            stderr="".join(self._stderr_chunks),
            duration_seconds=time.time() - self._started_at,
        )


def write_prompt_file(directory: pathlib.Path, prompt: str) -> pathlib.Path:
    """Prompt handed over as a file (Grok prefers --prompt-file for long
    prompts); the file lives in the run's tmp dir and is cleaned with it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "prompt.md"
    path.write_text(prompt, encoding="utf-8")
    return path
