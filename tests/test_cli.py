import unittest
from contextlib import redirect_stderr
from io import StringIO

from main import parse_cli_args


class CliTests(unittest.TestCase):
    def test_normalizes_aliases_times_and_venue_specific_spaces(self):
        command = parse_cli_args(
            [
                "-v",
                "qdb",
                "54",
                "qdb",
                "-d",
                "2026-07-20",
                "-t",
                "19:00/2",
                "19:00",
                "-s",
                "5",
                "--venue-spaces",
                "qdb:10,9",
                "--no-reflow",
                "--skip-pay",
            ]
        )

        self.assertEqual(command.request.venues, ["60", "86"])
        self.assertEqual(
            command.request.target_times,
            [("19:00", 2), ("19:00", 1)],
        )
        self.assertEqual(
            command.request.preferred_spaces,
            {"60": ["10号", "9号"], "86": ["5号"]},
        )
        self.assertFalse(command.request.retry_returned_slots)
        self.assertTrue(command.skip_pay)

    def test_rejects_invalid_calendar_date(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_cli_args(["-v", "qdb", "-d", "2026-02-30", "-t", "19:00"])


if __name__ == "__main__":
    unittest.main()
