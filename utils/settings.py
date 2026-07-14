from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when config.ini is missing or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class IAAASettings:
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class EpeSettings:
    phone: str


@dataclass(frozen=True, slots=True)
class RecognitionSettings:
    method: str
    username: str
    password: str
    softid: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    method: str
    email: str | None = None
    password: str | None = None
    sendkey: str | None = None


@dataclass(frozen=True, slots=True)
class AppSettings:
    iaaa: IAAASettings
    epe: EpeSettings
    recognition: RecognitionSettings
    notification: NotificationSettings


def _required(config: ConfigParser, section: str, option: str) -> str:
    if not config.has_section(section):
        raise ConfigurationError(f"Missing config section [{section}]")

    value = config.get(section, option, fallback="").strip()
    if not value:
        raise ConfigurationError(f"Missing config value [{section}] {option}")
    return value


def _load_recognition(config: ConfigParser) -> RecognitionSettings:
    method = _required(config, "recognize", "method").lower()
    if method == "ttshitu":
        section = "recognize:ttshitu"
        return RecognitionSettings(
            method=method,
            username=_required(config, section, "username"),
            password=_required(config, section, "password"),
        )
    if method == "chaojiying":
        section = "recognize:chaojiying"
        return RecognitionSettings(
            method=method,
            username=_required(config, section, "username"),
            password=_required(config, section, "password"),
            softid=_required(config, section, "softid"),
        )
    raise ConfigurationError(
        "Invalid [recognize] method; expected 'ttshitu' or 'chaojiying'"
    )


def _load_notification(config: ConfigParser) -> NotificationSettings:
    method = config.get("notify", "method", fallback="none").strip().lower()
    if method == "none":
        return NotificationSettings(method=method)
    if method == "email":
        section = "notify:email"
        return NotificationSettings(
            method=method,
            email=_required(config, section, "email"),
            password=_required(config, section, "password"),
        )
    if method in {"sc3", "sct", "bark"}:
        return NotificationSettings(
            method=method,
            sendkey=_required(config, f"notify:{method}", "sendkey"),
        )
    raise ConfigurationError(
        "Invalid [notify] method; expected 'email', 'sc3', 'sct', 'bark' or 'none'"
    )


def load_settings(path: Path) -> AppSettings:
    config = ConfigParser()
    loaded = config.read(path, encoding="utf-8")
    if not loaded:
        raise ConfigurationError(
            f"Config file not found: {path}. Copy config.sample.ini to config.ini first."
        )

    return AppSettings(
        iaaa=IAAASettings(
            username=_required(config, "iaaa", "username"),
            password=_required(config, "iaaa", "password"),
        ),
        epe=EpeSettings(phone=_required(config, "epe", "phone")),
        recognition=_load_recognition(config),
        notification=_load_notification(config),
    )
