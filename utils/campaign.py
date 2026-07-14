import base64
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo

from .client import (
    EpeAPIError,
    EpeClient,
    EpeUnavailableError,
    TransportUnavailableError,
)
from .config import LOGS_DIR
from .domain import (
    AvailabilitySnapshot,
    PreferredSpaces,
    ReservationKey,
    ReservationRequest,
    ReservationResult,
    ReservationSelection,
    ReservationTrade,
)
from .encrypt import generate_uuid
from .epe import EpeGateway, RESERVATION_INFO_URL, parse_availability
from .errors import (
    AttemptFailureAction,
    NoCandidateError,
    SlotTakenError,
    classify_attempt_failure,
)
from .logger import Logger
from .recognize import Recognizer
from .time import get_release_time, wait_until


MAX_CAPTCHA_TURNS = 8
RETURNED_SLOT_OFFSETS_MINUTES = (11, 12, 13)
AttemptResult = TypeVar("AttemptResult")


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _save_captcha(image_base64: str, now: datetime) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"{now.strftime('%Y-%m-%d_%H-%M-%S-%f')[:-3]}.png"
    path.write_bytes(base64.b64decode(image_base64))
    return path


@dataclass(frozen=True, slots=True)
class CampaignRuntime:
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.perf_counter
    now: Callable[[], datetime] = _shanghai_now
    epoch_ms: Callable[[], int] = lambda: int(time.time() * 1000)
    choose: Callable[[list[str]], str] = random.choice
    make_uid: Callable[[], str] = generate_uuid
    save_captcha: Callable[[str, datetime], Path] = _save_captcha


@dataclass(frozen=True, slots=True)
class ReservationWindow:
    start_at: datetime
    max_attempts: int
    label: str


def select_reservation(
    info_data: dict | AvailabilitySnapshot,
    venue: str,
    target_date: str,
    target_times: list[tuple[str, int]],
    preferred_spaces: list[str],
    rejected_reservations: set[ReservationKey],
    logger: Logger,
    chooser: Callable[[list[str]], str] = random.choice,
) -> ReservationSelection | None:
    snapshot = (
        info_data
        if isinstance(info_data, AvailabilitySnapshot)
        else parse_availability(info_data)
    )
    slots_info = sorted(snapshot.slots, key=lambda slot: slot.begin_time)
    begin_time_to_slot_idx = {
        slot.begin_time: index for index, slot in enumerate(slots_info)
    }
    if target_date not in snapshot.spaces_by_date:
        raise ValueError(
            f"Target date {target_date} not found in reservationDateSpaceInfo"
        )
    spaces_res_info = snapshot.spaces_by_date[target_date]

    for begin_time, requested_slots_count in target_times:
        slots_count = requested_slots_count
        if begin_time not in begin_time_to_slot_idx:
            logger.warning(
                f"Venue {venue} ('{begin_time}', {slots_count}) begin time not found in spaceTimeInfo, skipping"
            )
            continue
        begin_slot_idx = begin_time_to_slot_idx[begin_time]

        if begin_slot_idx + slots_count > len(slots_info):
            slots_max_count = len(slots_info) - begin_slot_idx
            logger.warning(
                f"Venue {venue} ('{begin_time}', {slots_count}) does not have enough following slots, reducing to {slots_max_count}"
            )
            slots_count = slots_max_count

        target_slots_info = slots_info[begin_slot_idx : begin_slot_idx + slots_count]
        available_space_to_trades: dict[str, list[ReservationTrade]] = {}
        for space_res_info in spaces_res_info:
            trades = [
                space_res_info.trades_by_time_id.get(slot.id, {})
                for slot in target_slots_info
            ]
            if not all(trade.get("reservationStatus") == 1 for trade in trades):
                continue

            candidate_trades = [
                ReservationTrade(
                    time_id=slot.id,
                    begin_time=slot.begin_time,
                    end_time=slot.end_time,
                    space_id=space_res_info.id,
                    space_name=space_res_info.name,
                    order_fee=int(trade["orderFee"]),
                )
                for slot, trade in zip(target_slots_info, trades)
            ]
            candidate_key: ReservationKey = (
                venue,
                tuple((trade.space_id, trade.time_id) for trade in candidate_trades),
            )
            if candidate_key not in rejected_reservations:
                available_space_to_trades[space_res_info.name] = candidate_trades

        if not available_space_to_trades:
            logger.info(
                f"Venue {venue} ('{begin_time}', {slots_count}) has no available spaces"
            )
            continue

        logger.info(
            f"Venue {venue} ('{begin_time}', {slots_count}) available spaces: {list(available_space_to_trades.keys())}"
        )
        logger.breathe()
        selected_space = next(
            (space for space in preferred_spaces if space in available_space_to_trades),
            None,
        )
        if selected_space is None:
            selected_space = chooser(list(available_space_to_trades))
        return ReservationSelection(
            venue=venue,
            space=selected_space,
            trades=available_space_to_trades[selected_space],
        )
    return None


def find_reservation(
    client: EpeClient | EpeGateway,
    venues: list[str],
    target_date: str,
    target_times: list[tuple[str, int]],
    preferred_spaces: PreferredSpaces,
    rejected_reservations: set[ReservationKey],
    logger: Logger,
    chooser: Callable[[list[str]], str] = random.choice,
) -> ReservationSelection | None:
    for venue in venues:
        if isinstance(client, EpeGateway):
            info_data = client.fetch_availability(venue, target_date)
        else:
            info_data = client.epe_get(
                RESERVATION_INFO_URL,
                params={"venueSiteId": venue, "searchDate": target_date},
            )
        selection = select_reservation(
            info_data=info_data,
            venue=venue,
            target_date=target_date,
            target_times=target_times,
            preferred_spaces=(
                preferred_spaces.get(venue, [])
                if isinstance(preferred_spaces, dict)
                else preferred_spaces
            ),
            rejected_reservations=rejected_reservations,
            logger=logger,
            chooser=chooser,
        )
        if selection is not None:
            return selection
    return None


def build_reservation_windows(
    release_time: datetime,
    retry_returned_slots: bool,
) -> list[ReservationWindow]:
    windows = [
        ReservationWindow(
            start_at=release_time,
            max_attempts=MAX_CAPTCHA_TURNS,
            label="main reservation window",
        )
    ]
    if retry_returned_slots:
        windows.extend(
            ReservationWindow(
                start_at=release_time + timedelta(minutes=offset),
                max_attempts=MAX_CAPTCHA_TURNS,
                label=f"returned-slot window at 12:{offset:02d}:00",
            )
            for offset in RETURNED_SLOT_OFFSETS_MINUTES
        )
    return windows


def wait_for_epe(
    client: EpeClient,
    venue: str,
    target_date: str,
    logger: Logger,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    logger.warning("EPE returned HTTP 502; starting 1-second heartbeat polling")
    while True:
        sleep(1)
        try:
            client.epe_get(
                RESERVATION_INFO_URL,
                params={"venueSiteId": venue, "searchDate": target_date},
                timeout=1.0,
                max_attempts=1,
            )
            logger.info("EPE heartbeat succeeded; resuming reservation flow")
            logger.breathe()
            return
        except (EpeUnavailableError, TransportUnavailableError) as error:
            logger.warning(
                f"EPE is still unavailable ({error}); polling again in 1 second"
            )


def run_reservation_window(
    attempt: Callable[[], AttemptResult],
    heartbeat: Callable[[], None],
    max_attempts: int,
    logger: Logger,
    retry_delay: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> AttemptResult | None:
    turn = 1
    while turn <= max_attempts:
        try:
            return attempt()
        except Exception as error:
            action = classify_attempt_failure(error)
            if action is AttemptFailureAction.RETRY_CAPTCHA:
                logger.warning(
                    "Captcha recognition service unavailable; retrying with a new captcha: "
                    f"{error}"
                )
                logger.breathe()
                continue
            if action is AttemptFailureAction.WAIT_FOR_EPE:
                logger.warning(
                    f"Attempt {turn}/{max_attempts} paused without consuming its budget: {error}"
                )
                heartbeat()
                continue

            logger.warning(f"Attempt {turn}/{max_attempts} failed: {error}")
            if turn < max_attempts:
                logger.warning(f"Retrying in {retry_delay} seconds...")
                sleep(retry_delay)
            logger.breathe()
            turn += 1
    return None


def run_with_transport_recovery(
    action: Callable[[], AttemptResult],
    retry_until: datetime,
    label: str,
    logger: Logger,
    retry_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = _shanghai_now,
) -> AttemptResult:
    while True:
        try:
            return action()
        except (EpeUnavailableError, TransportUnavailableError) as error:
            current_time = now()
            if current_time >= retry_until:
                raise
            remaining = (retry_until - current_time).total_seconds()
            delay = min(retry_delay, max(0.1, remaining))
            logger.warning(
                f"{label} paused by temporary network failure until retry: {error}"
            )
            logger.warning(f"Retrying {label} in {delay:.1f} seconds...")
            logger.breathe()
            sleep(delay)


class ReservationAttempt:
    def __init__(
        self,
        request: ReservationRequest,
        gateway: EpeGateway,
        recognizer: Recognizer,
        logger: Logger,
        runtime: CampaignRuntime,
    ) -> None:
        self.request = request
        self.gateway = gateway
        self.recognizer = recognizer
        self.logger = logger
        self.runtime = runtime

    def run(
        self,
        client_point_uid: str,
        rejected_reservations: set[ReservationKey],
    ) -> ReservationResult:
        challenge = self.gateway.issue_captcha(
            client_point_uid,
            timestamp_ms=self.runtime.epoch_ms(),
        )
        image_path = self.runtime.save_captcha(
            challenge.image_base64,
            self.runtime.now(),
        )
        self.logger.info(f"Captcha image saved to: {image_path}")
        self.logger.info(f"Words to click: {challenge.words}")
        self.logger.debug(f"Captcha token: {challenge.token}")
        self.logger.debug(f"Captcha secret key: {challenge.secret_key}")
        self.logger.breathe()

        recognize_result = self.recognizer.recognize_captcha(
            challenge.image_base64, challenge.words
        )
        recognized_points = json.dumps(
            [{"x": x, "y": y} for x, y in recognize_result],
            separators=(",", ":"),
        )
        self.gateway.verify_captcha(challenge, recognized_points)
        captcha_verified_at = self.runtime.monotonic()
        self.logger.info("Captcha verified successfully!")
        self.logger.breathe()

        selection = find_reservation(
            client=self.gateway,
            venues=self.request.venues,
            target_date=self.request.target_date,
            target_times=self.request.target_times,
            preferred_spaces=self.request.preferred_spaces,
            rejected_reservations=rejected_reservations,
            logger=self.logger,
            chooser=self.runtime.choose,
        )
        if selection is None:
            self.logger.breathe()
            raise NoCandidateError(
                "None of the target venues and times have available spaces"
            )

        self.logger.info(
            f"Selected: venue {selection.venue}, {selection.selected_time} "
            f"{selection.space}场地 (CNY {selection.total_fee})"
        )
        self.logger.debug("Trades to submit:")
        for trade in selection.trades:
            self.logger.debug(f"  - {trade}")
        self.logger.breathe()

        elapsed = self.runtime.monotonic() - captcha_verified_at
        if elapsed < 1:
            self.logger.info(f"Sleep for {1 - elapsed:.2f} seconds...")
            self.logger.breathe()
            self.runtime.sleep(1 - elapsed)

        try:
            order = self.gateway.submit_order(
                selection=selection,
                target_date=self.request.target_date,
                points_json=recognized_points,
                challenge=challenge,
            )
        except Exception as submit_error:
            if (
                isinstance(submit_error, EpeAPIError)
                and submit_error.code == 250
                and "已被其他人预约" in submit_error.message
            ):
                rejected_reservations.add(selection.key)
                self.logger.warning(
                    f"Marked venue {selection.venue}, {selection.selected_time} "
                    f"{selection.space}场地 unavailable locally"
                )
                raise SlotTakenError(str(submit_error)) from submit_error

            self.logger.warning(
                f"Reservation submit result is uncertain: {submit_error}"
            )
            self.logger.info("Checking for a matching unpaid order...")
            while True:
                try:
                    order = self.gateway.find_unpaid_order(
                        venue=selection.venue,
                        target_date=self.request.target_date,
                        selected_space=selection.space,
                        begin_time=selection.trades[0].begin_time,
                    )
                    if order is None and "未支付的订单" in str(submit_error):
                        order = self.gateway.find_unpaid_order(
                            venue=selection.venue,
                            target_date=self.request.target_date,
                            selected_space=None,
                            begin_time=selection.trades[0].begin_time,
                        )
                    break
                except (EpeUnavailableError, TransportUnavailableError):
                    wait_for_epe(
                        client=self.gateway.client,
                        venue=selection.venue,
                        target_date=self.request.target_date,
                        logger=self.logger,
                        sleep=self.runtime.sleep,
                    )
            if order is None:
                raise submit_error

            selected_space = order.venue_space_name or selection.space
            selected_time = selection.selected_time
            recovered_start = order.reservation_start_date or ""
            recovered_end = order.reservation_end_date or ""
            if len(recovered_start) >= 5 and len(recovered_end) >= 5:
                selected_time = f"{recovered_start[-5:]}-{recovered_end[-5:]}"
            self.logger.info(
                f"Recovered matching unpaid order: {selected_time} {selected_space}场地"
            )
        else:
            selected_space = selection.space
            selected_time = selection.selected_time

        self.logger.info(
            "Successfully submitted reservation order"
            f"{f' (ID: {order.id})' if order.id else ''}"
        )
        self.logger.info("Check the order online: https://epe.pku.edu.cn/venue/orders")
        self.logger.breathe()
        return ReservationResult(
            venue=selection.venue,
            space=selected_space,
            selected_time=selected_time,
            trade_no=order.trade_no,
        )


class ReservationCampaign:
    def __init__(
        self,
        request: ReservationRequest,
        gateway: EpeGateway,
        attempt: ReservationAttempt,
        logger: Logger,
        runtime: CampaignRuntime | None = None,
    ) -> None:
        self.request = request
        self.gateway = gateway
        self.attempt = attempt
        self.logger = logger
        self.runtime = runtime or CampaignRuntime()

    @property
    def windows(self) -> list[ReservationWindow]:
        return build_reservation_windows(
            get_release_time(self.request.target_date),
            self.request.retry_returned_slots,
        )

    def run(self) -> ReservationResult:
        client_point_uid = f"point-{self.runtime.make_uid()}"
        for window in self.windows:
            wait_until(
                window.start_at,
                self.logger,
                window.label,
                strict=True,
                now=self.runtime.now,
                sleep=self.runtime.sleep,
            )
            self.logger.info(
                f"Starting {window.label} with {window.max_attempts} attempt(s)"
            )
            self.logger.breathe()
            rejected_reservations: set[ReservationKey] = set()
            result = run_reservation_window(
                attempt=lambda: self.attempt.run(
                    client_point_uid,
                    rejected_reservations,
                ),
                heartbeat=lambda: wait_for_epe(
                    client=self.gateway.client,
                    venue=self.request.venues[0],
                    target_date=self.request.target_date,
                    logger=self.logger,
                    sleep=self.runtime.sleep,
                ),
                max_attempts=window.max_attempts,
                logger=self.logger,
                sleep=self.runtime.sleep,
            )
            if result is not None:
                return result
            self.logger.warning(f"No reservation completed in {window.label}")
            self.logger.breathe()

        total_attempts = sum(window.max_attempts for window in self.windows)
        raise NoCandidateError(
            f"Failed to find available spaces after {total_attempts} reservation attempts"
        )
