"""The narrow adapter contract every provider implements.

probe()      -> ProviderProbe (info + capabilities + problems)
start()      -> stream of normalized AgentEvents
resume()     -> same, continuing a native session
cancel()     -> kill the run's full process group
normalize()  -> provider event dict -> list[AgentEvent]

All provider-specific branching lives inside adapter modules; orchestration
code sees only these types.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Iterator, List, Optional

from ..core import (AgentEvent, AgentRequest, EventType, PermissionMappingError,
                    PERMISSION_PROFILES, ProviderCapabilities, ProviderInfo,
                    ProviderProbe, SessionRef, redact_event, secret_values)
from ..process import SupervisedProcess, build_child_env
from .. import runtime as run_ctrl

_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:[-+._a-zA-Z0-9]*)?)")
# Fallback flags tried in order when `--version` is silent or non-semver.
_VERSION_ARGV = (
    ["--version"],
    ["-V"],
    ["version"],
    ["--help"],
)


class ProviderAdapter:
    name: str = ""
    #: static capability baseline; probe() may refine it
    capabilities: ProviderCapabilities = ProviderCapabilities()

    def __init__(self, provider_config: Optional[Dict[str, Any]] = None):
        self.config = provider_config or {}
        self._active: Dict[str, SupervisedProcess] = {}
        self.last_session: Optional[SessionRef] = None
        self.last_outcome = None
        self.last_result_payload: Dict[str, Any] = {}
        # Optional cross-process tracking (set by the orchestrator for a run).
        self._run_dir: Optional[pathlib.Path] = None
        self._active_role: str = ""

    def bind_run(self, run_dir: Optional[pathlib.Path], role: str = "") -> None:
        """Attach this adapter to a harness run for live PID / cancel tracking."""
        self._run_dir = run_dir
        self._active_role = role or self.name

    # -- discovery -----------------------------------------------------------

    def executable(self) -> Optional[str]:
        """Resolved path suitable for argv[0] on Linux/macOS/Windows.

        Never return a bare name that CreateProcess cannot resolve — Windows
        needs the `.cmd` / `.exe` path from PATH / PATHEXT.
        """
        from ..platform_util import resolve_executable
        configured = str(self.config.get("command") or self.name)
        return resolve_executable(configured)

    def require_executable(self) -> str:
        path = self.executable()
        if path:
            return path
        name = self.config.get("command") or self.name
        raise PermissionMappingError(
            f"'{name}' not found on PATH — install {self.name} or set "
            f"providers.{self.name}.command in absoloop.toml")

    def argv_program(self) -> str:
        """Program for argv construction: resolved path when available.

        Falls back to the configured bare name so unit tests can inspect argv
        shapes offline. `_run` calls `require_executable()` before spawn so
        Windows never CreateProcess-es an unresolved bare name in production.
        """
        return self.executable() or str(self.config.get("command") or self.name)

    def probe(self) -> ProviderProbe:
        path = self.executable()
        problems: List[str] = []
        version = ""
        auth_hint = ""
        if path is None:
            problems.append(
                f"'{self.config.get('command') or self.name}' not found on PATH — "
                f"install {self.name} or set providers.{self.name}.command in absoloop.toml")
        else:
            version = self._version(path)
            auth_hint = self.auth_hint()
            if not version:
                problems.append(
                    f"{self.name} is installed but version could not be detected — "
                    f"try `{path} --version` manually")
        return ProviderProbe(
            info=ProviderInfo(name=self.name, executable=path,
                              version=version, auth_hint=auth_hint),
            capabilities=self.capabilities,
            problems=problems)

    def _version(self, path: str) -> str:
        from ..platform_util import prepare_spawn_argv
        for args in _VERSION_ARGV:
            try:
                result = subprocess.run(
                    prepare_spawn_argv([path, *args]),
                    capture_output=True, text=True, timeout=15,
                    stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                continue
            text = (result.stdout or "") + "\n" + (result.stderr or "")
            match = _VERSION_RE.search(text)
            if match:
                return match.group(1)
            # Non-semver CLIs (some codex builds) still print a useful banner.
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.lower().startswith("usage"):
                    return stripped[:60]
        return ""

    def auth_hint(self) -> str:
        """Safe, non-invasive auth readiness hint. Never reads credential
        file contents — presence checks and env var names only."""
        return ""

    # -- lifecycle -----------------------------------------------------------

    def build_argv(self, request: AgentRequest, resume: Optional[SessionRef],
                   workdir: pathlib.Path) -> "tuple[List[str], Optional[str]]":
        """(argv, stdin_text). Implemented per provider."""
        raise NotImplementedError

    def normalize(self, raw: Dict[str, Any]) -> List[AgentEvent]:
        raise NotImplementedError

    def map_permissions(self, profile: str) -> List[str]:
        """Profile -> provider-native argv flags; unknown profile fails closed."""
        raise NotImplementedError

    def check_profile(self, profile: str) -> None:
        if profile not in PERMISSION_PROFILES:
            raise PermissionMappingError(
                f"unknown permission profile {profile!r} for provider {self.name}; "
                f"expected one of {PERMISSION_PROFILES} — failing closed")

    def provider_extra_env(self, request: AgentRequest) -> Dict[str, str]:
        """Provider-forced env merged into the child after allowlist pass-through.
        Values here always reach the process (unlike allowlisted parent keys)."""
        return {}

    def start(self, request: AgentRequest, run_id: str = "adhoc") -> Iterator[AgentEvent]:
        return self._run(request, None, run_id)

    def resume(self, session: SessionRef, request: AgentRequest,
               run_id: str = "adhoc") -> Iterator[AgentEvent]:
        if not self.capabilities.session_resume:
            raise PermissionMappingError(f"{self.name} does not support session resume")
        return self._run(request, session, run_id)

    def cancel(self, run_id: str) -> None:
        proc = self._active.get(run_id)
        if proc is not None:
            proc.cancel()

    def _run(self, request: AgentRequest, session: Optional[SessionRef],
             run_id: str) -> Iterator[AgentEvent]:
        self.check_profile(request.permission_profile)
        # Fail before spawn if PATH resolution missed (critical on Windows).
        self.require_executable()
        workdir = pathlib.Path(tempfile.mkdtemp(prefix=f"absoloop-{self.name}-"))
        argv, stdin_text = self.build_argv(request, session, workdir)
        # Prefer resolved path even if build_argv used a bare fallback.
        resolved = self.executable()
        if resolved and argv:
            argv = [resolved, *argv[1:]]
        merged_extra = dict(request.extra_env or {})
        merged_extra.update(self.provider_extra_env(request))
        env = build_child_env(self.config.get("env_allowlist") or [],
                              merged_extra)
        secrets_to_hide = secret_values(env)
        cancel_flag = (run_ctrl.cancel_flag_path(self._run_dir)
                       if self._run_dir is not None else None)
        proc = SupervisedProcess(argv=argv, cwd=request.cwd, env=env,
                                 timeout_seconds=request.timeout_seconds,
                                 stdin_text=stdin_text,
                                 cancel_flag=cancel_flag).start()
        self._active[run_id] = proc
        self.last_session = None
        self.last_result_payload = {}
        saw_terminal = False
        if self._run_dir is not None and proc.pid is not None:
            run_ctrl.register_child(self._run_dir, role=self._active_role,
                                    provider=self.name, pid=proc.pid,
                                    pgid=proc.pgid)
        try:
            yield redact_event(AgentEvent(
                type=EventType.RUN_STARTED, provider=self.name,
                text=f"{self.name} started", raw_type="_absoloop_spawn",
                data={"argv_program": argv[0], "pid": proc.pid}), secrets_to_hide)
            for raw in proc.jsonl_events():
                for event in self.normalize(raw):
                    if event.type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED):
                        saw_terminal = True
                    yield redact_event(event, secrets_to_hide)
            outcome = proc.outcome()
            # External cancel writes the flag then kills; if the kill won the
            # race, still report cancelled so the run record stays accurate.
            if (not outcome.cancelled and self._run_dir is not None
                    and run_ctrl.cancel_requested(self._run_dir)):
                outcome.cancelled = True
            self.last_outcome = outcome
            if not saw_terminal:
                if outcome.cancelled:
                    yield AgentEvent(type=EventType.RUN_FAILED, provider=self.name,
                                     text="run cancelled", data={"cancelled": True})
                elif outcome.timed_out:
                    yield AgentEvent(type=EventType.RUN_FAILED, provider=self.name,
                                     text=f"timed out after {request.timeout_seconds:.0f}s",
                                     data={"timed_out": True})
                elif outcome.exit_code == 0:
                    yield AgentEvent(type=EventType.RUN_COMPLETED, provider=self.name,
                                     text="process exited cleanly")
                else:
                    yield AgentEvent(type=EventType.RUN_FAILED, provider=self.name,
                                     text=f"exit code {outcome.exit_code}",
                                     data={"exit_code": outcome.exit_code})
            elif outcome.cancelled:
                # Provider emitted a terminal event mid-kill; prefer cancelled.
                yield AgentEvent(type=EventType.RUN_FAILED, provider=self.name,
                                 text="run cancelled", data={"cancelled": True})
        finally:
            self._active.pop(run_id, None)
            if self._run_dir is not None:
                run_ctrl.unregister_child(self._run_dir, self._active_role)
            if self.last_outcome is None:
                self.last_outcome = proc.outcome()
