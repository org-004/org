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

import json
import re
import unicodedata
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

from .ledger import Ledger
from .util import now, read_json, write_json

BODY_MAX = 400
MAX_TRIES = 3
TIMEOUT_S = 5
TITLE = "org: a plan paused"
FINISHED_TITLE = "org: a plan finished"
TEST_TITLE = "org: test"
TEST_TEXT = "org: test message from org. If you can read this, pauses of a plan will reach you."
_DROP = ("Cc", "Cf", "Zl", "Zp")


def alerts_path(ledger: Ledger):
    return ledger.root / "alerts.jsonl"


def outbox_path(ledger: Ledger):
    return ledger.root / "outbox.json"


def alert(ledger: Ledger, run_id: str, kind: str, reason: str) -> dict:
    """Append {"at": time, "run": run id, "kind": kind, "reason": reason} to alerts.jsonl as one
    line and return it. `kind` is `pause`, `retry`, `resume`, `redispatch`, `finished` or `test`."""
    entry = {"at": now(), "run": run_id, "kind": kind, "reason": reason}
    path = alerts_path(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii keeps U+2028 and friends escaped, so one alert is always one line for any reader.
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    return entry


def alerts(ledger: Ledger) -> List[dict]:
    """Every line of alerts.jsonl, oldest first; a line that is not a JSON object is skipped."""
    out = []
    try:
        with open(alerts_path(ledger), "r", encoding="utf-8", errors="replace") as f:
            for line in f.read().split("\n"):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def clean(text: str, limit: int) -> str:
    """`text` as one line that is safe to show on a phone, at most `limit` characters.

    Control and format characters, line and paragraph separators included, become spaces and
    runs of whitespace one space. Nothing is left that a client would turn into a link: no
    `scheme://`, no `www.`, no `name.tld` or dotted address, no `@` between words (the dots and
    the `://` are spaced out or replaced, so the words can still be read). Cut text ends in `…`.
    """
    # Why each step: NFKC first, so a full-width dot or at-sign cannot slip past the rules below.
    # Then the characters that draw nothing or break the line become spaces. `://` becomes a space,
    # `www.` loses its dot, a dot between two word characters becomes a space (a dot that ends a
    # sentence is followed by a space or the end, so it stays) and an `@` between two word
    # characters becomes ` at `. None of these steps can make a new `://` or dot-pair, so one pass is enough.
    text = unicodedata.normalize("NFKC", str(text))
    text = "".join(" " if (unicodedata.category(c) in _DROP or c.isspace()) else c for c in text)
    text = text.replace("://", " ")
    text = re.sub(r"(?i)\bwww\.", lambda m: m.group(0)[:3] + " ", text)
    text = re.sub(r"(?<=\w)\.(?=\w)", " ", text)
    text = re.sub(r"(?<=\w)@(?=\w)", " at ", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(limit - 1, 0)].rstrip() + "…"
    return text[:limit]


def pause_body(run_id: str, reason: str, held: int) -> str:
    """`org: <run> paused: <reason>. <n> run(s) held. org status`, at most BODY_MAX characters:
    the reason is cleaned and cut to what the rest leaves room for."""
    head = "org: %s paused: " % clean(run_id, 100)
    tail = ". %d run(s) held. org status" % held
    room = max(BODY_MAX - len(head) - len(tail), 1)
    return (head + clean(reason, room) + tail)[:BODY_MAX]


def _header_ok(value) -> bool:
    """A header name or value is used only when it is a string without a newline that latin-1 can carry."""
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, so the 3xx answer itself is what post reports: a topic address must not be forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _enqueue(ledger: Ledger, run_id: str, title: str, body: str) -> None:
    box = read_json(outbox_path(ledger), None)
    if not isinstance(box, dict):
        box = {}
    box.setdefault("pending", []).append({"run": run_id, "title": title, "body": body, "tries": 0, "error": None, "at": now()})
    box.setdefault("failed", [])
    write_json(outbox_path(ledger), box)


def post(cfg: dict, title: str, body: str) -> Tuple[Optional[int], Optional[str]]:
    """One POST of `body` (UTF-8, `Content-Type: text/plain; charset=utf-8`) to `notify.url`, with
    a `Title` header and every header in `notify.headers`. Returns (HTTP status, None) for any
    answer, a 4xx or 5xx included, or (None, why) when nothing answered or no address is set.
    Only http and https addresses are used, a redirect is not followed, and the wait is TIMEOUT_S."""
    try:
        notify = cfg.get("notify") or {}
        url = str(notify.get("url") or "")
        if not url:
            return None, "no notify.url is set"
        if not url.lower().startswith(("http://", "https://")):
            return None, "notify.url is not an http or https address"
        headers = {"Content-Type": "text/plain; charset=utf-8", "Title": title}
        for name, value in (notify.get("headers") or {}).items():
            if _header_ok(name) and _header_ok(value) and name:
                headers[name] = value
        if not _header_ok(title):
            headers["Title"] = TITLE
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # noqa: BLE001  sending must never raise
        why = getattr(e, "reason", None) or e
        return None, clean(str(why) or type(e).__name__, 120)


def pause(ledger: Ledger, run_id: str, reason: str, held: int) -> None:
    """Record one pause: `alert(..., "pause", reason)`, and, when `notify.url` is set, put
    `pause_body` in the outbox to be sent by `flush`. Called once for each pause, by `graph.tell`."""
    alert(ledger, run_id, "pause", reason)
    if (ledger.config().get("notify") or {}).get("url"):
        _enqueue(ledger, run_id, TITLE, pause_body(run_id, reason, held))


def finished(ledger: Ledger, run_id: str, outcomes: List[Tuple[str, str]], total_usd: float) -> None:
    """Record that a plan ended with nothing held, once: an alert of kind `finished` on its last
    run, and, with `notify.url` set, one message in the outbox naming each run's outcome
    (`outcomes` is (run id, state) in order) and the total spent, cleaned and cut as a pause is."""
    what = ", ".join("%s %s" % (rid, state) for rid, state in outcomes)
    detail = "%s; $%.2f spent" % (what, total_usd)
    last = outcomes[-1][0] if outcomes else ""
    alert(ledger, last, "finished", "the plan finished: " + detail)
    if (ledger.config().get("notify") or {}).get("url"):
        head, tail = "org: plan finished: ", ". org status"
        body = head + clean(detail, max(BODY_MAX - len(head) - len(tail), 1)) + tail
        _enqueue(ledger, last, FINISHED_TITLE, body[:BODY_MAX])


def flush(ledger: Ledger) -> None:
    """Send what the outbox holds. Each entry is {"run", "title", "body", "tries", "error", "at"}.
    A send that gets a 2xx removes the entry; any other outcome counts a try, and after MAX_TRIES
    the entry moves to the outbox's `failed` list with its last error. One try per entry per
    call, so retries fall on later pumps. Does nothing, and reads nothing but the outbox's
    existence, when there is no outbox. Never raises."""
    try:
        if not outbox_path(ledger).exists():
            return
        box = read_json(outbox_path(ledger), None)
        if not isinstance(box, dict) or not box.get("pending"):
            return
        cfg = ledger.config()
        still, gone = [], box.get("failed") or []
        for entry in box["pending"]:
            status, why = post(cfg, entry.get("title") or TITLE, entry.get("body") or "")
            if status is not None and 200 <= status < 300:
                continue
            entry["tries"] = int(entry.get("tries") or 0) + 1
            entry["error"] = why if status is None else "HTTP %d" % status
            (gone if entry["tries"] >= MAX_TRIES else still).append(entry)
        box["pending"], box["failed"] = still, gone
        write_json(outbox_path(ledger), box)
    except Exception:  # noqa: BLE001  an alert that cannot be sent must not stop a pump
        pass


def failed(ledger: Ledger, run_id: str) -> Optional[str]:
    """The sentence for a `!` line when a message about this run could not be sent after
    MAX_TRIES, with the last error; None otherwise."""
    if not outbox_path(ledger).exists():
        return None
    box = read_json(outbox_path(ledger), None)
    for entry in reversed((box or {}).get("failed") or [] if isinstance(box, dict) else []):
        if isinstance(entry, dict) and entry.get("run") == run_id:
            what = "the alert for this pause" if entry.get("title") == TITLE else "the message about this plan"
            return "%s could not be sent to notify.url after %d tries (%s)" % (what, MAX_TRIES, entry.get("error") or "no answer")
    return None


def test(ledger: Ledger) -> Tuple[int, str]:
    """`org notify test`: (exit code, the line to print). Without `notify.url`, 1 and a sentence
    saying so and how to set it. With it, one message sent as a pause would be: 0 and the HTTP
    status for a 2xx, 1 and the status or the error for anything else."""
    cfg = ledger.config()
    if not (cfg.get("notify") or {}).get("url"):
        return 1, ("no notify.url is set: pauses of a plan show in `org status` and the dashboard only. "
                   "`org config set notify.url <address that takes a POST>` sets one")
    alert(ledger, "", "test", "org notify test")
    status, why = post(cfg, TEST_TITLE, clean(TEST_TEXT, BODY_MAX))
    if status is None:
        return 1, "could not send: %s" % why
    if 200 <= status < 300:
        return 0, "sent: HTTP %d" % status
    return 1, "notify.url answered HTTP %d" % status


def doctor_row(cfg: dict) -> Tuple[bool, str, str, str]:
    """The `alerts` row of `org doctor`: (ok, name, what was found, what to do)."""
    url = str((cfg.get("notify") or {}).get("url") or "")
    if url:
        # The address may carry a topic that is as good as a password: show where, not what.
        from urllib.parse import urlsplit

        return True, "alerts", "pauses of a plan are sent to %s" % (urlsplit(url).hostname or "notify.url"), ""
    return (False, "alerts", "no notify.url: a pause of a plan will be visible only in `org status` and the dashboard",
            "org config set notify.url <address that takes a POST>, then `org notify test`")
