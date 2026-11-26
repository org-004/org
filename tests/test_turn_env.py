"""What a turn of the manager's loop runs under, and the two refusals of `org approve` for the manager."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from org import manager, spec  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.util import now, write_json  # noqa: E402

os.environ["ORG_SECRETS_DIR"] = os.path.join(tempfile.gettempdir(), "org-tests-no-secrets")

DRAFT = """---
title: Small thing
project: acme
repos: acme/web
delivery: merge
max_cost_usd: 40
max_hours: 6
%s---

## Intent

A small thing.

## Context

The page lives in acme-web.

## Requirements

- R1 (MUST): the page says hello.

## Out of scope

- Everything else.

## Acceptance criteria

- AC1: `npm test -- hello` passes in acme-web.
"""
FORECAST = "expect_usd: 12\nexpect_hours: 2\n"


def matches(rule: str, command: str) -> bool:
    """Claude Code's prefix rules, simply: `Bash(x *)` matches a command that starts with `x ` and
    `Bash(x)` matches exactly x. A `*` inside a rule stands for any run of characters."""
    found = re.fullmatch(r"Bash\((.*)\)", rule)
    if not found:
        return False  # a rule for another tool
    inner = found.group(1)
    return re.fullmatch(".*".join(re.escape(p) for p in inner.split("*")), command) is not None


def listed(rules, command) -> bool:
    return any(matches(r, command) for r in rules)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-turn-env-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = Ledger(self.tmp / "ledger")
        self.ledger.init()
        self.set_manager(direct=True, permission_mode="bypassPermissions")

    def set_manager(self, **kw):
        write_json(self.ledger.config_path, dict(self.ledger.config(), manager=kw))


class WhatATurnRunsUnder(Base):
    def test_what_a_turn_runs_under_has_no_sandbox(self):
        s = manager.turn_settings(self.ledger)
        self.assertNotIn("sandbox", s)
        self.assertEqual(s["permissions"]["ask"], [])

    def test_what_a_turn_runs_under_denies_root_and_other_machines(self):
        deny = manager.turn_settings(self.ledger)["permissions"]["deny"]
        for command in ("sudo ls", "su", "ssh host", "ssh", "scp a b:c", "scp"):
            self.assertTrue(listed(deny, command), command)

    def test_what_a_turn_runs_under_denies_the_commands_that_stop_the_organization(self):
        deny = manager.turn_settings(self.ledger)["permissions"]["deny"]
        forms = ["org login", "org login --x", "org down", "org down --yes", "org node add a b", "org node rm a",
                 "org --home /x login", "org --home /x down", "org --home /x node add a", "org --home /x node rm a",
                 "python3 -m org login", "python -m org down", "python3 -m org node rm a", "python -m org node add a",
                 str(manager.BIN / "org") + " login", str(manager.BIN / "org") + " node rm a"]
        for command in forms:
            self.assertTrue(listed(deny, command), command)

    def test_what_a_turn_runs_under_leaves_the_rest_free(self):
        p = manager.turn_settings(self.ledger)["permissions"]
        for command in ("git push origin main", "incus list", "docker ps", "org approve 001-x", "org upgrade abc",
                        "org --home /x approve 001-x", "org loop status", "org node ls", "org spec check 1"):
            self.assertFalse(listed(p["deny"], command), command)
            self.assertFalse(listed(p["ask"], command), command)

    def test_what_a_turn_runs_under_is_a_direct_chat_and_a_chat_is_not_changed(self):
        chat = manager.settings(self.ledger, "talk")
        turn = manager.turn_settings(self.ledger)
        extra = [r for r in turn["permissions"]["deny"] if r not in chat["permissions"]["deny"]]
        self.assertTrue(extra)
        self.assertEqual([r for r in chat["permissions"]["deny"] if r not in turn["permissions"]["deny"]], [])
        for command in ("ssh host", "scp a b", "org login", "org down", "org node add x", "org node rm x"):
            self.assertFalse(listed(chat["permissions"]["deny"], command), command)
        self.assertEqual({k: v for k, v in turn.items() if k != "permissions"}, {k: v for k, v in chat.items() if k != "permissions"})

    def test_what_a_turn_runs_under_has_the_token_and_the_actor(self):
        with mock.patch("org.secrets.github_token", return_value="ghs_x"):
            env = manager.turn_env(self.ledger)
            chat = manager.launch_env(self.ledger, github=True, actor=True)
        self.assertEqual((env["GH_TOKEN"], env["ORG_ACTOR"]), ("ghs_x", "manager"))
        self.assertEqual((chat["GH_TOKEN"], chat["ORG_ACTOR"]), ("ghs_x", "manager"))

    def test_what_a_turn_runs_under_is_not_what_a_question_or_review_runs_under(self):
        with mock.patch("org.secrets.github_token", return_value="ghs_x"), \
                mock.patch.dict(os.environ, {"ORG_ACTOR": "manager", "GH_TOKEN": "ghs_x"}):
            env = manager.launch_env(self.ledger)
        self.assertNotIn("ORG_ACTOR", env)
        self.assertNotIn("GH_TOKEN", env)

    def test_what_a_turn_runs_under_needs_both_settings(self):
        self.assertEqual(manager.turn_problems(self.ledger), [])
        for kw in ({"direct": True}, {"permission_mode": "bypassPermissions"}, {}):
            self.set_manager(**kw)
            self.assertTrue(manager.turn_problems(self.ledger), kw)
        # And the loop, turned on and due for its bootstrap, starts nothing while either is missing.
        from org import loop

        for kw in ({"direct": True}, {"permission_mode": "bypassPermissions"}):
            self.set_manager(**kw)
            write_json(self.ledger.config_path, dict(self.ledger.config(), loop={"enabled": True}))
            with mock.patch.object(loop, "_launch") as launch:
                self.assertIsNone(loop.tick(self.ledger, clock=lambda: 1795687200.0))
            launch.assert_not_called()
            self.assertEqual(loop.status(self.ledger)["problems"], manager.turn_problems(self.ledger))

    def test_what_a_turn_runs_under_reads_a_chats_cost_from_the_transcripts(self):
        home = self.tmp / "home"
        folder = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.ledger.desk))
        folder.mkdir(parents=True)
        self.assertIsNone(manager.chat_cost_estimate(self.ledger, 0, home=home))
        usage = {"input_tokens": 1000000, "output_tokens": 0}
        lines = [{"type": "assistant", "timestamp": "2026-11-26T10:00:00.000Z", "message": {"id": "m1", "model": "claude-sonnet-5-5", "usage": usage}},
                 {"type": "assistant", "timestamp": "2026-11-26T10:00:01.000Z", "message": {"id": "m1", "model": "claude-sonnet-5-5", "usage": usage}},
                 {"type": "assistant", "timestamp": "2026-11-20T10:00:00.000Z", "message": {"id": "m0", "model": "claude-sonnet-5-5", "usage": usage}},
                 {"type": "user", "timestamp": "2026-11-26T10:00:02.000Z", "message": {"content": "hi"}}]
        (folder / "a.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
        since = 1795687200 - 3600 * 24  # 2026-11-25 10:00 UTC
        self.assertAlmostEqual(manager.chat_cost_estimate(self.ledger, since, home=home), 2.0, places=6)


class TheTwoNumbers(Base):
    def org(self, *args, actor=False, ok=None):
        env = dict(os.environ, ORG_SECRETS_DIR=str(self.tmp / "no-secrets"))
        env.pop("ORG_ACTOR", None)
        if actor:
            env["ORG_ACTOR"] = "manager"
        p = subprocess.run([sys.executable, "-m", "org", "--home", str(self.ledger.root)] + list(args),
                           capture_output=True, text=True, cwd=str(ROOT), timeout=60, env=env)
        if ok is not None:
            self.assertEqual(p.returncode == 0, ok, p.stdout + p.stderr)
        return p

    def draft(self, header=FORECAST):
        out = self.org("spec", "new", "Small thing", "--project", "acme", ok=True).stdout.split()
        Path(out[1]).write_text(DRAFT % header)
        return out[0]

    def brake(self):
        write_json(self.ledger.budget_path, {"weekly": {"at": now(), "utilization": 0.81, "resets_at": now() + 3600,
                                                          "status": "allowed", "run_id": "x"}})

    def test_the_two_numbers_refuse_approve_at_the_brake_to_the_manager_only(self):
        spec_id = self.draft()
        self.brake()
        p = self.org("approve", spec_id, "--no-dispatch", actor=True, ok=False)
        self.assertIn("weekly limit", p.stderr)
        self.assertIsNone(self.ledger.spec_meta(spec_id), "nothing was approved")
        self.org("approve", spec_id, "--no-dispatch", ok=True)
        self.assertIsNotNone(self.ledger.spec_meta(spec_id))

    def test_the_two_numbers_are_asked_of_the_managers_specs_and_not_of_a_persons(self):
        spec_id = self.draft(header="")
        p = self.org("approve", spec_id, "--no-dispatch", actor=True, ok=False)
        self.assertIn("expect_usd and expect_hours", p.stderr)
        self.assertIsNone(self.ledger.spec_meta(spec_id))
        self.org("approve", spec_id, "--no-dispatch", ok=True)
        self.assertEqual(self.org("dispatch", spec_id, actor=True, ok=False).returncode, 1)

    def test_the_two_numbers_are_checked_by_spec_check(self):
        for header, fragment in (("expect_usd: 0\n", "must be positive"), ("expect_usd: 41\n", "more than max_cost_usd"),
                                 ("expect_hours: 7\n", "more than max_hours"), ("expect_hours: soon\n", "must be a number")):
            errors, _ = spec.check_text(self.ledger, DRAFT % header, "001-x")
            self.assertTrue(any(fragment in e for e in errors), (header, errors))
        errors, warnings = spec.check_text(self.ledger, DRAFT % FORECAST, "001-x")
        self.assertEqual(errors, [])
        self.assertFalse(any("unknown key" in w for w in warnings))

    def test_the_two_numbers_hold_for_release_at_the_brake(self):
        self.brake()
        write_json(self.ledger.run_dir("001-x-r1") / "run.json", {"run_id": "001-x-r1", "spec_id": "001-x", "state": "queued"})
        p = self.org("release", "001-x-r1", actor=True, ok=False)
        self.assertIn("weekly limit", p.stderr)

    def test_the_two_numbers_leave_approve_without_sha_as_it_stands(self):
        help_text = self.org("approve", "--help", ok=True).stdout
        self.assertRegex(help_text, r"\[--sha SHA\]")
        spec_id = self.draft()
        p = self.org("approve", spec_id, "--sha", "0000000000", "--no-dispatch", ok=False)
        self.assertIn("sha mismatch", p.stderr)
        self.assertIsNone(self.ledger.spec_meta(spec_id))
        self.assertIn("approved", self.org("approve", spec_id, "--no-dispatch", ok=True).stdout)
        self.assertIsNotNone(self.ledger.spec_meta(spec_id))


if __name__ == "__main__":
    unittest.main()
