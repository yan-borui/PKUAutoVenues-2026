import unittest

from utils.notify import NullNotificationAdapter, SafeNotifier, create_notifier
from utils.recognize import (
    ChaojiyingRecognizer,
    TTShituRecognizer,
    create_recognizer,
)
from utils.settings import NotificationSettings, RecognitionSettings


class FakeLogger:
    def __init__(self):
        self.errors = []

    def debug(self, _message):
        pass

    def info(self, _message):
        pass

    def warning(self, _message):
        pass

    def error(self, message):
        self.errors.append(message)

    def breathe(self):
        pass


class FakeClient:
    pass


class FailingNotificationAdapter:
    method = "failing"

    def send(self, title, content):
        raise TimeoutError("notification timeout")


class AdapterTests(unittest.TestCase):
    def test_recognizer_factory_selects_real_adapter(self):
        logger = FakeLogger()
        client = FakeClient()

        ttshitu = create_recognizer(
            RecognitionSettings("ttshitu", "user", "password"),
            client=client,
            logger=logger,
        )
        chaojiying = create_recognizer(
            RecognitionSettings("chaojiying", "user", "password", "123"),
            client=client,
            logger=logger,
        )

        self.assertIsInstance(ttshitu, TTShituRecognizer)
        self.assertIsInstance(chaojiying, ChaojiyingRecognizer)

    def test_none_notifier_is_a_null_adapter_and_performs_no_io(self):
        notifier = create_notifier(
            NotificationSettings(method="none"),
            logger=FakeLogger(),
        )

        self.assertIsInstance(notifier.adapter, NullNotificationAdapter)
        self.assertTrue(notifier.notify_message("title", "content"))

    def test_safe_notifier_keeps_adapter_failures_out_of_main_flow(self):
        logger = FakeLogger()
        notifier = SafeNotifier(FailingNotificationAdapter(), logger)

        self.assertFalse(notifier.notify_message("title", "content"))
        self.assertIn("notification timeout", logger.errors[0])


if __name__ == "__main__":
    unittest.main()
