import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from main import (
    build_reservation_windows,
    run_reservation_window,
    run_with_transport_recovery,
    wait_for_epe,
)
from utils.client import EpeClient, EpeUnavailableError, TransportUnavailableError
from utils.recognize import CaptchaRecognitionTransportError


class FakeLogger:
    def info(self, _message):
        pass

    def warning(self, _message):
        pass

    def breathe(self):
        pass


class RetryScheduleTests(unittest.TestCase):
    def setUp(self):
        self.logger = FakeLogger()

    def test_http_502_is_typed_before_json_parsing(self):
        response = Mock(status_code=502)
        response.json.side_effect = AssertionError(
            "502 body must not be parsed as JSON"
        )
        client = object.__new__(EpeClient)
        client.cg_auth_token = None

        with patch("utils.client.Client._request", return_value=response):
            with self.assertRaises(EpeUnavailableError):
                client._epe_request(
                    "GET",
                    "https://epe.pku.edu.cn/venue-server/api/test",
                    params={},
                    headers={},
                )

        response.json.assert_not_called()

    def test_502_does_not_consume_reservation_attempt_budget(self):
        result = object()
        attempts = 0
        heartbeat_calls = 0

        def attempt():
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise TransportUnavailableError(
                    "GET",
                    "https://epe.pku.edu.cn/venue-server/api/test",
                    3,
                    TimeoutError("timed out"),
                )
            if attempts <= 3:
                raise EpeUnavailableError("GET", "/api/test")
            return result

        def heartbeat():
            nonlocal heartbeat_calls
            heartbeat_calls += 1

        actual = run_reservation_window(
            attempt=attempt,
            heartbeat=heartbeat,
            max_attempts=1,
            logger=self.logger,
            sleep=lambda _seconds: None,
        )

        self.assertIs(actual, result)
        self.assertEqual(attempts, 4)
        self.assertEqual(heartbeat_calls, 3)

    def test_ordinary_failures_consume_attempt_budget(self):
        attempts = 0

        def attempt():
            nonlocal attempts
            attempts += 1
            raise ValueError("no available space")

        actual = run_reservation_window(
            attempt=attempt,
            heartbeat=lambda: None,
            max_attempts=2,
            logger=self.logger,
            sleep=lambda _seconds: None,
        )

        self.assertIsNone(actual)
        self.assertEqual(attempts, 2)

    def test_captcha_service_timeout_retries_without_epe_heartbeat(self):
        result = object()
        attempts = 0
        heartbeat_calls = 0

        def attempt():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise CaptchaRecognitionTransportError(
                    TransportUnavailableError(
                        "POST",
                        "http://api.ttshitu.com/predict",
                        1,
                        TimeoutError("timed out"),
                    )
                )
            return result

        def heartbeat():
            nonlocal heartbeat_calls
            heartbeat_calls += 1

        actual = run_reservation_window(
            attempt=attempt,
            heartbeat=heartbeat,
            max_attempts=1,
            logger=self.logger,
            sleep=lambda _seconds: None,
        )

        self.assertIs(actual, result)
        self.assertEqual(attempts, 2)
        self.assertEqual(heartbeat_calls, 0)

    def test_transport_failure_retries_until_action_succeeds(self):
        attempts = 0
        sleeps = []
        result = object()
        now = datetime(2026, 6, 22, 11, 59, tzinfo=ZoneInfo("Asia/Shanghai"))

        def action():
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                raise TransportUnavailableError(
                    "GET",
                    "https://epe.pku.edu.cn/venue-server/loginto",
                    3,
                    TimeoutError("timed out"),
                )
            return result

        actual = run_with_transport_recovery(
            action=action,
            retry_until=now + timedelta(minutes=1),
            label="login",
            logger=self.logger,
            sleep=sleeps.append,
            now=lambda: now,
        )

        self.assertIs(actual, result)
        self.assertEqual(attempts, 3)
        self.assertEqual(sleeps, [1.0, 1.0])

    def test_transport_recovery_does_not_retry_ordinary_errors(self):
        attempts = 0
        now = datetime(2026, 6, 22, 11, 59, tzinfo=ZoneInfo("Asia/Shanghai"))

        def action():
            nonlocal attempts
            attempts += 1
            raise ValueError("bad credentials")

        with self.assertRaisesRegex(ValueError, "bad credentials"):
            run_with_transport_recovery(
                action=action,
                retry_until=now + timedelta(minutes=1),
                label="login",
                logger=self.logger,
                sleep=lambda _seconds: None,
                now=lambda: now,
            )

        self.assertEqual(attempts, 1)

    def test_default_schedule_gives_each_window_eight_attempts(self):
        release_time = datetime(2026, 6, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        windows = build_reservation_windows(
            release_time,
            retry_returned_slots=True,
        )

        self.assertEqual([window.max_attempts for window in windows], [8, 8, 8, 8])
        self.assertEqual(
            [window.start_at for window in windows],
            [
                release_time,
                release_time + timedelta(minutes=11),
                release_time + timedelta(minutes=12),
                release_time + timedelta(minutes=13),
            ],
        )

    def test_disabling_returned_slots_uses_all_attempts_at_release(self):
        release_time = datetime(2026, 6, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        windows = build_reservation_windows(
            release_time,
            retry_returned_slots=False,
        )

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].start_at, release_time)
        self.assertEqual(windows[0].max_attempts, 8)

    def test_heartbeat_polls_once_per_second_until_epe_recovers(self):
        client = Mock()
        client.epe_get.side_effect = [
            EpeUnavailableError("GET", "/api/reservation/day/info"),
            TransportUnavailableError(
                "GET",
                "https://epe.pku.edu.cn/venue-server/api/reservation/day/info",
                3,
                TimeoutError("timed out"),
            ),
            {},
        ]
        sleeps = []

        wait_for_epe(
            client=client,
            venue="86",
            target_date="2026-06-20",
            logger=self.logger,
            sleep=sleeps.append,
        )

        self.assertEqual(sleeps, [1, 1, 1])
        self.assertEqual(client.epe_get.call_count, 3)
        for call in client.epe_get.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 1.0)
            self.assertEqual(call.kwargs["max_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
