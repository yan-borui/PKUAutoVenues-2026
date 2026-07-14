import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from utils.campaign import CampaignRuntime, ReservationCampaign
from utils.client import EpeUnavailableError, TransportUnavailableError
from utils.domain import ReservationRequest, ReservationResult
from utils.errors import AttemptFailureAction, classify_attempt_failure
from utils.recognize import CaptchaRecognitionTransportError


class FakeLogger:
    def debug(self, _message):
        pass

    def info(self, _message):
        pass

    def warning(self, _message):
        pass

    def breathe(self):
        pass


class FakeGateway:
    client = object()


class WindowResetAttempt:
    def __init__(self):
        self.calls = []

    def run(self, client_uid, rejected):
        self.calls.append((client_uid, set(rejected), id(rejected)))
        if len(self.calls) <= 8:
            rejected.add(("60", (("5", "101"),)))
            raise ValueError("consume this window budget")
        return ReservationResult(
            venue="60",
            space="5号",
            selected_time="16:00-17:00",
            trade_no="TRADE-123",
        )


class CampaignTests(unittest.TestCase):
    def test_rejected_reservations_reset_between_windows_and_uid_is_stable(self):
        request = ReservationRequest(
            venues=["60"],
            target_date="2026-06-16",
            target_times=[("16:00", 1)],
            preferred_spaces=[],
            retry_returned_slots=True,
        )
        attempt = WindowResetAttempt()
        runtime = CampaignRuntime(
            sleep=lambda _seconds: None,
            now=lambda: datetime(2026, 6, 13, 12, 11, tzinfo=ZoneInfo("Asia/Shanghai")),
            make_uid=lambda: "stable-uid",
        )
        campaign = ReservationCampaign(
            request=request,
            gateway=FakeGateway(),
            attempt=attempt,
            logger=FakeLogger(),
            runtime=runtime,
        )

        result = campaign.run()

        self.assertEqual(result.trade_no, "TRADE-123")
        self.assertEqual(len(attempt.calls), 9)
        self.assertEqual(attempt.calls[8][1], set())
        self.assertNotEqual(attempt.calls[0][2], attempt.calls[8][2])
        self.assertEqual(
            {client_uid for client_uid, _, _ in attempt.calls},
            {"point-stable-uid"},
        )

    def test_failure_classification_preserves_attempt_budget_policy(self):
        transport = TransportUnavailableError(
            "GET", "https://example.test", 1, TimeoutError("timeout")
        )
        cases = [
            (
                CaptchaRecognitionTransportError(transport),
                AttemptFailureAction.RETRY_CAPTCHA,
            ),
            (transport, AttemptFailureAction.WAIT_FOR_EPE),
            (
                EpeUnavailableError("GET", "/api/test"),
                AttemptFailureAction.WAIT_FOR_EPE,
            ),
            (ValueError("ordinary"), AttemptFailureAction.CONSUME_BUDGET),
        ]

        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.assertIs(classify_attempt_failure(error), expected)


if __name__ == "__main__":
    unittest.main()
