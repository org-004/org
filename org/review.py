"""The review between two specs of a plan: one headless manager session that reads a finished
run's report and says whether the specs waiting on it still describe the world it left.

It is the manager chat's `ask` mode and no more: the same settings (`manager.settings(ledger,
"ask")`), `--permission-mode dontAsk`, the persona with org/prompts/review.md after it. It can
read, and run the read-only `org` commands; it cannot draft, approve, dispatch, stop or release.
Its answer is a JSON object validated against `schemas.REVIEW` and by `check`, never read from
prose.

This module decides nothing. `run` is the body of a process of its own (`org review-run`), which
org/graph.py starts with `start` and whose end it notices; the process writes what the session
answered, or how it failed, to the run's `review.json`, and org/graph.py bounds what the answer
may do. What the report says is text an engineer wrote from public sources: the prompt says it is
data, and the bounds hold whether or not the model believes that.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .ledger import Ledger

PROMPTS = Path(__file__).parent / "prompts"
# The first words of the message a review session is given. tests/fake_claude.py knows a review by them.
OPENING = "Review the finished run `%s`"
REASON_MAX = 300
NOTE_MAX = 600
# How a review process ended, as `kind` in review.json.
KINDS = ("answer", "invalid", "error", "timeout")


def outcome_path(ledger: Ledger, run_id: str) -> Path:
    return ledger.run_dir(run_id) / "review.json"


def outcome(ledger: Ledger, run_id: str, attempt: int) -> Optional[dict]:
    """What the review process of this attempt wrote, or None when it has written nothing:
    {"attempt": n, "kind": one of KINDS, "answer": the validated answer or None,
     "problems": [why an answer was invalid], "error": str or None, "cost_usd": dollars, "at": time}.
    A file left by an earlier attempt is not this attempt's outcome."""
    raise NotImplementedError


def message(ledger: Ledger, run_id: str) -> str:
    """What the session is given, beginning with OPENING: the run's approved spec, its report
    (mirror/report.md), the output of `org review` for it (`status.scorecard`), and the approved
    text of every spec in `graph.waiters`, each under a heading that names it and each fenced as
    quoted material. It ends by naming the waiting specs' ids, which are the `spec` values that
    `notes` must use."""
    raise NotImplementedError


def check(answer, waiting: List[str]) -> List[str]:
    """Why an answer cannot be used, [] when it can: `schemas.problems` against `schemas.REVIEW`;
    a reason longer than REASON_MAX or empty; and for `continue`, a note missing for a spec in
    `waiting`, a note for a spec that is not in it, a spec noted twice, or a note longer than
    NOTE_MAX or empty."""
    raise NotImplementedError


def session(ledger: Ledger, text: str, timeout_s: float) -> dict:
    """Run one headless manager session on `text` and return
    {"answer": the `structured_output` of its result or None, "error": str or None,
     "timed_out": bool, "cost_usd": dollars}.

    The command is `manager.ask`'s with three differences: `--json-schema` with `schemas.REVIEW`
    and no partial messages; a session of its own each time, never the chat's; and `--model` and
    `--effort` from `manager.review_model` and `manager.review_effort`, falling back to
    `manager.model` and `manager.effort`, each flag left out when neither is set. The settings
    are exactly `manager.settings(ledger, "ask")`, the environment `manager.launch_env`. A session
    still running after `timeout_s` is killed and reported as timed out. Never raises.
    """
    raise NotImplementedError


def run(ledger: Ledger, run_id: str, attempt: int = 1) -> int:
    """The review process: one session for one finished run, then its outcome written whole to
    `outcome_path` (the shape `outcome` returns). The timeout is `graph.review_timeout_min`.
    Kind `answer` when `check` passes, `invalid` when it does not, `error` when the session
    failed or gave no structured answer, `timeout` when it was killed. Returns 0 when it wrote an
    outcome of any kind. It never writes run.json: org/graph.py does, under its own lock."""
    raise NotImplementedError


def start(ledger: Ledger, run_id: str, attempt: int) -> dict:
    """Start `org review-run <run> --attempt <n>` for this ledger as a process that belongs to
    nobody, and return {"pid", "boot", "at"} for it, the shape of `dispatch._me()`.

    A launcher starts it in a session of its own and exits at once, so the process is init's
    child: a pump inside a long-lived process, such as the dashboard, would otherwise keep it as
    a zombie, which `dispatch._gone` takes for alive. Any earlier `review.json` is removed first.
    """
    raise NotImplementedError
