import unittest
from unittest.mock import patch

from main import find_reservation, select_reservation

TARGET_DATE = "2026-06-16"


class FakeLogger:

    def debug(self, _message):
        pass

    def info(self, _message):
        pass

    def warning(self, _message):
        pass

    def breathe(self):
        pass


class FakeClient:

    def __init__(self, venue_data):
        self.venue_data = venue_data
        self.requested_venues = []

    def epe_get(self, _url, params):
        venue = params["venueSiteId"]
        self.requested_venues.append(venue)
        return self.venue_data[venue]


def reservation_info(*space_names):
    slots = [
        {"id": 101, "beginTime": "16:00", "endTime": "17:00"},
        {"id": 102, "beginTime": "17:00", "endTime": "18:00"},
    ]
    spaces = []
    for index, space_name in enumerate(space_names, start=1):
        spaces.append(
            {
                "id": index,
                "spaceName": space_name,
                "101": {"reservationStatus": 1, "orderFee": 20},
                "102": {"reservationStatus": 1, "orderFee": 20},
            }
        )
    return {
        "spaceTimeInfo": slots,
        "reservationDateSpaceInfo": {TARGET_DATE: spaces},
    }


class ReservationSelectionTests(unittest.TestCase):

    def setUp(self):
        self.logger = FakeLogger()

    def test_rejected_selection_is_not_selected_again(self):
        info = reservation_info("1号", "5号")
        rejected = set()

        first = select_reservation(
            info,
            venue="60",
            target_date=TARGET_DATE,
            target_times=[("16:00", 1)],
            preferred_spaces=["5号", "1号"],
            rejected_reservations=rejected,
            logger=self.logger,
        )
        self.assertIsNotNone(first)
        self.assertEqual(first.space, "5号")

        rejected.add(first.key)
        second = select_reservation(
            info,
            venue="60",
            target_date=TARGET_DATE,
            target_times=[("16:00", 1)],
            preferred_spaces=["5号", "1号"],
            rejected_reservations=rejected,
            logger=self.logger,
        )
        self.assertIsNotNone(second)
        self.assertEqual(second.space, "1号")

    def test_rejecting_long_selection_keeps_shorter_fallback_available(self):
        info = reservation_info("5号")
        rejected = set()

        long_selection = select_reservation(
            info,
            venue="60",
            target_date=TARGET_DATE,
            target_times=[("16:00", 2)],
            preferred_spaces=["5号"],
            rejected_reservations=rejected,
            logger=self.logger,
        )
        rejected.add(long_selection.key)

        shorter_selection = select_reservation(
            info,
            venue="60",
            target_date=TARGET_DATE,
            target_times=[("16:00", 2), ("16:00", 1)],
            preferred_spaces=["5号"],
            rejected_reservations=rejected,
            logger=self.logger,
        )
        self.assertIsNotNone(shorter_selection)
        self.assertEqual(len(shorter_selection.trades), 1)

    def test_first_available_venue_short_circuits_fallback_request(self):
        client = FakeClient(
            {
                "60": reservation_info("1号"),
                "86": reservation_info("2号"),
            }
        )

        selection = find_reservation(
            client=client,
            venues=["60", "86"],
            target_date=TARGET_DATE,
            target_times=[("16:00", 1)],
            preferred_spaces=[],
            rejected_reservations=set(),
            logger=self.logger,
        )

        self.assertEqual(selection.venue, "60")
        self.assertEqual(client.requested_venues, ["60"])

    def test_falls_back_to_next_venue_when_first_has_no_candidate(self):
        client = FakeClient(
            {
                "60": reservation_info(),
                "86": reservation_info("2号"),
            }
        )

        selection = find_reservation(
            client=client,
            venues=["60", "86"],
            target_date=TARGET_DATE,
            target_times=[("16:00", 1)],
            preferred_spaces=[],
            rejected_reservations=set(),
            logger=self.logger,
        )

        self.assertEqual(selection.venue, "86")
        self.assertEqual(client.requested_venues, ["60", "86"])

    def test_venue_specific_spaces_apply_to_matching_venue(self):
        client = FakeClient(
            {
                "60": reservation_info("1号", "9号"),
                "86": reservation_info("2号"),
            }
        )

        selection = find_reservation(
            client=client,
            venues=["60", "86"],
            target_date=TARGET_DATE,
            target_times=[("16:00", 1)],
            preferred_spaces={"60": ["9号"]},
            rejected_reservations=set(),
            logger=self.logger,
        )

        self.assertEqual(selection.venue, "60")
        self.assertEqual(selection.space, "9号")
        self.assertEqual(client.requested_venues, ["60"])

    def test_venue_specific_spaces_do_not_apply_to_fallback_venue(self):
        client = FakeClient(
            {
                "60": reservation_info(),
                "86": reservation_info("9号", "1号"),
            }
        )

        with patch("main.random.choice", return_value="1号"):
            selection = find_reservation(
                client=client,
                venues=["60", "86"],
                target_date=TARGET_DATE,
                target_times=[("16:00", 1)],
                preferred_spaces={"60": ["9号"]},
                rejected_reservations=set(),
                logger=self.logger,
            )

        self.assertEqual(selection.venue, "86")
        self.assertEqual(selection.space, "1号")
        self.assertEqual(client.requested_venues, ["60", "86"])


if __name__ == "__main__":
    unittest.main()
