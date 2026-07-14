import time

from .client import Client, TransportUnavailableError, get_response_json
from .config import CONFIG_FILE
from .logger import Logger
from .encrypt import md5_hash
from .settings import RecognitionSettings, load_settings


class CaptchaRecognitionTransportError(Exception):
    """Raised when the external captcha recognition service is unavailable."""

    def __init__(self, cause: TransportUnavailableError):
        self.cause = cause
        super().__init__(str(cause))


class Recognizer:
    def __init__(
        self,
        settings: RecognitionSettings | None = None,
        client: Client | None = None,
        logger: Logger | None = None,
    ):
        self._settings = settings or load_settings(CONFIG_FILE).recognition
        self._method = self._settings.method
        self._client = client or Client(self._method)
        self._logger = logger or Logger("recognizer")

    def recognize_captcha(
        self, image_base64: str, words: list[str]
    ) -> list[tuple[int, int]]:
        start = time.perf_counter()

        if self._method == "ttshitu":
            result = self._ttshitu(image_base64, words)
        elif self._method == "chaojiying":
            result = self._chaojiying(image_base64, words)
        else:
            raise Exception(
                "Invalid recognition method, must be 'ttshitu' or 'chaojiying'"
            )

        elapsed = time.perf_counter() - start
        self._logger.info(
            f"Recognized captcha by {self._method} in {elapsed:.2f} seconds: {result}"
        )
        self._logger.breathe()

        # "234,47|168,90|101,63" -> [(234, 47), (168, 90), (101, 63)]
        result_list = [
            (int(item.split(",")[0]), int(item.split(",")[1]))
            for item in result.split("|")
        ]

        if len(result_list) != len(words):
            # 认为是这个 case 超出模型的识别能力，重试可能没用，直接 raise Exception 让主流程换一张图片
            raise Exception(
                f"The number of recognized coordinates ({len(result_list)}) does not match the number of words ({len(words)})"
            )

        return result_list

    def _ttshitu(self, image_base64: str, words: list[str]) -> str:
        settings = getattr(self, "_settings", None)
        if settings is None:
            settings = load_settings(CONFIG_FILE).recognition
        try:
            resp = self._client.post(
                "http://api.ttshitu.com/predict",
                data={
                    "username": settings.username,
                    "password": settings.password,
                    "typeid": "43",  # 快速点选，http://www.ttshitu.com/news/9c2cae0531a147d2bafac3cd737109e7.html
                    "image": image_base64,
                    "content": "".join(words),
                },
                timeout=2.0,
                max_attempts=1,
            )
        except TransportUnavailableError as e:
            raise CaptchaRecognitionTransportError(e) from e

        return get_response_json(resp)["data"]["result"]

    def _chaojiying(self, image_base64: str, words: list[str]) -> str:
        settings = self._settings
        if settings.softid is None:
            raise ValueError("Chaojiying softid is required")
        resp = self._client.post(
            "https://upload.chaojiying.net/Upload/Processing.php",
            data={
                "user": settings.username,
                "pass2": md5_hash(settings.password),
                "softid": settings.softid,
                "codetype": "9801",
                "str_debug": f"{{8a:{','.join(words)}/8a}}",
                "file_base64": image_base64,
            },
            timeout=4.0,
        )
        return get_response_json(resp)["pic_str"]
