#!/usr/bin/env python3
"""Tests for cheap_subagents.py — run with: python3 -m pytest -q  (or python3 test_cheap_subagents.py)"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SCRIPT = HERE / "cheap_subagents.py"
sys.path.insert(0, str(HERE))
import cheap_subagents as cs  # noqa: E402


def call(tool_input, env=None, tool_name="Agent"):
    """Run the script as Claude Code would: JSON on stdin, JSON (or nothing) on stdout."""
    e = {**os.environ, "CHEAP_SUBAGENTS_LOG": "", **(env or {})}
    e.pop("CHEAP_SUBAGENTS", None) if env is None or "CHEAP_SUBAGENTS" not in env else None
    p = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"tool_name": tool_name, "tool_input": tool_input, "session_id": "t"}),
        capture_output=True, text=True, env=e,
    )
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


class DecisionTable(unittest.TestCase):
    def model_for(self, stype, prompt, explicit=None):
        ti = {"subagent_type": stype, "prompt": prompt, "description": "t"}
        if explicit:
            ti["model"] = explicit
        m, _reason, _upd = cs.decide(ti)
        return m

    def test_explore_is_haiku(self):
        self.assertEqual(self.model_for("Explore", "find where auth tokens are validated"), "haiku")

    def test_guide_and_statusline_are_haiku(self):
        self.assertEqual(self.model_for("claude-code-guide", "how do hooks work"), "haiku")
        self.assertEqual(self.model_for("statusline-setup", "set up my status line"), "haiku")

    def test_general_purpose_default_sonnet(self):
        self.assertEqual(self.model_for("general-purpose", "implement retry logic in client.py and add tests"), "sonnet")

    def test_general_purpose_trivial_drops_to_haiku(self):
        self.assertEqual(self.model_for("general-purpose", "list all files importing requests"), "haiku")
        self.assertEqual(self.model_for("general-purpose", "summarize README.md"), "haiku")

    def test_plan_keeps_parent(self):
        self.assertIsNone(self.model_for("Plan", "plan the migration"))

    def test_heavy_signals_keep_parent(self):
        for p in [
            "do a security review of the auth module",
            "what is the root cause of the flaky test",
            "evaluate the trade-offs between kafka and sqs",
            "find the race condition in the worker pool",
            "architect the new ingestion pipeline",
        ]:
            self.assertIsNone(self.model_for("general-purpose", p), p)
            self.assertIsNone(self.model_for("Explore", p), p)  # heavy beats type

    def test_review_and_critique_keep_parent(self):
        self.assertIsNone(self.model_for("general-purpose", "review the diff in PR 42"))
        self.assertIsNone(self.model_for("general-purpose", "critique this design doc"))

    def test_short_build_prompt_ignores_ambiguous_heavy(self):
        self.assertEqual(self.model_for("general-purpose", "Build Layer 1 HTTP probes and prove they work"), "sonnet")
        self.assertIsNone(self.model_for("general-purpose", "Build the auth module, then do a security review"))

    def test_review_pattern_is_specific(self):
        self.assertIsNone(self.model_for("general-purpose", "review the diff in PR 42"))
        self.assertIsNone(self.model_for("general-purpose", "do a code review of client.py"))
        self.assertEqual(self.model_for("general-purpose", "update the review queue page title"), "sonnet")

    def test_prove_pattern_is_specific(self):
        self.assertIsNone(self.model_for("general-purpose", "prove that the lock ordering is deadlock-free and correct"))
        self.assertIsNone(self.model_for("general-purpose", "give a formal proof of the invariant"))

    def test_unknown_agent_type_scored_by_prompt(self):
        self.assertEqual(self.model_for("my-custom-reviewer", "find all usages of foo()"), "haiku")
        self.assertEqual(self.model_for("my-custom-reviewer", "rewrite the parser to support streaming"), "sonnet")

    def test_tag_overrides_and_is_stripped(self):
        ti = {"subagent_type": "Explore", "prompt": "[model:opus] find the thing"}
        m, reason, upd = cs.decide(ti)
        self.assertEqual(m, "opus")
        self.assertEqual(upd["prompt"], "find the thing")
        self.assertNotIn("[model:", upd["prompt"])

    def test_tag_keep_removes_model(self):
        ti = {"subagent_type": "Explore", "prompt": "[model:keep] find the thing", "model": "haiku"}
        m, _, upd = cs.decide(ti)
        self.assertIsNone(m)
        self.assertNotIn("model", upd)

    def test_explicit_cheaper_never_upgraded(self):
        self.assertEqual(self.model_for("general-purpose", "implement the feature", explicit="haiku"), "haiku")

    def test_explicit_pricier_is_downgraded_by_default(self):
        self.assertEqual(self.model_for("Explore", "find the config loader", explicit="opus"), "haiku")

    def test_respect_explicit_env(self):
        os.environ["CHEAP_SUBAGENTS_RESPECT_EXPLICIT"] = "1"
        try:
            self.assertEqual(self.model_for("Explore", "find the config loader", explicit="opus"), "opus")
        finally:
            del os.environ["CHEAP_SUBAGENTS_RESPECT_EXPLICIT"]


class HookContract(unittest.TestCase):
    """The stdin/stdout contract Claude Code actually depends on."""

    def test_rewrites_model_with_valid_hook_json(self):
        out = call({"subagent_type": "Explore", "prompt": "find x"})
        h = out["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        self.assertEqual(h["permissionDecision"], "allow")
        self.assertEqual(h["updatedInput"]["model"], "haiku")
        self.assertEqual(h["updatedInput"]["prompt"], "find x")

    def test_silent_when_nothing_changes(self):
        # Plan + no explicit model → no output at all (lets normal flow continue untouched)
        self.assertIsNone(call({"subagent_type": "Plan", "prompt": "plan it"}))

    def test_ignores_other_tools(self):
        self.assertIsNone(call({"command": "ls"}, tool_name="Bash"))

    def test_disabled_by_env(self):
        self.assertIsNone(call({"subagent_type": "Explore", "prompt": "find x"}, env={"CHEAP_SUBAGENTS": "0"}))

    def test_garbage_stdin_is_harmless(self):
        p = subprocess.run([sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True,
                           env={**os.environ, "CHEAP_SUBAGENTS_LOG": ""})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")

    def test_custom_tiers_via_env(self):
        out = call({"subagent_type": "Explore", "prompt": "find x"}, env={"CHEAP_SUBAGENTS_CHEAP": "sonnet"})
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")

    def test_log_written(self):
        with tempfile.TemporaryDirectory() as d:
            logp = Path(d) / "log.jsonl"
            call({"subagent_type": "Explore", "prompt": "find x"}, env={"CHEAP_SUBAGENTS_LOG": str(logp)})
            rec = json.loads(logp.read_text().strip())
            self.assertEqual(rec["to"], "haiku")
            self.assertEqual(rec["subagent_type"], "Explore")
            self.assertIsNone(rec["match"])  # Explore is type-tiered; no pattern fired

    def test_log_match_has_context(self):
        with tempfile.TemporaryDirectory() as d:
            logp = Path(d) / "log.jsonl"
            prompt = "please look over everything carefully and find the race condition in the worker pool scheduler code"
            call({"subagent_type": "general-purpose", "prompt": prompt}, env={"CHEAP_SUBAGENTS_LOG": str(logp)})
            rec = json.loads(logp.read_text().strip())
            self.assertIsNone(rec["to"])
            i = prompt.index("race condition")
            self.assertEqual(rec["match"], prompt[i - 30:i + len("race condition") + 30])


class Installer(unittest.TestCase):
    def test_install_is_idempotent_and_uninstall_clean(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "HOME": d}
            sp = Path(d) / ".claude" / "settings.json"
            sp.parent.mkdir()
            sp.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                      "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}))
            for _ in range(2):  # twice → still exactly one entry
                subprocess.run([sys.executable, str(SCRIPT), "--install"], check=True, capture_output=True, env=env)
            s = json.loads(sp.read_text())
            ours = [g for g in s["hooks"]["PreToolUse"] if g["matcher"] == "Agent"]
            self.assertEqual(len(ours), 1)
            self.assertEqual(ours[0]["hooks"][0]["args"], [str(SCRIPT)])
            self.assertEqual(s["permissions"]["allow"], ["Bash(ls)"])            # untouched
            self.assertEqual(len(s["hooks"]["PreToolUse"]), 2)                   # other hook kept
            subprocess.run([sys.executable, str(SCRIPT), "--uninstall"], check=True, capture_output=True, env=env)
            s = json.loads(sp.read_text())
            self.assertEqual([g["matcher"] for g in s["hooks"]["PreToolUse"]], ["Bash"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
