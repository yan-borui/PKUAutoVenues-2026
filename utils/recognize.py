import time
from typing import Protocol

from .client import Client, TransportUnavailableError, get_response_json
from .config import CONFIG_FILE
from .encrypt import md5_hash
from .logger import Logger
from .settings import RecognitionSettings, load_settings


class CaptchaRecognitionTransportError(Exception):
    """Raised when the external captcha recognition service is unavailable."""

    def __init__(self, cause: TransportUnavailableError):
        self.cause = cause
        super().__init__(str(cause))


class CaptchaRecognizer(Protocol):
    def recognize_captcha(
        self, image_base64: str, words: list[str]
    ) -> list[tuple[int, int]]: ...


class BaseRecognizer:
    method: str

    def __init__(self, client: Client, logger: Logger) -> None:
        self._client = client
        self._logger = logger

    def recognize_captcha(
        self, image_base64: str, words: list[str]
    ) -> list[tuple[int, int]]:
        start = time.perf_counter()
        result = self._recognize(image_base64, words)
        elapsed = time.perf_counter() - start
        self._logger.info(
            f"Recognized captcha by {self.method} in {elapsed:.2f} seconds: {result}"
        )
        self._logger.breathe()

        try:
            points = [
                (int(item.split(",")[0]), int(item.split(",")[1]))
                for item in result.split("|")
            ]
        except (IndexError, ValueError) as error:
            raise ValueError(
                f"Invalid coordinate response from {self.method}: {result!r}"
            ) from error
        if len(points) != len(words):
            raise ValueError(
                f"The number of recognized coordinates ({len(points)}) does not match the number of words ({len(words)})"
            )
        return points

    def _recognize(self, image_base64: str, words: list[str]) -> str:
        raise NotImplementedError


class TTShituRecognizer(BaseRecognizer):
    method = "ttshitu"

    def __init__(
        self,
        settings: RecognitionSettings,
        client: Client,
        logger: Logger,
    ) -> None:
        super().__init__(client, logger)
        self._settings = settings

    def _recognize(self, image_base64: str, words: list[str]) -> str:
        try:
            response = self._client.post(
                "http://api.ttshitu.com/predict",
                data={
                    "username": self._settings.username,
                    "password": self._settings.password,
                    "typeid": "43",
                    "image": image_base64,
                    "content": "".join(words),
                },
                timeout=2.0,
                max_attempts=1,
            )
        except TransportUnavailableError as error:
            raise CaptchaRecognitionTransportError(error) from error
        return get_response_json(response)["data"]["result"]


class ChaojiyingRecognizer(BaseRecognizer):
    method = "chaojiying"

    def __init__(
        self,
        settings: RecognitionSettings,
        client: Client,
        logger: Logger,
    ) -> None:
        if settings.softid is None:
            raise ValueError("Chaojiying softid is required")
        super().__init__(client, logger)
        self._settings = settings

    def _recognize(self, image_base64: str, words: list[str]) -> str:
        response = self._client.post(
            "https://upload.chaojiying.net/Upload/Processing.php",
            data={
                "user": self._settings.username,
                "pass2": md5_hash(self._settings.password),
                "softid": self._settings.softid,
                "codetype": "9801",
                "str_debug": f"{{8a:{','.join(words)}/8a}}",
                "file_base64": image_base64,
            },
            timeout=4.0,
        )
        return get_response_json(response)["pic_str"]


def create_recognizer(
    settings: RecognitionSettings,
    client: Client | None = None,
    logger: Logger | None = None,
) -> CaptchaRecognizer:
    adapter_types = {
        "ttshitu": TTShituRecognizer,
        "chaojiying": ChaojiyingRecognizer,
    }
    try:
        adapter_type = adapter_types[settings.method]
    except KeyError as error:
        raise ValueError(
            f"Unsupported recognition method: {settings.method}"
        ) from error
    return adapter_type(
        settings,
        client or Client(settings.method),
        logger or Logger("recognizer"),
    )


class Recognizer:
    """Compatibility facade; new code should use create_recognizer()."""

    def __init__(
        self,
        settings: RecognitionSettings | None = None,
        client: Client | None = None,
        logger: Logger | None = None,
    ) -> None:
        settings = settings or load_settings(CONFIG_FILE).recognition
        self._settings = settings
        self._client = client or Client(settings.method)
        self._logger = logger or Logger("recognizer")
        self._adapter = create_recognizer(
            settings,
            client=self._client,
            logger=self._logger,
        )

    def recognize_captcha(
        self, image_base64: str, words: list[str]
    ) -> list[tuple[int, int]]:
        return self._adapter.recognize_captcha(image_base64, words)

    def _ttshitu(self, image_base64: str, words: list[str]) -> str:
        settings = getattr(self, "_settings", None)
        if settings is None:
            settings = load_settings(CONFIG_FILE).recognition
        logger = getattr(self, "_logger", None)
        if logger is None:
            logger = Logger("recognizer")
        adapter = TTShituRecognizer(
            settings,
            self._client,
            logger,
        )
        return adapter._recognize(image_base64, words)

    def _chaojiying(self, image_base64: str, words: list[str]) -> str:
        adapter = ChaojiyingRecognizer(
            self._settings,
            self._client,
            self._logger,
        )
        return adapter._recognize(image_base64, words)
