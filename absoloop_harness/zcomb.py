"""ZComb Kanban UI for Absoloop mission monitoring (default monitor).

Vendors the ZCombinator dashboard (``zcomb/monitor``) and bridges Absoloop
telemetry (``.absoloop/tmp/monitor.json`` + ``live.jsonl`` + state/runtime)
into ZComb's agents/tasks/activity/metrics state files under
``.absoloop/zcomb/state/``.

CLI:
  ``absoloop`` / ``absoloop new`` — briefing/launch, then ZComb Kanban (default)
  ``absoloop zcomb [-C project] [--port N] [--no-browser]`` — dashboard only
  ``absoloop --watch`` — classic terminal watcher instead of the browser UI

The gear menu in the UI can switch the monitor back to the terminal watcher
(persisted as ``monitor`` in ``.absoloop/zcomb/ui-settings.json``).

Requires Node.js 18+. First run installs and builds ``monitor/``. Default
URL: http://localhost:3141. See ``zcomb/README.md``.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_PORT = 3141
# Minimum dashboard REST API version this harness expects. Servers that
# report an older (or no) version on /api/health are replaced instead of
# retargeted, so the UI never hits 404s on newer endpoints. Keep in sync
# with API_VERSION in zcomb/monitor/server.js.
DASHBOARD_API_VERSION = 2
HEARTBEAT_STALE_SECONDS = 90
# A spawned teammate is shown "active" while the mission is live and its
# spawn event is younger than this; afterwards it flips to "done".
TEAMMATE_ACTIVE_SECONDS = 600
TEAMMATE_SPAWN_PREFIX = "spawn teammate · "
TEAMMATE_TOOL_NAMES = (
    "Task", "Agent", "SpawnAgent", "spawn_subagent", "spawn_agent",
)

# Quirky teammate names, themed by what the teammate was spawned to do.
TEAMMATE_NAME_THEMES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("critic", "review", "adversar", "audit", "skeptic", "challenge"),
     ("Grumbles von Nitpick", "The Side-Eye Sommelier", "Redline Rhonda",
      "Doubtfire the Unimpressed", "Squint Eastwood")),
    (("test", "qa", "verif", "validat", "coverage"),
     ("Sgt. Breaksalot", "Captain Edge-Case", "Wanda the Wrecker",
      "Flake Detector Hector", "The Assertion Goblin")),
    (("research", "search", "explore", "investigat", "find", "scan", "analy"),
     ("Indiana Grep", "Ferret of Facts", "Rabbit-Hole Ranger",
      "Snoop Docs", "Magnifying-Glass Gus")),
    (("fix", "repair", "debug", "patch", "bug"),
     ("Duct-Tape Da Vinci", "Patchwork Pete", "The Solder Goblin",
      "Wrench Wench", "Kintsugi Kevin")),
    (("build", "implement", "write", "code", "creat", "develop"),
     ("Tinker Tantrum", "Keyboard Kraken", "The Commit Gremlin",
      "Scaffold Salamander", "Bricklayer Byte")),
    (("doc", "readme", "spec", "summar"),
     ("Quill Quibbler", "The Footnote Fiend", "Sir Scribbles-a-Lot",
      "Parchment Piranha")),
    (("secur", "vuln", "threat", "pentest"),
     ("Paranoia Petunia", "Lockpick Lenny", "Tinfoil Tarantula",
      "The Zero-Trust Walrus")),
    (("plan", "design", "architect", "strateg"),
     ("Blueprint Banshee", "Doodle Oracle", "Whiteboard Wizard Wes",
      "The Napkin-Sketch Sphinx")),
)
TEAMMATE_FALLBACK_NAMES = (
    "Wildcard Wombat", "Gizmo Gremlin", "Free-Range Frankie",
    "The Loose Cannonball", "Odd-Job Ozzy", "Miscellaneous Mabel",
)


def extract_zcomb_flag(argv: list[str]) -> tuple[list[str], bool]:
    """Pull `--zcomb` out of argv so it can ride on any absoloop invocation."""
    want = False
    cleaned: list[str] = []
    for arg in argv:
        if arg == "--zcomb":
            want = True
        else:
            cleaned.append(arg)
    return cleaned, want
KIND_TO_ACTIVITY = {
    "say": "status_change",
    "think": "research",
    "tool": "task_started",
    "verdict": "task_completed",
    "error": "error",
    "usage": "heartbeat",
    "start": "phase_start",
}

PHASE_PIPELINE = [
    (0, "Scaffold"),
    (1, "Execute"),
    (2, "Integrity"),
    (3, "Critic"),
    (4, "Human Gate"),
    (5, "Deliver"),
]

# Mission-file statuses that win over live monitor.json telemetry. Codex critic
# / report steps can leave monitor at FINAL_REVIEW with a fresh heartbeat after
# state.json has already advanced to AWAITING_APPROVAL — CLI approve works, but
# the Kanban Approve button stays dark if we keep trusting the monitor.
_GATE_OR_TERMINAL_STATUSES = frozenset({
    "AWAITING_APPROVAL",
    "COMPLETED",
    "BLOCKED",
    "BUDGET_EXHAUSTED",
    "REJECTED",
    "STOPPED",
})


def zcomb_home() -> pathlib.Path:
    env = os.environ.get("ABSOLOOP_HOME")
    if env:
        root = pathlib.Path(env).expanduser().resolve()
        if (root / "zcomb" / "monitor").is_dir():
            return root / "zcomb"
    here = pathlib.Path(__file__).resolve().parent.parent
    return here / "zcomb"


def monitor_dir() -> pathlib.Path:
    return zcomb_home() / "monitor"


def project_state_dir(project: pathlib.Path) -> pathlib.Path:
    return project / ".absoloop" / "zcomb" / "state"


def _read_json(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def _iso(ts: Any) -> str:
    if isinstance(ts, (int, float)) and ts > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) broadcasts CTRL_C_EVENT to this console.
        from .platform_util import windows_pid_alive
        return windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def monitor_is_live(monitor: dict) -> bool:
    heartbeat = monitor.get("heartbeat_ts")
    fresh = (isinstance(heartbeat, (int, float))
             and time.time() - heartbeat < HEARTBEAT_STALE_SECONDS)
    return fresh and _pid_alive(monitor.get("pid"))


def _agent_status(live: bool, role: str, monitor: dict, state: dict) -> str:
    status = str(state.get("status") or monitor.get("status") or "").upper()
    if status in ("COMPLETED",):
        return "done"
    if status in ("BLOCKED", "BUDGET_EXHAUSTED", "REJECTED"):
        return "blocked"
    if not live:
        return "idle"
    agent = str(monitor.get("agent") or "").lower()
    phase = str(monitor.get("phase") or "").lower()
    if role == "builder":
        if "critic" in agent or "critic" in phase:
            return "idle"
        return "active"
    if role == "critic":
        if "critic" in agent or "critic" in phase or status == "FINAL_REVIEW":
            return "active"
        if status == "AWAITING_APPROVAL":
            return "done"
        return "idle"
    return "idle"


def _objective_history(runtime: dict, abs_dir: pathlib.Path) -> list[dict]:
    """Original objective plus each extend continuation note (oldest first).

    The Kanban objective bar shows the latest continuation by default and
    exposes the full chain in a dropdown. History is rebuilt from the
    append-only ledger (type=extension) with runtime.continuation as a
    fallback when the ledger has not caught up yet.
    """
    history: list[dict] = []
    objective = str(runtime.get("objective") or "").strip()
    if objective:
        history.append({
            "kind": "objective",
            "text": objective,
            "loopId": None,
            "previousLoopId": None,
            "ts": None,
        })

    ledger_path = abs_dir / "ledger.jsonl"
    if ledger_path.is_file():
        try:
            lines = ledger_path.read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "extension":
                continue
            note = str(event.get("note") or "").strip()
            if not note:
                continue
            history.append({
                "kind": "continuation",
                "text": note,
                "loopId": str(event.get("loop_id") or "").strip() or None,
                "previousLoopId": (
                    str(event.get("previous_loop_id") or "").strip() or None),
                "ts": event.get("ts") if isinstance(event.get("ts"),
                                                    (int, float)) else None,
            })

    cont = runtime.get("continuation")
    if isinstance(cont, dict):
        note = str(cont.get("note") or "").strip()
        if note:
            already = any(
                entry.get("kind") == "continuation" and entry.get("text") == note
                for entry in history
            )
            if not already:
                history.append({
                    "kind": "continuation",
                    "text": note,
                    "loopId": str(runtime.get("loop_id") or "").strip() or None,
                    "previousLoopId": (
                        str(cont.get("previous_loop_id") or "").strip() or None),
                    "ts": None,
                })
    return history


def _displayed_objective(history: list[dict], objective: str) -> str:
    """Latest continuation note, else the original objective."""
    for entry in reversed(history):
        if entry.get("kind") == "continuation" and entry.get("text"):
            return str(entry["text"])
    return objective


def _extension_end_ts(abs_dir: pathlib.Path, loop_id: str) -> Optional[float]:
    """Timestamp when ``loop_id`` was superseded (next extension event)."""
    lid = (loop_id or "").strip()
    if not lid:
        return None
    ledger_path = abs_dir / "ledger.jsonl"
    if not ledger_path.is_file():
        return None
    try:
        lines = ledger_path.read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "extension":
            continue
        prev = str(event.get("previous_loop_id") or "").strip()
        if prev != lid:
            continue
        ts = event.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            return float(ts)
    return None


def _loop_clock_bounds(
    abs_dir: pathlib.Path,
    loop_id: str,
    *,
    current_loop_id: str,
    current_started: float,
    current_ended: float,
) -> tuple[Optional[float], Optional[float]]:
    """Return ``(started_at, ended_at)`` for a loop id.

    Archived Absoloop states often omit ``ended_at``; fall back to the next
    extension timestamp, then the next archived loop's ``started_at``.
    """
    lid = (loop_id or "").strip()
    if not lid:
        return None, None
    current = (current_loop_id or "").strip()
    if lid == current and current_started > 0:
        ended = (current_ended if current_ended > current_started else None)
        return float(current_started), ended

    state = _read_json(abs_dir / "runs" / lid / "state.json")
    if not state:
        return None, None
    started = state.get("started_at")
    if not isinstance(started, (int, float)) or started <= 0:
        return None, None
    started_f = float(started)
    ended: Optional[float] = None
    for candidate in (state.get("ended_at"), state.get("updated_at")):
        if isinstance(candidate, (int, float)) and candidate > started_f:
            ended = float(candidate)
            break
    if ended is None:
        ext_ts = _extension_end_ts(abs_dir, lid)
        if ext_ts is not None and ext_ts > started_f:
            ended = ext_ts
    if ended is None:
        # Next archive by start time after this loop.
        runs_dir = abs_dir / "runs"
        if runs_dir.is_dir():
            next_starts: list[float] = []
            for entry in runs_dir.iterdir():
                if not entry.is_dir() or entry.name == lid:
                    continue
                other = _read_json(entry / "state.json")
                other_start = other.get("started_at") if other else None
                if (isinstance(other_start, (int, float))
                        and other_start > started_f):
                    next_starts.append(float(other_start))
            if current_started > started_f:
                next_starts.append(float(current_started))
            if next_starts:
                ended = min(next_starts)
    return started_f, ended


def _loop_wall_seconds(
    abs_dir: pathlib.Path,
    loop_id: str,
    *,
    current_loop_id: str,
    current_started: float,
    current_ended: float,
) -> Optional[float]:
    """Wall-clock seconds for a loop id — live metrics or archived state."""
    started, ended = _loop_clock_bounds(
        abs_dir, loop_id,
        current_loop_id=current_loop_id,
        current_started=current_started,
        current_ended=current_ended,
    )
    if started is None:
        return None
    lid = (loop_id or "").strip()
    current = (current_loop_id or "").strip()
    if lid == current and ended is None:
        ended = time.time()
    if ended is None or ended < started:
        return None
    return max(0.0, float(ended) - float(started))


def _enrich_objective_history_elapsed(
    history: list[dict],
    abs_dir: pathlib.Path,
    *,
    current_loop_id: str,
    current_started: float,
    current_ended: float,
) -> list[dict]:
    """Attach elapsedSeconds / clock anchors next to each history loop id.

    The original-objective row often lacks a loopId; fill it from the first
    continuation's previousLoopId (or the current loop) so the dropdown can
    show that segment's wall time too.
    """
    if not history:
        return history
    enriched: list[dict] = [dict(entry) for entry in history]
    current = (current_loop_id or "").strip()

    for index, entry in enumerate(enriched):
        if entry.get("kind") == "objective" and not entry.get("loopId"):
            prior = None
            for later in enriched[index + 1:]:
                if later.get("kind") == "continuation":
                    prior = (str(later.get("previousLoopId") or "").strip()
                             or None)
                    if prior:
                        break
            entry["loopId"] = prior or (current or None)

    for entry in enriched:
        lid = str(entry.get("loopId") or "").strip()
        if not lid:
            entry["elapsedSeconds"] = None
            entry["startedAt"] = None
            entry["endedAt"] = None
            continue
        started, ended = _loop_clock_bounds(
            abs_dir, lid,
            current_loop_id=current,
            current_started=current_started,
            current_ended=current_ended,
        )
        entry["startedAt"] = started
        entry["endedAt"] = ended
        entry["elapsedSeconds"] = _loop_wall_seconds(
            abs_dir, lid,
            current_loop_id=current,
            current_started=current_started,
            current_ended=current_ended,
        )
    return enriched


def _format_elapsed_hms(seconds: Optional[float]) -> str:
    """Match the ZComb dropdown clock: ``HH:MM:SS``."""
    if seconds is None:
        return ""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _kind_label(kind: str) -> str:
    return "Continuation" if kind == "continuation" else "Original objective"


def render_objectives_archive_markdown(
    history: list[dict],
    *,
    displayed_text: str = "",
    project_name: str = "",
) -> str:
    """Markdown twin of the ZComb objective dropdown (newest first).

    Each entry: kind label (+ ``· current``), loop id, elapsed, then full text.
    """
    active = (displayed_text or "").strip()
    # Dropdown reverses oldest→newest so the active note sits at the top.
    items = list(reversed(history or []))
    gen = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime()).strip()
    lines = [
        "# Absoloop · Objectives & Continuations",
        "",
        "Archive of the ZComb objective dropdown — same labels, loop ids, "
        "elapsed times, and full statement text.",
        "",
        f"- **Project:** {project_name or '—'}",
        f"- **Generated:** {gen}",
        f"- **Entries:** {len(items)}",
        f"- **Current (displayed):** "
        + (active[:120] + ("…" if len(active) > 120 else "") if active else "—"),
        "",
        "---",
        "",
    ]
    if not items:
        lines += ["_No objective / continuation history recorded yet._", ""]
        return "\n".join(lines)

    for entry in items:
        kind = str(entry.get("kind") or "objective")
        text = str(entry.get("text") or "").strip()
        loop_id = str(entry.get("loopId") or "").strip()
        elapsed = _format_elapsed_hms(entry.get("elapsedSeconds"))
        is_current = bool(active) and text == active
        label = _kind_label(kind).upper()
        if is_current:
            label = f"{label} · CURRENT"
        lines.append(f"## {label}")
        lines.append("")
        meta_bits = []
        if loop_id:
            meta_bits.append(f"`{loop_id}`")
        if elapsed:
            meta_bits.append(f"`{elapsed}`")
        if meta_bits:
            lines.append(" · ".join(meta_bits))
            lines.append("")
        lines.append(text or "_(empty)_")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def write_objectives_archive(
    project: pathlib.Path,
    history: list[dict],
    *,
    displayed_text: str = "",
    project_name: str = "",
) -> Optional[pathlib.Path]:
    """Write ``.absoloop/reports/OBJECTIVES.md`` (+ JSON) for the mission."""
    abs_dir = pathlib.Path(project).resolve() / ".absoloop"
    reports = abs_dir / "reports"
    try:
        reports.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    md_path = reports / "OBJECTIVES.md"
    json_path = reports / "OBJECTIVES.json"
    markdown = render_objectives_archive_markdown(
        history,
        displayed_text=displayed_text,
        project_name=project_name or pathlib.Path(project).name,
    )
    payload = {
        "project": project_name or pathlib.Path(project).name,
        "generatedAt": time.time(),
        "displayedText": (displayed_text or "").strip(),
        "history": history,
    }
    try:
        md_path.write_text(markdown, encoding="utf-8")
        json_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return None
    return md_path


def _read_live_events(path: pathlib.Path, limit: int = 200) -> list[dict]:
    """Last `limit` parsed events from live.jsonl (oldest first)."""
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in lines[-limit:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _read_jsonl_dicts(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            rows.append(event)
    return rows


def _clock_label(ts: Any) -> str:
    """Local HH:MM:SS matching the Absoloop CLI progress prefix."""
    if isinstance(ts, (int, float)) and ts > 0:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    return time.strftime("%H:%M:%S", time.localtime())


def _parse_json_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _load_critic_structured(project: pathlib.Path, result_rel: str) -> dict:
    """Pull critic recommendation / summary / turns from an agent result file."""
    if not result_rel:
        return {}
    path = project / result_rel
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    structured = _parse_json_maybe(payload.get("structured_output"))
    if not isinstance(structured, dict):
        structured = {}
    if not structured:
        nested = _parse_json_maybe(payload.get("result"))
        if isinstance(nested, dict) and (
            "recommendation" in nested or "summary" in nested
        ):
            structured = nested
    # Codex / Grok often write the schema object as the whole file.
    if not structured and payload.get("recommendation"):
        structured = payload

    recommendation = str(structured.get("recommendation") or "").strip().upper()
    summary = " ".join(str(structured.get("summary") or "").split())
    findings_raw = structured.get("blocking_findings") or []
    findings: list[str] = []
    if isinstance(findings_raw, list):
        findings = [str(f).strip() for f in findings_raw if str(f).strip()]

    turns = payload.get("num_turns")
    if not isinstance(turns, int):
        turns = structured.get("num_turns")
    if not isinstance(turns, int):
        turns = None

    return {
        "recommendation": recommendation,
        "summary": summary,
        "blockingFindings": findings[:8],
        "turns": turns,
    }


def _current_run_start_ts(events: list[dict]) -> float:
    """Timestamp of the latest mission extension; 0.0 on the first run.

    Loop Results must refresh per run. The ledger and live.jsonl are
    append-only across ``--extend``, so critic/verdict rows at or before
    this boundary belong to prior loops and must not paint the panel.
    """
    start = 0.0
    for event in events:
        if event.get("type") == "extension":
            start = float(event.get("ts") or 0)
    return start


def _file_mtime(path: pathlib.Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _latest_critic_ledger_run(
    abs_dir: pathlib.Path,
    *,
    run_start: float = 0.0,
) -> dict:
    """Most recent current-run ledger agent_run whose result path names the critic."""
    latest: dict = {}
    for event in _read_jsonl_dicts(abs_dir / "ledger.jsonl"):
        if event.get("type") != "agent_run":
            continue
        ts = float(event.get("ts") or 0)
        if run_start > 0 and ts <= run_start:
            continue
        result = str(event.get("result") or "")
        if "critic" not in result.lower():
            continue
        latest = event
    return latest


def _latest_critic_result_rel(
    project: pathlib.Path,
    abs_dir: pathlib.Path,
    iteration: int,
    *,
    run_start: float = 0.0,
) -> str:
    """Prefer ledger path; fall back to current-run critic JSON on disk."""
    run = _latest_critic_ledger_run(abs_dir, run_start=run_start)
    result = str(run.get("result") or "").strip()
    if result:
        return result

    def _in_current_run(path: pathlib.Path) -> bool:
        if run_start <= 0:
            return True
        return _file_mtime(path) > run_start

    if iteration > 0:
        candidate = abs_dir / "tmp" / f"iteration-{iteration:04d}-critic.json"
        if candidate.is_file() and _in_current_run(candidate):
            return str(candidate.relative_to(project))
    tmp = abs_dir / "tmp"
    if tmp.is_dir():
        matches = [
            path for path in sorted(tmp.glob("iteration-*-critic.json"))
            if _in_current_run(path)
        ]
        if matches:
            return str(matches[-1].relative_to(project))
    return ""


def _verdict_from_live(
    live_events: list[dict],
    *,
    run_start: float = 0.0,
) -> dict:
    for event in reversed(live_events):
        if str(event.get("kind") or "") != "verdict":
            continue
        ts = float(event.get("ts") or 0)
        if run_start > 0 and ts <= run_start:
            continue
        detail = str(event.get("detail") or "").strip()
        if not detail:
            continue
        recommendation = ""
        summary = detail
        if ":" in detail:
            head, tail = detail.split(":", 1)
            recommendation = head.strip().upper()
            summary = tail.strip()
        return {
            "recommendation": recommendation,
            "summary": summary,
            "ts": event.get("ts"),
        }
    return {}


# -- Proposed Extension (LLM prompt/response chain for one-click extend) ------

_PROPOSE_CACHE_NAME = "proposed-extension.json"
_PROPOSE_LOCK_NAME = "proposed-extension.lock"
_PROPOSE_LLM_TIMEOUT = 90.0


def _extension_fingerprint(
    *,
    loop_id: str,
    iteration: int,
    status: str,
    stop_reason: str,
    recommendation: str,
    summary: str,
) -> str:
    raw = "|".join([
        str(loop_id or ""),
        str(iteration or 0),
        str(status or "").upper(),
        str(stop_reason or ""),
        str(recommendation or "").upper(),
        " ".join(str(summary or "").split())[:240],
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _proposed_extension_path(abs_dir: pathlib.Path) -> pathlib.Path:
    return abs_dir / "tmp" / _PROPOSE_CACHE_NAME


def _build_extension_prompt(ctx: dict) -> str:
    """User prompt for the extension-proposal LLM chain."""
    findings = ctx.get("blockingFindings") or []
    findings_block = (
        "\n".join(f"- {f}" for f in findings[:8])
        if findings else "- (none)"
    )
    objective = str(ctx.get("objective") or "").strip() or "(unknown)"
    summary = str(ctx.get("summary") or "").strip() or "(none)"
    return (
        "You are proposing the next Absoloop mission extension.\n"
        "Absoloop `extend` starts a follow-on run with fresh budgets; the "
        "`note` you write becomes the continuation objective (`-m`).\n\n"
        "Loop run context:\n"
        f"- Project: {ctx.get('projectName') or '(unknown)'}\n"
        f"- Original / current objective: {objective}\n"
        f"- Status: {ctx.get('status') or '—'}\n"
        f"- Stop reason: {ctx.get('stopReason') or '—'}\n"
        f"- Critic recommendation: {ctx.get('recommendation') or '—'}\n"
        f"- Iteration: {ctx.get('iteration') or 0}\n"
        f"- Spend (USD): {ctx.get('costUsd') or 0}\n"
        f"- Critic summary: {summary}\n"
        f"- Blocking findings:\n{findings_block}\n\n"
        "Think in two steps:\n"
        "1) analysis — what landed, what is still open, and the highest-leverage next slice\n"
        "2) proposal — a concrete continuation objective an operator can one-click extend\n\n"
        "Reply with ONLY valid JSON (no markdown fences) shaped as:\n"
        "{\n"
        '  "analysis": "2-4 sentences of situational analysis",\n'
        '  "note": "imperative continuation objective for absoloop extend -m '
        '(1-3 sentences, actionable, specific)",\n'
        '  "rationale": "1-2 sentences why this is the right next extend"\n'
        "}\n"
        "Constraints for note:\n"
        "- Do not repeat the original objective verbatim; advance it.\n"
        "- If there are blocking findings, address the top ones first.\n"
        "- If the run PASSED / COMPLETED, propose the natural next slice of work.\n"
        "- Keep note under 400 characters.\n"
    )


def _parse_extension_llm_response(text: str) -> dict:
    """Extract analysis/note/rationale from an LLM reply."""
    raw = (text or "").strip()
    if not raw:
        return {}
    # Prefer fenced JSON, then bare object, then whole string.
    candidates: list[str] = []
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.I)
    if fence:
        candidates.append(fence.group(1))
    brace = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    candidates.append(raw)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        note = str(data.get("note") or data.get("objective") or "").strip()
        if not note:
            continue
        return {
            "analysis": str(data.get("analysis") or "").strip(),
            "note": note[:800],
            "rationale": str(data.get("rationale") or "").strip(),
        }
    # Free-text fallback: first non-empty paragraph as note.
    for para in re.split(r"\n\s*\n", raw):
        cleaned = " ".join(para.strip().split())
        if cleaned and not cleaned.startswith("{"):
            return {"analysis": "", "note": cleaned[:800], "rationale": ""}
    return {}


def _heuristic_extension_proposal(ctx: dict) -> dict:
    """Deterministic proposal when LLM is unavailable — still chain-shaped."""
    prompt = _build_extension_prompt(ctx)
    objective_full = str(ctx.get("objective") or "").strip() or "the mission"
    objective = objective_full if len(objective_full) <= 120 else (
        objective_full[:117].rstrip() + "…"
    )
    summary = str(ctx.get("summary") or "").strip()
    summary_short = summary if len(summary) <= 160 else summary[:157].rstrip() + "…"
    findings = [str(f).strip() for f in (ctx.get("blockingFindings") or []) if str(f).strip()]
    recommendation = str(ctx.get("recommendation") or "").upper()
    status = str(ctx.get("status") or "").upper()

    if findings:
        top = findings[0]
        note = (
            f"Resolve: {top}. "
            f"Then re-verify against the objective “{objective}” and clear "
            f"remaining critic findings."
        )
        analysis = (
            f"Run ended {status or 'with findings'} ({recommendation or 'n/a'}). "
            f"Top blocker: {top}."
        )
        rationale = "Address the critic’s blocking finding before expanding scope."
    elif recommendation in ("HOLD", "REJECT", "UNREADABLE") or status in (
        "BLOCKED", "REJECTED",
    ):
        note = (
            f"Investigate and fix why the last loop stalled on “{objective}”"
            + (f" — critic: {summary_short}" if summary_short else "")
            + ". Re-run gates until critic PASS."
        )
        analysis = (
            f"Status {status or 'unknown'} with recommendation "
            f"{recommendation or 'none'}; need a corrective follow-on."
        )
        rationale = "Unblock the failed/held loop before taking on new work."
    elif status == "BUDGET_EXHAUSTED":
        note = (
            f"Continue “{objective}” from the last checkpoint with a tighter "
            f"slice: finish the nearest incomplete deliverable, verify gates, "
            f"and stop with a clear PASS."
        )
        analysis = "Budget stopped the loop before full closure; resume with a focused slice."
        rationale = "Fresh extend budgets let the mission finish the unfinished slice."
    else:
        note = (
            f"Building on the completed pass for “{objective}”, take the next "
            f"highest-leverage polish or adjacent capability"
            + (f" (critic: {summary_short})" if summary_short else "")
            + ". Keep gates green and leave a crisp operator-facing summary."
        )
        analysis = (
            f"Loop reached {status or 'a terminal state'} with "
            f"{recommendation or 'no'} critic stance — ready for a forward extend."
        )
        rationale = "Capitalize on a clean landing with a concrete next slice."

    note = " ".join(note.split())[:800]
    chain = [
        {"role": "prompt", "content": prompt},
        {"role": "analysis", "content": analysis},
        {"role": "response", "content": note},
    ]
    return {
        "status": "ready",
        "source": "heuristic",
        "engine": "",
        "note": note,
        "rationale": rationale,
        "chain": chain,
        "generatedAt": _iso(time.time()),
    }


def _llm_output_looks_unusable(text: str) -> str:
    """Return an error reason when CLI output is not a usable proposal."""
    low = (text or "").strip().lower()
    if not low:
        return "empty output"
    needles = (
        "not logged in",
        "please run /login",
        "please login",
        "authentication required",
        "unauthorized",
        "api key",
        "invalid api key",
        "credit balance",
        "usage limit",
    )
    for needle in needles:
        if needle in low:
            return f"engine auth/limit: {text.strip()[:160]}"
    return ""


def _extension_engine_order(preferred: str = "") -> list[str]:
    from shutil import which
    order: list[str] = []
    pref = (preferred or "").strip().lower()
    if pref in ("claude", "codex", "grok"):
        order.append(pref)
    for name in ("claude", "codex", "grok"):
        if name not in order:
            order.append(name)
    return [name for name in order if which(name)]


def _run_extension_engine(chosen: str, prompt: str) -> str:
    if chosen == "claude":
        argv = [
            "claude", "-p",
            "--bare",
            "--tools", "",
            "--permission-mode", "plan",
            "--output-format", "text",
            "--effort", "low",
            prompt,
        ]
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_PROPOSE_LLM_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    elif chosen == "codex":
        argv = ["codex", "exec", "--skip-git-repo-check", "-"]
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_PROPOSE_LLM_TIMEOUT,
        )
    else:
        argv = ["grok", "--prompt-file", "-"]
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_PROPOSE_LLM_TIMEOUT,
        )

    text = (proc.stdout or "").strip()
    if proc.returncode != 0 and not text:
        err = (proc.stderr or "").strip()[:300]
        raise RuntimeError(f"{chosen} exited {proc.returncode}: {err or 'no output'}")
    if not text:
        text = (proc.stderr or "").strip()
    if not text:
        raise RuntimeError(f"{chosen} returned empty proposal")
    bad = _llm_output_looks_unusable(text)
    if bad:
        raise RuntimeError(f"{chosen}: {bad}")
    return text


def _call_extension_llm(prompt: str, engine: str = "") -> tuple[str, str]:
    """One-shot headless LLM call. Returns (raw_text, engine_used).

    Tries preferred engine first, then other CLIs on PATH when auth/output fails.
    """
    engines = _extension_engine_order(engine)
    if not engines:
        raise RuntimeError("no LLM engine on PATH (claude/codex/grok)")

    errors: list[str] = []
    for chosen in engines:
        try:
            text = _run_extension_engine(chosen, prompt)
            return text, chosen
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            errors.append(str(exc))
            continue
    raise RuntimeError("; ".join(errors[:3]))


def _load_proposed_extension(abs_dir: pathlib.Path) -> dict:
    return _read_json(_proposed_extension_path(abs_dir))


def _write_proposed_extension(abs_dir: pathlib.Path, payload: dict) -> None:
    _atomic_write(_proposed_extension_path(abs_dir), payload)


def _compose_extension_proposal(
    *,
    ctx: dict,
    fingerprint: str,
    source: str,
    engine: str,
    note: str,
    rationale: str,
    analysis: str,
    prompt: str,
    raw_response: str = "",
    status: str = "ready",
    error: str = "",
) -> dict:
    chain = [
        {"role": "prompt", "content": prompt},
    ]
    if analysis:
        chain.append({"role": "analysis", "content": analysis})
    response_body = raw_response.strip() if raw_response.strip() else note
    if rationale and note and response_body == note:
        response_body = f"{note}\n\nWhy: {rationale}"
    chain.append({"role": "response", "content": response_body})
    payload = {
        "status": status,
        "source": source,
        "engine": engine or "",
        "fingerprint": fingerprint,
        "note": note,
        "rationale": rationale,
        "chain": chain,
        "generatedAt": _iso(time.time()),
    }
    if error:
        payload["error"] = error
    return payload


def _generate_proposed_extension(ctx: dict, fingerprint: str) -> dict:
    """Run LLM chain; fall back to heuristic on any failure."""
    prompt = _build_extension_prompt(ctx)
    preferred = str(ctx.get("engine") or "")
    try:
        raw, engine = _call_extension_llm(prompt, preferred)
        parsed = _parse_extension_llm_response(raw)
        if not parsed.get("note"):
            raise RuntimeError("LLM response missing note")
        return _compose_extension_proposal(
            ctx=ctx,
            fingerprint=fingerprint,
            source="llm",
            engine=engine,
            note=parsed["note"],
            rationale=parsed.get("rationale") or "",
            analysis=parsed.get("analysis") or "",
            prompt=prompt,
            raw_response=raw,
        )
    except Exception as exc:
        fallback = _heuristic_extension_proposal(ctx)
        return _compose_extension_proposal(
            ctx=ctx,
            fingerprint=fingerprint,
            source="heuristic",
            engine="",
            note=fallback["note"],
            rationale=fallback.get("rationale") or "",
            analysis=next(
                (s["content"] for s in fallback["chain"] if s["role"] == "analysis"),
                "",
            ),
            prompt=prompt,
            status="ready",
            error=str(exc)[:240],
        )


def _kick_extension_worker(
    project: pathlib.Path,
    abs_dir: pathlib.Path,
    fingerprint: str,
) -> None:
    """Detached worker so the bridge loop never blocks on the LLM."""
    lock = abs_dir / "tmp" / _PROPOSE_LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        if lock.is_file():
            age = time.time() - lock.stat().st_mtime
            if age < _PROPOSE_LLM_TIMEOUT + 30:
                return
        lock.write_text(f"{os.getpid()}:{fingerprint}:{time.time()}\n",
                        encoding="utf-8")
    except OSError:
        return

    ctx_path = abs_dir / "tmp" / "proposed-extension-ctx.json"
    # Context file is written by caller before kick.
    if not ctx_path.is_file():
        return

    repo_root = str(zcomb_home().parent)
    stub = (
        "from absoloop_harness.zcomb import generate_proposed_extension_main; "
        f"generate_proposed_extension_main({str(project)!r}, {fingerprint!r})"
    )
    try:
        subprocess.Popen(
            [sys.executable, "-c", stub],
            cwd=repo_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "ABSOLOOP_EXTEND_PROPOSE": "sync"},
        )
    except OSError:
        try:
            lock.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            if lock.exists():
                lock.unlink()
        except OSError:
            pass


def generate_proposed_extension_main(project: str, fingerprint: str) -> int:
    """Worker entry: read ctx, generate, write cache, clear lock."""
    proj = pathlib.Path(project)
    abs_dir = proj / ".absoloop"
    ctx_path = abs_dir / "tmp" / "proposed-extension-ctx.json"
    lock = abs_dir / "tmp" / _PROPOSE_LOCK_NAME
    try:
        ctx = _read_json(ctx_path)
        if not ctx:
            return 1
        fp = fingerprint or str(ctx.get("fingerprint") or "")
        payload = _generate_proposed_extension(ctx, fp)
        _write_proposed_extension(abs_dir, payload)
        return 0
    except Exception:
        return 1
    finally:
        try:
            lock.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            if lock.exists():
                try:
                    lock.unlink()
                except OSError:
                    pass
        except OSError:
            pass


def _ensure_proposed_extension(
    project: pathlib.Path,
    *,
    abs_dir: pathlib.Path,
    ctx: dict,
    fingerprint: str,
) -> dict:
    """Return cached / generating / freshly-built Proposed Extension payload."""
    cached = _load_proposed_extension(abs_dir)
    if (
        cached.get("fingerprint") == fingerprint
        and cached.get("status") == "ready"
        and str(cached.get("note") or "").strip()
    ):
        return cached

    mode = (os.environ.get("ABSOLOOP_EXTEND_PROPOSE") or "").strip().lower()
    if mode in ("sync", "1", "true", "yes"):
        payload = _generate_proposed_extension(ctx, fingerprint)
        _write_proposed_extension(abs_dir, payload)
        return payload

    # Async path: seed a generating stub (heuristic preview) and kick worker.
    if cached.get("fingerprint") == fingerprint and cached.get("status") == "generating":
        return cached

    prompt = _build_extension_prompt(ctx)
    preview = _heuristic_extension_proposal(ctx)
    generating = _compose_extension_proposal(
        ctx=ctx,
        fingerprint=fingerprint,
        source="heuristic",
        engine="",
        note=preview["note"],
        rationale=preview.get("rationale") or "",
        analysis=next(
            (s["content"] for s in preview["chain"] if s["role"] == "analysis"),
            "",
        ),
        prompt=prompt,
        status="generating",
    )
    generating["preview"] = True
    ctx_out = {**ctx, "fingerprint": fingerprint}
    try:
        _atomic_write(abs_dir / "tmp" / "proposed-extension-ctx.json", ctx_out)
        _write_proposed_extension(abs_dir, generating)
        _kick_extension_worker(project, abs_dir, fingerprint)
    except OSError:
        pass
    return generating


def _build_run_results(
    project: pathlib.Path,
    *,
    abs_dir: pathlib.Path,
    state: dict,
    runtime: dict,
    source: dict,
    status: str,
    iteration: int,
    awaiting: bool,
    live_events: list[dict],
) -> dict:
    """CLI-parity critic / spend / stop snapshot for the Kanban Run Results panel."""
    empty = {
        "available": False,
        "updatedAt": None,
        "clock": None,
        "critic": None,
        "verdict": None,
        "spend": None,
        "mission": None,
        "proposedExtension": None,
    }
    if awaiting or not (abs_dir / "runtime.json").is_file():
        return empty

    cost_usd = float(source.get("cost_usd") or state.get("cost_usd") or 0)
    tokens_total = source.get("tokens_total")
    if tokens_total is None:
        tokens_total = state.get("tokens_total") or 0
    try:
        tokens_total = int(tokens_total or 0)
    except (TypeError, ValueError):
        tokens_total = 0
    max_cost = float(runtime.get("max_cost_usd") or 0)
    pct_used = int(round(100 * cost_usd / max_cost)) if max_cost > 0 else 0
    remaining = max(0.0, max_cost - cost_usd) if max_cost > 0 else 0.0
    stop_reason = str(
        state.get("stop_reason") or source.get("stop_reason") or ""
    ).strip() or None

    spend = {
        "costUsd": round(cost_usd, 4),
        "tokensTotal": tokens_total,
        "maxCostUsd": max_cost,
        "pctUsed": pct_used,
        "remainingUsd": round(remaining, 4),
    }

    mission = None
    status_u = str(status or "").upper()
    if status_u in _GATE_OR_TERMINAL_STATUSES or stop_reason:
        mission = {
            "status": status_u or str(state.get("status") or ""),
            "stopReason": stop_reason,
            "iteration": iteration,
            "costUsd": round(cost_usd, 4),
            "tokensTotal": tokens_total,
        }

    # Scope critic / live verdict to the active loop. Codex REJECT → extend
    # commonly leaves prior critic JSON + ledger/live rows in place while the
    # next run is still EXECUTING; without this boundary the panel keeps the
    # old REJECT paint until the new critic finishes.
    ledger_events = _read_jsonl_dicts(abs_dir / "ledger.jsonl")
    run_start = _current_run_start_ts(ledger_events)
    ledger_run = _latest_critic_ledger_run(abs_dir, run_start=run_start)
    result_rel = _latest_critic_result_rel(
        project, abs_dir, iteration, run_start=run_start)
    structured = _load_critic_structured(project, result_rel) if result_rel else {}
    live_verdict = _verdict_from_live(live_events, run_start=run_start)

    critic = None
    if ledger_run:
        wall = float(ledger_run.get("wall_seconds") or 0)
        run_cost = float(ledger_run.get("cost_usd") or 0)
        run_tokens = ledger_run.get("tokens")
        try:
            run_tokens = int(run_tokens) if run_tokens is not None else None
        except (TypeError, ValueError):
            run_tokens = None
        limit = ledger_run.get("limit_reached") or None
        exit_code = ledger_run.get("exit_code")
        outcome = (
            "finished"
            if exit_code in (0, None) and not limit
            else "FAILED"
        )
        turns = structured.get("turns")
        critic = {
            "wallSeconds": int(round(wall)) if wall else 0,
            "costUsd": round(run_cost, 4),
            "tokens": run_tokens,
            "turns": turns if isinstance(turns, int) else None,
            "outcome": outcome,
            "limitReached": str(limit) if limit else None,
            "ts": ledger_run.get("ts"),
            "engine": str(ledger_run.get("engine") or ""),
        }

    recommendation = (
        structured.get("recommendation")
        or live_verdict.get("recommendation")
        or ""
    )
    summary = structured.get("summary") or live_verdict.get("summary") or ""
    # Findings file from HOLD/REJECT path when structured payload is thin.
    findings_rel = str(state.get("latest_critic_findings") or "").strip()
    if findings_rel and not structured.get("blockingFindings"):
        findings_payload = _read_json(project / findings_rel)
        if findings_payload:
            if not recommendation:
                recommendation = str(
                    findings_payload.get("recommendation") or ""
                ).strip().upper()
            if not summary:
                summary = " ".join(
                    str(findings_payload.get("summary") or "").split())
            raw_findings = findings_payload.get("blocking_findings") or []
            if isinstance(raw_findings, list):
                structured = {
                    **structured,
                    "blockingFindings": [
                        str(f).strip() for f in raw_findings if str(f).strip()
                    ][:8],
                }

    verdict = None
    if recommendation or summary:
        verdict = {
            "recommendation": recommendation or "UNREADABLE",
            "summary": summary,
            "blockingFindings": list(structured.get("blockingFindings") or []),
            "ts": live_verdict.get("ts") or (ledger_run.get("ts") if ledger_run else None),
        }

    # Show the panel once we have spend with activity, a critic, a verdict,
    # or a terminal/gate mission status — mirrors when the CLI prints these lines.
    available = bool(
        critic
        or verdict
        or mission
        or (cost_usd > 0 and iteration > 0)
    )
    anchor_ts = (
        (critic or {}).get("ts")
        or (verdict or {}).get("ts")
        or state.get("updated_at")
        or source.get("heartbeat_ts")
        or source.get("ended_at")
        or time.time()
    )

    proposed_extension = None
    if available and mission and status_u in _GATE_OR_TERMINAL_STATUSES:
        objective = str(
            runtime.get("objective")
            or state.get("objective")
            or ""
        ).strip()
        loop_id = str(runtime.get("loop_id") or state.get("loop_id") or "")
        rec = str((verdict or {}).get("recommendation") or "").upper()
        summary = str((verdict or {}).get("summary") or "").strip()
        findings = list((verdict or {}).get("blockingFindings") or [])
        fingerprint = _extension_fingerprint(
            loop_id=loop_id,
            iteration=iteration,
            status=status_u,
            stop_reason=stop_reason or "",
            recommendation=rec,
            summary=summary,
        )
        ctx = {
            "objective": objective,
            "status": status_u,
            "stopReason": stop_reason or "",
            "recommendation": rec,
            "summary": summary,
            "blockingFindings": findings,
            "iteration": iteration,
            "costUsd": round(cost_usd, 4),
            "projectName": project.name,
            "loopId": loop_id,
            "engine": str(runtime.get("engine") or ""),
        }
        try:
            proposed_extension = _ensure_proposed_extension(
                project, abs_dir=abs_dir, ctx=ctx, fingerprint=fingerprint)
        except Exception:
            proposed_extension = _heuristic_extension_proposal(ctx)
            proposed_extension["fingerprint"] = fingerprint

    return {
        "available": available,
        "updatedAt": _iso(anchor_ts),
        "clock": _clock_label(anchor_ts),
        "critic": critic,
        "verdict": verdict,
        "spend": spend if available else None,
        "mission": mission,
        "proposedExtension": proposed_extension,
    }


def _teammate_focus(detail: str) -> str:
    """Extract the teammate's focus from a 'spawn teammate · …' detail line."""
    if not detail.startswith(TEAMMATE_SPAWN_PREFIX):
        return ""
    rest = detail[len(TEAMMATE_SPAWN_PREFIX):].strip()
    for tool in TEAMMATE_TOOL_NAMES:
        if rest == tool:
            return "general support"
        prefix = f"{tool}: "
        if rest.startswith(prefix):
            return rest[len(prefix):].strip() or "general support"
    return rest or "general support"


def quirky_teammate_name(focus: str, used: Optional[set[str]] = None) -> str:
    """Deterministic off-the-wall name themed to the teammate's focus."""
    low = focus.lower()
    pool: tuple[str, ...] = TEAMMATE_FALLBACK_NAMES
    for keywords, names in TEAMMATE_NAME_THEMES:
        if any(k in low for k in keywords):
            pool = names
            break
    seed = int(hashlib.sha1(low.encode("utf-8")).hexdigest()[:8], 16)
    for offset in range(len(pool)):
        candidate = pool[(seed + offset) % len(pool)]
        if used is None or candidate not in used:
            if used is not None:
                used.add(candidate)
            return candidate
    # Pool exhausted — number the overflow so every teammate stays distinct.
    n = 2
    base = pool[seed % len(pool)]
    while f"{base} {n}" in (used or set()):
        n += 1
    name = f"{base} {n}"
    if used is not None:
        used.add(name)
    return name


def _collect_teammates(events: list[dict]) -> dict[str, dict]:
    """Fold spawn events into a registry keyed by focus text."""
    teammates: dict[str, dict] = {}
    used_names: set[str] = set()
    for event in events:
        if str(event.get("kind") or "") != "tool":
            continue
        detail = str(event.get("detail") or "")
        focus = _teammate_focus(detail)
        if not focus:
            continue
        key = focus.lower()
        ts = event.get("ts")
        ts = float(ts) if isinstance(ts, (int, float)) else time.time()
        if key in teammates:
            record = teammates[key]
            record["spawns"] += 1
            record["last_ts"] = max(record["last_ts"], ts)
        else:
            teammates[key] = {
                "id": f"teammate-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:6]}",
                "name": quirky_teammate_name(focus, used_names),
                "focus": focus,
                "spawns": 1,
                "first_ts": ts,
                "last_ts": ts,
            }
    return teammates


_TOOL_VERBS = {
    "bash": "Ran",
    "read": "Read",
    "write": "Wrote",
    "edit": "Edited",
    "multiedit": "Edited",
    "strreplace": "Edited",
    "notebookedit": "Edited notebook",
    "grep": "Searched code for",
    "glob": "Looked for files matching",
    "ls": "Listed",
    "webfetch": "Fetched",
    "websearch": "Searched the web for",
    "todowrite": "Updated the plan",
}


def _humanize_shell(command: str) -> str:
    """Compress a raw shell command into a readable one-liner."""
    cmd = " ".join(command.split())
    # Drop redirection noise that dominates raw command dumps.
    cmd = re.sub(r"\s*(?:2>&1|>\s*/dev/null|2>\s*/dev/null)\s*", " ", cmd).strip()
    segments = [s.strip() for s in re.split(r"\s*(?:&&|\|\||;)\s*", cmd) if s.strip()]
    first = segments[0] if segments else cmd
    if len(first) > 100:
        first = first[:97] + "…"
    extra = len(segments) - 1
    label = f"Ran `{first}`"
    if extra > 0:
        label += f" (+{extra} more step{'s' if extra != 1 else ''})"
    return label


def _humanize_structured_output(payload: str) -> str:
    """Summarize a StructuredOutput JSON blob instead of dumping it raw."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        summary = str(data.get("summary") or data.get("status") or "").strip()
        parts = []
        if summary:
            parts.append(summary[:140])
        artifacts = data.get("changed_artifacts")
        if isinstance(artifacts, list) and artifacts:
            parts.append(f"{len(artifacts)} changed artifact"
                         + ("s" if len(artifacts) != 1 else ""))
        if parts:
            return "Reported results · " + " · ".join(parts)
        keys = ", ".join(list(data.keys())[:4])
        return f"Reported structured results ({keys})"
    return "Reported structured results"


# Codex (and occasionally Grok) emit the mission result JSON as a say line.
# Activity truncates to ~240 chars, so the blob is often incomplete.
_DONE_SUMMARY_PREFIX = re.compile(
    r'^\s*\{\s*"done"\s*:\s*(?:true|false)\s*,\s*"summary"\s*:\s*"',
    re.IGNORECASE,
)


def _summary_from_result_blob(detail: str) -> Optional[str]:
    """Return the human summary from a done/summary JSON blob, if present."""
    text = detail.strip()
    if not text.startswith("{"):
        return None
    data: Any = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        try:
            data, _ = json.JSONDecoder().raw_decode(text)
        except (json.JSONDecodeError, TypeError):
            data = None
    if isinstance(data, dict) and "done" in data and "summary" in data:
        summary = str(data.get("summary") or "").strip()
        return summary or None
    match = _DONE_SUMMARY_PREFIX.match(text)
    if not match:
        return None
    rest = text[match.end():]
    chars: list[str] = []
    escaped = False
    for ch in rest:
        if escaped:
            if ch == "n":
                chars.append("\n")
            elif ch == "t":
                chars.append("\t")
            elif ch == '"':
                chars.append('"')
            elif ch == "\\":
                chars.append("\\")
            else:
                chars.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            break
        chars.append(ch)
    summary = "".join(chars).strip()
    return summary or None


def _humanize_activity(kind: str, detail: str) -> str:
    """Turn raw narration lines into readable activity-feed messages."""
    detail = detail.strip()
    if kind in ("say", "think", "verdict"):
        summary = _summary_from_result_blob(detail)
        if summary:
            return summary
    if kind != "tool" or ": " not in detail:
        return detail
    tool, _, rest = detail.partition(": ")
    rest = rest.strip()
    low = tool.strip().lower()
    if low == "bash":
        return _humanize_shell(rest)
    if low == "structuredoutput":
        return _humanize_structured_output(rest)
    verb = _TOOL_VERBS.get(low)
    if verb:
        if len(rest) > 140:
            rest = rest[:137] + "…"
        return f"{verb} {rest}"
    return detail


def _task(tid: str, title: str, status: str, assignee: Optional[str],
          priority: str, phase: int, created: str, updated: str,
          deps: Optional[list[str]] = None,
          description: str = "",
          kind: str = "",
          details: Optional[dict] = None) -> dict:
    payload = {
        "id": tid,
        "title": title,
        "status": status,
        "assignee": assignee,
        "priority": priority,
        "dependencies": deps or [],
        "phase": phase,
        "createdAt": created,
        "updatedAt": updated,
        "description": description or "",
    }
    if kind:
        payload["kind"] = kind
    if details:
        # Structured card detail for the Kanban expand modal — only emit
        # keys that carry a real value so the payload stays lean.
        clean = {k: v for k, v in details.items() if v not in (None, "", [])}
        if clean:
            payload["details"] = clean
    return payload


_PAST_STATUS_LABELS = {
    "COMPLETED": "Completed",
    "AWAITING_APPROVAL": "Awaiting approval",
    "BLOCKED": "Blocked",
    "BUDGET_EXHAUSTED": "Budget exhausted",
    "REJECTED": "Rejected",
    "STOPPED": "Stopped",
}

_KANBAN_SESSION_NAME = "kanban-session.json"


def _kanban_session_path(abs_dir: pathlib.Path) -> pathlib.Path:
    return abs_dir / "zcomb" / _KANBAN_SESSION_NAME


def _list_archive_ids(abs_dir: pathlib.Path) -> list[str]:
    runs_dir = abs_dir / "runs"
    if not runs_dir.is_dir():
        return []
    return sorted(
        p.name for p in runs_dir.iterdir()
        if p.is_dir() and (p / "state.json").is_file()
    )


def ensure_kanban_session(
    abs_dir: pathlib.Path, *, reset: bool = False,
) -> dict:
    """Session baseline of archives already on disk when Kanban attached.

    Past-run Done cards only include loops archived *after* this baseline
    (i.e. Extend/Resume within this dashboard session).
    """
    path = _kanban_session_path(abs_dir)
    if not reset and path.is_file():
        data = _read_json(path)
        if isinstance(data.get("baselineArchiveIds"), list):
            return data
    payload = {
        "startedAt": time.time(),
        "baselineArchiveIds": _list_archive_ids(abs_dir),
    }
    _atomic_write(path, payload)
    return payload


def reset_kanban_session(project: pathlib.Path) -> dict:
    """Start a fresh Kanban session baseline (retarget / new dashboard bind)."""
    return ensure_kanban_session(project / ".absoloop", reset=True)


def _extension_notes_by_loop(abs_dir: pathlib.Path) -> dict[str, str]:
    """Map new loop_id → continuation note from the ledger."""
    notes: dict[str, str] = {}
    ledger_path = abs_dir / "ledger.jsonl"
    if not ledger_path.is_file():
        return notes
    try:
        lines = ledger_path.read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return notes
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "extension":
            continue
        loop_id = str(event.get("loop_id") or "").strip()
        note = str(event.get("note") or "").strip()
        if loop_id and note:
            notes[loop_id] = note
    return notes


def _archived_loop_summaries(
    abs_dir: pathlib.Path, current_loop_id: str,
) -> list[dict]:
    """Prior loops under .absoloop/runs/ (oldest first), excluding current."""
    runs_dir = abs_dir / "runs"
    if not runs_dir.is_dir():
        return []
    current = (current_loop_id or "").strip()
    notes = _extension_notes_by_loop(abs_dir)
    rows: list[dict] = []
    for entry in sorted(
        (p for p in runs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    ):
        if entry.name == current:
            continue
        state = _read_json(entry / "state.json")
        if not state:
            continue
        status_u = str(state.get("status") or "").upper() or "UNKNOWN"
        try:
            iteration = int(state.get("iteration") or 0)
        except (TypeError, ValueError):
            iteration = 0
        try:
            cost = float(state.get("cost_usd") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        focus = notes.get(entry.name) or ""
        rows.append({
            "loopId": entry.name,
            "status": status_u,
            "iteration": iteration,
            "costUsd": cost,
            "startedAt": state.get("started_at"),
            "focus": focus,
        })
    return rows


def _gist(text: Any, limit: int) -> str:
    """Whitespace-normalized snippet capped at ``limit`` chars."""
    blob = " ".join(str(text or "").split())
    if len(blob) <= limit:
        return blob
    return blob[: limit - 1].rstrip() + "…"


_ITER_PREFIX_RE = re.compile(r"^iteration\s+\d+\b[\s:,\-–—]*", re.IGNORECASE)


def _iteration_insights(abs_dir: pathlib.Path, iteration: int) -> dict:
    """Distill one iteration's builder result + critic verdict into card data.

    Reads the structured JSON the builder emits at the end of each
    iteration (``tmp/iteration-NNNN-agent-result.json``) plus the critic
    verdict for the same iteration when present.
    """
    tmp = abs_dir / "tmp"
    out: dict = {}
    agent = _read_json(tmp / f"iteration-{iteration:04d}-agent-result.json")
    summary = " ".join(str(agent.get("summary") or "").split())
    if summary:
        # Builders often open with "Iteration N ..." — redundant on a card
        # that already carries the iteration number.
        cleaned = _ITER_PREFIX_RE.sub("", summary).strip()
        if cleaned:
            cleaned = cleaned[0].upper() + cleaned[1:]
        out["summary"] = cleaned or summary
    changed = agent.get("changed_artifacts")
    if isinstance(changed, list) and changed:
        out["changedArtifacts"] = [str(p) for p in changed[:8]]
        out["filesChanged"] = len(changed)
    commands = agent.get("commands_run")
    if isinstance(commands, list) and commands:
        out["commandsRun"] = [_gist(c, 100) for c in commands[:6] if str(c).strip()]
    risks = agent.get("risks")
    if isinstance(risks, list) and risks:
        out["risks"] = [_gist(r, 180) for r in risks[:4] if str(r).strip()]
    critic = _read_json(tmp / f"iteration-{iteration:04d}-critic.json")
    # Some engines wrap the critic schema in a run envelope.
    nested = critic.get("structured_output")
    if isinstance(nested, dict) and nested.get("recommendation"):
        critic = nested
    rec = str(critic.get("recommendation") or "").strip()
    if rec:
        out["criticVerdict"] = rec.upper()
        csum = " ".join(str(critic.get("summary") or "").split())
        if csum:
            out["criticSummary"] = _gist(csum, 240)
    return out


def _report_excerpt(path: pathlib.Path, limit: int = 700) -> str:
    """Plain-text excerpt from an archived report.md for Kanban search."""
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Drop markdown chrome for denser search hits.
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            continue
        lines.append(stripped.lstrip("> ").lstrip("- ").lstrip("* "))
    blob = " ".join(lines)
    blob = re.sub(r"`+", "", blob)
    blob = re.sub(r"\s+", " ", blob).strip()
    if len(blob) > limit:
        return blob[: limit - 1] + "…"
    return blob


_STOP_REASON_LABELS = {
    "cost_budget": "cost budget hit",
    "wall_clock_budget": "wall-clock budget hit",
    "iteration_budget": "iteration budget hit",
    "accepted_pending_human_gate": "awaiting human gate",
    "repeated_failures": "repeated failures",
}


def _parse_report_insights(path: pathlib.Path) -> dict:
    """Distill an Absoloop report.md into card-ready data points.

    Extracts the at-a-glance metrics, the changed-files footprint, and a
    human outcome line from the run-arc tail so Kanban cards can show
    signal instead of raw markdown.
    """
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict = {}

    m = re.search(r"^> Generated (.+)$", text, re.MULTILINE)
    if m:
        raw = m.group(1).strip()
        out["generatedAt"] = raw
        try:
            dt = datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M")
            out["generatedShort"] = dt.strftime("%b %d %H:%M")
        except ValueError:
            out["generatedShort"] = raw

    m = re.search(
        r"^\|\s*Iterations\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", text, re.MULTILINE)
    if m:
        out["iterationsUsed"] = int(m.group(1))
        out["iterationsBudget"] = int(m.group(2))
    m = re.search(
        r"^\|\s*Spend\s*\|\s*\$([\d.,]+)(?:\s*\(([^)]*)\))?\s*\|\s*\$([\d.,]+)",
        text, re.MULTILINE)
    if m:
        try:
            out["spendUsd"] = float(m.group(1).replace(",", ""))
            out["budgetUsd"] = float(m.group(3).replace(",", ""))
        except ValueError:
            pass
        if m.group(2):
            out["tokens"] = m.group(2).strip()

    cf = re.search(
        r"^## Changed files\s*$(.*?)(?=^## |\Z)", text,
        re.MULTILINE | re.DOTALL)
    if cf:
        files = re.findall(r"^- `([^`]+)`", cf.group(1), re.MULTILINE)
        if files:
            out["filesChanged"] = len(files)
            areas: dict[str, int] = {}
            for f in files:
                parts = f.split("/")
                if len(parts) > 2:
                    area = "/".join(parts[:2])
                elif len(parts) == 2:
                    area = parts[0]
                else:
                    area = parts[0]
                areas[area] = areas.get(area, 0) + 1
            top = sorted(areas.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
            out["areas"] = [a for a, _ in top]

    arc = re.search(
        r"^## Run arc\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if arc:
        events = [
            ln.strip() for ln in arc.group(1).splitlines()
            if ln.strip().startswith("- ")
        ]
        approved = any("Human gate · approved" in e for e in events)
        delivered = any("Delivered" in e for e in events)
        rejected = any("Human gate · rejected" in e for e in events)
        stop = next((e for e in reversed(events) if "Stop ·" in e), "")
        if approved and delivered:
            out["outcome"] = "Approved & delivered"
        elif approved:
            out["outcome"] = "Human-approved"
        elif rejected:
            out["outcome"] = "Rejected at human gate"
        elif stop:
            sm = re.search(r"Stop · ([A-Z_]+)\*{0,2}\s*(?:—|-)\s*(\S+)", stop)
            if sm:
                status_word = sm.group(1).replace("_", " ").title()
                reason = _STOP_REASON_LABELS.get(
                    sm.group(2), sm.group(2).replace("_", " "))
                out["outcome"] = (
                    f"{status_word} — {reason}"
                    if reason and reason.lower() not in status_word.lower()
                    else status_word
                )
    return out


def _report_card_lines(
    insights: dict,
    fallback_status: str,
    title_outcome: str = "",
) -> list[str]:
    """Two concise data-point lines for a report/past-run card face.

    ``title_outcome`` is the outcome text already visible in the card
    title — skipped here so the face doesn't repeat itself.
    """
    outcome = insights.get("outcome") or fallback_status
    line1_bits = []
    if outcome and outcome != title_outcome:
        line1_bits.append(outcome)
    used = insights.get("iterationsUsed")
    if used is not None:
        budget = insights.get("iterationsBudget")
        line1_bits.append(f"{used}/{budget} iters" if budget else f"{used} iters")
    spend = insights.get("spendUsd")
    if spend:
        line1_bits.append(f"${spend:.2f}")
    files = insights.get("filesChanged")
    if files:
        line1_bits.append(f"{files} file" + ("s" if files != 1 else ""))
    lines = [" · ".join(line1_bits)] if line1_bits else []
    line2_bits = []
    areas = insights.get("areas") or []
    if areas:
        line2_bits.append(", ".join(areas))
    if insights.get("generatedShort"):
        line2_bits.append(insights["generatedShort"])
    if line2_bits:
        lines.append(" · ".join(line2_bits))
    return lines


def _discover_report_archives(abs_dir: pathlib.Path) -> list[dict]:
    """Loop report archives from ``reports/`` and ``runs/*/report.md``."""
    found: dict[str, dict] = {}

    reports_root = abs_dir / "reports"
    if reports_root.is_dir():
        for entry in sorted(reports_root.iterdir()):
            if not entry.is_dir():
                continue
            md = entry / "report.md"
            if not md.is_file():
                continue
            meta = _read_json(entry / "meta.json")
            lid = str(meta.get("loopId") or entry.name).strip() or entry.name
            found[lid] = {
                "loopId": lid,
                "path": md,
                "html": entry / "report.html",
                "status": str(meta.get("status") or ""),
                "objective": str(meta.get("objective") or ""),
                "missionId": str(meta.get("missionId") or ""),
            }

    runs_root = abs_dir / "runs"
    if runs_root.is_dir():
        for entry in sorted(runs_root.iterdir()):
            if not entry.is_dir():
                continue
            md = entry / "report.md"
            if not md.is_file():
                continue
            lid = entry.name
            state = _read_json(entry / "state.json")
            row = found.get(lid, {
                "loopId": lid,
                "path": md,
                "html": entry / "report.html",
                "status": "",
                "objective": "",
                "missionId": "",
            })
            row["path"] = md
            if (entry / "report.html").is_file():
                row["html"] = entry / "report.html"
            if state:
                row["status"] = row["status"] or str(state.get("status") or "")
                row["missionId"] = (
                    row["missionId"] or str(state.get("mission_id") or ""))
            found[lid] = row

    # Live report for the current loop (searchable while the mission is open).
    live_md = abs_dir / "report.md"
    if live_md.is_file():
        runtime = _read_json(abs_dir / "runtime.json")
        state = _read_json(abs_dir / "state.json")
        lid = str((runtime or {}).get("loop_id")
                  or (state or {}).get("mission_id")
                  or "current").strip()
        if lid and lid not in found:
            found[lid] = {
                "loopId": lid,
                "path": live_md,
                "html": abs_dir / "report.html",
                "status": str((state or {}).get("status") or "LIVE"),
                "objective": str((runtime or {}).get("objective") or ""),
                "missionId": str((state or {}).get("mission_id") or ""),
            }
    return list(found.values())


def _report_archive_tasks(
    abs_dir: pathlib.Path,
    *,
    now: str,
) -> list[dict]:
    """Searchable Done cards for archived (and live) mission reports."""
    cards: list[dict] = []
    for index, row in enumerate(_discover_report_archives(abs_dir), start=1):
        loop_id = row["loopId"]
        status = (row.get("status") or "").replace("_", " ").title() or "Archived"
        objective = " ".join(str(row.get("objective") or "").split())
        insights = _parse_report_insights(row["path"])
        outcome = insights.get("outcome") or status
        title = f"Report #{index} · {_gist(outcome, 42)}"
        # Card face: distilled data points, then searchable tail (clamped
        # away on the board, still hit by search).
        desc_parts = _report_card_lines(insights, status, title_outcome=outcome)
        tail_bits = [loop_id]
        if objective:
            tail_bits.append(_gist(objective, 100))
        desc_parts.append(" · ".join(tail_bits))
        excerpt = _report_excerpt(row["path"])
        if excerpt:
            desc_parts.append(excerpt)
        html_path = row.get("html")
        has_html = isinstance(html_path, pathlib.Path) and html_path.is_file()
        cards.append(_task(
            f"report-{loop_id}",
            title,
            "done",
            None,
            "low",
            6,
            now,
            now,
            description="\n".join(desc_parts),
            kind="report",
            details={
                "loopId": loop_id,
                "statusLabel": status,
                "outcome": outcome,
                "objective": objective,
                "missionId": str(row.get("missionId") or ""),
                "iterations": insights.get("iterationsUsed"),
                "maxIterations": insights.get("iterationsBudget"),
                "costUsd": insights.get("spendUsd"),
                "budgetUsd": insights.get("budgetUsd"),
                "tokens": insights.get("tokens"),
                "filesChanged": insights.get("filesChanged"),
                "areas": insights.get("areas"),
                "generatedAt": insights.get("generatedAt"),
                "excerpt": _report_excerpt(row["path"], limit=1600),
                "reportUrl": f"/api/report/{loop_id}",
                "reportFormat": "html" if has_html else "md",
            },
        ))
    return cards


def _past_run_tasks(
    abs_dir: pathlib.Path,
    *,
    current_loop_id: str,
    now: str,
) -> list[dict]:
    """One compact Done card per loop archived during this Kanban session."""
    session = ensure_kanban_session(abs_dir)
    baseline = {
        str(x) for x in (session.get("baselineArchiveIds") or []) if str(x)
    }
    cards: list[dict] = []
    session_rows = [
        row for row in _archived_loop_summaries(abs_dir, current_loop_id)
        if row["loopId"] not in baseline
    ]
    for index, row in enumerate(session_rows, start=1):
        loop_id = row["loopId"]
        status_u = row["status"]
        if status_u in _PAST_STATUS_LABELS:
            label = _PAST_STATUS_LABELS[status_u]
        elif status_u in ("EXECUTING", "FINAL_REVIEW", "RUNNING", "READY", "STARTING"):
            label = "Archived"
        else:
            label = status_u.replace("_", " ").title() or "Archived"
        title = f"Loop {index} · {label}"
        iters = int(row["iteration"] or 0)
        focus = " ".join(str(row.get("focus") or "").split())
        # Fold report body into description so Task Board search finds it.
        report_md = abs_dir / "runs" / loop_id / "report.md"
        if not report_md.is_file():
            report_md = abs_dir / "reports" / loop_id / "report.md"
        insights = _parse_report_insights(report_md)
        if not insights.get("iterationsUsed") and iters:
            insights["iterationsUsed"] = iters
        if not insights.get("spendUsd") and row["costUsd"]:
            insights["spendUsd"] = float(row["costUsd"])
        desc_lines = _report_card_lines(insights, label)
        if focus:
            desc_lines.append(f"Focus: {_gist(focus, 90)}")
        excerpt = _report_excerpt(report_md)
        desc_lines.append(loop_id)
        if excerpt:
            desc_lines.append(excerpt)
        desc = "\n".join(desc_lines)
        created = _iso(row.get("startedAt")) if row.get("startedAt") else now
        report_html = abs_dir / "runs" / loop_id / "report.html"
        if not report_html.is_file():
            report_html = abs_dir / "reports" / loop_id / "report.html"
        has_report = report_md.is_file() or report_html.is_file()
        cards.append(_task(
            f"run-{loop_id}",
            title,
            "done",
            None,
            "low",
            5,
            created,
            now,
            description=desc,
            kind="past_run",
            details={
                "loopId": loop_id,
                "statusLabel": label,
                "outcome": insights.get("outcome") or label,
                "iterations": insights.get("iterationsUsed") or iters,
                "maxIterations": insights.get("iterationsBudget"),
                "costUsd": insights.get("spendUsd")
                or (round(float(row["costUsd"] or 0), 2) or None),
                "budgetUsd": insights.get("budgetUsd"),
                "tokens": insights.get("tokens"),
                "filesChanged": insights.get("filesChanged"),
                "areas": insights.get("areas"),
                "generatedAt": insights.get("generatedAt"),
                "focus": focus,
                "excerpt": _report_excerpt(report_md, limit=1600),
                "reportUrl": f"/api/report/{loop_id}" if has_report else "",
                "reportFormat": "html" if report_html.is_file() else "md",
            },
        ))
    return cards


def _pipeline_status(name: str, live: bool, status: str, phase: str,
                     iteration: int, max_iter: int) -> str:
    """Map Absoloop lifecycle → Kanban column for a named pipeline task."""
    status = (status or "").upper()
    phase = (phase or "").lower()
    terminal_fail = status in ("BLOCKED", "BUDGET_EXHAUSTED", "REJECTED", "STOPPED")
    terminal_done = status == "COMPLETED"

    order = ["scaffold", "execute", "integrity", "critic", "gate", "deliver"]
    idx = order.index(name)

    # Which stage is "current"?
    if terminal_done:
        current = 5  # deliver done
    elif terminal_fail:
        current = 1 if name == "execute" else idx
    elif status == "AWAITING_APPROVAL":
        current = 4
    elif status == "FINAL_REVIEW" or "critic" in phase or "integrity" in phase:
        current = 3 if "critic" in phase else 2
    elif live and phase in ("preparing", "starting"):
        # Runner is still in its consolidated preparation block —
        # /goal work has not begun yet.
        current = 0
    elif live or status in ("EXECUTING", "READY"):
        current = 1
    else:
        current = 0 if iteration == 0 else 1

    if terminal_fail and name == "execute":
        return "failed"
    if idx < current:
        return "done"
    if idx == current:
        if name == "gate" and status == "AWAITING_APPROVAL":
            return "review"
        if name == "scaffold":
            if live and phase in ("preparing", "starting"):
                return "in_progress"
            return "done" if iteration > 0 or live or status else "in_progress"
        return "in_progress" if live or status in (
            "EXECUTING", "FINAL_REVIEW", "AWAITING_APPROVAL", "READY"
        ) else "assigned"
    if idx == current + 1:
        return "inbox"
    return "inbox"


_RESTART_MARKER_NAME = "restarting.json"
_RESTART_MARKER_TTL_SECONDS = 120.0


def _restart_marker_path(abs_dir: pathlib.Path) -> pathlib.Path:
    return abs_dir / "zcomb" / _RESTART_MARKER_NAME


def write_restart_marker(
    project: pathlib.Path,
    *,
    action: str = "",
    previous_loop_id: str = "",
    note: str = "",
) -> dict:
    """Signal ZComb that Extend/Resume just launched — keep UI in STARTING."""
    abs_dir = project / ".absoloop"
    payload = {
        "ts": time.time(),
        "action": str(action or ""),
        "previousLoopId": str(previous_loop_id or ""),
        "note": str(note or "")[:500],
    }
    _atomic_write(_restart_marker_path(abs_dir), payload)
    return payload


def clear_restart_marker(abs_dir: pathlib.Path) -> None:
    path = _restart_marker_path(abs_dir)
    try:
        path.unlink(missing_ok=True)  # type: ignore[call-arg]
    except TypeError:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _active_restart_marker(abs_dir: pathlib.Path) -> dict:
    """Fresh Extend/Resume marker, else {}."""
    data = _read_json(_restart_marker_path(abs_dir))
    if not data:
        return {}
    try:
        ts = float(data.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0 or (time.time() - ts) > _RESTART_MARKER_TTL_SECONDS:
        clear_restart_marker(abs_dir)
        return {}
    return data


def _awaiting_new_run(abs_dir: pathlib.Path, runtime: dict, state: dict,
                      monitor: dict, live: bool,
                      restart_marker: Optional[dict] = None) -> bool:
    """True when a mission is scaffolded/extended but the runner has not
    produced a live heartbeat for this loop yet.

    Covers: fresh `absoloop` scaffold (runtime only), post-extend gap
    (state.json archived), stale monitor leftover from a prior loop_id,
    and the brief COMPLETED→extend window (restart marker).
    """
    if not (abs_dir / "runtime.json").is_file():
        return False
    if live:
        return False
    if not (abs_dir / "state.json").is_file():
        return True
    loop_id = str(runtime.get("loop_id") or "").strip()
    monitor_loop = str(monitor.get("loop_id") or "").strip()
    if loop_id and monitor_loop and monitor_loop != loop_id:
        return True
    # Dashboard Extend/Resume just fired — keep awaiting even while the old
    # COMPLETED state.json is briefly still on disk.
    marker = restart_marker if restart_marker is not None else _active_restart_marker(abs_dir)
    if marker:
        prev = str(marker.get("previousLoopId") or "").strip()
        status_m = str(state.get("status") or "").upper()
        if prev and loop_id and loop_id != prev:
            return True
        if prev and (not loop_id or loop_id == prev) and (
            status_m in _GATE_OR_TERMINAL_STATUSES or status_m in ("", "READY")
        ):
            return True
        if not prev:
            return True
    # State exists but run never started (READY with no iterations / no start).
    status = str(state.get("status") or "").upper()
    iteration = int(state.get("iteration") or 0)
    if status in ("", "READY") and iteration == 0 and not state.get("started_at"):
        return True
    return False


def _run_key(loop_id: str, *, awaiting: bool, started: Any = None) -> str:
    """Stable identity the Kanban UI uses to reset on a new run/project."""
    lid = (loop_id or "mission").strip() or "mission"
    if awaiting:
        return f"{lid}:pending"
    if isinstance(started, (int, float)) and started > 0:
        return f"{lid}:{int(started)}"
    return f"{lid}:active"


def build_bridge_state(project: pathlib.Path) -> dict:
    """Translate Absoloop mission artifacts into ZComb dashboard state."""
    abs_dir = project / ".absoloop"
    tmp = abs_dir / "tmp"
    monitor = _read_json(tmp / "monitor.json")
    state = _read_json(abs_dir / "state.json")
    runtime = _read_json(abs_dir / "runtime.json")
    live = monitor_is_live(monitor)

    loop_id = str(runtime.get("loop_id") or "").strip()
    monitor_loop = str(monitor.get("loop_id") or "").strip()
    # Ignore leftover telemetry from a previous loop so the board does not
    # flash the old pipeline while a new run is activating.
    if loop_id and monitor_loop and monitor_loop != loop_id:
        monitor = {}
        live = False

    restart_marker = _active_restart_marker(abs_dir)
    awaiting = _awaiting_new_run(
        abs_dir, runtime, state, monitor, live,
        restart_marker=restart_marker,
    )

    source = monitor if live and monitor else state
    status = str(source.get("status") or state.get("status") or "READY")
    state_status = str(state.get("status") or "").strip()
    monitor_status = str(monitor.get("status") or "").strip() if monitor else ""
    # Status precedence (must match `absoloop approve`, which reads state.json):
    # 1) Active Extend/Resume restart hold → STARTING
    # 2) state.json terminal/gate (COMPLETED, AWAITING_APPROVAL, …) wins —
    #    never let a stale monitor AWAITING_APPROVAL re-enable Approve after
    #    the mission was already approved.
    # 3) monitor-only AWAITING_APPROVAL while state is still FINAL_REVIEW /
    #    EXECUTING (Codex wind-down) → enable Approve immediately.
    # 4) awaiting-new-run → STARTING
    if restart_marker and awaiting and not live:
        status = "STARTING"
        live = False
    elif state_status.upper() in _GATE_OR_TERMINAL_STATUSES:
        if restart_marker and not live:
            status = "STARTING"
            awaiting = True
            live = False
        else:
            status = state_status
            live = False
            awaiting = False
    elif monitor_status.upper() == "AWAITING_APPROVAL":
        if restart_marker and not live:
            status = "STARTING"
            awaiting = True
            live = False
        else:
            status = "AWAITING_APPROVAL"
            awaiting = False
    elif awaiting:
        status = "STARTING"
    awaiting_approval = status.upper() == "AWAITING_APPROVAL"

    # Drop the restart marker once the new/continued run has real telemetry.
    if restart_marker:
        prev_loop = str(restart_marker.get("previousLoopId") or "").strip()
        advanced = (
            live
            or (
                loop_id
                and prev_loop
                and loop_id != prev_loop
                and (abs_dir / "state.json").is_file()
                and state_status.upper() not in ("", "READY")
                and state_status.upper() not in _GATE_OR_TERMINAL_STATUSES
            )
        )
        if advanced:
            clear_restart_marker(abs_dir)
            restart_marker = {}
    phase = str(monitor.get("phase") or "")
    iteration = int(source.get("iteration") or state.get("iteration") or 0)
    max_iter = int(runtime.get("max_iterations") or 0)
    engine = str(monitor.get("engine") or runtime.get("engine")
                 or (runtime.get("builder") or {}).get("engine") or "builder")
    objective = str(runtime.get("objective") or "Absoloop mission").strip()
    objective_history = _objective_history(runtime, abs_dir)
    displayed_objective = _displayed_objective(objective_history, objective)
    mission_id = str(state.get("mission_id") or monitor.get("mission_id")
                     or runtime.get("mission_id") or loop_id or "mission")
    # Prefer durable started_at from state/monitor. Never invent time.time()
    # here — that made runKey (and the Kanban elapsed clock) reset every sync.
    started_raw = state.get("started_at")
    if not isinstance(started_raw, (int, float)) or started_raw <= 0:
        started_raw = monitor.get("started_at") if monitor else None
    if isinstance(started_raw, (int, float)) and started_raw > 0:
        started = float(started_raw)
    else:
        started = 0.0
    created = _iso(started) if started else _iso(time.time())
    updated = _iso(monitor.get("heartbeat_ts") or time.time())
    now = _iso(time.time())
    project_name = project.name or str(project)
    run_key = _run_key(loop_id or mission_id, awaiting=awaiting, started=started)

    ended_at = 0.0
    status_u_for_clock = str(status or "").upper()
    if status_u_for_clock in _GATE_OR_TERMINAL_STATUSES and not awaiting:
        for candidate in (
            state.get("ended_at"),
            state.get("updated_at"),
            monitor.get("ended_at") if monitor else None,
            monitor.get("heartbeat_ts") if monitor else None,
        ):
            if isinstance(candidate, (int, float)) and candidate > 0:
                ended_at = float(candidate)
                break

    # Per-loop wall times for the objective-history dropdown (next to loop id).
    if not awaiting:
        objective_history = _enrich_objective_history_elapsed(
            objective_history, abs_dir,
            current_loop_id=loop_id,
            current_started=started,
            current_ended=ended_at,
        )

    builder_id = "builder-01"
    critic_id = "critic-01"
    activity_detail = ""
    last = monitor.get("last_activity")
    if isinstance(last, dict):
        activity_detail = str(last.get("detail") or "").strip()

    live_events = _read_live_events(tmp / "live.jsonl")
    teammates = _collect_teammates(live_events)

    agents = [
        {
            "id": builder_id,
            "name": f"Builder ({engine})",
            "role": "Repair-loop builder — iterates toward the /goal contract",
            "status": _agent_status(live, "builder", monitor, state),
            "currentTask": (
                (activity_detail[:120] or f"iteration {iteration}")
                if live and _agent_status(
                    live, "builder", monitor, state) == "active"
                else None
            ),
            "metrics": {
                "tasksCompleted": max(0, iteration - (1 if live else 0)),
                "errors": int(state.get("repeated_failure_count")
                              or monitor.get("repeated_failure_count") or 0),
            },
        },
        {
            "id": critic_id,
            "name": "Critic",
            "role": "Independent adversarial reviewer — does not take the builder's word",
            "status": _agent_status(live, "critic", monitor, state),
            "currentTask": ("reviewing acceptance evidence"
                            if _agent_status(live, "critic", monitor, state) == "active"
                            else None),
            "metrics": {
                "tasksCompleted": 1 if status in (
                    "AWAITING_APPROVAL", "COMPLETED") else 0,
                "errors": 0,
            },
        },
    ]

    # Pipeline tasks + current iteration card
    cost_usd = float(source.get("cost_usd") or 0)
    iter_label = f"{iteration}/{max_iter}" if max_iter else f"iter {iteration}"
    execute_desc = f"Engine {engine} · iteration {iter_label}"
    if live and activity_detail:
        execute_desc += f" · now: {activity_detail[:110]}"
    critic_desc = f"Independent adversarial review of iteration {iteration} evidence"
    if teammates:
        critic_desc += (f" · {len(teammates)} teammate"
                        + ("s" if len(teammates) != 1 else "") + " spawned")
    gate_desc = ("Awaiting your decision — approve or reject from Mission Controls"
                 if status == "AWAITING_APPROVAL"
                 else "Human reviews the critic-passed result before delivery")
    deliver_desc = f"Hand off accepted work for {mission_id}"
    if cost_usd:
        deliver_desc += f" · ${cost_usd:.2f} spent"

    pipe_defs = [
        # One consolidated prep card: scaffold, skills, runner sync, and the
        # /goal contract all live here rather than as separate setup noise.
        ("scaffold", "Prepare mission — scaffold · skills · /goal contract",
         builder_id, "high", 0,
         [], f"{mission_id} · {objective[:150]}"),
        ("execute", f"Execute repair iterations ({iter_label})",
         builder_id, "high", 1, ["task-scaffold"], execute_desc),
        ("integrity", "Integrity check before critic", builder_id, "medium", 2,
         ["task-execute"],
         "Builder self-audits acceptance evidence against the /goal contract"),
        ("critic", "Adversarial critic review", critic_id, "high", 3,
         ["task-integrity"], critic_desc),
        ("gate", "Human approval gate", None, "high", 4, ["task-critic"],
         gate_desc),
        ("deliver", "Deliver accepted work", builder_id, "medium", 5,
         ["task-gate"], deliver_desc),
    ]
    mission_details = {
        "loopId": loop_id,
        "missionId": mission_id,
        "objective": objective,
        "engine": engine,
        "iteration": iteration,
        "maxIterations": max_iter or None,
        "costUsd": round(cost_usd, 2) or None,
        "statusLabel": status.replace("_", " ").title(),
    }
    tasks = []
    for name, title, assignee, priority, phase_n, deps, desc in pipe_defs:
        col = _pipeline_status(name, live, status, phase, iteration, max_iter)
        tasks.append(_task(
            f"task-{name}", title, col, assignee, priority, phase_n,
            created, updated, deps, description=desc,
            details=dict(mission_details)))

    # One card per completed/current iteration for kanban depth
    for i in range(1, max(iteration, 0) + 1):
        if i < iteration:
            col = "done"
        elif i == iteration and live and status in ("EXECUTING", "READY"):
            col = "in_progress"
        elif i == iteration and status == "FINAL_REVIEW":
            col = "review"
        elif i == iteration and status in ("BLOCKED", "BUDGET_EXHAUSTED", "REJECTED"):
            col = "failed"
        elif i == iteration and status in ("AWAITING_APPROVAL", "COMPLETED"):
            col = "done"
        else:
            col = "done"
        insights = _iteration_insights(abs_dir, i)
        is_live_iter = i == iteration and live and status in ("EXECUTING", "READY")
        iter_no = f"#{i}/{max_iter}" if max_iter else f"#{i}"

        # Title: lead with what the builder actually did, not the number.
        gist = _gist(insights.get("summary", ""), 72)
        if gist:
            iter_title = f"{iter_no} {gist}"
        elif is_live_iter and activity_detail:
            iter_title = f"{iter_no} {_gist(activity_detail, 72)}"
        else:
            iter_title = f"{iter_no} Repair pass toward objective"

        desc_lines = []
        if is_live_iter and activity_detail:
            desc_lines.append(f"Now: {_gist(activity_detail, 120)}")
        stat_bits = []
        if insights.get("filesChanged"):
            n = insights["filesChanged"]
            stat_bits.append(f"{n} artifact" + ("s" if n != 1 else ""))
        if insights.get("criticVerdict"):
            stat_bits.append(f"critic: {insights['criticVerdict'].lower()}")
        if stat_bits:
            desc_lines.append(" · ".join(stat_bits))
        if not desc_lines:
            desc_lines.append(_gist(objective, 110) or f"Repair pass {iter_no}")
        tasks.append(_task(
            f"iter-{i:04d}",
            iter_title,
            col, builder_id, "medium", 1, created, updated,
            ["task-execute"] if i == 1 else [f"iter-{i - 1:04d}"],
            description="\n".join(desc_lines),
            details={
                **mission_details,
                "iteration": i,
                "summary": insights.get("summary", ""),
                "changedArtifacts": insights.get("changedArtifacts") or [],
                "filesChanged": insights.get("filesChanged"),
                "commandsRun": insights.get("commandsRun") or [],
                "risks": insights.get("risks") or [],
                "criticVerdict": insights.get("criticVerdict", ""),
                "criticSummary": insights.get("criticSummary", ""),
                "nowLine": activity_detail if (
                    is_live_iter and activity_detail) else "",
            }))

    # Prior loops stay visible: one consolidated Done card each (not wiped
    # on Extend). Current loop keeps its live pipeline / waiting cards.
    past_run_tasks = _past_run_tasks(
        abs_dir, current_loop_id=loop_id, now=now)
    report_tasks = _report_archive_tasks(abs_dir, now=now)
    # Avoid duplicate cards when a past_run already covers the same loop.
    past_ids = {t["id"] for t in past_run_tasks}
    report_tasks = [
        t for t in report_tasks
        if f"run-{t['id'].removeprefix('report-')}" not in past_ids
    ]

    if not (abs_dir / "runtime.json").is_file():
        # Empty project — idle placeholder so the UI isn't blank
        tasks = [
            _task("task-waiting", "Waiting for an Absoloop mission in this project",
                  "inbox", None, "low", 0, now, now,
                  description="Run `absoloop` in this project to start a mission."),
        ]
        agents = [
            {**agents[0], "status": "idle", "currentTask": None,
             "metrics": {"tasksCompleted": 0, "errors": 0}},
            {**agents[1], "status": "idle", "currentTask": None,
             "metrics": {"tasksCompleted": 0, "errors": 0}},
        ]
        awaiting = True
        awaiting_approval = False
        run_key = _run_key("", awaiting=True)
        objective = ""
        objective_history = []
        displayed_objective = ""
        mission_id = ""
        loop_id = ""
        status = "IDLE"
        past_run_tasks = []
        report_tasks = []
    elif awaiting:
        # New / extended run activated — clear prior *current* pipeline and
        # wait for the runner, but keep archived loops as Done cards.
        wait_title = "Waiting for new Absoloop run to start"
        wait_focus = displayed_objective or objective
        wait_desc = (
            f"Project {project_name}"
            + (f" · {loop_id}" if loop_id else "")
            + (f" · {wait_focus[:140]}" if wait_focus else "")
            + " — Kanban refreshes when the runner becomes live."
        )
        tasks = [
            *past_run_tasks,
            *report_tasks,
            _task("task-waiting", wait_title, "inbox", None, "high", 0,
                  now, now, description=wait_desc),
            _task("task-objective",
                  (f"Objective: {wait_focus[:120]}" if wait_focus
                   else "Objective pending"),
                  "assigned", builder_id, "high", 0, now, now,
                  ["task-waiting"],
                  description="New mission objective loaded; waiting for "
                              "builder heartbeat."),
        ]
        agents = [
            {**agents[0], "status": "idle",
             "currentTask": "waiting for runner…",
             "metrics": {"tasksCompleted": 0, "errors": 0}},
            {**agents[1], "status": "idle", "currentTask": None,
             "metrics": {"tasksCompleted": 0, "errors": 0}},
        ]
        teammates = {}
        live_events = []
    else:
        # Live / settled mission: keep archives + report cards above the pipeline.
        prefix = [*past_run_tasks, *report_tasks]
        if prefix:
            tasks = [*prefix, *tasks]

    # Spawned teammates get first-class agent cards with quirky names.
    for record in teammates.values():
        recent = live and (time.time() - record["last_ts"]) < TEAMMATE_ACTIVE_SECONDS
        agents.append({
            "id": record["id"],
            "name": record["name"],
            "role": f"Spawned teammate — {record['focus'][:140]}",
            "status": "active" if recent else "done",
            "currentTask": record["focus"][:120] if recent else None,
            "metrics": {"tasksCompleted": record["spawns"], "errors": 0},
        })

    # Activity from live.jsonl
    activity: list[dict] = []
    for event in live_events:
        agent_name = str(event.get("agent") or "builder").lower()
        agent_id = critic_id if "critic" in agent_name else builder_id
        kind = str(event.get("kind") or "say")
        detail = str(event.get("detail") or kind)
        activity_type = KIND_TO_ACTIVITY.get(kind, "status_change")
        focus = _teammate_focus(detail) if kind == "tool" else ""
        record = teammates.get(focus.lower()) if focus else None
        if record:
            agent_id = record["id"]
            activity_type = "spawned"
            message = f"{record['name']} joins the team — {record['focus']}"
        else:
            message = _humanize_activity(kind, detail)
        activity.append({
            "timestamp": _iso(event.get("ts")),
            "agentId": agent_id,
            "type": activity_type,
            "message": message[:240],
        })

    if awaiting:
        activity = [{
            "timestamp": now,
            "agentId": builder_id,
            "type": "session_start",
            "message": (
                f"New run pending"
                + (f" · {project_name}" if project_name else "")
                + (f" · {loop_id}" if loop_id else "")
                + (f": {objective[:140]}" if objective else "")
            ),
        }]
    elif not activity:
        activity.append({
            "timestamp": now,
            "agentId": builder_id,
            "type": "session_start",
            "message": f"{mission_id}: {objective[:160]}",
        })

    # Metrics / phase progress
    done_tasks = sum(1 for t in tasks if t["status"] == "done")
    failed_tasks = sum(1 for t in tasks if t["status"] == "failed")
    total = len(tasks) or 1
    completion = 100 if status == "COMPLETED" else int(100 * done_tasks / total)
    if status == "AWAITING_APPROVAL":
        completion = max(completion, 85)

    phase_progress = []
    for phase_n, name in PHASE_PIPELINE:
        matching = [t for t in tasks if t["phase"] == phase_n]
        if not matching:
            prog = 0
        else:
            prog = int(100 * sum(1 for t in matching if t["status"] == "done")
                       / len(matching))
        phase_progress.append({"phase": phase_n, "name": name, "progress": prog})

    elapsed_h = max(1e-6, (time.time() - float(started or time.time())) / 3600.0)
    tasks_per_hour = round(max(0, iteration) / elapsed_h, 1)

    if awaiting:
        completion = 0
        tasks_per_hour = 0.0
        phase_progress = [
            {"phase": phase_n, "name": name, "progress": 0}
            for phase_n, name in PHASE_PIPELINE
        ]

    run_results = _build_run_results(
        project,
        abs_dir=abs_dir,
        state=state,
        runtime=runtime,
        source=source,
        status=status,
        iteration=0 if awaiting else iteration,
        awaiting=awaiting,
        live_events=live_events,
    )

    return {
        "agents": {"agents": agents},
        "tasks": {"tasks": tasks},
        "metrics": {
            "completionPct": completion,
            "errorRate": round(failed_tasks / total, 3),
            "tasksPerHour": tasks_per_hour,
            "phases": phase_progress,
            "missionId": mission_id,
            "loopId": loop_id,
            "objective": objective,
            "displayedObjective": displayed_objective,
            "objectiveHistory": objective_history,
            "status": status,
            "live": live,
            "awaitingRun": awaiting,
            "awaitingApproval": bool(awaiting_approval),
            "runKey": run_key,
            "projectName": project_name,
            # Mission wall-clock anchors for the header elapsed timer.
            "startedAt": started if started > 0 and not awaiting else None,
            "endedAt": ended_at if ended_at > 0 and not awaiting else None,
            # Active loop engine/model for the header (monitor → runtime).
            "engine": (
                str(monitor.get("engine") or "").strip()
                or (engine if engine != "builder" else "")
                or str(runtime.get("engine") or "").strip()
                or None
            ),
            "model": (
                str(monitor.get("model") or "").strip()
                or str(runtime.get("model") or "").strip()
                or None
            ),
            # Gear-menu prefs: next-loop engine/model + available engines.
            "settings": _bridge_settings(
                project, runtime=runtime, monitor=monitor,
                active_engine=engine if engine != "builder" else "",
            ),
        },
        "activity": activity,
        "riskAnalysis": {
            "summary": (
                (f"Awaiting new run · {project_name}"
                 + (f" · {loop_id}" if loop_id else "")
                 + (f" · {displayed_objective[:80]}"
                    if displayed_objective else ""))
                if awaiting else
                (f"Mission {mission_id} · status {status}"
                 + (f" · phase {phase}" if phase else ""))
            ),
            "iteration": 0 if awaiting else iteration,
            "maxIterations": max_iter,
            "costUsd": 0.0 if awaiting else float(source.get("cost_usd") or 0),
            "tokensTotal": 0 if awaiting else (source.get("tokens_total") or 0),
        },
        "runResults": run_results,
    }


_UI_SETTINGS_NAME = "ui-settings.json"
_ENGINE_ORDER = ("claude", "codex", "grok")
# Mission monitor surfaces: ZComb browser Kanban (default) or the classic
# terminal watcher (`absoloop watch`).
MONITOR_MODES = ("zcomb", "watch")
DEFAULT_MONITOR = "zcomb"


def ui_settings_path(project: pathlib.Path) -> pathlib.Path:
    return project / ".absoloop" / "zcomb" / _UI_SETTINGS_NAME


def _engine_on_path(name: str) -> bool:
    """True when an engine CLI is resolvable (PATH + common install dirs)."""
    if shutil.which(name):
        return True
    home = pathlib.Path.home()
    candidates = (
        home / ".local" / "bin" / name,
        home / "bin" / name,
        pathlib.Path("/opt/homebrew/bin") / name,
        pathlib.Path("/usr/local/bin") / name,
    )
    return any(path.is_file() and os.access(path, os.X_OK) for path in candidates)


def settings_catalog() -> dict[str, Any]:
    """Engines/models for the ZComb gear menu — PATH-available engines marked."""
    from absoloop_harness.models import ENGINE_MODELS, MODEL_LABELS

    engines: list[dict[str, Any]] = []
    for name in _ENGINE_ORDER:
        models = [
            {
                "id": model_id,
                "label": MODEL_LABELS.get(name, {}).get(model_id, model_id),
            }
            for model_id in ENGINE_MODELS.get(name, ())
        ]
        engines.append({
            "id": name,
            "label": name.capitalize(),
            "available": _engine_on_path(name),
            "models": models,
        })
    return {"engines": engines}


def load_loop_settings(project: pathlib.Path) -> dict[str, Any]:
    """Read-only settings payload for the gear menu (no file writes)."""
    project = project.expanduser().resolve()
    abs_dir = project / ".absoloop"
    runtime: dict[str, Any] = {}
    runtime_path = abs_dir / "runtime.json"
    if runtime_path.is_file():
        try:
            loaded = json.loads(runtime_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                runtime = loaded
        except (OSError, json.JSONDecodeError):
            runtime = {}
    monitor = None
    monitor_path = abs_dir / "tmp" / "monitor.json"
    if monitor_path.is_file():
        try:
            loaded = json.loads(monitor_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                monitor = loaded
        except (OSError, json.JSONDecodeError):
            monitor = None
    settings = _bridge_settings(
        project, runtime=runtime, monitor=monitor,
        active_engine=str((monitor or {}).get("engine")
                          or runtime.get("engine") or ""),
    )
    return {"ok": True, "settings": settings}


def read_ui_settings(project: pathlib.Path) -> dict[str, Any]:
    path = ui_settings_path(project)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def monitor_preference(project: pathlib.Path) -> str:
    """Preferred mission monitor: 'zcomb' (browser, default) or 'watch'."""
    pref = str(read_ui_settings(project).get("monitor") or "").strip().lower()
    return pref if pref in MONITOR_MODES else DEFAULT_MONITOR


def save_monitor_preference(project: pathlib.Path, monitor: str) -> dict[str, Any]:
    """Persist the monitor choice into ui-settings.json (merge, not rewrite)."""
    monitor = (monitor or "").strip().lower()
    if monitor not in MONITOR_MODES:
        return {"ok": False,
                "error": f"Unknown monitor '{monitor}' — use zcomb or watch"}
    project = project.expanduser().resolve()
    ui = read_ui_settings(project)
    ui["monitor"] = monitor
    ui["savedAt"] = time.time()
    out = ui_settings_path(project)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(ui, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not write ui-settings: {exc}"}
    return {"ok": True, "monitor": monitor}


def _absoloop_bin() -> str:
    """The absoloop CLI that owns this install (falls back to PATH)."""
    candidate = zcomb_home().parent / "bin" / "absoloop"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("absoloop") or "absoloop"


def open_watcher_terminal(project: pathlib.Path) -> dict[str, Any]:
    """Open a new terminal window running `absoloop watch -C <project>`.

    Best-effort per platform: Terminal.app on macOS, the first common
    emulator on Linux, `start cmd /k` on Windows.
    """
    project = project.expanduser().resolve()
    watch_cmd = [_absoloop_bin(), "watch", "-C", str(project)]
    try:
        if sys.platform == "darwin":
            shell_cmd = " ".join(shlex.quote(part) for part in watch_cmd)
            osa = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(
                ["osascript",
                 "-e", f'tell application "Terminal" to do script "{osa}"',
                 "-e", 'tell application "Terminal" to activate'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif os.name == "nt":
            shell_cmd = subprocess.list2cmdline(watch_cmd)
            subprocess.Popen(
                ["cmd", "/c", "start", "Absoloop Watch", "cmd", "/k", shell_cmd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            shell_cmd = " ".join(shlex.quote(part) for part in watch_cmd)
            launchers = (
                ["x-terminal-emulator", "-e", "sh", "-c", shell_cmd],
                ["gnome-terminal", "--", "sh", "-c", shell_cmd],
                ["konsole", "-e", "sh", "-c", shell_cmd],
                ["xterm", "-e", "sh", "-c", shell_cmd],
            )
            for launcher in launchers:
                if shutil.which(launcher[0]):
                    subprocess.Popen(
                        launcher,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    break
            else:
                return {
                    "ok": False,
                    "error": ("No terminal emulator found — run "
                              f"'absoloop watch -C {project}' manually"),
                }
    except OSError as exc:
        return {"ok": False, "error": f"Could not open terminal: {exc}"}
    return {
        "ok": True,
        "command": " ".join(shlex.quote(part) for part in watch_cmd),
        "message": "Watcher opened in a terminal window",
    }


def switch_monitor(project: pathlib.Path, monitor: str) -> dict[str, Any]:
    """Gear-menu monitor switch: persist the preference; when switching to
    the terminal watcher, also open it right away."""
    saved = save_monitor_preference(project, monitor)
    if not saved.get("ok"):
        return saved
    if saved["monitor"] == "watch":
        opened = open_watcher_terminal(project)
        if not opened.get("ok"):
            return {**saved, "ok": False,
                    "error": opened.get("error") or "Could not open watcher"}
        return {**saved,
                "message": ("Watcher opened — new missions will use the "
                            "terminal watcher"),
                "command": opened.get("command")}
    return {**saved,
            "message": "ZComb UI is the mission monitor again"}


def save_loop_settings(
    project: pathlib.Path,
    *,
    engine: str = "",
    model: str = "",
    theme: str = "",
) -> dict[str, Any]:
    """Persist theme + next-loop engine/model. Never stops a live runner.

    Writes ``.absoloop/zcomb/ui-settings.json`` and updates ``runtime.json``
    ``engine`` / ``model``. The active process keeps its CLI argv; the next
    ``resume`` / ``extend`` / start picks these up.
    """
    from absoloop_harness.models import resolve_model

    project = project.expanduser().resolve()
    abs_dir = project / ".absoloop"
    if not abs_dir.is_dir():
        return {"ok": False, "error": f"No .absoloop/ under {project}"}

    catalog = settings_catalog()
    available_ids = [e["id"] for e in catalog["engines"] if e.get("available")]
    available = set(available_ids)
    if not available_ids:
        return {
            "ok": False,
            "error": "No engine on PATH — install claude, codex, or grok",
        }

    runtime_path = abs_dir / "runtime.json"
    runtime: dict[str, Any] = {}
    if runtime_path.is_file():
        try:
            loaded = json.loads(runtime_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                runtime = loaded
        except (OSError, json.JSONDecodeError):
            runtime = {}

    ui = read_ui_settings(project)
    next_engine = (engine or ui.get("engine") or runtime.get("engine") or "").strip()
    if next_engine not in available:
        # Prefer an explicit request only when it's installed; else first available.
        if engine and engine.strip() in _ENGINE_ORDER and engine.strip() not in available:
            return {
                "ok": False,
                "error": f"Engine '{engine.strip()}' is not available on PATH",
            }
        next_engine = available_ids[0]

    next_model = resolve_model(next_engine, model or str(ui.get("model") or ""))
    next_theme = (theme or ui.get("theme") or "dark").strip().lower()
    if next_theme not in ("dark", "light"):
        next_theme = "dark"

    runtime["engine"] = next_engine
    runtime["model"] = next_model
    try:
        runtime_path.write_text(
            json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not update runtime.json: {exc}"}

    payload = {
        "theme": next_theme,
        "engine": next_engine,
        "model": next_model,
        "monitor": (str(ui.get("monitor") or "").strip().lower()
                    if str(ui.get("monitor") or "").strip().lower() in MONITOR_MODES
                    else DEFAULT_MONITOR),
        "savedAt": time.time(),
        "applyOn": "next_loop",
    }
    out = ui_settings_path(project)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not write ui-settings: {exc}"}

    return {
        "ok": True,
        "theme": next_theme,
        "engine": next_engine,
        "model": next_model,
        "applyOn": "next_loop",
        "message": "Saved — applies on the next loop (won't interrupt a running run)",
        "settings": _bridge_settings(
            project, runtime=runtime, monitor=None,
            active_engine=str(runtime.get("engine") or ""),
        ),
    }


def _bridge_settings(
    project: pathlib.Path,
    *,
    runtime: dict,
    monitor: Optional[dict],
    active_engine: str = "",
) -> dict[str, Any]:
    """Settings payload embedded in metrics for the gear menu."""
    from absoloop_harness.models import resolve_model

    catalog = settings_catalog()
    ui = read_ui_settings(project)
    monitor = monitor if isinstance(monitor, dict) else {}
    live_engine = str(
        monitor.get("engine") or active_engine
        or runtime.get("engine") or ""
    ).strip()
    live_model = str(monitor.get("model") or runtime.get("model") or "").strip()

    next_engine = str(
        ui.get("engine") or runtime.get("engine") or live_engine or ""
    ).strip()
    available_ids = [e["id"] for e in catalog["engines"] if e.get("available")]
    if next_engine not in available_ids and available_ids:
        next_engine = available_ids[0]
    next_model = resolve_model(
        next_engine,
        str(ui.get("model") or runtime.get("model") or live_model or ""),
    )
    theme = str(ui.get("theme") or "dark").strip().lower()
    if theme not in ("dark", "light"):
        theme = "dark"
    monitor = str(ui.get("monitor") or "").strip().lower()
    if monitor not in MONITOR_MODES:
        monitor = DEFAULT_MONITOR

    pending = bool(
        (ui.get("engine") or ui.get("model"))
        and (
            str(ui.get("engine") or "") != live_engine
            or str(ui.get("model") or "") != live_model
        )
    )

    return {
        "theme": theme,
        "engine": next_engine,
        "model": next_model,
        "monitor": monitor,
        "activeEngine": live_engine,
        "activeModel": live_model,
        "pendingNextLoop": pending,
        "engines": catalog["engines"],
        "savedAt": ui.get("savedAt"),
        "applyOn": "next_loop",
    }


def sync_state(project: pathlib.Path, state_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Write bridged ZComb state for `project`. Returns the state directory."""
    out = state_dir or project_state_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    bridged = build_bridge_state(project)
    _atomic_write(out / "agents.json", bridged["agents"])
    _atomic_write(out / "tasks.json", bridged["tasks"])
    _atomic_write(out / "metrics.json", bridged["metrics"])
    _atomic_write(out / "risk-analysis.json", bridged["riskAnalysis"])
    _atomic_write(out / "run-results.json", bridged.get("runResults") or {
        "available": False,
    })
    # activity.jsonl — rewrite from the bridged snapshot (source of truth is live.jsonl)
    activity_path = out / "activity.jsonl"
    tmp = activity_path.with_suffix(".jsonl.tmp")
    body = "\n".join(json.dumps(row, ensure_ascii=False)
                     for row in bridged["activity"])
    if body:
        body += "\n"
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(activity_path)

    # Keep a durable twin of the objective dropdown under .absoloop/reports/.
    metrics = bridged.get("metrics") or {}
    write_objectives_archive(
        project,
        metrics.get("objectiveHistory") or [],
        displayed_text=str(metrics.get("displayedObjective") or ""),
        project_name=str(metrics.get("projectName") or project.name),
    )
    return out


def _dashboard_sources_newer_than_dist(mon: pathlib.Path,
                                       dist_index: pathlib.Path) -> bool:
    """True when monitor source is newer than the last vite build."""
    if not dist_index.is_file():
        return True
    try:
        dist_mtime = dist_index.stat().st_mtime
    except OSError:
        return True
    watch_roots = (
        mon / "src",
        mon / "index.html",
        mon / "package.json",
        mon / "vite.config.ts",
        mon / "vite.config.js",
        mon / "tsconfig.json",
    )
    for root in watch_roots:
        if not root.exists():
            continue
        if root.is_file():
            try:
                if root.stat().st_mtime > dist_mtime:
                    return True
            except OSError:
                return True
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime > dist_mtime:
                    return True
            except OSError:
                return True
    return False


def ensure_dashboard_built(*, force: bool = False) -> None:
    """npm install + vite build under zcomb/monitor when needed."""
    mon = monitor_dir()
    if not mon.is_dir():
        raise FileNotFoundError(
            f"ZComb monitor not found at {mon}. Re-clone Absoloop with the zcomb/ tree.")
    if not shutil.which("node") or not shutil.which("npm"):
        raise RuntimeError(
            "Node.js 18+ (with npm) is required for absoloop --zcomb.\n"
            "  Install: https://nodejs.org  or  brew install node")
    node_modules = mon / "node_modules"
    dist_index = mon / "dist" / "index.html"
    if force or not node_modules.is_dir():
        print("  Installing ZComb dashboard dependencies…")
        subprocess.run(["npm", "install"], cwd=mon, check=True)
    needs_build = (
        force
        or not dist_index.is_file()
        or _dashboard_sources_newer_than_dist(mon, dist_index)
    )
    if needs_build:
        print("  Building ZComb dashboard…")
        subprocess.run(["npm", "run", "build"], cwd=mon, check=True)


def _port_in_use(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health",
                                    timeout=0.4) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _dashboard_api_version(port: int) -> int:
    """API version reported by a running dashboard (0 for pre-version builds)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health",
                                    timeout=1.0) as resp:
            if resp.status != 200:
                return 0
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return 0
    version = body.get("apiVersion")
    return version if isinstance(version, int) else 0


def _pids_listening_on(port: int) -> list[int]:
    """PIDs with a TCP listen socket on `port` (best-effort via lsof)."""
    try:
        out = subprocess.check_output(
            ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def stop_dashboard(port: int = DEFAULT_PORT, *, timeout: float = 5.0) -> bool:
    """Stop a ZComb dashboard listening on `port`. Returns True when free."""
    if not _port_in_use(port):
        return True
    pids = _pids_listening_on(port)
    if not pids:
        return False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_in_use(port):
            return True
        time.sleep(0.1)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return not _port_in_use(port)


def retarget_dashboard(project: pathlib.Path, port: int = DEFAULT_PORT,
                       state_dir: Optional[pathlib.Path] = None) -> bool:
    """Ask a running ZComb server to point at a new project/state dir.

    Returns True when the live dashboard acknowledged the retarget.
    """
    out = state_dir or project_state_dir(project)
    # New dashboard bind → new Kanban session (hide pre-existing archives).
    try:
        reset_kanban_session(project)
    except OSError:
        pass
    payload = json.dumps({
        "project": str(project.resolve()),
        "stateDir": str(out.resolve()),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/retarget",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode("utf-8"))
            return bool(body.get("ok"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False


def ensure_dashboard(project: pathlib.Path, state_dir: pathlib.Path,
                     port: int = DEFAULT_PORT) -> tuple[Optional[subprocess.Popen], str]:
    """Attach to a live dashboard or start/replace one for `project`.

    Prefer HTTP retarget when the server supports it. If the listener is an
    older build (stale API version, no `/api/retarget`) or retarget fails,
    replace the process so new runs do not stay stuck on stale state or hit
    404s on endpoints the current UI expects.

    Returns `(proc_or_None, status)` where status is one of:
    `started`, `retargeted`, `restarted`, `failed`.
    """
    try:
        reset_kanban_session(project)
    except OSError:
        pass
    if not _port_in_use(port):
        proc = start_server(state_dir, port, project=project)
        if wait_ready(port, timeout=20):
            return proc, "started"
        return proc, "failed"

    if (_dashboard_api_version(port) >= DASHBOARD_API_VERSION
            and retarget_dashboard(project, port=port, state_dir=state_dir)):
        return None, "retargeted"

    # Pre-retarget servers answer /api/health but 404 on /api/retarget.
    if not stop_dashboard(port):
        return None, "failed"
    proc = start_server(state_dir, port, project=project)
    if wait_ready(port, timeout=20):
        return proc, "restarted"
    return proc, "failed"


def start_server(state_dir: pathlib.Path, port: int,
                 project: Optional[pathlib.Path] = None) -> subprocess.Popen:
    mon = monitor_dir()
    env = os.environ.copy()
    env["ZCOMB_PORT"] = str(port)
    env["ZCOMB_STATE_DIR"] = str(state_dir.resolve())
    env["PORT"] = str(port)
    if project is not None:
        env["ZCOMB_PROJECT"] = str(project.resolve())
    env.setdefault("ABSOLOOP_HOME", str(zcomb_home().parent))
    log_path = state_dir.parent / "dashboard.log"
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "a", encoding="utf-8")
    proc = subprocess.Popen(
        ["node", "server.js"],
        cwd=mon,
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._absoloop_log = log_f  # type: ignore[attr-defined]
    return proc


def wait_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_in_use(port):
            return True
        time.sleep(0.25)
    return False


def open_browser(url: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
    except Exception:
        pass


def _bridge_loop_main(project: str, state_dir: str, interval: float) -> None:
    """Entry for the detached bridge subprocess (keeps Kanban fresh)."""
    proj = pathlib.Path(project)
    out = pathlib.Path(state_dir)
    while True:
        try:
            sync_state(proj, out)
        except Exception:
            pass
        # Poll faster while Extend/Resume is activating so the UI flips quickly.
        abs_dir = proj / ".absoloop"
        delay = 0.35 if _active_restart_marker(abs_dir) else max(0.5, interval)
        time.sleep(delay)


def spawn_background(project: pathlib.Path, *, port: int = DEFAULT_PORT,
                     open_url: bool = True,
                     interval: float = 2.0) -> Optional[subprocess.Popen]:
    """Best-effort: sync, start server + bridge loop, open browser.

    Used when `absoloop … --zcomb` launches a mission — never raises into
    the mission path. Returns the dashboard Popen (or None if already up).

    When a dashboard is already listening, retarget it to this project so the
    open Kanban refreshes for the new objective/run instead of staying on
    stale state.
    """
    try:
        ensure_dashboard_built()
        # Sync first so the UI's next poll sees awaiting-run / new objective
        # immediately (before the runner has written monitor.json).
        state_dir = sync_state(project)
        proc, status = ensure_dashboard(project, state_dir, port)
        if status == "failed":
            print(f"  warning: ZComb dashboard did not become ready on :{port}",
                  file=sys.stderr)
            return proc
        if status == "retargeted":
            print(f"  ZComb UI retargeted → {project.name}")
        elif status == "restarted":
            print(f"  ZComb UI restarted → {project.name}")
        # Detached bridge keeps state files fresh while the mission runs.
        subprocess.Popen(
            [sys.executable, "-c",
             "from absoloop_harness.zcomb import _bridge_loop_main; "
             f"_bridge_loop_main({str(project)!r}, {str(state_dir)!r}, {interval!r})"],
            cwd=str(zcomb_home().parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "ABSOLOOP_HOME": str(zcomb_home().parent)},
        )
        url = f"http://localhost:{port}"
        print(f"  ZComb UI → {url}")
        if open_url:
            open_browser(url)
        return proc
    except Exception as exc:
        print(f"  warning: could not start ZComb UI ({exc})", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Web Mission Briefing — the browser Launch modal replaces the terminal card.
#
# Handshake (all files live in the project's ZComb state dir, which the
# dashboard server also reads/writes):
#   launch.json            CLI writes {status: pending, request} → the UI
#                          shows the Launch modal → the server rewrites it
#                          as {status: submitted, submission} (or cancelled)
#                          → the CLI marks it launched/aborted/error.
#   launch-heartbeat.json  CLI touches this every few seconds while waiting,
#                          so the UI never resurrects a stale request after
#                          a killed CLI.
# ---------------------------------------------------------------------------

LAUNCH_FILE_NAME = "launch.json"
LAUNCH_HEARTBEAT_NAME = "launch-heartbeat.json"
LAUNCH_HEARTBEAT_INTERVAL = 4.0


def launch_file_path(state_dir: pathlib.Path) -> pathlib.Path:
    return state_dir / LAUNCH_FILE_NAME


def read_launch_state(state_dir: pathlib.Path) -> dict:
    return _read_json(launch_file_path(state_dir))


def write_launch_state(state_dir: pathlib.Path, payload: dict) -> None:
    _atomic_write(launch_file_path(state_dir), payload)


def touch_launch_heartbeat(state_dir: pathlib.Path) -> None:
    _atomic_write(state_dir / LAUNCH_HEARTBEAT_NAME, {"ts": time.time()})


def mark_launch_status(state_dir: pathlib.Path, status: str, **extra: Any) -> None:
    """Update launch.json's status in place (request/submission preserved)."""
    data = read_launch_state(state_dir)
    data["status"] = status
    data["ts"] = time.time()
    data.update(extra)
    write_launch_state(state_dir, data)


def begin_web_briefing(project: pathlib.Path, request: dict,
                       *, port: int = DEFAULT_PORT) -> Optional[pathlib.Path]:
    """Open the dashboard with a pending Launch request for `project`.

    Builds/starts (or retargets) the ZComb server, writes the launch
    request, and opens the browser on the Launch modal. Returns the state
    directory to poll, or None when the dashboard could not be started.
    """
    ensure_dashboard_built()
    state_dir = sync_state(project)
    write_launch_state(state_dir, {
        "status": "pending",
        "ts": time.time(),
        "request": request,
    })
    touch_launch_heartbeat(state_dir)
    proc, status = ensure_dashboard(project, state_dir, port)
    if status == "failed":
        write_launch_state(state_dir, {"status": "error",
                                       "error": "dashboard failed to start"})
        if proc is not None and proc.poll() is None:
            proc.terminate()
        return None
    open_browser(f"http://localhost:{port}")
    return state_dir


def wait_for_web_briefing(state_dir: pathlib.Path,
                          *, poll: float = 0.5) -> dict:
    """Block until the browser submits or cancels the Launch modal.

    Returns the final launch.json payload; Ctrl-C yields
    ``{"status": "aborted"}`` (and marks the file so the UI closes).
    """
    last_beat = 0.0
    try:
        while True:
            data = read_launch_state(state_dir)
            status = str(data.get("status") or "")
            if status in ("submitted", "cancelled"):
                return data
            if status not in ("pending",):
                return {"status": "error",
                        "error": f"launch request lost (status={status or 'gone'})"}
            now = time.time()
            if now - last_beat >= LAUNCH_HEARTBEAT_INTERVAL:
                touch_launch_heartbeat(state_dir)
                last_beat = now
            time.sleep(poll)
    except KeyboardInterrupt:
        mark_launch_status(state_dir, "aborted")
        return {"status": "aborted"}


def zcomb_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="absoloop zcomb",
        description=(
            "ZComb Kanban dashboard for monitoring an Absoloop mission. "
            "Requires Node.js 18+. For briefing + launch with Kanban, use "
            "'absoloop --zcomb' (same process as bare 'absoloop')."
        ),
        epilog=(
            "examples:\n"
            "  absoloop --zcomb\n"
            "  absoloop \"Make all tests pass\" --zcomb\n"
            "  absoloop zcomb -C ./my-mission --port 3141\n"
            "\n"
            "Dashboard: http://localhost:3141  ·  state: .absoloop/zcomb/state/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-C", "--project", default=".",
                        help="project directory (default: current directory)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"dashboard port (default: {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the browser")
    parser.add_argument("--once", action="store_true",
                        help="sync state once and exit (no server)")
    parser.add_argument("--build", action="store_true",
                        help="force npm install + vite rebuild")
    parser.add_argument("-n", "--interval", type=float, default=2.0,
                        help="bridge sync interval in seconds (default: 2)")
    args = parser.parse_args(argv)

    project = pathlib.Path(args.project).expanduser().resolve()
    if not project.is_dir():
        print(f"error: project directory not found: {project}", file=sys.stderr)
        return 1

    try:
        ensure_dashboard_built(force=args.build)
    except (RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    state_dir = sync_state(project)
    print(f"  Bridged Absoloop → ZComb state at {state_dir}")

    if args.once:
        return 0

    port = args.port
    url = f"http://localhost:{port}"
    if _port_in_use(port):
        print(f"  Dashboard already running at {url}")
    else:
        print(f"  Starting ZComb dashboard on {url} …")
    proc, status = ensure_dashboard(project, state_dir, port)
    if status == "failed":
        print(f"error: dashboard failed to start (see "
              f"{state_dir.parent / 'dashboard.log'})", file=sys.stderr)
        if proc is not None and proc.poll() is None:
            proc.terminate()
        return 1
    if status == "retargeted":
        print(f"  Retargeted dashboard → {project}")
    elif status == "restarted":
        print(f"  Replaced stale dashboard → {project}")
    else:
        print(f"  ✔ Dashboard ready at {url}")

    if not args.no_browser:
        open_browser(url)

    print(f"  Syncing every {args.interval:g}s · Ctrl-C to stop")
    try:
        while True:
            sync_state(project, state_dir)
            if proc is not None and proc.poll() is not None:
                print("error: dashboard process exited", file=sys.stderr)
                return 1
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        print("\n  Stopping ZComb bridge.")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return 0


__all__ = [
    "extract_zcomb_flag",
    "build_bridge_state",
    "sync_state",
    "zcomb_command",
    "spawn_background",
    "begin_web_briefing",
    "wait_for_web_briefing",
    "read_launch_state",
    "write_launch_state",
    "mark_launch_status",
    "touch_launch_heartbeat",
    "launch_file_path",
    "retarget_dashboard",
    "ensure_dashboard",
    "stop_dashboard",
    "ensure_dashboard_built",
    "write_restart_marker",
    "clear_restart_marker",
    "ensure_kanban_session",
    "reset_kanban_session",
    "settings_catalog",
    "read_ui_settings",
    "load_loop_settings",
    "save_loop_settings",
    "monitor_preference",
    "save_monitor_preference",
    "switch_monitor",
    "open_watcher_terminal",
    "DEFAULT_PORT",
    "DEFAULT_MONITOR",
    "MONITOR_MODES",
]
