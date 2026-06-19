import logging
import unittest

from utils.client import Client, EpeClient
from utils.logger import Logger, format_log_value, sanitize_log_message


class CaptureLogger:
    def __init__(self):
        self.messages = []

    def debug(self, message):
        self.messages.append(message)


class FakeResponse:
    status_code = 200
    text = '{"code": 200, "message": "ok", "data": {"ok": true}}'

    def __init__(self):
        self.json_calls = 0

    def json(self):
        self.json_calls += 1
        return {
            "code": 200,
            "message": "ok",
            "data": {"ok": True},
        }


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.cookies = []

    def request(self, _method, _url, **_kwargs):
        return self.response


class LoggingTests(unittest.TestCase):
    def test_epe_request_reuses_json_parsed_for_logging(self):
        response = FakeResponse()
        client = EpeClient("test-json-cache")
        client.session = FakeSession(response)

        data = client.epe_get(
            "https://epe.pku.edu.cn/venue-server/api/test",
            params={},
        )

        self.assertEqual(data, {"ok": True})
        self.assertEqual(response.json_calls, 1)

    def test_same_named_logger_does_not_duplicate_handlers(self):
        logger_name = "test-handler-dedup"
        raw_logger = logging.getLogger(logger_name)
        for handler in list(raw_logger.handlers):
            raw_logger.removeHandler(handler)

        try:
            Logger(logger_name)
            Logger(logger_name)

            self.assertEqual(len(raw_logger.handlers), 2)
            self.assertEqual(
                sum(
                    isinstance(handler, logging.FileHandler)
                    for handler in raw_logger.handlers
                ),
                1,
            )
        finally:
            for handler in list(raw_logger.handlers):
                raw_logger.removeHandler(handler)
                handler.close()

    def test_sensitive_and_large_values_are_safe_to_log(self):
        sanitized = sanitize_log_message(
            "captcha token: abc123 password=secret phone: 13800000000"
        )

        self.assertNotIn("abc123", sanitized)
        self.assertNotIn("secret", sanitized)
        self.assertNotIn("13800000000", sanitized)
        self.assertIn("<redacted>", sanitized)
        self.assertEqual(format_log_value("file_base64", "x" * 1000), "<1000 chars>")

    def test_json_debug_log_keeps_shape_without_full_payloads(self):
        capture = CaptureLogger()
        client = object.__new__(Client)
        client._logger = capture

        client._log_json(
            {
                "password": "secret",
                "imageBase64": "x" * 500,
                "readkey": "SCT-secret-read-key",
                "items": [
                    {"id": 1, "token": "abc"},
                    {"id": 2, "name": "second"},
                    {"id": 3, "name": "third"},
                ],
            },
            1,
        )

        output = "\n".join(capture.messages)
        self.assertNotIn("secret", output)
        self.assertNotIn("abc", output)
        self.assertNotIn("SCT-secret-read-key", output)
        self.assertIn("imageBase64: <500 chars>", output)
        self.assertIn("items: Array(3)", output)
        self.assertIn("[2]: ... (1 more items)", output)


if __name__ == "__main__":
    unittest.main()
