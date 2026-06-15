import unittest

from utils.orders import extract_order_info, recover_unpaid_order


class FakeClient:
    def __init__(self, orders_data):
        self.orders_data = orders_data
        self.calls = []

    def epe_get(self, url, params):
        self.calls.append((url, params))
        return self.orders_data


class OrderRecoveryTest(unittest.TestCase):
    def test_extracts_nested_order_info_from_submit_response(self):
        order = extract_order_info(
            {
                "orderInfo": {
                    "id": 123,
                    "tradeNo": "TRADE-123",
                }
            }
        )

        self.assertEqual(order["id"], 123)
        self.assertEqual(order["tradeNo"], "TRADE-123")

    def test_recovers_matching_unpaid_order_after_ambiguous_submit_failure(self):
        client = FakeClient(
            {
                "content": [
                    {
                        "id": 123,
                        "tradeNo": "TRADE-123",
                        "orderStatus": 1,
                        "payStatus": 1,
                        "venueSiteId": 86,
                        "reservationDate": "2026-06-18",
                        "reservationStartDate": "2026-06-18 21:00",
                        "reservationEndDate": "2026-06-18 22:00",
                        "venueSpaceName": "5号",
                    }
                ]
            }
        )

        order = recover_unpaid_order(
            client,
            venue="86",
            target_date="2026-06-18",
            selected_space="5号",
            begin_time="21:00",
        )

        self.assertEqual(order["tradeNo"], "TRADE-123")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1], {"page": 0, "size": 20})

    def test_does_not_recover_unrelated_unpaid_order(self):
        client = FakeClient(
            {
                "content": [
                    {
                        "id": 456,
                        "tradeNo": "TRADE-456",
                        "orderStatus": 1,
                        "payStatus": 1,
                        "venueSiteId": 60,
                        "reservationDate": "2026-06-19",
                        "reservationStartDate": "2026-06-19 20:00",
                        "venueSpaceName": "2号",
                    }
                ]
            }
        )

        order = recover_unpaid_order(
            client,
            venue="86",
            target_date="2026-06-18",
            selected_space="5号",
            begin_time="21:00",
        )

        self.assertIsNone(order)

    def test_recovers_same_slot_on_different_space_after_unpaid_order_error(self):
        client = FakeClient(
            {
                "content": [
                    {
                        "id": 123,
                        "tradeNo": "TRADE-123",
                        "orderStatus": "1",
                        "payStatus": "1",
                        "venueSiteId": "86",
                        "reservationDate": "2026-06-18",
                        "reservationStartDate": "2026-06-18 21:00",
                        "reservationEndDate": "2026-06-18 22:00",
                        "venueSpaceName": "5号",
                    }
                ]
            }
        )

        order = recover_unpaid_order(
            client,
            venue="86",
            target_date="2026-06-18",
            selected_space=None,
            begin_time="21:00",
        )

        self.assertEqual(order["venueSpaceName"], "5号")

    def test_does_not_recover_order_without_enough_matching_metadata(self):
        client = FakeClient(
            {
                "content": [
                    {
                        "id": 789,
                        "tradeNo": "TRADE-789",
                        "orderStatus": 1,
                        "payStatus": 1,
                    }
                ]
            }
        )

        order = recover_unpaid_order(
            client,
            venue="86",
            target_date="2026-06-18",
            selected_space=None,
            begin_time="21:00",
        )

        self.assertIsNone(order)


if __name__ == "__main__":
    unittest.main()
