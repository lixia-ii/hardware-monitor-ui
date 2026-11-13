"""Management login: captcha, rate limits, pairing codes, approval, device tokens and mail."""
import json
from pathlib import Path
import smtplib
import struct
import tempfile
import threading
import unittest
import zlib
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
        self.auth.new_captcha(pair, "192.168.1.9")
        answer = self.auth.captchas[auth.digest(pair)][0]
        return self.auth.request(pair, answer.lower(), "192.168.1.9", agent)

    def test_captcha_is_single_use_and_expires(self):
        self.auth.new_captcha("pair-a", "ip")
        with self.assertRaises(WebError) as wrong:
            self.auth.request("pair-a", "00000", "ip", "")
        self.assertIn("验证码错误", str(wrong.exception))
        with self.assertRaises(WebError) as gone:
            self.auth.request("pair-a", "00000", "ip", "")
        self.assertIn("过期", str(gone.exception))
        self.auth.new_captcha("pair-a", "ip")
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

    def test_captcha_refresh_limit_per_browser_and_shared_address(self):
        with patch.object(captcha, "png", return_value=b"png"):
            for _ in range(auth.CAPTCHA_LIMIT):
                self.auth.new_captcha("busy", "ip-a")
            with self.assertRaises(WebError) as error:
                self.auth.new_captcha("busy", "ip-a")
            self.assertEqual(error.exception.status, 429)
            # Another browser behind the same address (NAT, localhost) still gets captchas.
            self.auth.new_captcha("neighbour", "ip-a")
            # Rotating the cookie only reaches the address-wide bound.
            for index in range(auth.CAPTCHA_LIMIT * (auth.SHARED_SOURCES - 1) - 1):
                self.auth.new_captcha(f"pair-{index}", "ip-a")
            with self.assertRaises(WebError):
                self.auth.new_captcha("another-cookie", "ip-a")
            self.auth.new_captcha("other-browser", "ip-b")
            self.clock.now += auth.SOURCE_WINDOW
            self.auth.new_captcha("another-cookie", "ip-a")

    def test_refused_captcha_refresh_keeps_the_current_one(self):
        with patch.object(captcha, "png", return_value=b"png"):
            for _ in range(auth.CAPTCHA_LIMIT):
                self.auth.new_captcha("pair-a", "ip")
            answer = self.auth.captchas[auth.digest("pair-a")][0]
            with self.assertRaises(WebError):
                self.auth.new_captcha("pair-a", "ip")
        self.auth.request("pair-a", answer, "ip", "")

    def test_failed_pairing_submissions_are_limited_by_source(self):
        for _ in range(auth.REQUEST_LIMIT):
            with self.assertRaises(WebError) as error:
                self.auth.request("pair-a", "wrong", "ip-a", "")
            self.assertEqual(error.exception.status, 400)
        with self.assertRaises(WebError) as error:
            self.auth.request("pair-a", "wrong", "ip-a", "")
        self.assertEqual(error.exception.status, 429)
        for index in range(auth.REQUEST_LIMIT * (auth.SHARED_SOURCES - 1)):
            with self.assertRaises(WebError) as error:
                self.auth.request(f"pair-{index}", "wrong", "ip-a", "")
            self.assertEqual(error.exception.status, 400)
        with self.assertRaises(WebError) as error:
            self.auth.request("new-cookie", "wrong", "ip-a", "")
        self.assertEqual(error.exception.status, 429)
        with self.assertRaises(WebError) as error:
            self.auth.request("new-cookie", "wrong", "ip-b", "")
        self.assertEqual(error.exception.status, 400)
        self.clock.now += auth.SOURCE_WINDOW
        with self.assertRaises(WebError) as error:
            self.auth.request("new-cookie", "wrong", "ip-a", "")
        self.assertEqual(error.exception.status, 400)

    def test_mail_cooldown_does_not_use_up_request_quota_or_captcha(self):
        self.ask("pair-a")
        self.auth.new_captcha("pair-b", "192.168.1.9")
        answer = self.auth.captchas[auth.digest("pair-b")][0]
        for _ in range(auth.REQUEST_LIMIT + 2):
            with self.assertRaises(WebError) as error:
                self.auth.request("pair-b", answer, "192.168.1.9", "")
            self.assertIn("秒后再试", str(error.exception))
        self.clock.now += auth.MAIL_INTERVAL
        self.assertIsNotNone(self.auth.request("pair-b", answer, "192.168.1.9", "")["pending"])

    def test_full_source_table_forgets_the_longest_idle_source(self):
        with patch.object(captcha, "png", return_value=b"png"), patch.object(auth, "MAX_SOURCES", 4):
            self.auth.new_captcha("pair-a", "ip-a")
            self.clock.now += 1
            self.auth.new_captcha("pair-b", "ip-b")
            self.clock.now += 1
            self.auth.new_captcha("pair-c", "ip-c")
            self.assertEqual(len(self.auth.sources), 4)
            self.assertNotIn(("captcha", "ip-a"), self.auth.sources)
            self.assertIn(("captcha", "ip-b"), self.auth.sources)
            self.assertIn(("captcha", "ip-c", auth.digest("pair-c")), self.auth.sources)

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

    def test_failed_removal_keeps_device_visible_and_can_be_retried(self):
        for operation in ("single", "others", "logout"):
            for failure in ("replace", "mkdir"):
                with self.subTest(operation=operation, failure=failure):
                    with self.auth.lock:
                        token = self.auth._issue("A", "ip")
                    ident = self.auth.devices(None)[0]["id"]
                    def remove():
                        if operation == "logout":
                            self.auth.logout(token)
                        else:
                            self.auth.revoke(ident if operation == "single" else "others", None)
                    target = patch.object(auth.os, "replace", side_effect=OSError) if failure == "replace" else patch.object(Path, "mkdir", side_effect=OSError)
                    with target, self.assertLogs(auth.log, level="ERROR"):
                        with self.assertRaises(WebError) as error:
                            remove()
                    self.assertEqual(error.exception.status, 500)
                    self.assertEqual(self.auth.devices(None)[0]["id"], ident)
                    self.assertIsNotNone(auth.Auth(clock=self.clock).check(token, "ip")[0])
                    remove()
                    self.assertIsNone(self.auth.check(token, "ip")[0])
                    self.assertIsNone(auth.Auth(clock=self.clock).check(token, "ip")[0])

    def test_failed_issue_preserves_device_evicted_at_capacity(self):
        with patch.object(auth, "MAX_DEVICES", 1), self.auth.lock:
            token = self.auth._issue("A", "ip")
            with patch.object(auth.os, "replace", side_effect=OSError), self.assertLogs(auth.log, level="ERROR"):
                with self.assertRaises(WebError):
                    self.auth._issue("B", "ip")
        self.assertEqual([d["name"] for d in self.auth.devices(None)], ["A"])
        self.assertIsNotNone(self.auth.check(token, "ip")[0])
        self.assertIsNotNone(auth.Auth(clock=self.clock).check(token, "ip")[0])

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
    def test_image_contains_only_flattened_png_pixels(self):
        for _ in range(20):
            text = captcha.answer()
            self.assertEqual(len(text), 5)
            self.assertTrue(set(text) <= set(captcha.ALPHABET))
            image = captcha.png(text)
            self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
            offset, chunks = 8, {}
            while offset < len(image):
                size = struct.unpack(">I", image[offset:offset + 4])[0]
                kind = image[offset + 4:offset + 8]
                data = image[offset + 8:offset + 8 + size]
                crc = struct.unpack(">I", image[offset + 8 + size:offset + 12 + size])[0]
                self.assertEqual(crc, zlib.crc32(kind + data))
                chunks[kind] = data
                offset += size + 12
            self.assertEqual(offset, len(image))
            self.assertEqual(list(chunks), [b"IHDR", b"IDAT", b"IEND"])
            self.assertEqual(struct.unpack(">IIBBBBB", chunks[b"IHDR"]), (160, 56, 8, 2, 0, 0, 0))
            pixels = zlib.decompress(chunks[b"IDAT"])
            self.assertEqual(len(pixels), 56 * (160 * 3 + 1))
            self.assertTrue(all(pixels[y * 481] == 0 for y in range(56)))
            self.assertGreater(len(set(pixels)), 50)

    def test_same_answer_has_randomized_pixels(self):
        self.assertNotEqual(captcha.png("23457"), captcha.png("23457"))


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
