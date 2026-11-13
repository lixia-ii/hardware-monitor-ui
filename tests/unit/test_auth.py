"""Management login: captcha, rate limits, pairing codes, approval, device tokens and mail."""
import json
from pathlib import Path
import smtplib
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from hwmonitor import paths
from hwmonitor.system import auth, captcha, mailer
from hwmonitor.system.webbridge import WebError

MAIL = {**mailer.DEFAULT, "smtp_host": "smtp.example.com", "smtp_user": "me@example.com",
        "smtp_pass_protected": "protected", "mail_from": "me@example.com", "mail_to": "phone@example.com"}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data = patch.object(paths, "DATA", Path(self.temp.name))
        data.start()
        self.addCleanup(data.stop)
        self.clock = Clock()
        self.sent = []
        self.mail_done = threading.Event()
        def send(config, subject, body):
            self.sent.append((subject, body))
            self.mail_done.set()
        self.auth = auth.Auth(load_mail=lambda: dict(MAIL), send_mail=send, clock=self.clock)

    def ask(self, pair="pair-a", agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) AppleWebKit/605 Version/18.0 Mobile Safari/604.1"):
        self.auth.new_captcha(pair)
        answer = self.auth.captchas[auth.digest(pair)][0]
        return self.auth.request(pair, answer.lower(), "192.168.1.9", agent)

    def test_captcha_is_single_use_and_expires(self):
        self.auth.new_captcha("pair-a")
        with self.assertRaises(WebError) as wrong:
            self.auth.request("pair-a", "00000", "ip", "")
        self.assertIn("验证码错误", str(wrong.exception))
        with self.assertRaises(WebError) as gone:
            self.auth.request("pair-a", "00000", "ip", "")
        self.assertIn("过期", str(gone.exception))
        self.auth.new_captcha("pair-a")
        answer = self.auth.captchas[auth.digest("pair-a")][0]
        self.clock.now += auth.CAPTCHA_TTL + 1
        with self.assertRaises(WebError):
            self.auth.request("pair-a", answer, "ip", "")
        self.assertEqual(self.auth.pending, {})

    def test_code_is_mailed_and_shown_locally_but_not_returned(self):
        result = self.ask()
        self.assertEqual(result["pending"]["mail"], "sending")
        self.assertTrue(self.mail_done.wait(3))
        code = self.auth.active()[0]["code"]
        self.assertNotIn(code, json.dumps(result))
        self.assertIn(code, self.sent[0][0])
        self.assertIn("192.168.1.9", self.sent[0][1])
        events = self.auth.drain()
        self.assertEqual(events[0][0], "pairing")
        self.assertIn(code, auth.describe(*events[0]))
        self.assertIn("iPhone", auth.describe(*events[0]))

    def test_unconfigured_and_failed_mail_still_pair_locally(self):
        self.auth.load_mail = lambda: dict(mailer.DEFAULT)
        self.assertEqual(self.ask()["pending"]["mail"], "unconfigured")
        failed = threading.Event()
        def boom(*args):
            failed.set()
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        self.auth.load_mail, self.auth.send_mail = lambda: dict(MAIL), boom
        self.clock.now += auth.MAIL_INTERVAL
        self.ask("pair-b")
        self.assertTrue(failed.wait(3))
        for _ in range(100):
            if any(kind == "mail_failed" for kind, _ in list(self.auth.events.queue)):
                break
            threading.Event().wait(.01)
        kinds = [kind for kind, _ in self.auth.drain()]
        self.assertIn("mail_failed", kinds)
        self.assertEqual({p["mail"] for p in self.auth.active()}, {"unconfigured", "failed"})

    def test_requests_are_rate_limited_for_everyone(self):
        self.ask("pair-a")
        with self.assertRaises(WebError) as limited:
            self.ask("pair-b")
        self.assertEqual(limited.exception.status, 429)
        for index in range(4):
            self.clock.now += auth.MAIL_INTERVAL
            self.ask(f"pair-{index}")
        self.clock.now += auth.MAIL_INTERVAL
        with self.assertRaises(WebError) as hourly:
            self.ask("pair-x")
        self.assertIn("分钟", str(hourly.exception))
        self.clock.now += 3600
        self.ask("pair-y")

    def test_verify_binds_to_requesting_browser_and_limits_attempts(self):
        self.ask("pair-a")
        code = self.auth.active()[0]["code"]
        with self.assertRaises(WebError):
            self.auth.verify("pair-b", code)
        wrong = "000000" if code != "000000" else "111111"
        for attempt in range(auth.MAX_ATTEMPTS - 1):
            with self.assertRaises(WebError) as error:
                self.auth.verify("pair-a", wrong)
            self.assertIn("还可尝试", str(error.exception))
        with self.assertRaises(WebError) as error:
            self.auth.verify("pair-a", wrong)
        self.assertIn("次数过多", str(error.exception))
        with self.assertRaises(WebError):
            self.auth.verify("pair-a", code)  # Burnt: the right code no longer works.

    def test_code_expires(self):
        self.ask()
        code = self.auth.active()[0]["code"]
        self.clock.now += auth.CODE_TTL + 1
        self.assertEqual(self.auth.active(), [])
        with self.assertRaises(WebError):
            self.auth.verify("pair-a", code)

    def test_new_request_replaces_own_pending(self):
        self.ask("pair-a")
        self.clock.now += auth.MAIL_INTERVAL
        self.ask("pair-a")
        self.assertEqual(len(self.auth.active()), 1)

    def test_token_lifecycle_and_persistence(self):
        self.ask()
        token = self.auth.verify("pair-a", self.auth.active()[0]["code"])
        stored = Path(self.temp.name, "management_devices.json").read_text(encoding="utf-8")
        self.assertNotIn(token, stored)
        self.assertIn(auth.digest(token), stored)
        device, renew = self.auth.check(token, "192.168.1.10")
        self.assertEqual(device["name"], "iPhone · Safari")
        self.assertFalse(renew)
        self.assertEqual(self.auth.check("forged", "ip"), (None, False))
        self.assertEqual(self.auth.check(None, "ip"), (None, False))
        # A restart keeps the device signed in.
        again = auth.Auth(clock=self.clock)
        self.assertIsNotNone(again.check(token, "ip")[0])
        self.clock.now += auth.REFRESH + 1
        self.assertTrue(again.check(token, "ip")[1])
        self.clock.now += auth.SESSION_AGE - 10
        self.assertIsNotNone(again.check(token, "ip")[0])  # Sliding expiry.
        self.clock.now += auth.SESSION_AGE + 1
        self.assertIsNone(again.check(token, "ip")[0])

    def test_approval_issues_token_to_requester_only(self):
        self.ask("pair-a")
        request = self.auth.active()[0]
        self.assertTrue(self.mail_done.wait(3))
        self.assertEqual(self.auth.status("pair-a")[1], None)
        self.auth.decide(request["id"], True)
        self.assertEqual(self.auth.active(), [])
        self.assertEqual(self.auth.status("pair-b"), ({"pending": None}, None))
        status, token = self.auth.status("pair-a")
        self.assertIsNotNone(self.auth.check(token, "ip")[0])
        self.assertEqual(self.auth.status("pair-a"), ({"pending": None}, None))
        with self.assertRaises(WebError):
            self.auth.decide(request["id"], True)

    def test_reject_and_revoke(self):
        self.ask("pair-a")
        self.auth.decide(self.auth.active()[0]["id"], False)
        self.assertEqual(self.auth.status("pair-a")[0], {"pending": None})
        with self.auth.lock:
            first = self.auth._issue("A", "ip")
            second = self.auth._issue("B", "ip")
            third = self.auth._issue("C", "ip")
        current = auth.digest(first)
        listed = self.auth.devices(current)
        self.assertEqual(sum(d["current"] for d in listed), 1)
        self.assertNotIn("hash", listed[0])
        self.auth.revoke(next(d["id"] for d in listed if d["name"] == "B"), current)
        self.assertIsNone(self.auth.check(second, "ip")[0])
        with self.assertRaises(WebError):
            self.auth.revoke("missing", current)
        self.assertEqual(self.auth.revoke("others", current), 1)
        self.assertIsNone(self.auth.check(third, "ip")[0])
        self.assertIsNotNone(self.auth.check(first, "ip")[0])
        self.auth.revoke("others", None)  # Desktop: everyone.
        self.assertIsNone(self.auth.check(first, "ip")[0])

    def test_device_count_is_bounded(self):
        with self.auth.lock:
            tokens = []
            for index in range(auth.MAX_DEVICES + 2):
                self.clock.now += 1  # Eviction drops the least recently used device.
                tokens.append(self.auth._issue(str(index), "ip"))
        self.assertEqual(len(self.auth.devices(None)), auth.MAX_DEVICES)
        self.assertIsNotNone(self.auth.check(tokens[-1], "ip")[0])
        self.assertIsNone(self.auth.check(tokens[0], "ip")[0])

    def test_failed_save_does_not_issue_token(self):
        with patch.object(auth.os, "replace", side_effect=OSError), self.auth.lock:
            with self.assertRaises(WebError):
                self.auth._issue("A", "ip")
        self.assertEqual(self.auth.devices(None), [])

    def test_corrupt_device_file_is_ignored(self):
        Path(self.temp.name, "management_devices.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(self.auth.devices(None), [])

    def test_helpers(self):
        self.assertEqual(auth.cookies('a=1; hm_session=abc'), {"a": "1", "hm_session": "abc"})
        self.assertEqual(auth.cookies('"broken'), {})
        cookie = auth.set_cookie("hm_session", "x", 10)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(auth.device_name("Mozilla/5.0 (Windows NT 10.0) Chrome/130 Safari/537 Edg/130"), "Windows · Edge")
        self.assertEqual(auth.device_name("Mozilla/5.0 (iPhone) AppleWebKit/605 Mobile/15E148"), "iPhone · 主屏幕应用")
        self.assertEqual(auth.device_name(None), "未知设备")


class CaptchaTests(unittest.TestCase):
    def test_answer_not_in_markup(self):
        for _ in range(20):
            text = captcha.answer()
            self.assertEqual(len(text), 5)
            self.assertTrue(set(text) <= set(captcha.ALPHABET))
            image = captcha.svg(text)
            self.assertTrue(image.startswith("<svg"))
            self.assertNotIn("<text", image)
            self.assertNotIn(text, image.replace(" ", ""))


class MailerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data = patch.object(paths, "DATA", Path(self.temp.name))
        data.start()
        self.addCleanup(data.stop)

    def test_validate_save_and_mask(self):
        self.assertEqual(mailer.load(), mailer.DEFAULT)
        self.assertFalse(mailer.configured(mailer.load()))
        mailer.save({**MAIL, "smtp_port": 587, "mail_to": "evil@example.com\r\nBcc: x@y", "extra": 1})
        loaded = mailer.load()
        self.assertEqual(loaded["smtp_port"], 587)
        self.assertEqual(loaded["mail_to"], "")  # Header injection rejected.
        self.assertNotIn("extra", loaded)
        self.assertEqual(mailer.validate({"smtp_port": True})["smtp_port"], 465)
        self.assertEqual(mailer.mask("lixiahuang@qq.com"), "li***@qq.com")
        self.assertEqual(mailer.mask(""), "")

    def test_message_headers(self):
        mail = mailer.message(MAIL, "配对码 123456", "<b>123456</b>")
        self.assertEqual(mail["To"], "phone@example.com")
        self.assertIn("me@example.com", mail["From"])
        self.assertIsNotNone(mail["Message-ID"])
        self.assertIsNotNone(mail["Date"])
        self.assertIn("123456", mail.get_body(("html",)).get_content())

    def test_send_ssl_and_starttls_with_retries(self):
        with patch.object(mailer.credentials, "unprotect", return_value="secret"), \
                patch.object(mailer.smtplib, "SMTP_SSL") as ssl_client, patch.object(mailer.time, "sleep"):
            client = ssl_client.return_value
            client.send_message.side_effect = [OSError("timeout"), None]
            mailer.send(MAIL, "s", "<p>b</p>")
            client.login.assert_called_with("me@example.com", "secret")
            self.assertEqual(client.send_message.call_count, 2)
        with patch.object(mailer.credentials, "unprotect", return_value="secret"), \
                patch.object(mailer.smtplib, "SMTP") as plain:
            mailer.send({**MAIL, "smtp_port": 587}, "s", "<p>b</p>")
            plain.return_value.starttls.assert_called_once()
        with patch.object(mailer.credentials, "unprotect", return_value="secret"), \
                patch.object(mailer.smtplib, "SMTP_SSL") as ssl_client, patch.object(mailer.time, "sleep") as sleep:
            ssl_client.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"no")
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                mailer.send(MAIL, "s", "b")
            sleep.assert_not_called()  # A wrong authorization code is not retried.
        with self.assertRaises(ValueError):
            mailer.send(mailer.DEFAULT, "s", "b")


class MailSetupTests(unittest.TestCase):
    def test_interactive_setup_keeps_blank_values_and_protects_code(self):
        from hwmonitor import server
        with tempfile.TemporaryDirectory() as directory, patch.object(paths, "DATA", Path(directory)):
            answers = iter(["smtp.qq.com", "abc", "465", "me@qq.com", "me@qq.com", "", "phone@qq.com", "n"])
            with patch("builtins.input", lambda prompt: next(answers)), patch("getpass.getpass", return_value="auth-code"), \
                    patch.object(server.sys, "stdout"), \
                    patch("hwmonitor.system.credentials.protect", side_effect=lambda value: "protected:" + value):
                self.assertEqual(server.mail_setup(), 0)
            saved = mailer.load()
            self.assertEqual((saved["smtp_host"], saved["smtp_port"], saved["mail_to"]), ("smtp.qq.com", 465, "phone@qq.com"))
            self.assertEqual(saved["mail_from_name"], "硬件监控")  # Left blank: default kept.
            self.assertEqual(saved["smtp_pass_protected"], "protected:auth-code")
            self.assertNotIn("auth-code", Path(directory, "mail.json").read_text(encoding="utf-8").replace("protected:auth-code", ""))

    def test_terminal_backspace_characters_are_applied(self):
        from hwmonitor import server
        self.assertEqual(server.typed("smtx" + chr(8) + "p.qq.com "), "smtp.qq.com")
        self.assertEqual(server.typed("ab" + chr(127) + chr(127) + chr(127) + "c" + chr(27)), "c")
