import unittest
from unittest.mock import Mock

from utils.recognize import Recognizer


class TTShituRecognitionTests(unittest.TestCase):
    def test_ttshitu_uses_one_short_request(self):
        recognizer = object.__new__(Recognizer)
        recognizer._client = Mock()
        response = Mock()
        response.json.return_value = {"data": {"result": "1,2"}}
        recognizer._client.post.return_value = response

        result = recognizer._ttshitu("image", ["字"])

        self.assertEqual(result, "1,2")
        recognizer._client.post.assert_called_once()
        _, kwargs = recognizer._client.post.call_args
        self.assertEqual(kwargs["timeout"], 2.0)
        self.assertEqual(kwargs["max_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
