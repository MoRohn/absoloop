"""Absoloop Mission Briefing UX — unique, fast, review-before-launch.

The interactive path collapses to: say what you want → glance at the
briefing card → hit Enter. Editing is optional single-key tweaks
(``o`` / ``e`` / ``m`` / ``d`` / ``n`` / ``g``), never a gauntlet of
confirmations. Color respects ``NO_COLOR`` and non-TTY stdout.

Consumed by ``bin/absoloop`` after scaffolding arguments are resolved;
``-y`` / ``--yes`` skips the card entirely.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Color (NO_COLOR / non-tty aware)
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
# Palette borrowed from the Absoloop infinity ribbon — not purple-default AI.
_CYAN = "\x1b[38;2;0;205;225m"
_GREEN = "\x1b[38;2;40;205;120m"
_GOLD = "\x1b[38;2;255;190;15m"
_ORANGE = "\x1b[38;2;255;145;20m"
_PINK = "\x1b[38;2;255;25;95m"
_BLUE = "\x1b[38;2;0;105;255m"


def tint(style: str, text: str) -> str:
    # First styled print arms stdout so a cp1252 console cannot kill the CLI.
    from .platform_util import ensure_stdio
    ensure_stdio()
    if not _USE_COLOR:
        return text
    codes = {
        "bold": _BOLD, "dim": _DIM, "cyan": _CYAN, "green": _GREEN,
        "gold": _GOLD, "orange": _ORANGE, "pink": _PINK, "blue": _BLUE,
        "ok": _GREEN, "warn": _ORANGE, "err": _PINK, "accent": _CYAN,
    }
    code = codes.get(style, "")
    return f"{code}{text}{_RESET}" if code else text


# ---------------------------------------------------------------------------
# Mission flavor — short, memorable lines keyed to classify_mission kinds
# ---------------------------------------------------------------------------

FLAVOR: Dict[str, Tuple[str, str]] = {
    "tests": (
        "Red to green",
        "Turn failing tests into proof. No shortcuts, no skipped specs.",
    ),
    "bugfix": (
        "Bug hunt",
        "Reproduce, root-cause, fix. Leave a trap so it cannot return.",
    ),
    "feature": (
        "Build mode",
        "Thin end-to-end slice first. Make it real before making it fancy.",
    ),
    "refactor": (
        "Surgical pass",
        "Same behavior, cleaner bones. Checks stay green the whole way.",
    ),
    "perf": (
        "Speed run",
        "Measure, change one hotspot, re-measure. Feelings are not data.",
    ),
    "docs": (
        "Truth in ink",
        "Every example must run. If you wrote it, you verified it.",
    ),
    "general": (
        "Open mission",
        "Synergetic Loops. Evidence wins. The critic does not take your word.",
    ),
}

DELIVERY_BLURBS = {
    "local": "stay in the working tree (you commit)",
    "git":   "land on branch absoloop/<loop_id>",
    "out":   "export a delivery pack under out/<loop_id>/",
}


@dataclass
class Briefing:
    """Everything the operator sees and confirms before the loop starts."""
    target: str                          # absolute path or display path
    target_name: str
    adopting: bool
    objective: str
    delivery: str
    engine: str
    kinds: List[str]
    model: str = ""                      # engine model id / alias
    max_iterations: int = 50
    max_cost_usd: float = 50.0
    max_wall_hours: float = 3.0
    engines_available: Tuple[str, ...] = ()


def slug_from_objective(objective: str, fallback: str = "mission") -> str:
    """Derive a short project folder name from free-text objective."""
    words = re.findall(r"[A-Za-z0-9]+", objective.lower())
    stop = {"the", "a", "an", "all", "and", "or", "to", "of", "in", "for",
            "with", "make", "fix", "add", "this", "that", "please"}
    kept = [w for w in words if w not in stop][:4]
    slug = "-".join(kept) if kept else fallback
    return slug[:40] or fallback


def looks_like_objective(token: str) -> bool:
    """Positional args with spaces or sentence-y shape are objectives,
    not project names — so `absoloop "fix the tests"` Just Works."""
    if not token:
        return False
    if " " in token or "\n" in token:
        return True
    if len(token) > 48:
        return True
    if token.endswith((".", "!", "?")):
        return True
    return False


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n" + tint("dim", "Aborted."))
        raise SystemExit(1)
    return answer or default


def pick(prompt: str, options: Sequence[str], default: str,
         labels: Optional[Dict[str, str]] = None,
         name_width: int = 8) -> str:
    """Single-key / number / name picker. Enter keeps the default."""
    labels = labels or {}
    width = max(name_width, max((len(name) for name in options), default=8))
    print(tint("bold", prompt))
    for index, name in enumerate(options, 1):
        mark = tint("ok", "●") if name == default else tint("dim", "○")
        extra = labels.get(name, "")
        tag = tint("gold", "  ← default") if name == default else ""
        print(f"  {mark} {index}. {name:<{width}} {tint('dim', extra)}{tag}")
    while True:
        answer = ask("Your pick", default)
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        if answer in options:
            return answer
        print(tint("warn", f"  pick 1–{len(options)} or one of: {', '.join(options)}"))


def pick_model(engine: str, current: str = "") -> str:
    """Curated model picker for an engine, with a free-text custom escape."""
    from .models import default_model, model_labels, models_for, resolve_model

    catalog = list(models_for(engine))
    if not catalog:
        typed = ask("  Model id", current or "")
        return typed or current or ""
    current = resolve_model(engine, current)
    options = list(catalog)
    if current not in options:
        options.insert(0, current)
    options.append("custom")
    labels = model_labels(engine)
    labels.setdefault("custom", "type a model id / alias")
    if current == default_model(engine):
        labels[current] = (labels.get(current, "") + " · Absoloop default").strip(" ·")
    choice = pick("  Model", options, current, labels, name_width=14)
    if choice == "custom":
        typed = ask("  Model id", current)
        return typed or current
    return choice


def render_card(brief: Briefing) -> str:
    """The one-screen Mission Briefing — review this, then launch."""
    from .models import default_model, resolve_model

    primary = brief.kinds[0] if brief.kinds else "general"
    title, tagline = FLAVOR.get(primary, FLAVOR["general"])
    kinds_label = " · ".join(brief.kinds) if brief.kinds else "general"
    where = "adopt" if brief.adopting else "create"
    engine_ok = brief.engine in brief.engines_available
    engine_mark = tint("ok", "ready") if engine_ok else tint("err", "not on PATH")
    delivery = DELIVERY_BLURBS.get(brief.delivery, brief.delivery)
    model = resolve_model(brief.engine, brief.model)
    model_note = ""
    if model == default_model(brief.engine):
        model_note = tint("dim", "  (best available)")

    width = 58
    rule = "─" * width
    # Show '.' when cwd-scoped so rename/retarget stays unambiguous.
    folder = pathlib_name(brief.target)
    proj_label = (f".  {tint('dim', f'({folder})')}" if brief.target_name == "."
                  else brief.target_name)
    lines = [
        "",
        tint("accent", "∞") + " " + tint("bold", "MISSION BRIEFING")
        + tint("dim", f"  ·  {title}"),
        tint("dim", rule),
        f"  {tint('gold', 'objective')}  {brief.objective}",
        f"  {tint('dim', tagline)}",
        "",
        f"  {tint('cyan', 'project')}   {proj_label}  "
        f"{tint('dim', f'({where})')}",
        f"  {tint('cyan', 'profile')}   {kinds_label}",
        f"  {tint('cyan', 'engine')}    {brief.engine}  {engine_mark}",
        f"  {tint('cyan', 'model')}     {model}{model_note}",
        f"  {tint('cyan', 'delivery')}  {brief.delivery}  "
        f"{tint('dim', delivery)}",
        f"  {tint('cyan', 'budgets')}   {brief.max_iterations} iterations · "
        f"${brief.max_cost_usd:g} · {brief.max_wall_hours:g}h wall",
        tint("dim", rule),
        "  " + tint("bold", "Enter") + " launch   "
        + tint("dim", "o") + " objective   "
        + tint("dim", "e") + " engine   "
        + tint("dim", "m") + " model",
        "  " + tint("dim", "d") + " delivery   "
        + tint("dim", "n") + " rename   "
        + tint("dim", "g") + " preview /goal   "
        + tint("dim", "q") + " abort",
        "",
    ]
    return "\n".join(lines)


def pathlib_name(path: str) -> str:
    import pathlib
    return pathlib.Path(path).name or path


def review_loop(
    brief: Briefing,
    *,
    engines: Sequence[str],
    engine_labels: Dict[str, str],
    deliveries: Sequence[str],
    classify: Callable[[str], List[str]],
    preview_goal: Optional[Callable[[Briefing], None]] = None,
) -> Optional[Briefing]:
    """Show the briefing card until the operator launches, edits, or quits.

    Returns the final Briefing to launch, or None if aborted.
    """
    current = brief
    while True:
        current = replace(current, kinds=classify(current.objective) or ["general"])
        print(render_card(current))
        try:
            key = input(tint("bold", "  ▶ ") + tint("dim", "ready? ") ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n" + tint("dim", "Aborted."))
            return None

        if key in ("", "y", "yes", "l", "launch", "go", "start"):
            if current.engine not in current.engines_available:
                print(tint("err",
                           f"  '{current.engine}' is not on PATH — pick another "
                           "engine (e) or install it first."))
                continue
            print()
            print(tint("ok", "  ∞  Locking in.")
                  + tint("dim", " Preparing the workspace…"))
            print()
            return current
        if key in ("q", "quit", "abort", "no"):
            print(tint("dim", "  Mission scrubbed."))
            return None
        if key in ("o", "objective", "obj"):
            new_obj = ask("  New objective", current.objective)
            if new_obj:
                current = replace(current, objective=new_obj)
            continue
        if key in ("e", "engine"):
            from .models import default_model

            new_engine = pick("  Engine", engines, current.engine, engine_labels)
            # Switching engines resets to that engine's best model unless the
            # operator already pinned a custom id for the same engine.
            new_model = (current.model if new_engine == current.engine
                         else default_model(new_engine))
            current = replace(current, engine=new_engine, model=new_model)
            continue
        if key in ("m", "model"):
            current = replace(
                current, model=pick_model(current.engine, current.model))
            continue
        if key in ("d", "delivery", "deliver"):
            current = replace(
                current,
                delivery=pick("  Delivery", deliveries, current.delivery,
                              DELIVERY_BLURBS))
            continue
        if key in ("n", "name", "rename"):
            new_name = ask("  Project name (. = this directory)",
                           current.target_name)
            if new_name and (re.fullmatch(r"[A-Za-z0-9._-]+", new_name)
                             or new_name == "."):
                import pathlib
                path = (pathlib.Path.cwd() if new_name == "."
                        else pathlib.Path.cwd() / new_name).resolve()
                current = replace(current, target_name=new_name,
                                  target=str(path), adopting=path.is_dir())
            else:
                print(tint("warn", "  use letters, numbers, dots, dashes, "
                                   "underscores — or '.' for this directory"))
            continue
        if key in ("g", "goal", "preview"):
            if preview_goal:
                preview_goal(current)
            else:
                print(tint("dim", "  (goal preview unavailable yet)"))
            continue
        print(tint("warn", "  keys: Enter launch · o · e · m · d · n · g · q"))


def opening_line(engines_available: Sequence[str]) -> str:
    if engines_available:
        lane = ", ".join(engines_available)
        return (tint("bold", "Absoloop")
                + tint("dim", " — Synergetic Loops. ")
                + tint("dim", f"Engines online: {lane}."))
    return (tint("bold", "Absoloop")
            + tint("dim", " — Synergetic Loops. ")
            + tint("warn", "No engines on PATH yet (claude / codex / grok)."))


def prep_summary(brief: Briefing, goal_rel: str,
                 notes: Sequence[str] = (),
                 launching: bool = True) -> None:
    """The consolidated preparation report.

    Every scaffold/setup step (runner sync, skill installs, git init,
    migrations, goal write) lands in this single block, so the console
    stays quiet until /goal work actually begins. When launching, the
    closing line is the explicit hand-off to the loop."""
    width = 58
    rule = "─" * width
    where = "adopted" if brief.adopting else "created"
    print(tint("accent", "  ∞ ") + tint("bold", "MISSION PREP")
          + tint("dim", "  ·  workspace ready"))
    print(tint("dim", "  " + rule))
    print(f"    {tint('cyan', 'workspace')}  {brief.target}  "
          + tint("dim", f"({where})"))
    print(f"    {tint('cyan', 'goal')}       {goal_rel}  "
          + tint("dim", "(contract the loop re-reads every iteration)"))
    print(f"    {tint('cyan', 'delivery')}   {brief.delivery}  "
          + tint("dim", DELIVERY_BLURBS.get(brief.delivery, "")))
    for note in notes:
        print(tint("dim", f"    · {note}"))
    print(tint("dim", "  " + rule))
    if launching:
        print(tint("ok", "  ∞ Prep complete — /goal takes over.")
              + tint("dim", f"  engine {brief.engine}"
                     + (f" · model {brief.model}" if brief.model else "")))
        print()
