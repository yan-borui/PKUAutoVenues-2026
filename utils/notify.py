import re
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Protocol

from .client import Client, get_response_json
from .config import CONFIG_FILE
from .logger import Logger
from .settings import NotificationSettings, load_settings


class NotificationAdapter(Protocol):
    method: str

    def send(self, title: str, content: str) -> None: ...


class NullNotificationAdapter:
    method = "none"

    def send(self, title: str, content: str) -> None:
        return None


class EmailNotificationAdapter:
    method = "email"

    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password

    def send(self, title: str, content: str) -> None:
        domain = self.email.split("@")[-1]
        if domain == "stu.pku.edu.cn":
            host = "smtphz.qiye.163.com"
        elif domain in {"pku.edu.cn", "qq.com", "163.com", "126.com"}:
            host = f"smtp.{domain}"
        else:
            raise ValueError(
                "Unsupported email address, must end with '@stu.pku.edu.cn', '@pku.edu.cn', '@qq.com', '@163.com' or '@126.com'"
            )

        message = MIMEText(content, "plain")
        message["From"] = formataddr(("PKUAutoVenues", self.email))
        message["To"] = self.email
        message["Subject"] = title
        try:
            server = smtplib.SMTP_SSL(host, port=465)
            server.login(self.email, self.password)
            server.sendmail(self.email, self.email, message.as_string())
            server.quit()
        except Exception as error:
            raise RuntimeError(
                "Maybe the email address or password (authorization code) is incorrect: "
                f"({error.__class__.__name__}) {error}"
            ) from error


class SC3NotificationAdapter:
    method = "sc3"

    def __init__(self, sendkey: str, client: Client) -> None:
        match = re.match(r"^sctp(\d+)t", sendkey)
        if match is None:
            raise ValueError(
                "Invalid SC3 sendkey, must follow a format like 'sctp<number>t...'"
            )
        self.sendkey = sendkey
        self.uid = match.group(1)
        self.client = client

    def send(self, title: str, content: str) -> None:
        response = self.client.post(
            f"https://{self.uid}.push.ft07.com/send/{self.sendkey}.send",
            data={
                "title": title,
                "desp": content.replace("\n", "\n\n"),
                "short": content,
                "tags": "PKUAutoVenues",
            },
            timeout=60.0,
        )
        data = get_response_json(response)
        if data["code"] != 0:
            raise RuntimeError(
                f"Maybe the SC3 sendkey is incorrect: ({data['code']}) {data.get('error')}"
            )


class SCTNotificationAdapter:
    method = "sct"

    def __init__(self, sendkey: str, client: Client) -> None:
        self.sendkey = sendkey
        self.client = client

    def send(self, title: str, content: str) -> None:
        response = self.client.post(
            f"https://sctapi.ftqq.com/{self.sendkey}.send",
            data={
                "title": title,
                "desp": content.replace("\n", "\n\n"),
                "noip": "1",
                "channel": "9",
            },
            timeout=60.0,
        )
        data = get_response_json(response)
        if data["code"] == 40001 and data.get("scode") == 471:
            raise RuntimeError(
                f"SCT exceeded daily sending frequency limit: {data.get('message')}"
            )
        if data["code"] != 0:
            raise RuntimeError(
                f"Maybe the SCT sendkey is incorrect: ({data['code']}) {data.get('message')}"
            )


class BarkNotificationAdapter:
    method = "bark"

    def __init__(self, sendkey: str, client: Client) -> None:
        self.sendkey = sendkey
        self.client = client

    def send(self, title: str, content: str) -> None:
        response = self.client.post(
            f"https://api.day.app/{self.sendkey}",
            data={
                "title": title,
                "body": content,
                "group": "PKUAutoVenues",
                "badge": "1",
            },
            timeout=60.0,
        )
        data = get_response_json(response)
        if data["code"] != 200:
            raise RuntimeError(
                f"Maybe the Bark sendkey is incorrect: ({data['code']}) {data['message']}"
            )


class SafeNotifier:
    def __init__(self, adapter: NotificationAdapter, logger: Logger) -> None:
        self.adapter = adapter
        self.logger = logger

    def notify_message(self, title: str, content: str) -> bool:
        if self.adapter.method == "none":
            self.logger.warning(
                f"Notification method is 'none', skipping notification: {title}"
            )
            self.logger.breathe()
            return True
        try:
            self.adapter.send(title, content)
        except Exception as error:
            self.logger.error(
                f"Failed to send the notification message by {self.adapter.method}: "
                f"({error.__class__.__name__}) {error}"
            )
            self.logger.breathe()
            return False
        self.logger.info(
            f"Sent a notification message by {self.adapter.method}: {title}"
        )
        self.logger.breathe()
        return True


def _build_adapter(
    settings: NotificationSettings,
    client: Client | None,
) -> NotificationAdapter:
    if settings.method == "none":
        return NullNotificationAdapter()
    if settings.method == "email":
        if settings.email is None or settings.password is None:
            raise ValueError("Email notification credentials are required")
        return EmailNotificationAdapter(settings.email, settings.password)
    if settings.sendkey is None:
        raise ValueError(f"{settings.method} sendkey is required")
    http_client = client or Client(settings.method)
    adapter_types = {
        "sc3": SC3NotificationAdapter,
        "sct": SCTNotificationAdapter,
        "bark": BarkNotificationAdapter,
    }
    try:
        adapter_type = adapter_types[settings.method]
    except KeyError as error:
        raise ValueError(
            f"Unsupported notification method: {settings.method}"
        ) from error
    return adapter_type(settings.sendkey, http_client)


def create_notifier(
    settings: NotificationSettings,
    client: Client | None = None,
    logger: Logger | None = None,
) -> SafeNotifier:
    return SafeNotifier(
        _build_adapter(settings, client),
        logger or Logger("notifier"),
    )


class Notifier(SafeNotifier):
    """Compatibility facade; new code should use create_notifier()."""

    def __init__(
        self,
        settings: NotificationSettings | None = None,
        client: Client | None = None,
        logger: Logger | None = None,
    ) -> None:
        settings = settings or load_settings(CONFIG_FILE).notification
        super().__init__(
            _build_adapter(settings, client),
            logger or Logger("notifier"),
        )
