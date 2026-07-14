import unittest
from unittest.mock import Mock

from utils.recognize import TTShituRecognizer
from utils.settings import RecognitionSettings


class FakeLogger:
    def info(self, _message):
        pass

    def breathe(self):
        pass


class TTShituRecognitionTests(unittest.TestCase):
    def test_ttshitu_uses_one_short_request(self):
        client = Mock()
        recognizer = TTShituRecognizer(
            RecognitionSettings("ttshitu", "user", "password"),
            client,
            FakeLogger(),
        )
        response = Mock()
        response.json.return_value = {"data": {"result": "1,2"}}
        client.post.return_value = response

        result = recognizer.recognize_captcha("image", ["字"])

        self.assertEqual(result, [(1, 2)])
        client.post.assert_called_once()
        _, kwargs = client.post.call_args
        self.assertEqual(kwargs["timeout"], 2.0)
        self.assertEqual(kwargs["max_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
