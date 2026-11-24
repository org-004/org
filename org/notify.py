"""Telling the person that a plan stopped: a line in the ledger, and one HTTP POST.

Every pause of a plan, and every retry and resume org made by itself, is appended to
`alerts.jsonl`. When `notify.url` is set, each pause is also sent there: a POST whose body is one
line of plain text, made by org itself with the standard library, to that one address. A push
service turns it into a notification on a phone or forwards it as mail; which service is the
person's choice and lives in config.json. There is no mail here and no secret of org's own.

The reason in a pause was written by a model that read text from public sources, and the person
will read it on a phone. So the body is made in code (`pause_body`, `clean`): one line, at most
BODY_MAX characters, nothing in it that a phone would draw as a link, no control characters.

A send that fails is tried again on later pumps, MAX_TRIES times in all, then kept as failed and
shown in `org status`. Sending never raises and never stands in the way of anything else.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .ledger import Ledger

BODY_MAX = 400
MAX_TRIES = 3
TIMEOUT_S = 5
TITLE = "org: a plan paused"


def alerts_path(ledger: Ledger):
    return ledger.root / "alerts.jsonl"


def outbox_path(ledger: Ledger):
    return ledger.root / "outbox.json"


def alert(ledger: Ledger, run_id: str, kind: str, reason: str) -> dict:
    """Append {"at": time, "run": run id, "kind": kind, "reason": reason} to alerts.jsonl as one
    line and return it. `kind` is `pause`, `retry`, `resume`, `redispatch`, `finished` or `test`."""
    raise NotImplementedError


def alerts(ledger: Ledger) -> List[dict]:
    """Every line of alerts.jsonl, oldest first; a line that is not a JSON object is skipped."""
    raise NotImplementedError


def clean(text: str, limit: int) -> str:
    """`text` as one line that is safe to show on a phone, at most `limit` characters.

    Control and format characters, line and paragraph separators included, become spaces and
    runs of whitespace one space. Nothing is left that a client would turn into a link: no
    `scheme://`, no `www.`, no `name.tld` or dotted address, no `@` between words (the dots and
    the `://` are spaced out or replaced, so the words can still be read). Cut text ends in `…`.
    """
    raise NotImplementedError


def pause_body(run_id: str, reason: str, held: int) -> str:
    """`org: <run> paused: <reason>. <n> run(s) held. org status`, at most BODY_MAX characters:
    the reason is cleaned and cut to what the rest leaves room for."""
    raise NotImplementedError


def post(cfg: dict, title: str, body: str) -> Tuple[Optional[int], Optional[str]]:
    """One POST of `body` (UTF-8, `Content-Type: text/plain; charset=utf-8`) to `notify.url`, with
    a `Title` header and every header in `notify.headers`. Returns (HTTP status, None) for any
    answer, a 4xx or 5xx included, or (None, why) when nothing answered or no address is set.
    Only http and https addresses are used, a redirect is not followed, and the wait is TIMEOUT_S."""
    raise NotImplementedError


def pause(ledger: Ledger, run_id: str, reason: str, held: int) -> None:
    """Record one pause: `alert(..., "pause", reason)`, and, when `notify.url` is set, put
    `pause_body` in the outbox to be sent by `flush`. Called once for each pause, by `graph.tell`."""
    raise NotImplementedError


def finished(ledger: Ledger, run_id: str, outcomes: List[Tuple[str, str]], total_usd: float) -> None:
    """Record that a plan ended with nothing held, once: an alert of kind `finished` on its last
    run, and, with `notify.url` set, one message in the outbox naming each run's outcome
    (`outcomes` is (run id, state) in order) and the total spent, cleaned and cut as a pause is."""
    raise NotImplementedError


def flush(ledger: Ledger) -> None:
    """Send what the outbox holds. Each entry is {"run", "title", "body", "tries", "error", "at"}.
    A send that gets a 2xx removes the entry; any other outcome counts a try, and after MAX_TRIES
    the entry moves to the outbox's `failed` list with its last error. One try per entry per
    call, so retries fall on later pumps. Does nothing, and reads nothing but the outbox's
    existence, when there is no outbox. Never raises."""
    raise NotImplementedError


def failed(ledger: Ledger, run_id: str) -> Optional[str]:
    """The sentence for a `!` line when a message about this run could not be sent after
    MAX_TRIES, with the last error; None otherwise."""
    raise NotImplementedError


def test(ledger: Ledger) -> Tuple[int, str]:
    """`org notify test`: (exit code, the line to print). Without `notify.url`, 1 and a sentence
    saying so and how to set it. With it, one message sent as a pause would be: 0 and the HTTP
    status for a 2xx, 1 and the status or the error for anything else."""
    cfg = ledger.config()
    if not (cfg.get("notify") or {}).get("url"):
        return 1, ("no notify.url is set: pauses of a plan show in `org status` and the dashboard only. "
                   "`org config set notify.url <address that takes a POST>` sets one")
    raise NotImplementedError


def doctor_row(cfg: dict) -> Tuple[bool, str, str, str]:
    """The `alerts` row of `org doctor`: (ok, name, what was found, what to do)."""
    url = str((cfg.get("notify") or {}).get("url") or "")
    if url:
        # The address may carry a topic that is as good as a password: show where, not what.
        from urllib.parse import urlsplit

        return True, "alerts", "pauses of a plan are sent to %s" % (urlsplit(url).hostname or "notify.url"), ""
    return (False, "alerts", "no notify.url: a pause of a plan will be visible only in `org status` and the dashboard",
            "org config set notify.url <address that takes a POST>, then `org notify test`")
