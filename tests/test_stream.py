"""The meter's count of spend, against the result events the CLI printed in real runs."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from org.stream import Meter  # noqa: E402

MODEL = "claude-fable-5-2"
EMPTY = {}  # the per-turn field present and empty: the turn used no tokens

# (total_cost_usd, this turn's costUSD or EMPTY), as the ledger shows them for three runs of 2026-11-25.
RUN_013 = [(5.8424, 5.8424), (33.0561, 27.2137)] + [(33.0561, EMPTY)] * 9
RUN_012 = [(6.5858, 6.5858), (43.0656, 36.4798), (81.8850, 38.8194), (81.8850, EMPTY), (85.6939, 3.8089),
           (89.3683, 3.6744), (97.1655, 7.7972), (108.4230, 11.2575)]
RUN_014 = [(0.7121, 0.7121), (0.7121, EMPTY), (2.7288, 2.0167), (13.0614, 10.3326), (15.9693, 2.9079),
           (22.7224, 6.7531), (29.0281, 6.3057)]
LEDGER = {"013": (RUN_013, 33.0561), "012": (RUN_012, 108.4230), "014": (RUN_014, 29.0281)}


def result(total, turn, since="present", model_usage=True, sid="s"):
    """A `result` event: since is "present" (the per-turn field as given) or "absent"."""
    ev = {"type": "result", "subtype": "success", "session_id": sid, "total_cost_usd": total}
    if model_usage:
        ev["modelUsage"] = {MODEL: {"costUSD": total, "outputTokens": int(total * 1000)}}
    if since == "present":
        ev["model_usage_since_last_result"] = {} if turn is EMPTY else {MODEL: {"costUSD": turn}}
    return ev


def rows(meter):
    return sum(r["usd"] for r in meter.by_model.values())


def restarted(meter):
    """The meter a runner rebuilds from state.json after a restart."""
    return Meter({}, meter.confirmed_usd, meter.by_model, meter.cumulative, meter.totals)


class NoTokensTest(unittest.TestCase):
    def test_no_tokens_turns_of_the_three_ledger_runs_are_counted_as_spent(self):
        for run, (events, last) in LEDGER.items():
            m = Meter({})
            for total, turn in events:
                m.feed(result(total, turn))
            self.assertAlmostEqual(m.confirmed_usd, last, places=4, msg=run)
            self.assertAlmostEqual(rows(m), last, places=4, msg=run)

    def test_no_tokens_turns_cost_the_same_whether_the_per_turn_field_is_present_empty_or_absent(self):
        # With modelUsage on every result (R3), the per-turn field may come and go within a session.
        for run, (events, last) in LEDGER.items():
            for mask in range(1 << len(events)):
                if len(events) > 8 and mask % 7:
                    continue  # run 013's eleven results: a sample of the 2048 mixes is enough
                m = Meter({})
                for n, (total, turn) in enumerate(events):
                    m.feed(result(total, turn, "absent" if mask >> n & 1 else "present"))
                self.assertAlmostEqual(m.confirmed_usd, last, places=4, msg=(run, mask))
                self.assertAlmostEqual(rows(m), last, places=4, msg=(run, mask))
        # A CLI that reports neither per-turn nor per-model usage is counted from its totals alone.
        for run, (events, last) in LEDGER.items():
            m = Meter({})
            for total, turn in events:
                m.feed(result(total, turn, "absent", model_usage=False))
            self.assertAlmostEqual(m.confirmed_usd, last, places=4, msg=run)

    def test_no_tokens_by_model_sums_to_the_count_when_model_usage_is_given(self):
        for run, (events, last) in LEDGER.items():
            for since in ("present", "absent"):
                m = Meter({})
                for total, turn in events:
                    m.feed(result(total, turn, since))
                self.assertAlmostEqual(rows(m), m.confirmed_usd, places=4, msg=(run, since))

    def test_no_tokens_counts_survive_a_restart_of_the_runner_between_any_two_results(self):
        for run, (events, last) in LEDGER.items():
            for since, usage in (("present", True), ("absent", True), ("absent", False)):
                for cut in range(1, len(events)):
                    m = Meter({})
                    for k, (total, turn) in enumerate(events):
                        if k == cut:
                            m = restarted(m)
                        m.feed(result(total, turn, since, usage))
                    self.assertAlmostEqual(m.confirmed_usd, last, places=4, msg=(run, since, usage, cut))

    def test_no_tokens_an_empty_per_turn_field_costs_nothing_whatever_the_session_total(self):
        m = Meter({})
        m.feed(result(40.0, EMPTY))
        self.assertEqual(m.confirmed_usd, 0.0)
        summary = m.feed(dict(result(40.0, EMPTY), is_error=True, num_turns=1))
        self.assertEqual(summary["cost_usd"], 0.0)
        self.assertEqual(m.confirmed_usd, 0.0)

    def test_no_tokens_the_acceptance_one_liners_print_what_the_spec_says(self):
        m = Meter({})
        for t in (1.0, 1.5, 1.5):
            m.feed({"type": "result", "session_id": "s", "total_cost_usd": t})
        self.assertEqual(round(m.confirmed_usd, 4), 1.5)
        m = Meter({})
        m.feed({"type": "result", "session_id": "s", "total_cost_usd": 2.0, "model_usage_since_last_result": {"m": {"costUSD": 2.0}}})
        for _ in range(2):
            m.feed({"type": "result", "session_id": "s", "total_cost_usd": 2.0, "model_usage_since_last_result": {}})
        self.assertEqual(round(m.confirmed_usd, 4), 2.0)

    def test_no_tokens_totals_are_differenced_per_session_and_taken_whole_when_new_or_gone_backwards(self):
        m = Meter({})
        for sid, total in (("a", 1.0), ("a", 1.25), ("b", 0.5), ("a", 1.25), ("b", 0.75), ("a", 0.25), ("a", 0.5)):
            m.feed({"type": "result", "session_id": sid, "total_cost_usd": total})
        # a: 1.0 + 0.25 + 0 + 0.25 (went backwards: a new process) + 0.25; b: 0.5 + 0.25.
        self.assertAlmostEqual(m.confirmed_usd, 2.5, places=9)
        self.assertEqual(m.totals, {"a": 0.5, "b": 0.75})

    def test_no_tokens_a_meter_from_an_older_state_without_totals_still_counts(self):
        m = Meter({}, 3.0, {MODEL: {"usd": 3.0}}, {})  # as a runner at f717dc5 left it
        m.feed({"type": "result", "session_id": "s", "total_cost_usd": 4.0})
        self.assertAlmostEqual(m.confirmed_usd, 7.0, places=9)


if __name__ == "__main__":
    unittest.main()
