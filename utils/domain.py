from dataclasses import dataclass
from typing import Any, Mapping


ReservationKey = tuple[str, tuple[tuple[str, str], ...]]
PreferredSpaces = list[str] | dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    venues: list[str]
    target_date: str
    target_times: list[tuple[str, int]]
    preferred_spaces: PreferredSpaces
    retry_returned_slots: bool


@dataclass(frozen=True, slots=True)
class ReservationSlot:
    id: str
    begin_time: str
    end_time: str


@dataclass(frozen=True, slots=True)
class ReservationTrade:
    time_id: str
    begin_time: str
    end_time: str
    space_id: str
    space_name: str
    order_fee: int


@dataclass(frozen=True, slots=True)
class SpaceAvailability:
    id: str
    name: str
    trades_by_time_id: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class AvailabilitySnapshot:
    slots: tuple[ReservationSlot, ...]
    spaces_by_date: Mapping[str, tuple[SpaceAvailability, ...]]


@dataclass(frozen=True, slots=True)
class ReservationSelection:
    venue: str
    space: str
    trades: list[ReservationTrade]

    @property
    def key(self) -> ReservationKey:
        return (
            self.venue,
            tuple((trade.space_id, trade.time_id) for trade in self.trades),
        )

    @property
    def selected_time(self) -> str:
        return f"{self.trades[0].begin_time}-{self.trades[-1].end_time}"

    @property
    def total_fee(self) -> int:
        return sum(trade.order_fee for trade in self.trades)


@dataclass(frozen=True, slots=True)
class ReservationResult:
    venue: str
    space: str
    selected_time: str
    trade_no: str


@dataclass(frozen=True, slots=True)
class CaptchaChallenge:
    image_base64: str
    words: list[str]
    token: str
    secret_key: str


@dataclass(frozen=True, slots=True)
class Order:
    trade_no: str
    id: str | None = None
    venue_space_name: str | None = None
    reservation_start_date: str | None = None
    reservation_end_date: str | None = None


@dataclass(frozen=True, slots=True)
class Payment:
    fee: str
