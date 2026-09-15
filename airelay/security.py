"""密钥学相关：本地密钥生成/校验、上游密钥加密、管理员会话令牌。

设计要点（对应方案 7.1 / 12 节）：
- 本地分发密钥明文只在创建时返回一次，库中只存 `prefix` + `key_hash`（加 pepper 的 SHA-256）。
- 上游渠道密钥用本地对称密钥（Fernet）加密落库，主密钥存放在数据目录的 secrets 文件里，权限 0600。
- 管理员会话用 HMAC 签名的紧凑令牌，不引入额外 JWT 依赖。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

KEY_PREFIX = "sk-relay"
_PREFIX_SEGMENT_LEN = 8
_SECRET_LEN = 32  # token_urlsafe 的字节数
ADMIN_TOKEN_BYTES = 32


def generate_local_key(pepper: str) -> tuple[str, str, str]:
    """生成一个本地分发密钥。

    返回 `(明文密钥, prefix, key_hash)`。明文形如
    `sk-relay-ab12cd34-<随机串>`，prefix 即用户可辨识的中段。
    """
    prefix = secrets.token_hex(max(_PREFIX_SEGMENT_LEN // 2, 1))
    body = secrets.token_urlsafe(_SECRET_LEN)
    plaintext = f"{KEY_PREFIX}-{prefix}-{body}"
    return plaintext, prefix, hash_local_key(plaintext, pepper)


def hash_local_key(plaintext: str, pepper: str) -> str:
    """对明文密钥做加 pepper 的 SHA-256。"""
    digest = hashlib.sha256()
    digest.update(pepper.encode("utf-8"))
    digest.update(plaintext.encode("utf-8"))
    return digest.hexdigest()


def verify_local_key(plaintext: str, key_hash: str, pepper: str) -> bool:
    return hmac.compare_digest(hash_local_key(plaintext, pepper), key_hash)


def parse_key_prefix(plaintext: str) -> str | None:
    """从明文密钥里取出 prefix 段，用于按索引查库。"""
    parts = plaintext.split("-", 3)
    if len(parts) < 3 or parts[0] != "sk":
        return None
    if parts[1] != "relay":
        return None
    return parts[2] if len(parts) >= 3 else None


def mask_key(prefix: str) -> str:
    return f"{KEY_PREFIX}-{prefix}-****************"


def new_request_id() -> str:
    return "req_" + secrets.token_hex(12)


# --------------------------------------------------------------------------- #
# 上游密钥加密
# --------------------------------------------------------------------------- #
class SecretBox:
    """基于 Fernet 的对称加解密；主密钥持久化在数据目录。"""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_master_key(cls, master_key: bytes) -> "SecretBox":
        return cls(master_key)

    @staticmethod
    def generate_master_key() -> bytes:
        return Fernet.generate_key()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            raise ValueError("上游密钥解密失败：主密钥可能已被更换，请重新填写该渠道的 API Key") from None

    def try_decrypt(self, ciphertext: str) -> str:
        try:
            return self.decrypt(ciphertext)
        except ValueError:
            return ""


# --------------------------------------------------------------------------- #
# 管理员会话令牌
# --------------------------------------------------------------------------- #
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass
class AdminSession:
    subject: str = "admin"
    issued_at: int = 0
    expires_at: int = 0
    client: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def to_payload(self) -> dict[str, Any]:
        return {
            "sub": self.subject,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "cli": self.client,
            **self.extra,
        }


class SessionSigner:
    """HMAC-SHA256 签名的紧凑会话令牌：`<payload>.<sig>`。"""

    def __init__(self, secret: bytes, ttl_seconds: int = 12 * 3600) -> None:
        self._secret = secret
        self.ttl_seconds = ttl_seconds

    def issue(self, *, client: str = "", extra: dict[str, Any] | None = None) -> str:
        now = int(time.time())
        session = AdminSession(
            issued_at=now,
            expires_at=now + self.ttl_seconds,
            client=client,
            extra=extra or {},
        )
        payload = _b64e(json.dumps(session.to_payload(), separators=(",", ":")).encode("utf-8"))
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str) -> AdminSession | None:
        if not token or token.count(".") != 1:
            return None
        payload, signature = token.split(".", 1)
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        try:
            data = json.loads(_b64d(payload))
        except (ValueError, json.JSONDecodeError):
            return None
        session = AdminSession(
            subject=data.get("sub", "admin"),
            issued_at=int(data.get("iat", 0)),
            expires_at=int(data.get("exp", 0)),
            client=data.get("cli", ""),
        )
        if session.expired:
            return None
        return session

    def _sign(self, payload: str) -> str:
        return _b64e(hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest())


# --------------------------------------------------------------------------- #
# 数据目录下的机密文件
# --------------------------------------------------------------------------- #
@dataclass
class Secrets:
    pepper: str
    master_key: str
    admin_token: str
    session_secret: str

    def to_json(self) -> dict[str, str]:
        return {
            "pepper": self.pepper,
            "master_key": self.master_key,
            "admin_token": self.admin_token,
            "session_secret": self.session_secret,
        }


SECRETS_FILENAME = "secrets.json"


def load_or_create_secrets(data_dir: Path, *, rotate_admin_token: bool = False) -> Secrets:
    """读取数据目录下的机密文件，不存在则生成（权限 0600）。"""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / SECRETS_FILENAME

    raw: dict[str, str] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}

    changed = False
    pepper = raw.get("pepper") or _new_hex(32)
    if not raw.get("pepper"):
        changed = True
    master_key = raw.get("master_key") or SecretBox.generate_master_key().decode("ascii")
    if not raw.get("master_key"):
        changed = True
    session_secret = raw.get("session_secret") or _new_hex(32)
    if not raw.get("session_secret"):
        changed = True
    admin_token = raw.get("admin_token") or secrets.token_urlsafe(ADMIN_TOKEN_BYTES)
    if not raw.get("admin_token") or rotate_admin_token:
        changed = True
    if rotate_admin_token:
        admin_token = secrets.token_urlsafe(ADMIN_TOKEN_BYTES)

    secrets_obj = Secrets(
        pepper=pepper,
        master_key=master_key,
        admin_token=admin_token,
        session_secret=session_secret,
    )

    if changed or not path.exists():
        _write_private(path, json.dumps(secrets_obj.to_json(), indent=2))
    return secrets_obj


def rotate_admin_token(data_dir: Path) -> str:
    """重新生成管理员令牌并落盘，返回新令牌。"""
    updated = load_or_create_secrets(data_dir, rotate_admin_token=True)
    return updated.admin_token


def _write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # Windows / FAT 挂载下不生效，忽略
        pass


def _new_hex(nbytes: int) -> str:
    return secrets.token_hex(nbytes)
