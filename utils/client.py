import time
from requests import sessions
from requests.models import Response
from typing import Literal, Optional

from .logger import Logger
from .encrypt import calculate_sign


class EpeAPIError(Exception):

    def __init__(self, method: str, path: str, code: int | None, message: str):
        self.method = method
        self.path = path
        self.code = code
        self.message = message
        super().__init__(f"{method} {path} failed: ({code}) {message}")


class EpeUnavailableError(Exception):
    status_code = 502

    def __init__(self, method: str, path: str):
        self.method = method
        self.path = path
        super().__init__(f"{method} {path} failed: HTTP 502 Bad Gateway")


class TransportUnavailableError(Exception):
    def __init__(self, method: str, url: str, attempts: int, cause: Exception):
        self.method = method
        self.url = url
        self.attempts = attempts
        self.cause = cause
        super().__init__(
            f"{method} {url} failed after {attempts} transport attempts: {cause}"
        )


class Client:

    def __init__(self, name: str):
        self.session = sessions.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            }
        )
        self._logger = Logger(name)
        self._logger.debug(f"Session initialized")
        self._logger.breathe()

    def _log_json(self, data, level: int) -> None:
        indent = "  " * level

        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    if len(v.keys()) >= 30:  # reservationDateSpaceInfo is too long
                        self._logger.debug(f"{indent}{k}: {v}")
                    else:
                        self._logger.debug(f"{indent}{k}:")
                        self._log_json(v, level + 1)
                elif isinstance(v, list):
                    self._logger.debug(f"{indent}{k}: Array({len(v)}) {v}")
                    self._log_json(v, level + 1)
                else:
                    self._logger.debug(f"{indent}{k}: {v}")

        elif isinstance(data, list):
            if level == 1:
                self._logger.debug(f"{indent}Array({len(data)}) {data}")
            if len(data) >= 1 and isinstance(data[0], (dict, list)):
                if isinstance(data[0], dict):
                    self._logger.debug(f"{indent}[0]:")
                    self._log_json(data[0], level + 1)
                else:
                    self._logger.debug(f"{indent}[0]: Array({len(data[0])}) {data[0]}")
                    self._log_json(data[0], level + 1)
                if len(data) >= 2:
                    self._logger.debug(f"{indent}[1]: ... ({len(data)} items in all)")

        else:
            self._logger.debug(f"{indent}{data}")

    def _request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        max_attempts: int = 3,
        retry_delay: float = 0.5,
        **kwargs,
    ) -> Response:
        self._logger.debug(f"Sending request: {method} {url}")

        for key, value in kwargs.items():
            if value is not None and value != {}:
                if key in ["params", "data", "headers"]:
                    self._logger.debug(f"  {key}:")
                    for k, v in value.items():
                        self._logger.debug(f"    {k}: {v}")
                elif not (key == "allow_redirects" and value is True):
                    self._logger.debug(f"  {key}: {value}")
        self._logger.breathe()

        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                break
            except Exception as e:
                last_error = e
                self._logger.warning(f"Attempt {attempt}/{max_attempts} failed: {e}")
                if attempt < max_attempts:
                    self._logger.warning(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                self._logger.breathe()
        else:
            self._logger.error(f"All {max_attempts} attempts failed, exiting")
            assert last_error is not None
            raise TransportUnavailableError(
                method,
                url,
                max_attempts,
                last_error,
            ) from last_error

        self._logger.debug(f"Response status: {resp.status_code}")

        try:
            resp_json = resp.json()
            self._logger.debug(f"Response JSON:")
            self._log_json(resp_json, 1)
        except Exception as e:
            resp_text = resp.text.strip()
            if resp_text.startswith("<!DOCTYPE"):
                resp_text = "(HTML)"
            self._logger.debug(
                f"Response text: {resp_text[:400] + '...' if len(resp_text) > 400 else resp_text}"
            )
        self._logger.breathe()

        self._logger.debug(f"Session cookies:")
        for cookie in self.session.cookies:
            self._logger.debug(
                f"  {cookie.name}: {cookie.value} (domain={cookie.domain}; path={cookie.path})"
            )
        self._logger.breathe()

        return resp

    def get(
        self,
        url: str,
        params: dict = {},
        headers: dict = {},
        timeout=10.0,
        allow_redirects=True,
        **kwargs,
    ) -> Response:
        kwargs.pop("data", None)
        return self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
            **kwargs,
        )

    def post(
        self,
        url: str,
        data: dict = {},
        headers: dict = {},
        timeout=10.0,
        allow_redirects=True,
        **kwargs,
    ) -> Response:
        kwargs.pop("params", None)
        return self._request(
            "POST",
            url,
            data=data,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
            **kwargs,
        )


class EpeClient(Client):

    def __init__(self, name: str):
        super().__init__(name)
        self.cg_auth_token: Optional[str] = None  # Local storage: dataSix

    def _epe_request(self, method: Literal["GET", "POST"], url: str, **kwargs) -> dict:
        # emitAjax

        timestamp = str(int(time.time() * 1000))

        if method == "GET":
            params = dict(kwargs["params"])
            params["nocache"] = timestamp
            kwargs["params"] = params
            payload = params
        else:
            payload = dict(kwargs["data"])

        path = url.removeprefix("https://epe.pku.edu.cn/venue-server")
        sign = calculate_sign(timestamp, path, payload)

        headers = dict(kwargs["headers"])
        headers.update(
            {
                "app-key": "8fceb735082b5a529312040b58ea780b",
                "timestamp": timestamp,
                "sign": sign,
            }
        )
        if self.cg_auth_token:
            headers["cgAuthorization"] = self.cg_auth_token
        kwargs["headers"] = headers

        resp = super()._request(method, url, **kwargs)
        # 可能会 raise Exception，保持 message 让父过程 catch

        if resp.status_code == EpeUnavailableError.status_code:
            raise EpeUnavailableError(method, path)

        try:
            resp_json = resp.json()
        except Exception as e:
            raise Exception(f"Failed to parse response as JSON: {e}")

        if not isinstance(resp_json, dict):
            raise Exception(
                f"Expected response JSON object, got {type(resp_json).__name__}"
            )

        code = resp_json.get("code")
        message = resp_json.get("message", "")
        data = resp_json.get("data", {})

        if code != 200:
            raise EpeAPIError(method, path, code, message)

        return data

    def epe_get(
        self,
        url: str,
        params: dict = {},
        headers: dict = {},
        timeout=10.0,
        allow_redirects=True,
        **kwargs,
    ) -> dict:
        kwargs.pop("data", None)
        return self._epe_request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
            **kwargs,
        )

    def epe_post(
        self,
        url: str,
        data: dict = {},
        headers: dict = {},
        timeout=10.0,
        allow_redirects=True,
        **kwargs,
    ) -> dict:
        kwargs.pop("params", None)
        return self._epe_request(
            "POST",
            url,
            data=data,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
            **kwargs,
        )
