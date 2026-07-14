import logging
import re
import sys
from typing import Any

from .config import LOG_FILE

MAX_LOG_VALUE_LENGTH = 300
SENSITIVE_KEY_NAMES = {
    "authorization",
    "captchatoken",
    "captchaverification",
    "cgauthorization",
    "email",
    "pass2",
    "password",
    "phone",
    "readkey",
    "secretkey",
    "sendkey",
    "sign",
    "sso-token",
    "sso_pku_token",
    "token",
    "user",
    "username",
}
SENSITIVE_KEY_PARTS = ("authorization", "password", "secret", "sendkey", "token")
COMPACT_KEY_PARTS = ("base64", "image")
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b("
    r"authorization|captchaToken|captchaVerification|cgAuthorization|email|pass2|"
    r"password|phone|readkey|secretKey|sendkey|sign|sso-token|sso_pku_token|token|"
    r"user|userName|username"
    r")(\s*[:=]\s*)([^\s,;)}\]]+)"
)
_SECRET_URL_PATTERNS = (
    re.compile(r"(https://sctapi\.ftqq\.com/)[^/\s]+(\.send\b)", re.IGNORECASE),
    re.compile(
        r"(https://[^/\s]+\.push\.ft07\.com/send/)[^/\s]+(\.send\b)",
        re.IGNORECASE,
    ),
    re.compile(r"(https://api\.day\.app/)[^/\s]+()", re.IGNORECASE),
)


def is_sensitive_key(key: Any) -> bool:
    key_text = str(key)
    key_lower = key_text.lower()
    if key_lower in SENSITIVE_KEY_NAMES:
        return True
    return any(part in key_lower for part in SENSITIVE_KEY_PARTS)


def format_log_value(key: Any, value: Any) -> str:
    if is_sensitive_key(key):
        return "<redacted>"

    value_text = str(value)
    key_lower = str(key).lower()
    if isinstance(value, str) and any(part in key_lower for part in COMPACT_KEY_PARTS):
        return f"<{len(value)} chars>"

    if len(value_text) > MAX_LOG_VALUE_LENGTH:
        omitted = len(value_text) - MAX_LOG_VALUE_LENGTH
        return f"{value_text[:MAX_LOG_VALUE_LENGTH]}... <truncated {omitted} chars>"

    return value_text


def sanitize_log_message(msg: str) -> str:
    msg = _KEY_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        msg,
    )
    for pattern in _SECRET_URL_PATTERNS:
        msg = pattern.sub(r"\1<redacted>\2", msg)
    return msg


class Logger:
    def __init__(self, name: str):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.DEBUG)

        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        self._console_handler = self._find_console_handler()
        if self._console_handler is None:
            self._console_handler = logging.StreamHandler(sys.stdout)
            self._logger.addHandler(self._console_handler)
        self._console_handler.setLevel(logging.INFO)
        self._console_handler.setFormatter(formatter)

        self._file_handler = self._find_file_handler()
        if self._file_handler is None:
            self._file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
            self._logger.addHandler(self._file_handler)
        self._file_handler.setLevel(logging.DEBUG)
        self._file_handler.setFormatter(formatter)

    def _find_console_handler(self) -> logging.StreamHandler | None:
        for handler in self._logger.handlers:
            if (
                isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, logging.FileHandler)
                and handler.stream is sys.stdout
            ):
                return handler
        return None

    def _find_file_handler(self) -> logging.FileHandler | None:
        target = str(LOG_FILE.resolve())
        for handler in self._logger.handlers:
            if (
                isinstance(handler, logging.FileHandler)
                and handler.baseFilename == target
            ):
                return handler
        return None

    def debug(self, msg: str) -> None:
        self._logger.debug(sanitize_log_message(str(msg)))

    def info(self, msg: str) -> None:
        self._logger.info(sanitize_log_message(str(msg)))

    def warning(self, msg: str) -> None:
        self._logger.warning(sanitize_log_message(str(msg)))

    def error(self, msg: str) -> None:
        self._logger.error(sanitize_log_message(str(msg)))

    def breathe(self) -> None:
        """Insert a blank line in the log file, see function `StreamHandler.emit`"""
        self._file_handler.stream.write("\n")
        self._file_handler.flush()
