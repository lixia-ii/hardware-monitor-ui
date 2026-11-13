"""Local Fernet key. A new key is only created when a password is entered."""
import logging
import os

from hwmonitor import paths


def _key(create=False):
    from cryptography.fernet import Fernet
    keyfile = paths.DATA / "credentials.key"
    try:
        return keyfile.read_bytes()
    except FileNotFoundError:
        if not create:
            raise ValueError("凭据密钥丢失，请重新输入密码") from None
    # Re-entering a password is the documented recovery from a lost key. Secrets made with
    # the old key stay as they are and keep failing to decrypt until they are re-entered too.
    logging.getLogger(__name__).warning("Credential key missing; creating a new key for the entered password")
    paths.DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(paths.DATA, 0o700)
    key = Fernet.generate_key()
    try:
        fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return keyfile.read_bytes()
    with os.fdopen(fd, "wb") as stream:
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    return key


def protect(password):
    from cryptography.fernet import Fernet
    return "fernet:" + Fernet(_key(create=True)).encrypt(password.encode()).decode("ascii")


def unprotect(value):
    from cryptography.fernet import Fernet, InvalidToken
    if not value.startswith("fernet:"):
        raise ValueError("凭据无法跨平台迁移，请重新输入密码")
    try:
        return Fernet(_key()).decrypt(value[7:].encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ValueError("凭据或密钥无效，请重新输入密码") from None
