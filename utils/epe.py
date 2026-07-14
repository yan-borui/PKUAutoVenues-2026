import json
import random
import time
from typing import Any, Mapping

from .client import EpeClient, get_response_json
from .domain import (
    AvailabilitySnapshot,
    CaptchaChallenge,
    Order,
    Payment,
    ReservationSelection,
    ReservationSlot,
    SpaceAvailability,
)
from .encrypt import encrypt_aes_ecb, encrypt_rsa, generate_order_pin
from .logger import Logger
from .orders import extract_order_info, recover_unpaid_order
from .settings import AppSettings


EPE_BASE_URL = "https://epe.pku.edu.cn/venue-server"
RESERVATION_INFO_URL = f"{EPE_BASE_URL}/api/reservation/day/info"


class EpeProtocolError(ValueError):
    """Raised when an EPE response does not satisfy the expected protocol shape."""


def _object(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EpeProtocolError(
            f"{context} must be an object, got {type(value).__name__}"
        )
    return value


def _required_string(data: Mapping[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if value is None or value == "":
        raise EpeProtocolError(f"{context} missing {key}")
    return str(value)


def parse_availability(data: Any) -> AvailabilitySnapshot:
    payload = _object(data, "reservation info")
    raw_slots = payload.get("spaceTimeInfo")
    if not isinstance(raw_slots, list):
        raise EpeProtocolError("reservation info spaceTimeInfo must be a list")

    slots = tuple(
        ReservationSlot(
            id=_required_string(
                _object(slot, "reservation slot"), "id", "reservation slot"
            ),
            begin_time=_required_string(
                _object(slot, "reservation slot"), "beginTime", "reservation slot"
            ),
            end_time=_required_string(
                _object(slot, "reservation slot"), "endTime", "reservation slot"
            ),
        )
        for slot in raw_slots
    )

    raw_dates = payload.get("reservationDateSpaceInfo")
    if not isinstance(raw_dates, Mapping):
        raise EpeProtocolError(
            "reservation info reservationDateSpaceInfo must be an object"
        )

    spaces_by_date: dict[str, tuple[SpaceAvailability, ...]] = {}
    for date, raw_spaces in raw_dates.items():
        if not isinstance(raw_spaces, list):
            raise EpeProtocolError(f"reservation spaces for {date} must be a list")
        spaces: list[SpaceAvailability] = []
        for raw_space in raw_spaces:
            space = _object(raw_space, "reservation space")
            trades = {
                slot.id: _object(space.get(slot.id, {}), "reservation trade")
                for slot in slots
            }
            spaces.append(
                SpaceAvailability(
                    id=_required_string(space, "id", "reservation space"),
                    name=_required_string(space, "spaceName", "reservation space"),
                    trades_by_time_id=trades,
                )
            )
        spaces_by_date[str(date)] = tuple(spaces)

    return AvailabilitySnapshot(slots=slots, spaces_by_date=spaces_by_date)


def parse_order(data: Any, context: str = "order") -> Order:
    order = _object(data, context)
    return Order(
        id=str(order["id"]) if order.get("id") is not None else None,
        trade_no=_required_string(order, "tradeNo", context),
        venue_space_name=(
            str(order["venueSpaceName"])
            if order.get("venueSpaceName") is not None
            else None
        ),
        reservation_start_date=(
            str(order["reservationStartDate"])
            if order.get("reservationStartDate") is not None
            else None
        ),
        reservation_end_date=(
            str(order["reservationEndDate"])
            if order.get("reservationEndDate") is not None
            else None
        ),
    )


class EpeGateway:
    """Owns the remote EPE/IAAA protocol and converts wire data to domain models."""

    def __init__(
        self,
        client: EpeClient,
        settings: AppSettings,
        logger: Logger,
    ) -> None:
        self.client = client
        self.settings = settings
        self.logger = logger

    def authenticate(self) -> None:
        self.client.get(f"{EPE_BASE_URL}/loginto")
        self.client.post(
            "https://iaaa.pku.edu.cn/iaaa/oauth.jsp",
            data={
                "appID": "ty",
                "appName": "北京大学体测系统",
                "redirectUrl": "https://epe.pku.edu.cn/ggtypt/dologin",
                "redirectLogonUrl": "https://epe.pku.edu.cn/ggtypt/dologin",
            },
        )
        response = self.client.post(
            "https://iaaa.pku.edu.cn/iaaa/oauthlogin.do",
            data={
                "appid": "ty",
                "userName": self.settings.iaaa.username,
                "password": encrypt_rsa(self.settings.iaaa.password),
                "randCode": "",
                "smsCode": "",
                "otpCode": "",
                "remTrustChk": "false",
                "redirUrl": "https://epe.pku.edu.cn/ggtypt/dologin",
            },
        )
        try:
            iaaa_data = _object(get_response_json(response), "IAAA login response")
        except Exception as error:
            if isinstance(error, EpeProtocolError):
                raise
            raise EpeProtocolError(f"Failed to parse IAAA response: {error}") from error
        if iaaa_data.get("success") is not True:
            errors = iaaa_data.get("errors")
            message = (
                errors.get("msg", "Unknown error")
                if isinstance(errors, Mapping)
                else "Unknown error"
            )
            raise EpeProtocolError(f"IAAA login failed: {message}")
        token = _required_string(iaaa_data, "token", "IAAA login response")
        self.logger.info("IAAA login successful")
        self.logger.debug(f"IAAA token: {token}")
        self.logger.breathe()

        self.client.get(
            "https://epe.pku.edu.cn/ggtypt/dologin",
            params={"_rand": random.random(), "token": token},
        )
        sso_token = self.client.session.cookies.get("sso_pku_token")
        if not sso_token:
            raise EpeProtocolError(
                "GGTYPT login failed: sso_pku_token cookie not found"
            )
        self.logger.info("GGTYPT login successful")
        self.logger.debug(f"sso_pku_token: {sso_token}")
        self.logger.breathe()

        login_data = self.client.epe_post(
            f"{EPE_BASE_URL}/api/login", headers={"sso-token": sso_token}
        )
        token_data = _object(login_data.get("token"), "EPE login token")
        self.client.cg_auth_token = _required_string(
            token_data, "access_token", "EPE login token"
        )
        self.logger.info("EPE login successful")
        self.logger.debug(f"cg_auth_token: {self.client.cg_auth_token}")
        self.logger.breathe()

        role_data = self.client.epe_post(
            f"{EPE_BASE_URL}/roleLogin", data={"roleid": 3}
        )
        role_token = _object(role_data.get("token"), "role login token")
        self.client.cg_auth_token = _required_string(
            role_token, "access_token", "role login token"
        )
        self.logger.info("Role login successful")
        self.logger.debug(
            f"cg_auth_token (with role info): {self.client.cg_auth_token}"
        )
        self.logger.breathe()

    def fetch_availability(self, venue: str, target_date: str) -> AvailabilitySnapshot:
        data = self.client.epe_get(
            RESERVATION_INFO_URL,
            params={"venueSiteId": venue, "searchDate": target_date},
        )
        return parse_availability(data)

    def issue_captcha(
        self,
        client_uid: str,
        timestamp_ms: int | None = None,
    ) -> CaptchaChallenge:
        data = self.client.epe_get(
            f"{EPE_BASE_URL}/api/captcha/get",
            params={
                "captchaType": "clickWord",
                "clientUid": client_uid,
                "ts": str(
                    timestamp_ms
                    if timestamp_ms is not None
                    else int(time.time() * 1000)
                ),
            },
        )
        if data.get("success") is not True:
            raise EpeProtocolError(f"Failed to get captcha: {data.get('repMsg')}")
        challenge = _object(data.get("repData"), "captcha repData")
        words = challenge.get("wordList")
        if not isinstance(words, list) or not all(
            isinstance(word, str) for word in words
        ):
            raise EpeProtocolError("captcha repData wordList must be a list of strings")
        return CaptchaChallenge(
            image_base64=_required_string(
                challenge, "originalImageBase64", "captcha repData"
            ),
            words=words,
            token=_required_string(challenge, "token", "captcha repData"),
            secret_key=_required_string(challenge, "secretKey", "captcha repData"),
        )

    def verify_captcha(self, challenge: CaptchaChallenge, points_json: str) -> None:
        data = self.client.epe_post(
            f"{EPE_BASE_URL}/api/captcha/check",
            data={
                "captchaType": "clickWord",
                "pointJson": encrypt_aes_ecb(points_json, challenge.secret_key),
                "token": challenge.token,
            },
        )
        if data.get("success") is not True:
            raise EpeProtocolError(
                "Failed to pass captcha check, maybe the recognition is wrong: "
                f"{data.get('repMsg')}"
            )

    def submit_order(
        self,
        selection: ReservationSelection,
        target_date: str,
        points_json: str,
        challenge: CaptchaChallenge,
    ) -> Order:
        data = self.client.epe_post(
            f"{EPE_BASE_URL}/api/reservation/order/submit",
            data={
                "captchaVerification": encrypt_aes_ecb(
                    challenge.token + "---" + points_json,
                    challenge.secret_key,
                ),
                "captchaToken": challenge.token,
                "reservationOrderJson": json.dumps(
                    [
                        {"spaceId": trade.space_id, "timeId": trade.time_id}
                        for trade in selection.trades
                    ],
                    separators=(",", ":"),
                ),
                "reservationDate": target_date,
                "weekStartDate": target_date,
                "reservationType": "-1",
                "orderPrice": selection.total_fee,
                "orderPin": generate_order_pin(),
                "venueSiteId": selection.venue,
                "phone": self.settings.epe.phone,
            },
            max_attempts=1,
        )
        return parse_order(extract_order_info(data), "submit response order")

    def find_unpaid_order(
        self,
        venue: str,
        target_date: str,
        selected_space: str | None,
        begin_time: str | None,
    ) -> Order | None:
        data = recover_unpaid_order(
            self.client,
            venue=venue,
            target_date=target_date,
            selected_space=selected_space,
            begin_time=begin_time,
        )
        return parse_order(data, "unpaid order") if data is not None else None

    def pay(self, trade_no: str) -> Payment:
        data = self.client.epe_post(
            f"{EPE_BASE_URL}/api/venue/finances/order/pay",
            data={"payType": "1", "venueTradeNo": trade_no, "isApp": "0"},
        )
        fee = data.get("payFee")
        if not fee:
            raise EpeProtocolError("pay response missing payFee")
        return Payment(fee=str(fee))
