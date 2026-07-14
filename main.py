import argparse
import base64
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
import random
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

from utils.client import (
    EpeAPIError,
    EpeClient,
    EpeUnavailableError,
    TransportUnavailableError,
)
from utils.domain import (
    AvailabilitySnapshot,
    PreferredSpaces,
    ReservationKey,
    ReservationResult,
    ReservationSelection,
    ReservationTrade,
)
from utils.epe import EpeGateway, RESERVATION_INFO_URL, parse_availability
from utils.logger import Logger
from utils.encrypt import generate_uuid
from utils.recognize import CaptchaRecognitionTransportError, Recognizer
from utils.notify import Notifier
from utils.time import get_next_weekday, get_release_time, wait_until
from utils.config import CONFIG_FILE, LOGS_DIR, LOG_FILE
from utils.settings import AppSettings, load_settings

MAX_CAPTCHA_TURNS = 8
RETURNED_SLOT_OFFSETS_MINUTES = (11, 12, 13)
AttemptResult = TypeVar("AttemptResult")


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
) -> ReservationSelection | None:
    snapshot = (
        info_data
        if isinstance(info_data, AvailabilitySnapshot)
        else parse_availability(info_data)
    )
    slots_info = sorted(snapshot.slots, key=lambda slot: slot.begin_time)
    begin_time_to_slot_idx: dict[str, int] = {
        slot.begin_time: i for i, slot in enumerate(slots_info)
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

        for space in preferred_spaces:
            if space in available_space_to_trades:
                selected_space = space
                break
        else:
            selected_space = random.choice(list(available_space_to_trades.keys()))

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
) -> ReservationSelection | None:
    for venue in venues:
        if isinstance(client, EpeGateway):
            info_data = client.fetch_availability(venue, target_date)
        else:
            info_data = client.epe_get(
                RESERVATION_INFO_URL,
                params={
                    "venueSiteId": venue,
                    "searchDate": target_date,
                },
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
        )
        if selection is not None:
            return selection

    return None


def build_reservation_windows(
    release_time: datetime,
    retry_returned_slots: bool,
) -> list[ReservationWindow]:
    if not retry_returned_slots:
        return [
            ReservationWindow(
                start_at=release_time,
                max_attempts=MAX_CAPTCHA_TURNS,
                label="main reservation window",
            )
        ]

    windows = [
        ReservationWindow(
            start_at=release_time,
            max_attempts=MAX_CAPTCHA_TURNS,
            label="main reservation window",
        )
    ]
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
                params={
                    "venueSiteId": venue,
                    "searchDate": target_date,
                },
                timeout=1.0,
                max_attempts=1,
            )
            logger.info("EPE heartbeat succeeded; resuming reservation flow")
            logger.breathe()
            return
        except (EpeUnavailableError, TransportUnavailableError) as e:
            logger.warning(f"EPE is still unavailable ({e}); polling again in 1 second")


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
        except CaptchaRecognitionTransportError as e:
            logger.warning(
                "Captcha recognition service unavailable; retrying with a new captcha: "
                f"{e}"
            )
            logger.breathe()
            continue
        except (EpeUnavailableError, TransportUnavailableError) as e:
            logger.warning(
                f"Attempt {turn}/{max_attempts} paused without consuming its budget: {e}"
            )
            heartbeat()
            continue
        except Exception as e:
            logger.warning(f"Attempt {turn}/{max_attempts} failed: {e}")
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
    now: Callable[[], datetime] = lambda: datetime.now(ZoneInfo("Asia/Shanghai")),
) -> AttemptResult:
    while True:
        try:
            return action()
        except (EpeUnavailableError, TransportUnavailableError) as e:
            current_time = now()
            if current_time >= retry_until:
                raise

            remaining = (retry_until - current_time).total_seconds()
            delay = min(retry_delay, max(0.1, remaining))
            logger.warning(
                f"{label} paused by temporary network failure until retry: {e}"
            )
            logger.warning(f"Retrying {label} in {delay:.1f} seconds...")
            logger.breathe()
            sleep(delay)


def login(
    client: EpeClient,
    logger: Logger,
    settings: AppSettings | None = None,
) -> None:
    settings = settings or load_settings(CONFIG_FILE)
    EpeGateway(client, settings, logger).authenticate()


def attempt_reservation(
    client: EpeClient,
    recognizer: Recognizer,
    client_point_uid: str,
    venues: list[str],
    target_date: str,
    target_times: list[tuple[str, int]],
    preferred_spaces: PreferredSpaces,
    rejected_reservations: set[ReservationKey],
    logger: Logger,
    settings: AppSettings | None = None,
) -> ReservationResult:
    settings = settings or load_settings(CONFIG_FILE)
    gateway = EpeGateway(client, settings, logger)
    challenge = gateway.issue_captcha(client_point_uid)
    image_base64 = challenge.image_base64
    words = challenge.words

    image_path = (
        LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S-%f')[:-3]}.png"
    )
    image_path.write_bytes(base64.b64decode(image_base64))

    logger.info(f"Captcha image saved to: {image_path}")
    logger.info(f"Words to click: {words}")
    logger.debug(f"Captcha token: {challenge.token}")
    logger.debug(f"Captcha secret key: {challenge.secret_key}")
    logger.breathe()

    recognize_result = recognizer.recognize_captcha(image_base64, words)
    recognized_points = json.dumps(
        [{"x": x, "y": y} for x, y in recognize_result],
        separators=(",", ":"),
    )

    gateway.verify_captcha(challenge, recognized_points)

    captcha_verified_at = time.perf_counter()
    logger.info("Captcha verified successfully!")
    logger.breathe()

    selection = find_reservation(
        client=gateway,
        venues=venues,
        target_date=target_date,
        target_times=target_times,
        preferred_spaces=preferred_spaces,
        rejected_reservations=rejected_reservations,
        logger=logger,
    )
    if selection is None:
        logger.breathe()
        raise Exception("None of the target venues and times have available spaces")

    selected_venue = selection.venue
    selected_space = selection.space
    selected_trades = selection.trades
    selected_time = selection.selected_time
    total_fee = selection.total_fee

    logger.info(
        f"Selected: venue {selected_venue}, {selected_time} "
        f"{selected_space}场地 (CNY {total_fee})"
    )
    logger.debug("Trades to submit:")
    for trade in selected_trades:
        logger.debug(f"  - {trade}")
    logger.breathe()

    elapsed = time.perf_counter() - captcha_verified_at
    if elapsed < 1:
        logger.info(f"Sleep for {1 - elapsed:.2f} seconds...")
        logger.breathe()
        time.sleep(1 - elapsed)

    try:
        order_info = gateway.submit_order(
            selection=selection,
            target_date=target_date,
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
            logger.warning(
                f"Marked venue {selected_venue}, {selected_time} "
                f"{selected_space}场地 unavailable locally"
            )
            raise

        logger.warning(f"Reservation submit result is uncertain: {submit_error}")
        logger.info("Checking for a matching unpaid order...")

        while True:
            try:
                order_info = gateway.find_unpaid_order(
                    venue=selected_venue,
                    target_date=target_date,
                    selected_space=selected_space,
                    begin_time=selected_trades[0].begin_time,
                )

                if order_info is None and "未支付的订单" in str(submit_error):
                    order_info = gateway.find_unpaid_order(
                        venue=selected_venue,
                        target_date=target_date,
                        selected_space=None,
                        begin_time=selected_trades[0].begin_time,
                    )
                break
            except (EpeUnavailableError, TransportUnavailableError):
                wait_for_epe(
                    client=client,
                    venue=selected_venue,
                    target_date=target_date,
                    logger=logger,
                )

        if order_info is None:
            raise submit_error

        recovered_space = order_info.venue_space_name
        if recovered_space:
            selected_space = str(recovered_space)

        recovered_start = order_info.reservation_start_date or ""
        recovered_end = order_info.reservation_end_date or ""
        if len(recovered_start) >= 5 and len(recovered_end) >= 5:
            selected_time = f"{recovered_start[-5:]}-{recovered_end[-5:]}"

        logger.info(
            f"Recovered matching unpaid order: {selected_time} {selected_space}场地"
        )

    trade_id = order_info.id
    trade_no = order_info.trade_no
    logger.info(
        "Successfully submitted reservation order"
        f"{f' (ID: {trade_id})' if trade_id else ''}"
    )
    logger.info("Check the order online: https://epe.pku.edu.cn/venue/orders")
    logger.breathe()

    return ReservationResult(
        venue=selected_venue,
        space=selected_space,
        selected_time=selected_time,
        trade_no=trade_no,
    )


def main(
    venues: list[str],
    target_date: str,
    target_times: list[tuple[str, int]],
    preferred_spaces: PreferredSpaces,
    skip_pay: bool,
    retry_returned_slots: bool,
):
    settings = load_settings(CONFIG_FILE)
    logger = Logger("main")
    logger.info(f"Running: {' '.join(sys.argv)}")
    logger.breathe()

    logger.info(f"Venue IDs by priority: {venues}")
    logger.info(f"Target date: {target_date}")
    logger.info(f"Target times:")
    for begin_time, slots_count in target_times:
        # 从 begin_time 开始，连续 slots_count 个时段
        logger.info(
            f"  - begin at {begin_time}, {f'{slots_count} consecutive slots' if slots_count > 1 else 'single slot'}"
        )
    logger.info(f"Preferred spaces: {preferred_spaces}")
    logger.info(f"Auto payment with campus card: {not skip_pay}")
    logger.info(f"Retry returned slots: {retry_returned_slots}")
    logger.breathe()

    release_time = get_release_time(target_date)
    login_time = release_time - timedelta(minutes=1)
    # captcha_time = release_time - timedelta(seconds=15)

    logger.info(f"Quota release time: {release_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Plan:")
    logger.info(f"  - login at {login_time.strftime('%H:%M:%S')}")
    for window in build_reservation_windows(release_time, retry_returned_slots):
        logger.info(
            f"  - {window.label} at {window.start_at.strftime('%H:%M:%S')} "
            f"({window.max_attempts} attempt(s))"
        )
    logger.breathe()

    client = EpeClient("epe")
    gateway = EpeGateway(client, settings, logger)
    recognizer = Recognizer(settings.recognition)
    notifier = Notifier(settings.notification)

    try:
        """
        Login
        """

        wait_until(login_time, logger, "login", strict=False)

        windows = build_reservation_windows(release_time, retry_returned_slots)
        login_retry_until = windows[-1].start_at + timedelta(minutes=1)
        run_with_transport_recovery(
            action=gateway.authenticate,
            retry_until=login_retry_until,
            label="login",
            logger=logger,
        )

        """
        Loop: Recognize captcha, fetch reservation info, and submit order
        """

        # A checked captcha can only be submitted once. Each release window gets
        # an independent attempt budget.
        client_point_uid = f"point-{generate_uuid()}"
        reservation_result: ReservationResult | None = None
        for window in windows:
            wait_until(window.start_at, logger, window.label, strict=True)
            logger.info(
                f"Starting {window.label} with {window.max_attempts} attempt(s)"
            )
            logger.breathe()

            # A rejected combination may become available again in a later window.
            rejected_reservations: set[ReservationKey] = set()
            reservation_result = run_reservation_window(
                attempt=lambda: attempt_reservation(
                    client=client,
                    recognizer=recognizer,
                    client_point_uid=client_point_uid,
                    venues=venues,
                    target_date=target_date,
                    target_times=target_times,
                    preferred_spaces=preferred_spaces,
                    rejected_reservations=rejected_reservations,
                    logger=logger,
                    settings=settings,
                ),
                heartbeat=lambda: wait_for_epe(
                    client=client,
                    venue=venues[0],
                    target_date=target_date,
                    logger=logger,
                ),
                max_attempts=window.max_attempts,
                logger=logger,
            )
            if reservation_result is not None:
                break

            logger.warning(f"No reservation completed in {window.label}")
            logger.breathe()

        if reservation_result is None:
            total_attempts = sum(window.max_attempts for window in windows)
            logger.error("All reservation windows exhausted, exiting")
            raise Exception(
                "Failed to find available spaces after "
                f"{total_attempts} reservation attempts"
            )

        selected_venue = reservation_result.venue
        selected_space = reservation_result.space
        selected_time = reservation_result.selected_time
        trade_no = reservation_result.trade_no

        """
        Pay with campus card (optional)
        """

        if skip_pay:
            logger.info("Skipped auto payment")
            logger.breathe()
            notifier.notify_message(
                "[PKUAutoVenues] 需要手动付款 >_<",
                f"已成功预约 {target_date} {selected_time}（场馆 {selected_venue}，{selected_space}场地），请在十分钟内手动完成支付",
            )
            return

        try:
            pay_fee = gateway.pay(trade_no).fee

            logger.info(
                f"Successfully paid CNY {pay_fee} for the reservation order with campus card"
            )
            logger.breathe()
            notifier.notify_message(
                "[PKUAutoVenues] 预约成功 OvO",
                f"已预约 {target_date} {selected_time}（场馆 {selected_venue}，{selected_space}场地），并成功用校园卡支付 {pay_fee} 元",
            )

        except Exception as e:
            logger.error(f"Failed to pay for the reservation order: {e}")
            logger.breathe()
            notifier.notify_message(
                "[PKUAutoVenues] 需要手动付款 >_<",
                f"已成功预约 {target_date} {selected_time}（场馆 {selected_venue}，{selected_space}场地），请在十分钟内手动完成支付 (Error: {e})",
            )

    except Exception as e:
        logger.error(str(e))
        logger.breathe()
        notifier.notify_message("[PKUAutoVenues] 预约失败 QAQ", str(e))

    finally:
        logger.info(f"Check the log file: {LOG_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PKU Auto Venues Reservation",
        epilog="Example: uv run main.py -v 五四 -d 7 -t 19:00/2 19:00 -s 9 8\nPlease check README.md for more usage examples.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--venue",
        "--venues",
        dest="venues",
        required=True,
        nargs="+",
        help="Venue site names or IDs by priority, e.g. qdb 86",
    )
    parser.add_argument(
        "-d",
        "--date",
        required=True,
        help="Target date or weekday, e.g. 2026-04-01, 6 (for next Saturday)",
    )
    parser.add_argument(
        "-t",
        "--times",
        required=True,
        nargs="+",
        help="Target begin times and durations, e.g. 15:00 (single slot) 17:00/2 (2 consecutive slots)",
    )
    parser.add_argument(
        "-s",
        "--spaces",
        nargs="*",
        default=[],
        help="Preferred space names (optional), e.g. 4号 5 (abbr for 5号)",
    )
    parser.add_argument(
        "--venue-spaces",
        action="append",
        default=[],
        metavar="VENUE:SPACE[,SPACE...]",
        help="Venue-specific preferred spaces, e.g. qdb:10,9,4. Overrides --spaces for that venue.",
    )
    parser.add_argument(
        "--skip-pay",
        action="store_true",
        help="Skip auto payment, need to manually pay within 10 minutes",
    )
    parser.add_argument(
        "--no-returned-slots",
        "--no-reflow",
        dest="retry_returned_slots",
        action="store_false",
        help="Disable returned-slot attempts at 12:11, 12:12, and 12:13",
    )
    args = parser.parse_args()

    # Process venue
    venue_aliases = {
        "qdb": "60",
        "邱德拔": "60",
        "54": "86",
        "ws": "86",
        "五四": "86",
    }

    def normalize_venue(venue_arg: str) -> str:
        if venue_arg in venue_aliases:
            return venue_aliases[venue_arg]
        else:
            try:
                int(venue_arg)
            except ValueError:
                parser.error(
                    f"Invalid -v/--venue item {venue_arg!r}: must be an alias or an integer"
                )
            return venue_arg

    venues = []
    for venue_arg in args.venues:
        venue = normalize_venue(venue_arg)
        if venue not in venues:
            venues.append(venue)

    # Process date
    if re.fullmatch(r"[1-7]", args.date):
        target_date = get_next_weekday(int(args.date))
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            parser.error(f"Invalid -d/--date {args.date!r}: not a valid calendar date")
        target_date = args.date
    else:
        parser.error(
            f"Invalid -d/--date {args.date!r}: must be in format YYYY-MM-DD (e.g. 2026-04-01) or an integer 1~7 (weekday)"
        )

    # Process times
    target_times: list[tuple[str, int]] = []
    for t in args.times:
        m = re.fullmatch(r"(\d{2}:\d{2})(?:/(\d+))?", t)
        if m is None:
            parser.error(
                f"Invalid -t/--times item {t!r}: must be HH:MM or HH:MM/N (e.g. 19:00, 19:00/2)"
            )

        begin_time = m.group(1)
        try:
            datetime.strptime(begin_time, "%H:%M")
        except ValueError:
            parser.error(
                f"Invalid -t/--times item {t!r}: {begin_time!r} is not a valid time"
            )

        slots_count = int(m.group(2)) if m.group(2) else 1
        if slots_count < 1:
            parser.error(f"Invalid -t/--times item {t!r}: must order at least 1 slot")

        target_times.append((begin_time, slots_count))

    # Process spaces
    def normalize_space(space_arg: str) -> str:
        try:
            return f"{int(space_arg)}号"
        except ValueError:
            return space_arg

    preferred_spaces: PreferredSpaces = []
    for s in args.spaces:
        preferred_spaces.append(normalize_space(s))

    if args.venue_spaces:
        preferred_spaces_by_venue = {
            venue: list(preferred_spaces) for venue in venues if preferred_spaces
        }
        for item in args.venue_spaces:
            if ":" not in item:
                parser.error(
                    f"Invalid --venue-spaces item {item!r}: must be VENUE:SPACE[,SPACE...]"
                )
            venue_arg, spaces_arg = item.split(":", 1)
            venue = normalize_venue(venue_arg)
            preferred_spaces_by_venue[venue] = [
                normalize_space(space) for space in spaces_arg.split(",") if space
            ]
        preferred_spaces = preferred_spaces_by_venue

    main(
        venues=venues,
        target_date=target_date,
        target_times=target_times,
        preferred_spaces=preferred_spaces,
        skip_pay=args.skip_pay,
        retry_returned_slots=args.retry_returned_slots,
    )
