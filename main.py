import argparse
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from utils.campaign import (
    MAX_CAPTCHA_TURNS,
    RETURNED_SLOT_OFFSETS_MINUTES,
    CampaignRuntime,
    ReservationAttempt,
    ReservationCampaign,
    ReservationWindow,
    build_reservation_windows,
    find_reservation as _find_reservation,
    run_reservation_window,
    run_with_transport_recovery,
    select_reservation as _select_reservation,
    wait_for_epe,
)
from utils.client import EpeClient
from utils.config import CONFIG_FILE, LOG_FILE
from utils.domain import (
    AvailabilitySnapshot,
    PreferredSpaces,
    ReservationKey,
    ReservationRequest,
    ReservationResult,
    ReservationSelection,
)
from utils.epe import EpeGateway
from utils.logger import Logger
from utils.notify import create_notifier
from utils.recognize import CaptchaRecognizer, create_recognizer
from utils.settings import AppSettings, load_settings
from utils.time import get_next_weekday, get_release_time, wait_until


__all__ = [
    "MAX_CAPTCHA_TURNS",
    "RETURNED_SLOT_OFFSETS_MINUTES",
    "ReservationWindow",
    "build_reservation_windows",
    "find_reservation",
    "run_reservation_window",
    "run_with_transport_recovery",
    "select_reservation",
    "wait_for_epe",
]


@dataclass(frozen=True, slots=True)
class CliCommand:
    request: ReservationRequest
    skip_pay: bool


def select_reservation(
    info_data: dict | AvailabilitySnapshot,
    venue: str,
    target_date: str,
    target_times: list[tuple[str, int]],
    preferred_spaces: list[str],
    rejected_reservations: set[ReservationKey],
    logger: Logger,
) -> ReservationSelection | None:
    return _select_reservation(
        info_data=info_data,
        venue=venue,
        target_date=target_date,
        target_times=target_times,
        preferred_spaces=preferred_spaces,
        rejected_reservations=rejected_reservations,
        logger=logger,
        chooser=random.choice,
    )


def find_reservation(
    client: EpeClient | EpeGateway,
    venues: list[str],
    target_date: str,
    target_times: list[tuple[str, int]],
    preferred_spaces: PreferredSpaces,
    rejected_reservations: set[ReservationKey],
    logger: Logger,
) -> ReservationSelection | None:
    return _find_reservation(
        client=client,
        venues=venues,
        target_date=target_date,
        target_times=target_times,
        preferred_spaces=preferred_spaces,
        rejected_reservations=rejected_reservations,
        logger=logger,
        chooser=random.choice,
    )


def login(
    client: EpeClient,
    logger: Logger,
    settings: AppSettings | None = None,
) -> None:
    resolved_settings = settings or load_settings(CONFIG_FILE)
    EpeGateway(client, resolved_settings, logger).authenticate()


def attempt_reservation(
    client: EpeClient,
    recognizer: CaptchaRecognizer,
    client_point_uid: str,
    venues: list[str],
    target_date: str,
    target_times: list[tuple[str, int]],
    preferred_spaces: PreferredSpaces,
    rejected_reservations: set[ReservationKey],
    logger: Logger,
    settings: AppSettings | None = None,
) -> ReservationResult:
    resolved_settings = settings or load_settings(CONFIG_FILE)
    request = ReservationRequest(
        venues=venues,
        target_date=target_date,
        target_times=target_times,
        preferred_spaces=preferred_spaces,
        retry_returned_slots=False,
    )
    runtime = CampaignRuntime()
    attempt = ReservationAttempt(
        request=request,
        gateway=EpeGateway(client, resolved_settings, logger),
        recognizer=recognizer,
        logger=logger,
        runtime=runtime,
    )
    return attempt.run(client_point_uid, rejected_reservations)


def main(
    venues: list[str],
    target_date: str,
    target_times: list[tuple[str, int]],
    preferred_spaces: PreferredSpaces,
    skip_pay: bool,
    retry_returned_slots: bool,
) -> bool:
    settings = load_settings(CONFIG_FILE)
    logger = Logger("main")
    logger.info(f"Running: {' '.join(sys.argv)}")
    logger.breathe()

    logger.info(f"Venue IDs by priority: {venues}")
    logger.info(f"Target date: {target_date}")
    logger.info("Target times:")
    for begin_time, slots_count in target_times:
        logger.info(
            f"  - begin at {begin_time}, "
            f"{f'{slots_count} consecutive slots' if slots_count > 1 else 'single slot'}"
        )
    logger.info(f"Preferred spaces: {preferred_spaces}")
    logger.info(f"Auto payment with campus card: {not skip_pay}")
    logger.info(f"Retry returned slots: {retry_returned_slots}")
    logger.breathe()

    release_time = get_release_time(target_date)
    login_time = release_time - timedelta(minutes=1)
    request = ReservationRequest(
        venues=venues,
        target_date=target_date,
        target_times=target_times,
        preferred_spaces=preferred_spaces,
        retry_returned_slots=retry_returned_slots,
    )

    logger.info(f"Quota release time: {release_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("Plan:")
    logger.info(f"  - login at {login_time.strftime('%H:%M:%S')}")
    for window in build_reservation_windows(release_time, retry_returned_slots):
        logger.info(
            f"  - {window.label} at {window.start_at.strftime('%H:%M:%S')} "
            f"({window.max_attempts} attempt(s))"
        )
    logger.breathe()

    client = EpeClient("epe")
    gateway = EpeGateway(client, settings, logger)
    recognizer = create_recognizer(settings.recognition)
    notifier = create_notifier(settings.notification)
    runtime = CampaignRuntime()
    reservation_attempt = ReservationAttempt(
        request=request,
        gateway=gateway,
        recognizer=recognizer,
        logger=logger,
        runtime=runtime,
    )
    campaign = ReservationCampaign(
        request=request,
        gateway=gateway,
        attempt=reservation_attempt,
        logger=logger,
        runtime=runtime,
    )

    try:
        wait_until(login_time, logger, "login", strict=False)
        windows = campaign.windows
        run_with_transport_recovery(
            action=gateway.authenticate,
            retry_until=windows[-1].start_at + timedelta(minutes=1),
            label="login",
            logger=logger,
        )
        result = campaign.run()

        if skip_pay:
            logger.info("Skipped auto payment")
            logger.breathe()
            notifier.notify_message(
                "[PKUAutoVenues] 需要手动付款 >_<",
                f"已成功预约 {target_date} {result.selected_time}（场馆 {result.venue}，{result.space}场地），请在十分钟内手动完成支付",
            )
            return True

        try:
            pay_fee = gateway.pay(result.trade_no).fee
            logger.info(
                f"Successfully paid CNY {pay_fee} for the reservation order with campus card"
            )
            logger.breathe()
            notifier.notify_message(
                "[PKUAutoVenues] 预约成功 OvO",
                f"已预约 {target_date} {result.selected_time}（场馆 {result.venue}，{result.space}场地），并成功用校园卡支付 {pay_fee} 元",
            )
        except Exception as error:
            logger.error(f"Failed to pay for the reservation order: {error}")
            logger.breathe()
            notifier.notify_message(
                "[PKUAutoVenues] 需要手动付款 >_<",
                f"已成功预约 {target_date} {result.selected_time}（场馆 {result.venue}，{result.space}场地），请在十分钟内手动完成支付 (Error: {error})",
            )
        return True
    except Exception as error:
        logger.error(str(error))
        logger.breathe()
        notifier.notify_message("[PKUAutoVenues] 预约失败 QAQ", str(error))
        return False
    finally:
        logger.info(f"Check the log file: {LOG_FILE}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PKU Auto Venues Reservation",
        epilog=(
            "Example: uv run main.py -v 五四 -d 7 -t 19:00/2 19:00 -s 9 8\n"
            "Please check README.md for more usage examples."
        ),
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
    return parser


def parse_cli_args(argv: list[str] | None = None) -> CliCommand:
    parser = build_parser()
    args = parser.parse_args(argv)
    venue_aliases = {
        "qdb": "60",
        "邱德拔": "60",
        "54": "86",
        "ws": "86",
        "五四": "86",
    }

    def normalize_venue(value: str) -> str:
        if value in venue_aliases:
            return venue_aliases[value]
        try:
            int(value)
        except ValueError:
            parser.error(
                f"Invalid -v/--venue item {value!r}: must be an alias or an integer"
            )
        return value

    venues: list[str] = []
    for venue_arg in args.venues:
        venue = normalize_venue(venue_arg)
        if venue not in venues:
            venues.append(venue)

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
            f"Invalid -d/--date {args.date!r}: must be in format YYYY-MM-DD "
            "(e.g. 2026-04-01) or an integer 1~7 (weekday)"
        )

    target_times: list[tuple[str, int]] = []
    for value in args.times:
        match = re.fullmatch(r"(\d{2}:\d{2})(?:/(\d+))?", value)
        if match is None:
            parser.error(
                f"Invalid -t/--times item {value!r}: must be HH:MM or HH:MM/N "
                "(e.g. 19:00, 19:00/2)"
            )
        begin_time = match.group(1)
        try:
            datetime.strptime(begin_time, "%H:%M")
        except ValueError:
            parser.error(
                f"Invalid -t/--times item {value!r}: {begin_time!r} is not a valid time"
            )
        slots_count = int(match.group(2)) if match.group(2) else 1
        if slots_count < 1:
            parser.error(
                f"Invalid -t/--times item {value!r}: must order at least 1 slot"
            )
        target_times.append((begin_time, slots_count))

    def normalize_space(value: str) -> str:
        try:
            return f"{int(value)}号"
        except ValueError:
            return value

    preferred_spaces: PreferredSpaces = [
        normalize_space(space) for space in args.spaces
    ]
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
            preferred_spaces_by_venue[normalize_venue(venue_arg)] = [
                normalize_space(space) for space in spaces_arg.split(",") if space
            ]
        preferred_spaces = preferred_spaces_by_venue

    return CliCommand(
        request=ReservationRequest(
            venues=venues,
            target_date=target_date,
            target_times=target_times,
            preferred_spaces=preferred_spaces,
            retry_returned_slots=args.retry_returned_slots,
        ),
        skip_pay=args.skip_pay,
    )


def run_cli(argv: list[str] | None = None) -> int:
    command = parse_cli_args(argv)
    request = command.request
    succeeded = main(
        venues=request.venues,
        target_date=request.target_date,
        target_times=request.target_times,
        preferred_spaces=request.preferred_spaces,
        skip_pay=command.skip_pay,
        retry_returned_slots=request.retry_returned_slots,
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
