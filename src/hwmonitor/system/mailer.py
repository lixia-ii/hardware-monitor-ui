"""SMTP mail for pairing codes; configured outside settings.json so the web page can never change it."""
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
import json
import logging
import os
from pathlib import Path
import smtplib
import ssl
import tempfile
import time

from hwmonitor import paths
from hwmonitor.system import credentials

log = logging.getLogger(__name__)
# Same fields as the shell scripts' mail_env; the authorization code is stored protected.
DEFAULT = {"smtp_host": "", "smtp_port": 465, "smtp_user": "", "smtp_pass_protected": "",
           "mail_from": "", "mail_from_name": "硬件监控", "mail_to": ""}


def path():
    return Path(paths.DATA) / "mail.json"


def validate(raw):
    out = dict(DEFAULT)
    if not isinstance(raw, dict):
        return out
    for key in ("smtp_host", "smtp_user", "mail_from", "mail_from_name", "mail_to"):
        value = raw.get(key)
        if isinstance(value, str) and "\n" not in value and "\r" not in value:
            out[key] = value.strip()[:200]
    port = raw.get("smtp_port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        out["smtp_port"] = port
    secret = raw.get("smtp_pass_protected")
    if isinstance(secret, str) and len(secret) <= 16000:
        out["smtp_pass_protected"] = secret
    return out


def load():
    try:
        return validate(json.loads(path().read_text(encoding="utf-8")))
    except FileNotFoundError:
        return dict(DEFAULT)
    except (OSError, ValueError):
        log.exception("Cannot load mail settings")
        return dict(DEFAULT)


def save(config):
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as f:
            temp = f.name
            json.dump(validate(config), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, target)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def configured(config):
    return all(config[k] for k in ("smtp_host", "smtp_user", "smtp_pass_protected", "mail_from", "mail_to"))


def mask(address):
    """li***@qq.com: enough to recognise the mailbox without publishing it."""
    name, at, domain = (address or "").partition("@")
    return (name[:2] + "***" + at + domain) if at else ""


def message(config, subject, html):
    mail = EmailMessage()
    mail["From"] = formataddr((config["mail_from_name"], config["mail_from"])) if config["mail_from_name"] else config["mail_from"]
    mail["To"] = config["mail_to"]
    mail["Subject"] = subject
    # Date and Message-ID keep the message out of spam folders, like send_mail.sh.
    mail["Date"] = formatdate(localtime=True)
    mail["Message-ID"] = make_msgid(domain=config["mail_from"].partition("@")[2] or None)
    mail.set_content("请使用支持 HTML 的邮件客户端查看。", charset="utf-8")
    mail.add_alternative(html, subtype="html", charset="utf-8")
    return mail


def send(config, subject, html, attempts=3, delay=3):
    """Raises OSError/ValueError/SMTPException after the last attempt; never logs the authorization code."""
    if not configured(config):
        raise ValueError("邮件尚未配置")
    password = credentials.unprotect(config["smtp_pass_protected"])
    mail = message(config, subject, html)
    context = ssl.create_default_context()
    for attempt in range(1, attempts + 1):
        try:
            # 465 is implicit TLS (smtps, as in send_mail.sh); other ports must upgrade with STARTTLS.
            if config["smtp_port"] == 465:
                client = smtplib.SMTP_SSL(config["smtp_host"], config["smtp_port"], timeout=30, context=context)
            else:
                client = smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=30)
            with client:
                if config["smtp_port"] != 465:
                    client.starttls(context=context)
                client.login(config["smtp_user"], password)
                client.send_message(mail)
            log.info("Mail sent attempt=%d id=%s", attempt, mail["Message-ID"])
            return
        except (OSError, smtplib.SMTPException) as error:
            log.warning("Mail failed attempt=%d: %s", attempt, type(error).__name__)
            if attempt == attempts or isinstance(error, smtplib.SMTPAuthenticationError):
                raise
            time.sleep(delay)
