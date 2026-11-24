"""What org tells the person about a pause: the alert line, the safe one-line body, the POST and its retries."""
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_notify import Receiver  # noqa: E402
from org import notify  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.util import read_json, write_json  # noqa: E402

DOTTED = re.compile(r"\w\.\w")


def assert_safe(case, text):
    case.assertNotIn("://", text)
    case.assertNotIn("www.", text.lower())
    case.assertIsNone(DOTTED.search(text), text)
    case.assertNotIn("\n", text)
    case.assertNotIn("\t", text)
    case.assertNotRegex(text, r"\w@\w")
    for c in text:
        case.assertNotIn(__import__("unicodedata").category(c), ("Cc", "Cf", "Zl", "Zp"), repr(c))


class NotifyCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ledger = Ledger(self.dir)
        self.ledger.init()
        # Nothing here may go through a proxy: the receiver is on this machine.
        patcher = mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, url, headers=None):
        write_json(self.ledger.config_path, {"notify": {"url": url, "headers": headers or {}}})

    def outbox(self):
        return read_json(notify.outbox_path(self.ledger))


class AlertTests(NotifyCase):
    def test_an_alert_is_one_json_line_with_time_run_kind_and_reason(self):
        got = notify.alert(self.ledger, "001-a-r1", "pause", "the review failed")
        lines = notify.alerts_path(self.ledger).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), got)
        self.assertEqual(sorted(got), ["at", "kind", "reason", "run"])
        self.assertEqual((got["run"], got["kind"], got["reason"]), ("001-a-r1", "pause", "the review failed"))

    def test_alerts_are_only_ever_appended_and_a_broken_line_is_skipped(self):
        notify.alert(self.ledger, "r1", "pause", "first\nsecond   line")
        with open(notify.alerts_path(self.ledger), "a") as f:
            f.write("not json\n[1]\n")
        notify.alert(self.ledger, "r2", "resume", "again")
        self.assertEqual([a["run"] for a in notify.alerts(self.ledger)], ["r1", "r2"])
        self.assertEqual(notify.alerts(self.ledger)[0]["reason"], "first\nsecond   line")

    def test_alerts_of_a_ledger_without_any_is_empty(self):
        self.assertEqual(notify.alerts(self.ledger), [])


class CleanTests(NotifyCase):
    def test_a_bidi_override_and_a_zero_width_space_are_gone(self):
        out = notify.clean("a‮b​c", 100)
        self.assertEqual(out, "a b c")

    def test_newlines_tabs_and_runs_of_spaces_become_one_space(self):
        self.assertEqual(notify.clean("one\r\ntwo\t\tthree   four five six", 100), "one two three four five six")

    def test_a_link_keeps_its_words_but_cannot_be_tapped(self):
        out = notify.clean("see https://evil.example/x?y now", 100)
        self.assertEqual(out, "see https evil example/x?y now")

    def test_www_a_dotted_host_and_an_ip_address_are_spaced_out(self):
        for text in ("www.evil.com", "visit evil.example.com today", "from 1.2.3.4 port", "WWW.Evil.com", "full－width ｅvil．com"):
            out = notify.clean(text, 100)
            assert_safe(self, out)

    def test_an_email_address_is_spelled_out(self):
        out = notify.clean("mail bob@evil.example please", 100)
        self.assertEqual(out, "mail bob at evil example please")

    def test_a_dot_that_ends_a_sentence_stays(self):
        self.assertEqual(notify.clean("It failed. Then it stopped.", 100), "It failed. Then it stopped.")

    def test_text_over_the_limit_is_cut_and_ends_in_an_ellipsis(self):
        out = notify.clean("word " * 100, 20)
        self.assertLessEqual(len(out), 20)
        self.assertTrue(out.endswith("…"))


class BodyTests(NotifyCase):
    def test_a_reason_with_a_link_a_newline_and_two_thousand_characters_arrives_as_one_safe_line(self):
        reason = "see https://evil.example.com/x and www.bad.org or bob@bad.org\nline two\x00\x1b‮ " + "long text " * 200
        self.assertGreater(len(reason), 2000)
        body = notify.pause_body("001-a-r1", reason, 3)
        self.assertLessEqual(len(body), 400)
        self.assertTrue(body.startswith("org: 001-a-r1 paused: "))
        self.assertTrue(body.endswith("…. 3 run(s) held. org status"))
        assert_safe(self, body)

    def test_a_short_reason_is_in_the_body_whole(self):
        self.assertEqual(notify.pause_body("001-a-r1", "the review failed", 1), "org: 001-a-r1 paused: the review failed. 1 run(s) held. org status")

    def test_a_run_id_is_cleaned_too(self):
        body = notify.pause_body("x.evil.com\nhttps://a.b", "r", 1)
        self.assertLessEqual(len(body), 400)
        assert_safe(self, body)


class PostTests(NotifyCase):
    def test_the_post_has_the_method_body_title_content_type_and_configured_headers(self):
        with Receiver() as r:
            self.configure(r.url, {"Authorization": "Bearer t", "Tags": "warning"})
            got = notify.post(self.ledger.config(), "org: a title", "héllo ☃")
            self.assertEqual(got, (200, None))
            req = r.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/topic")
        self.assertEqual(req["body"], "héllo ☃")
        self.assertEqual(req["headers"]["Title"], "org: a title")
        self.assertEqual(req["headers"]["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(req["headers"]["Authorization"], "Bearer t")
        self.assertEqual(req["headers"]["Tags"], "warning")

    def test_a_header_with_a_newline_or_a_value_that_is_not_text_is_ignored(self):
        with Receiver() as r:
            self.configure(r.url, {"Good": "yes", "Bad": "a\r\nInjected: 1", "Num": 5, "Bad\nName": "x"})
            self.assertEqual(notify.post(self.ledger.config(), "t", "b"), (200, None))
            headers = r.requests[0]["headers"]
        self.assertEqual(headers["Good"], "yes")
        for name in ("Bad", "Num", "Injected"):
            self.assertNotIn(name, headers)

    def test_an_error_status_is_an_answer_and_not_an_exception(self):
        with Receiver(statuses=[500, 404]) as r:
            self.configure(r.url)
            self.assertEqual(notify.post(self.ledger.config(), "t", "b"), (500, None))
            self.assertEqual(notify.post(self.ledger.config(), "t", "b"), (404, None))

    def test_a_redirect_is_not_followed_and_counts_as_its_status(self):
        with Receiver(statuses=[302]) as r:
            self.configure(r.url)
            self.assertEqual(notify.post(self.ledger.config(), "t", "b"), (302, None))
            self.assertEqual(len(r.requests), 1)

    def test_an_address_that_is_not_http_is_refused_without_a_request(self):
        for url in ("file:///etc/passwd", "ftp://example/x", "gopher://x"):
            self.configure(url)
            with mock.patch("urllib.request.OpenerDirector.open") as opened:
                status, why = notify.post(self.ledger.config(), "t", "b")
            self.assertIsNone(status)
            self.assertTrue(why)
            opened.assert_not_called()

    def test_no_address_is_no_answer(self):
        self.assertEqual(notify.post(self.ledger.config(), "t", "b")[0], None)

    def test_a_server_that_is_not_there_is_a_reason_and_not_an_exception(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.configure("http://127.0.0.1:%d/x" % port)
        status, why = notify.post(self.ledger.config(), "t", "b")
        self.assertIsNone(status)
        self.assertTrue(why)


class SendTests(NotifyCase):
    def test_nothing_is_sent_and_no_outbox_made_when_no_url_is_set_but_the_alert_is_written(self):
        notify.pause(self.ledger, "001-a-r1", "needs a person", 2)
        with mock.patch("urllib.request.OpenerDirector.open") as opened:
            notify.flush(self.ledger)
        opened.assert_not_called()
        self.assertFalse(notify.outbox_path(self.ledger).exists())
        self.assertEqual([(a["run"], a["kind"], a["reason"]) for a in notify.alerts(self.ledger)], [("001-a-r1", "pause", "needs a person")])

    def test_a_pause_with_a_url_waits_in_the_outbox_until_flush_sends_it(self):
        with Receiver() as r:
            self.configure(r.url)
            notify.pause(self.ledger, "001-a-r1", "needs a person", 2)
            entry = self.outbox()["pending"][0]
            self.assertEqual(sorted(entry), ["at", "body", "error", "run", "title", "tries"])
            self.assertEqual((entry["run"], entry["tries"], entry["title"]), ("001-a-r1", 0, notify.TITLE))
            self.assertEqual(r.requests, [])
            notify.flush(self.ledger)
            self.assertEqual(len(r.requests), 1)
            self.assertEqual(r.requests[0]["body"], "org: 001-a-r1 paused: needs a person. 2 run(s) held. org status")
            self.assertEqual(r.requests[0]["headers"]["Title"], notify.TITLE)
        self.assertEqual(self.outbox(), {"pending": [], "failed": []})

    def test_a_server_that_fails_twice_and_then_answers_gets_the_message_on_the_third_flush(self):
        with Receiver(statuses=[500, 500, 200]) as r:
            self.configure(r.url)
            notify.pause(self.ledger, "r1", "why", 1)
            notify.flush(self.ledger)
            self.assertEqual((self.outbox()["pending"][0]["tries"], self.outbox()["pending"][0]["error"]), (1, "HTTP 500"))
            notify.flush(self.ledger)
            self.assertEqual(self.outbox()["pending"][0]["tries"], 2)
            notify.flush(self.ledger)
            self.assertEqual(len(r.requests), 3)
        self.assertEqual(self.outbox(), {"pending": [], "failed": []})
        self.assertIsNone(notify.failed(self.ledger, "r1"))

    def test_a_server_that_fails_three_times_leaves_a_failed_entry_and_a_fourth_flush_sends_nothing(self):
        with Receiver(statuses=[500, 500, 500, 200]) as r:
            self.configure(r.url)
            notify.pause(self.ledger, "r1", "why", 1)
            for _ in range(3):
                notify.flush(self.ledger)
            box = self.outbox()
            self.assertEqual(box["pending"], [])
            self.assertEqual((box["failed"][0]["tries"], box["failed"][0]["error"]), (3, "HTTP 500"))
            self.assertEqual(notify.failed(self.ledger, "r1"), "the alert for this pause could not be sent to notify.url after 3 tries (HTTP 500)")
            self.assertIsNone(notify.failed(self.ledger, "r2"))
            notify.flush(self.ledger)
            self.assertEqual(len(r.requests), 3)

    def test_a_server_that_is_not_there_counts_a_try_and_flush_does_not_raise(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.configure("http://127.0.0.1:%d/x" % port)
        notify.pause(self.ledger, "r1", "why", 1)
        notify.flush(self.ledger)
        entry = self.outbox()["pending"][0]
        self.assertEqual(entry["tries"], 1)
        self.assertTrue(entry["error"])

    def test_a_non_http_url_is_a_failed_try_without_any_request(self):
        self.configure("file:///etc/passwd")
        notify.pause(self.ledger, "r1", "why", 1)
        with mock.patch("urllib.request.OpenerDirector.open") as opened:
            notify.flush(self.ledger)
        opened.assert_not_called()
        self.assertEqual(self.outbox()["pending"][0]["tries"], 1)

    def test_flush_with_no_outbox_reads_nothing_and_failed_is_none(self):
        with mock.patch.object(Ledger, "config", side_effect=AssertionError("config read")), \
                mock.patch("org.notify.read_json", side_effect=AssertionError("outbox read")):
            notify.flush(self.ledger)
            self.assertIsNone(notify.failed(self.ledger, "r1"))

    def test_flush_survives_a_damaged_outbox(self):
        notify.outbox_path(self.ledger).write_text("{not json")
        notify.flush(self.ledger)
        self.assertIsNone(notify.failed(self.ledger, "r1"))


class FinishedTests(NotifyCase):
    def test_a_finished_plan_is_an_alert_on_its_last_run_and_one_message(self):
        with Receiver() as r:
            self.configure(r.url)
            notify.finished(self.ledger, "002-b-r1", [("001-a-r1", "done"), ("002-b-r1", "partial")], 12.3)
            notify.flush(self.ledger)
            req = r.requests[0]
        a = notify.alerts(self.ledger)[0]
        self.assertEqual((a["run"], a["kind"]), ("002-b-r1", "finished"))
        self.assertEqual(a["reason"], "the plan finished: 001-a-r1 done, 002-b-r1 partial; $12.30 spent")
        self.assertEqual(req["headers"]["Title"], "org: a plan finished")
        self.assertTrue(req["body"].startswith("org: plan finished: 001-a-r1 done, 002-b-r1 partial; $12"))
        self.assertTrue(req["body"].endswith(". org status"))
        self.assertLessEqual(len(req["body"]), 400)
        assert_safe(self, req["body"])

    def test_a_finished_plan_with_no_url_is_only_an_alert(self):
        notify.finished(self.ledger, "r1", [("r1", "done")], 0)
        self.assertEqual(len(notify.alerts(self.ledger)), 1)
        self.assertFalse(notify.outbox_path(self.ledger).exists())

    def test_a_plan_with_very_many_runs_still_fits_in_the_body(self):
        with Receiver() as r:
            self.configure(r.url)
            notify.finished(self.ledger, "r9", [("%03d-spec-r1" % i, "done") for i in range(100)], 1)
            notify.flush(self.ledger)
            self.assertLessEqual(len(r.requests[0]["body"]), 400)


class TestCommandTests(NotifyCase):
    def test_without_a_url_it_says_so_and_exits_1_and_sends_nothing(self):
        code, line = notify.test(self.ledger)
        self.assertEqual(code, 1)
        self.assertIn("no notify.url is set", line)
        self.assertEqual(notify.alerts(self.ledger), [])

    def test_a_2xx_answer_is_exit_0_with_the_status_and_the_message_arrives_titled_org_test(self):
        with Receiver() as r:
            self.configure(r.url)
            self.assertEqual(notify.test(self.ledger), (0, "sent: HTTP 200"))
            req = r.requests[0]
        self.assertEqual(req["headers"]["Title"], "org: test")
        self.assertIn("If you can read this, pauses of a plan will reach you.", req["body"])
        self.assertEqual(notify.alerts(self.ledger)[0]["kind"], "test")

    def test_an_error_answer_is_exit_1_with_the_status(self):
        with Receiver(statuses=[500]) as r:
            self.configure(r.url)
            self.assertEqual(notify.test(self.ledger), (1, "notify.url answered HTTP 500"))

    def test_no_answer_is_exit_1_with_the_reason(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.configure("http://127.0.0.1:%d/x" % port)
        code, line = notify.test(self.ledger)
        self.assertEqual(code, 1)
        self.assertTrue(line.startswith("could not send: "))


class DoctorRowTests(NotifyCase):
    def test_with_a_url_the_row_names_the_host_and_not_the_secret_path(self):
        ok, name, found, todo = notify.doctor_row({"notify": {"url": "https://push.example.org/secret-topic-123?k=v"}})
        self.assertTrue(ok)
        self.assertEqual(name, "alerts")
        self.assertIn("push.example.org", found)
        self.assertNotIn("secret", found + todo)

    def test_without_a_url_the_row_warns_and_says_what_to_do(self):
        ok, name, found, todo = notify.doctor_row({"notify": {"url": None}})
        self.assertFalse(ok)
        self.assertIn("org status", found)
        self.assertIn("org notify test", todo)


if __name__ == "__main__":
    unittest.main()
