import unittest

from utils.domain import CaptchaChallenge, ReservationSelection, ReservationTrade
from utils.epe import EpeGateway, EpeProtocolError, RESERVATION_INFO_URL
from utils.settings import (
    AppSettings,
    EpeSettings,
    IAAASettings,
    NotificationSettings,
    RecognitionSettings,
)


class FakeLogger:
    def debug(self, _message):
        pass

    def info(self, _message):
        pass

    def breathe(self):
        pass


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeSession:
    def __init__(self):
        self.cookies = {"sso_pku_token": "sso-token"}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.session = FakeSession()
        self.cg_auth_token = None
        self.get_responses = {}
        self.post_responses = {}

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.get_responses.get(url, FakeResponse({}))

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.post_responses.get(url, FakeResponse({}))

    def epe_get(self, url, **kwargs):
        self.calls.append(("epe_get", url, kwargs))
        return self.get_responses[url]

    def epe_post(self, url, **kwargs):
        self.calls.append(("epe_post", url, kwargs))
        return self.post_responses[url]


def settings():
    return AppSettings(
        iaaa=IAAASettings(username="student", password="password"),
        epe=EpeSettings(phone="13800000000"),
        recognition=RecognitionSettings(
            method="ttshitu", username="captcha", password="secret"
        ),
        notification=NotificationSettings(method="none"),
    )


class EpeGatewayTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.gateway = EpeGateway(self.client, settings(), FakeLogger())

    def test_fetch_availability_parses_wire_shape_at_protocol_seam(self):
        self.client.get_responses[RESERVATION_INFO_URL] = {
            "spaceTimeInfo": [{"id": 101, "beginTime": "16:00", "endTime": "17:00"}],
            "reservationDateSpaceInfo": {
                "2026-06-16": [
                    {
                        "id": 5,
                        "spaceName": "5号",
                        "101": {"reservationStatus": 1, "orderFee": 20},
                    }
                ]
            },
        }

        snapshot = self.gateway.fetch_availability("60", "2026-06-16")

        self.assertEqual(snapshot.slots[0].begin_time, "16:00")
        self.assertEqual(snapshot.spaces_by_date["2026-06-16"][0].name, "5号")
        self.assertEqual(
            self.client.calls[0],
            (
                "epe_get",
                RESERVATION_INFO_URL,
                {"params": {"venueSiteId": "60", "searchDate": "2026-06-16"}},
            ),
        )

    def test_rejects_malformed_availability_at_protocol_seam(self):
        self.client.get_responses[RESERVATION_INFO_URL] = {"spaceTimeInfo": {}}

        with self.assertRaisesRegex(EpeProtocolError, "spaceTimeInfo must be a list"):
            self.gateway.fetch_availability("60", "2026-06-16")

    def test_submit_order_uses_single_attempt_and_returns_typed_order(self):
        url = "https://epe.pku.edu.cn/venue-server/api/reservation/order/submit"
        self.client.post_responses[url] = {
            "orderInfo": {"id": 123, "tradeNo": "TRADE-123"}
        }
        selection = ReservationSelection(
            venue="60",
            space="5号",
            trades=[
                ReservationTrade(
                    time_id="101",
                    begin_time="16:00",
                    end_time="17:00",
                    space_id="5",
                    space_name="5号",
                    order_fee=20,
                )
            ],
        )
        challenge = CaptchaChallenge(
            image_base64="image",
            words=["字"],
            token="token",
            secret_key="1234567890abcdef",
        )

        order = self.gateway.submit_order(
            selection, "2026-06-16", '[{"x":1,"y":2}]', challenge
        )

        self.assertEqual(order.trade_no, "TRADE-123")
        _, _, kwargs = self.client.calls[0]
        self.assertEqual(kwargs["max_attempts"], 1)
        self.assertEqual(kwargs["data"]["phone"], "13800000000")
        self.assertEqual(kwargs["data"]["orderPrice"], 20)

    def test_authenticate_owns_login_sequence_and_final_role_token(self):
        iaaa_url = "https://iaaa.pku.edu.cn/iaaa/oauthlogin.do"
        login_url = "https://epe.pku.edu.cn/venue-server/api/login"
        role_url = "https://epe.pku.edu.cn/venue-server/roleLogin"
        self.client.post_responses[iaaa_url] = FakeResponse(
            {"success": True, "token": "iaaa-token"}
        )
        self.client.post_responses[login_url] = {
            "token": {"access_token": "base-token"}
        }
        self.client.post_responses[role_url] = {"token": {"access_token": "role-token"}}

        self.gateway.authenticate()

        self.assertEqual(self.client.cg_auth_token, "role-token")
        self.assertEqual(
            [call[0] for call in self.client.calls],
            ["get", "post", "post", "get", "epe_post", "epe_post"],
        )


if __name__ == "__main__":
    unittest.main()
