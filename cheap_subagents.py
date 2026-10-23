#!/usr/bin/env python3
"""
cheap_subagents.py — single-file Claude Code harness that routes subagent work
to cheaper models unless the task actually needs high reasoning.

HOW IT WORKS
  Registered as a PreToolUse hook on the `Agent` tool. Every time the main
  session spawns a subagent, this script reads the tool input from stdin,
  decides a tier (haiku / sonnet / keep-parent), and rewrites the Agent
  call's `model` field via hookSpecificOutput.updatedInput.

INSTALL (writes the hook into ~/.claude/settings.json, idempotent)
  python3 cheap_subagents.py --install            # user-level (all projects)
  python3 cheap_subagents.py --install --project  # this repo only (.claude/settings.json)

UNINSTALL
  python3 cheap_subagents.py --uninstall [--project]

VERSION
  python3 cheap_subagents.py --version

TEST A DECISION WITHOUT CLAUDE
  echo '{"tool_name":"Agent","tool_input":{"subagent_type":"Explore","prompt":"find auth code"}}' \
    | python3 cheap_subagents.py

OVERRIDES
  • Per-call escape hatch: put [model:opus] / [model:sonnet] / [model:haiku] anywhere
    in the subagent prompt. The tag is stripped before the agent sees it.
  • Env vars (set in your shell or settings.json "env"):
      CHEAP_SUBAGENTS=0              disable routing entirely
      CHEAP_SUBAGENTS_CHEAP=haiku    model for trivial work  (default haiku)
      CHEAP_SUBAGENTS_MID=sonnet     model for normal work   (default sonnet)
      CHEAP_SUBAGENTS_RESPECT_EXPLICIT=1
                                     if the parent already set `model`, leave it alone
                                     (default 0 = downgrade anyway unless it's already cheaper)
      CHEAP_SUBAGENTS_LOG=~/.claude/cheap_subagents.jsonl
                                     audit log of every decision (default shown; "" disables)
  • Edit TYPE_TIERS / HEAVY_PATTERNS / TRIVIAL_PATTERNS below to tune.

No dependencies beyond the Python 3 stdlib.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

VERSION = "0.1.4"

# ──────────────────────────────────────────────────────────────────────────────
# Policy
# ──────────────────────────────────────────────────────────────────────────────

CHEAP = os.environ.get("CHEAP_SUBAGENTS_CHEAP", "haiku")
MID = os.environ.get("CHEAP_SUBAGENTS_MID", "sonnet")
KEEP = None  # None = don't touch `model`; the subagent inherits the parent model

# Tier by built-in / common subagent_type. Anything not listed is scored by prompt.
TYPE_TIERS: dict[str, str | None] = {
    # read-only search / lookup — haiku is plenty
    "Explore": CHEAP,
    "claude-code-guide": CHEAP,
    "statusline-setup": CHEAP,
    # general multi-step work — sonnet
    "general-purpose": MID,
    "claude": MID,
    # architecture / planning — leave on the parent (opus) model
    "Plan": KEEP,
}

# Prompt signals that the task really needs the strong model.
HEAVY_PATTERNS = [
    r"\barchitect",
    r"\bdesign (a|the|an) (system|schema|api|architecture)",
    r"\btrade-?offs?\b",
    r"\broot[- ]cause\b",
    r"\bsecurity (review|audit)\b",
    r"\bthreat model",
    r"\bformal proof\b",
    r"\bprove (that|the) .{0,40}(correct|safe|sound)",
    r"\bformal",
    r"\brefactor (the )?(entire|whole|core)\b",
    r"\bmigration plan\b",
    r"\bconcurrency\b",
    r"\brace condition",
    r"\bcryptograph",
    r"\bcomplex\b.*\breason",
    r"\bcareful(ly)? (reason|analy)",
    r"\bevaluate (multiple|several) (approaches|options)",
    r"\b(code|security|design|architecture) review\b",
    r"\breview (the|this) (pr|diff|patch|change)",
    r"\bcritique\b",
]

# Short "build X" prompts often mention review/proof words incidentally ("…and prove
# they work"). For those, only these patterns are strong enough to keep the parent.
BUILD_VERB_RE = r"^\s*(build|implement|add|create|write|fix|refactor)\b"
BUILD_PROMPT_CHARS = 1500
UNAMBIGUOUS_HEAVY = [
    r"\barchitect",
    r"\bsecurity (review|audit)\b",
    r"\bthreat model",
    r"\broot[- ]cause\b",
    r"\brace condition",
]

# Prompt signals that the task is trivial — haiku territory.
TRIVIAL_PATTERNS = [
    r"^\s*(find|locate|list|grep|search|where is|which file)",
    r"\bfind (all|the|any) (files?|references?|usages?|occurrences?)\b",
    r"\blist (all|the) \w+ (files|functions|classes|endpoints)\b",
    r"\bsummari[sz]e\b",
    r"\bread (the|this) file\b",
    r"\bwhat does .{0,40} (do|return)\??$",
    r"\bcheck (if|whether) .{0,60} (exists|is present|is used)\b",
    r"\brename\b|\bformat\b|\blint\b",
    r"\bextract\b.{0,40}\b(from|out of)\b",
]

# Long prompts with lots of files usually mean real synthesis work; bump to MID.
LONG_PROMPT_CHARS = 2500

MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}

# ──────────────────────────────────────────────────────────────────────────────
# Decision
# ──────────────────────────────────────────────────────────────────────────────

TAG_RE = re.compile(r"\[model:(haiku|sonnet|opus|fable|keep)\]", re.I)


def _first_match(patterns: list[str], text: str) -> tuple[str, re.Match] | None:
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return pat, m
    return None


def _context(m: re.Match | None, text: str, width: int = 30) -> str | None:
    if m is None:
        return None
    return text[max(0, m.start() - width):m.end() + width]


def decide(tool_input: dict) -> tuple[str | None, str, dict]:
    """Return (model_or_None, reason, updated_tool_input)."""
    model, reason, updated, _match = decide_with_match(tool_input)
    return model, reason, updated


def decide_with_match(tool_input: dict) -> tuple[str | None, str, dict, str | None]:
    """Like decide(), plus the matched text (with context) that drove the decision."""
    prompt: str = tool_input.get("prompt", "") or ""
    stype: str = tool_input.get("subagent_type", "") or ""
    explicit: str | None = tool_input.get("model")
    updated = dict(tool_input)

    # 1. explicit per-call tag wins
    m = TAG_RE.search(prompt)
    if m:
        tag = m.group(1).lower()
        updated["prompt"] = TAG_RE.sub("", prompt).strip()
        match = _context(m, prompt)
        if tag == "keep":
            updated.pop("model", None)
            return None, "tag:keep", updated, match
        updated["model"] = tag
        return tag, f"tag:{tag}", updated, match

    # 2. respect an explicit model from the parent if configured to
    if explicit and os.environ.get("CHEAP_SUBAGENTS_RESPECT_EXPLICIT", "0") == "1":
        return explicit, "explicit:respected", updated, None

    low = prompt.lower()

    # 3. heavy signals → keep parent model (short build prompts: unambiguous ones only)
    is_build = re.search(BUILD_VERB_RE, low) and len(prompt) < BUILD_PROMPT_CHARS
    hit = _first_match(UNAMBIGUOUS_HEAVY if is_build else HEAVY_PATTERNS, low)
    if hit:
        pat, m = hit
        updated.pop("model", None)
        return None, f"heavy:{pat}", updated, _context(m, low)

    # 4. type-based tier
    if stype in TYPE_TIERS:
        tier = TYPE_TIERS[stype]
        if tier is None:
            updated.pop("model", None)
            return None, f"type:{stype}:keep", updated, None
        # but trivial prompts on a MID type can still drop to CHEAP
        trivial = _first_match(TRIVIAL_PATTERNS, low) if tier == MID and len(prompt) < 400 else None
        if trivial:
            tier = CHEAP
            reason = f"type:{stype}+trivial"
        else:
            reason = f"type:{stype}"
    else:
        # 5. unknown / custom agent type → score the prompt
        trivial = _first_match(TRIVIAL_PATTERNS, low) if len(prompt) < LONG_PROMPT_CHARS else None
        if trivial:
            tier, reason = CHEAP, "prompt:trivial"
        else:
            tier, reason = MID, "prompt:default"
    match = _context(trivial[1], low) if trivial else None

    # 6. never *upgrade* an explicit cheaper choice
    if explicit and MODEL_RANK.get(explicit, 9) < MODEL_RANK.get(tier, 9):
        return explicit, f"explicit:cheaper({explicit})", updated, match

    updated["model"] = tier
    return tier, reason, updated, match


# ──────────────────────────────────────────────────────────────────────────────
# Hook entrypoint
# ──────────────────────────────────────────────────────────────────────────────

def log(entry: dict) -> None:
    path = os.environ.get("CHEAP_SUBAGENTS_LOG", "~/.claude/cheap_subagents.jsonl")
    if not path:
        return
    try:
        p = Path(os.path.expanduser(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def run_hook() -> int:
    if os.environ.get("CHEAP_SUBAGENTS", "1") == "0":
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # not our problem; let the call proceed untouched

    if data.get("tool_name") != "Agent":
        return 0

    tool_input = data.get("tool_input") or {}
    model, reason, updated, match = decide_with_match(tool_input)

    log({
        "ts": int(time.time()),
        "session": data.get("session_id"),
        "subagent_type": tool_input.get("subagent_type"),
        "from": tool_input.get("model"),
        "to": model,
        "reason": reason,
        "match": match,
        "desc": tool_input.get("description"),
    })

    if updated == tool_input:
        return 0  # nothing to change; stay silent

    # permissionDecision "allow" is what makes updatedInput take effect.
    # Agent spawns don't normally prompt anyway, so this changes nothing else.
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"cheap_subagents: {reason} -> {model or 'parent'}",
            "updatedInput": updated,
        }
    }
    sys.stdout.write(json.dumps(out))
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# Installer
# ──────────────────────────────────────────────────────────────────────────────

HOOK_MARK = "cheap_subagents.py"


def settings_path(project: bool) -> Path:
    if project:
        return Path.cwd() / ".claude" / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def install(project: bool) -> None:
    sp = settings_path(project)
    sp.parent.mkdir(parents=True, exist_ok=True)
    settings = json.loads(sp.read_text()) if sp.exists() else {}
    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])

    script = str(Path(__file__).resolve())
    entry = {
        "matcher": "Agent",
        "hooks": [{
            "type": "command",
            "command": sys.executable or "python3",
            "args": [script],
            "timeout": 10,
            "statusMessage": "routing subagent model",
        }],
    }

    # replace any existing copy
    pre[:] = [g for g in pre if not _is_ours(g)]
    pre.append(entry)
    sp.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"installed cheap_subagents {VERSION} → {sp}\n  script: {script}\n  restart Claude Code (or /hooks to verify)")


def uninstall(project: bool) -> None:
    sp = settings_path(project)
    if not sp.exists():
        print("nothing to do")
        return
    settings = json.loads(sp.read_text())
    pre = settings.get("hooks", {}).get("PreToolUse", [])
    before = len(pre)
    pre[:] = [g for g in pre if not _is_ours(g)]
    sp.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"removed {before - len(pre)} entr{'y' if before-len(pre)==1 else 'ies'} from {sp}")


def _is_ours(group: dict) -> bool:
    for h in group.get("hooks", []):
        blob = " ".join([str(h.get("command", "")), *map(str, h.get("args", []))])
        if HOOK_MARK in blob:
            return True
    return False


def stats() -> None:
    path = Path(os.path.expanduser(os.environ.get("CHEAP_SUBAGENTS_LOG", "~/.claude/cheap_subagents.jsonl")))
    if not path.exists():
        print("no log yet")
        return
    counts: dict[str, int] = {}
    for line in path.read_text().splitlines():
        try:
            to = json.loads(line).get("to") or "parent"
        except Exception:
            continue
        counts[to] = counts.get(to, 0) + 1
    total = sum(counts.values()) or 1
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{k:8} {v:6}  {100*v//total:3}%")


if __name__ == "__main__":
    args = sys.argv[1:]
    project = "--project" in args
    if "--version" in args:
        print(VERSION)
    elif "--install" in args:
        install(project)
    elif "--uninstall" in args:
        uninstall(project)
    elif "--stats" in args:
        stats()
    else:
        sys.exit(run_hook())
