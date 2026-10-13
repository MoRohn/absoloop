"""Codex CLI adapter.

Transport: `codex exec --json --sandbox <level> --cd <dir> -` with the
prompt sent over stdin ("-" tells codex exec to read the prompt from
stdin). Resume maps SessionRef.native_id to `codex exec resume <id>`.
Auth is native (`codex login` / ~/.codex); never copied.

codex --json events:
  {"type":"thread.started","thread_id":...}                        -> RUN_STARTED
  {"type":"item.started"/"item.completed","item":{...}}            -> TOOL_* / TEXT / PROGRESS / FILE_CHANGED
  {"type":"turn.completed","usage":{...}}                          -> USAGE
  {"type":"error","message":...}                                   -> RUN_FAILED
Process exit 0 without an error event                              -> RUN_COMPLETED (synthesized)

Resume argv variance: some Codex builds accept sandbox/json flags after
`resume <id>`, others only before. Config `resume_style` selects the form
(`exec-resume` default, or `exec-flags-then-resume`). Probe records which
form `codex exec resume --help` appears to document.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
from typing import Any, Dict, List, Optional

from ..core import (AgentEvent, AgentRequest, EventType, PermissionMappingError,
                    ProviderCapabilities, ProviderProbe, SessionRef)
from .base import ProviderAdapter

_SANDBOX_BY_PROFILE = {
    "read": "read-only",
    "edit": "workspace-write",
    "full": "danger-full-access",
}

# resume_style values:
#   exec-resume              : codex exec resume <id> --json --cd … -
#   exec-flags-then-resume   : codex exec --json --cd … resume <id> -
RESUME_STYLES = ("exec-resume", "exec-flags-then-resume")


class CodexAdapter(ProviderAdapter):
    name = "codex"
    capabilities = ProviderCapabilities(
        streaming_json=True, session_resume=True, structured_output=True,
        permission_modes=False, native_sandbox=True, turn_limit=False,
        prompt_via_stdin=True, cost_reporting=False)

    def auth_hint(self) -> str:
        if os.environ.get("OPENAI_API_KEY"):
            return "OPENAI_API_KEY set"
        if os.environ.get("CODEX_API_KEY"):
            return "CODEX_API_KEY set"
        codex_home = pathlib.Path(
            os.environ.get("CODEX_HOME", str(pathlib.Path.home() / ".codex")))
        for name in ("auth.json", "config.toml", "credentials.json"):
            path = codex_home / name
            if path.is_file():
                return f"cached login found ({path})"
        if codex_home.is_dir():
            return f"codex home present ({codex_home}) — run 'codex login' if prompts fail"
        return "no credentials detected — run 'codex login' or set OPENAI_API_KEY"

    def probe(self) -> ProviderProbe:
        probe = super().probe()
        if not probe.available:
            return probe
        style = self._detect_resume_style(probe.info.executable or "")
        # Stash for this process; also surface in problems if detection failed.
        self.config.setdefault("resume_style", style)
        if style not in RESUME_STYLES:
            probe.problems.append(
                "could not confirm `codex exec resume` help — defaulting to "
                "exec-resume argv shape; set providers.codex.resume_style if "
                "resume fails")
            self.config["resume_style"] = "exec-resume"
        return probe

    def _detect_resume_style(self, path: str) -> str:
        configured = str(self.config.get("resume_style") or "").strip()
        if configured in RESUME_STYLES:
            return configured
        if not path:
            return "exec-resume"
        from ..platform_util import prepare_spawn_argv
        try:
            result = subprocess.run(
                prepare_spawn_argv([path, "exec", "resume", "--help"]),
                capture_output=True, text=True, timeout=15,
                stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return "exec-resume"
        text = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
        # If help mentions sandbox/json before the resume subcommand narrative,
        # prefer flags-then-resume; otherwise the common modern shape.
        if "usage:" in text and text.find("resume") < text.find("--sandbox") \
                and "--sandbox" in text:
            return "exec-flags-then-resume"
        return "exec-resume"

    def _version(self, path: str) -> str:
        # Codex has shipped builds that print only a bare name on --version
        # and the real semver on `codex version` / help banners.
        version = super()._version(path)
        if version and version.lower() not in ("codex", "codex-cli", "unknown"):
            return version
        from ..platform_util import prepare_spawn_argv
        for args in (["version"], ["exec", "--version"], ["--help"]):
            try:
                result = subprocess.run(
                    prepare_spawn_argv([path, *args]),
                    capture_output=True, text=True, timeout=15,
                    stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                continue
            text = (result.stdout or "") + "\n" + (result.stderr or "")
            import re
            match = re.search(r"(\d+\.\d+(?:\.\d+)?(?:[-+._a-zA-Z0-9]*)?)", text)
            if match:
                return match.group(1)
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and "codex" in stripped.lower() and any(
                        ch.isdigit() for ch in stripped):
                    return stripped[:60]
        return version

    def map_permissions(self, profile: str) -> List[str]:
        self.check_profile(profile)
        sandbox = _SANDBOX_BY_PROFILE.get(profile)
        if sandbox is None:
            raise PermissionMappingError(f"no codex mapping for profile {profile!r}")
        args = ["--sandbox", sandbox]
        if profile == "full":
            # danger-full-access still prompts unless approvals are bypassed;
            # 'full' explicitly means unattended.
            args += ["--dangerously-bypass-approvals-and-sandbox"]
        return args

    def build_argv(self, request: AgentRequest, resume: Optional[SessionRef],
                   workdir: pathlib.Path):
        exe = self.argv_program()
        style = str(self.config.get("resume_style") or "exec-resume")
        common_flags = ["--json", "--cd", request.cwd]
        model = request.model or self.config.get("model") or ""
        if model:
            common_flags += ["--model", model]
        common_flags += self.map_permissions(request.permission_profile)

        if resume is None:
            argv = [exe, "exec", *common_flags, "-"]
            return argv, request.prompt

        if style == "exec-flags-then-resume":
            # Older shape: flags on `exec`, then the resume subcommand.
            argv = [exe, "exec", *common_flags, "resume", resume.native_id, "-"]
        else:
            # Modern shape: `codex exec resume <id>` then streaming flags.
            argv = [exe, "exec", "resume", resume.native_id, *common_flags, "-"]
        return argv, request.prompt

    def normalize(self, raw: Dict[str, Any]) -> List[AgentEvent]:
        etype = str(raw.get("type", ""))
        if "_absoloop_unparsed" in raw:
            return [AgentEvent(type=EventType.UNKNOWN, provider=self.name,
                               text=raw["_absoloop_unparsed"][:200],
                               raw_type="_unparsed", data=raw)]
        if etype == "thread.started":
            thread_id = str(raw.get("thread_id", "") or "")
            if thread_id:
                self.last_session = SessionRef(provider=self.name, native_id=thread_id)
            return [AgentEvent(type=EventType.RUN_STARTED, provider=self.name,
                               text="session started", raw_type=etype,
                               data={"session_id": thread_id})]
        if etype in ("item.started", "item.completed"):
            return self._item_events(etype, raw)
        if etype == "turn.completed" and isinstance(raw.get("usage"), dict):
            return [AgentEvent(type=EventType.USAGE, provider=self.name,
                               raw_type=etype, data={"usage": raw["usage"]})]
        if etype == "error":
            return [AgentEvent(type=EventType.RUN_FAILED, provider=self.name,
                               text=str(raw.get("message", "engine error")),
                               raw_type=etype, data=raw)]
        return [AgentEvent(type=EventType.UNKNOWN, provider=self.name,
                           raw_type=etype or "?", data=raw)]

    def _item_events(self, etype: str, raw: Dict[str, Any]) -> List[AgentEvent]:
        item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
        itype = str(item.get("item_type") or item.get("type") or "")
        started = etype == "item.started"
        if itype == "command_execution":
            if started:
                return [AgentEvent(type=EventType.TOOL_STARTED, provider=self.name,
                                   text=str(item.get("command", ""))[:200],
                                   raw_type=f"{etype}/{itype}", data={"item": item})]
            return [AgentEvent(type=EventType.TOOL_COMPLETED, provider=self.name,
                               text=str(item.get("command", ""))[:200],
                               raw_type=f"{etype}/{itype}",
                               data={"exit_code": item.get("exit_code")})]
        if itype == "file_change" and not started:
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            paths = [str(c.get("path", "?")) for c in changes if isinstance(c, dict)]
            return [AgentEvent(type=EventType.FILE_CHANGED, provider=self.name,
                               text=", ".join(paths[:6]), raw_type=f"{etype}/{itype}",
                               data={"paths": paths})]
        if itype == "agent_message" and not started:
            text = str(item.get("text", ""))
            self.last_result_payload = {"text": text}
            return [AgentEvent(type=EventType.TEXT_DELTA, provider=self.name,
                               text=text, raw_type=f"{etype}/{itype}")]
        if itype == "reasoning" and not started:
            summary = str(item.get("summary") or item.get("text") or "")
            if summary:
                return [AgentEvent(type=EventType.PROGRESS, provider=self.name,
                                   text=summary, raw_type=f"{etype}/{itype}")]
            return []
        if itype == "collab_tool_call":
            tool = str(item.get("tool") or "").strip()
            prompt = str(
                item.get("prompt")
                or item.get("message")
                or item.get("task_name")
                or ""
            ).strip()
            text = f"{tool}: {prompt}" if prompt else tool or "collab_tool_call"
            event_type = (
                EventType.TOOL_STARTED if started else EventType.TOOL_COMPLETED
            )
            return [AgentEvent(type=event_type, provider=self.name,
                               text=text, raw_type=f"{etype}/{itype}",
                               data={"item": item, "tool": tool})]
        return [AgentEvent(type=EventType.UNKNOWN, provider=self.name,
                           raw_type=f"{etype}/{itype or '?'}", data=raw)]
