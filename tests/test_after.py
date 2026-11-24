"""A spec's `after` line: how it is read, and every way `org spec check` and approval refuse one.

What a plan then does (waiting, the review, pauses) is in tests/test_graph.py. Here the ledger is
real and nothing runs except where a test says so.
"""
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import cli, dispatch, graph, spec, status  # noqa: E402
from test_incus import IncusCase  # noqa: E402  (the simulated Incus, the scripted claude, a ledger in /tmp)
from test_manager_side import GOOD  # noqa: E402


def text_of(title, after=None):
    """The good spec under another title, with an `after` line when one is given."""
    text = GOOD.replace("CSV export", title)
    return text if after is None else text.replace("\nproject:", "\nafter: %s\nproject:" % after, 1)


class AfterCase(IncusCase):
    def draft(self, title, after=None):
        spec_id = spec.new_draft(self.ledger, title, "acme")
        self.ledger.draft_path(spec_id).write_text(text_of(title, after))
        return spec_id

    def approve(self, spec_id):
        return spec.approve(self.ledger, spec_id, spec.draft_sha(self.ledger, spec_id)[:10])

    def errors(self, spec_id):
        return spec.check_draft(self.ledger, spec_id)[0]

    def org(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(["--home", str(self.ledger.root)] + list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class TheLine(unittest.TestCase):
    def test_after_is_a_list_of_ids_or_numbers_separated_by_commas(self):
        self.assertEqual(spec.parse("---\ntitle: t\nafter: 008, 9\n---\n")[0]["after"], ["008", "9"])
        self.assertEqual(spec.parse("---\ntitle: t\nafter: 008-a-cache\n---\n")[0]["after"], ["008-a-cache"])
        self.assertEqual(spec.parse("---\ntitle: t\nafter:\n---\n")[0]["after"], [])

    def test_a_spec_without_the_line_parses_and_checks_as_it_always_did(self):
        self.assertNotIn("after", spec.parse(GOOD)[0])
        self.assertEqual(spec.check(GOOD), ([], []))

    def test_the_line_is_a_known_key_and_not_warned_about(self):
        self.assertEqual(spec.check(text_of("Second", "1")), ([], []))


class WhatIsRefused(AfterCase):
    def test_a_spec_that_names_another_by_number_or_by_id_passes(self):
        first = self.draft("First")
        self.assertEqual(self.errors(self.draft("Second", "1")), [])
        self.assertEqual(self.errors(self.draft("Third", "001, " + first)), [])
        self.assertEqual(graph._named(self.ledger, ["001", first, "1"]), [first], "a spec named twice is waited for once")

    def test_a_spec_that_names_itself_is_refused_and_told_so(self):
        self.draft("First")
        second = self.draft("Second", "1, 2")
        self.assertEqual(self.errors(second), ["header: after: 2 is this spec itself"])
        with self.assertRaises(spec.ApprovalError) as refused:
            self.approve(second)
        self.assertIn("is this spec itself", str(refused.exception))
        self.assertIsNone(self.ledger.spec_meta(second), "nothing was approved")

    def test_a_spec_that_names_one_that_does_not_exist_is_refused_with_the_name(self):
        self.draft("First")
        second = self.draft("Second", "1, 7, 099-nothing")
        errors = self.errors(second)
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("header: after: no spec matches '7'", errors[0])
        self.assertIn("no spec matches '099-nothing'", errors[1])
        code, out, _ = self.org("spec", "check", second)
        self.assertEqual(code, 1)
        self.assertIn("error:   header: after:", out)
        self.assertIn("NOT READY", out)

    def test_a_number_that_is_not_a_specs_is_refused_however_it_is_written(self):
        # Every id begins with its own number, so a reference names one spec or none.
        self.draft("Cache one")
        self.draft("Cache two")
        third = self.draft("Third", "00, 001-cache-o")
        self.assertEqual(len(self.errors(third)), 1)
        self.assertIn("no spec matches '00'", self.errors(third)[0])

    def test_two_specs_that_come_after_each_other_are_a_cycle_whichever_is_checked(self):
        first = self.draft("First", "2")
        second = self.draft("Second", "1")
        self.assertEqual(self.errors(first), ["header: after: the specs form a cycle: 001 comes after 002 comes after 001"])
        self.assertEqual(self.errors(second), ["header: after: the specs form a cycle: 002 comes after 001 comes after 002"])

    def test_a_cycle_through_three_specs_is_found_and_named_in_order(self):
        self.draft("First", "3")
        self.draft("Second", "1")
        third = self.draft("Third", "2")
        self.assertEqual(self.errors(third), ["header: after: the specs form a cycle: 003 comes after 002 comes after 001 comes after 003"])

    def test_a_cycle_closed_by_an_approved_text_counts_even_when_its_draft_has_moved_on(self):
        first = self.draft("First")
        self.approve(first)
        second = self.draft("Second", "1")
        self.approve(second)
        # The second spec's draft no longer names the first; its approved text still does.
        self.ledger.draft_path(second).write_text(text_of("Second"))
        self.ledger.draft_path(first).write_text(text_of("First", "2"))
        self.assertEqual(self.errors(first), ["header: after: the specs form a cycle: 001 comes after 002 comes after 001"])

    def test_a_diamond_is_not_a_cycle(self):
        self.draft("First")
        self.draft("Second", "1")
        self.draft("Third", "1")
        self.assertEqual(self.errors(self.draft("Fourth", "2, 3")), [])

    def test_a_text_that_is_no_spec_of_the_ledger_yet_is_checked_for_what_it_names(self):
        self.draft("First")
        self.assertEqual(spec.check_text(self.ledger, text_of("Loose", "1"))[0], [])
        self.assertIn("no spec matches '5'", spec.check_text(self.ledger, text_of("Loose", "5"))[0][0])


class WithoutThePersonNoticing(AfterCase):
    def test_a_spec_that_names_no_other_leaves_no_trace_of_plans_in_the_ledger(self):
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        self.assertEqual(rec["state"], "dispatched")
        for key in ("after", "held", "paused", "review", "retry_of", "released_at"):
            self.assertNotIn(key, rec)
        self.assertFalse(graph.has_plan(self.ledger))
        self.assertEqual(dispatch.pump(self.ledger), [])
        self.assertEqual(self.wait_final(run_id)["state"], "done")
        dispatch.pump(self.ledger)
        status.collect(self.ledger, do_sync=True)
        for name in ("graph.json", "alerts.jsonl", "outbox.json"):
            self.assertFalse((self.ledger.root / name).exists(), name)
        self.assertFalse((self.ledger.run_dir(run_id) / "review.json").exists())
        self.assertFalse((self.ledger.runs / ".graph.lock").exists())
        view = status.run_view(self.ledger, run_id, None)
        self.assertEqual(view["warnings"], [])
        for key in ("after", "held", "paused", "review"):
            self.assertNotIn(key, view)
        self.assertEqual(graph.bundle_files(self.ledger, run_id), [])

    def test_a_run_of_a_spec_with_after_carries_the_ids_it_waits_for_and_marks_the_ledger(self):
        first = self.draft("First")
        self.approve(first)
        second = self.draft("Second", "1")
        meta = self.approve(second)
        self.assertEqual(meta["header"]["after"], ["1"], "the approved header is the text as written")
        self.assertEqual(graph.after_of(self.ledger, second), [first])
        self.assertEqual(graph.waiters(self.ledger, first), [second])
        self.assertEqual(graph.record_fields(self.ledger, self.ledger.spec_meta(first)), {})
        self.assertFalse(graph.has_plan(self.ledger))
        self.assertEqual(graph.record_fields(self.ledger, meta), {"after": [first]})
        self.assertTrue(graph.has_plan(self.ledger))

    def test_the_new_commands_answer_in_a_new_ledger_with_a_sentence(self):
        code, out, _ = self.org("notify", "test")
        self.assertEqual(code, 1)
        self.assertIn("no notify.url is set", out)
        code, _, err = self.org("release", "nothing")
        self.assertEqual(code, 1)
        self.assertIn("org: no run matches 'nothing'", err)
        code, out, _ = self.org("graph")
        self.assertEqual((code, out.startswith("No plans")), (0, True))


if __name__ == "__main__":
    unittest.main()
