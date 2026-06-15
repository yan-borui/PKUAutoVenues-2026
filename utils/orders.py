import json
from typing import Any


ORDERS_URL = "https://epe.pku.edu.cn/venue-server/api/orders/mine"


def _json_object(value: Any, context: str) -> dict:
    while isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"{context} is not a JSON object: {e}") from e

    if not isinstance(value, dict):
        raise ValueError(
            f"{context} must be a JSON object, got {type(value).__name__}"
        )

    return value


def extract_order_info(submit_data: Any) -> dict:
    submit_data = _json_object(submit_data, "submit response data")
    order_info = _json_object(
        submit_data.get("orderInfo", submit_data),
        "submit response orderInfo",
    )

    if not order_info.get("tradeNo"):
        raise ValueError("tradeNo not found in submit response")

    return order_info


def _matches(value: Any, expected: Any) -> bool:
    return str(value) == str(expected)


def _find_unpaid_order(
    orders_data: Any,
    venue: str,
    target_date: str,
    selected_space: str | None,
    begin_time: str | None,
) -> dict | None:
    orders_page = _json_object(orders_data, "orders response data")
    orders = orders_page.get("content", [])
    if not isinstance(orders, list):
        raise ValueError("orders response content must be a list")

    for raw_order in orders:
        try:
            order = _json_object(raw_order, "order")
        except ValueError:
            continue

        if not order.get("tradeNo"):
            continue
        if not _matches(order.get("orderStatus"), 1):
            continue
        if not _matches(order.get("payStatus"), 1):
            continue

        order_venue = order.get("venueSiteId")
        if order_venue is None or not _matches(order_venue, venue):
            continue

        date_fields = [
            order.get("reservationDate"),
            order.get("reservationStartDate"),
            order.get("reservationEndDate"),
            order.get("reservationDateDetail"),
        ]
        known_dates = [str(value) for value in date_fields if value]
        if not known_dates or not any(target_date in value for value in known_dates):
            continue

        order_space = order.get("venueSpaceName")
        if (
            selected_space is not None
            and (order_space is None or str(order_space) != selected_space)
        ):
            continue

        start_date = order.get("reservationStartDate")
        if (
            begin_time is not None
            and (start_date is None or begin_time not in str(start_date))
        ):
            continue

        return order

    return None


def recover_unpaid_order(
    client,
    venue: str,
    target_date: str,
    selected_space: str | None,
    begin_time: str | None,
) -> dict | None:
    orders_data = client.epe_get(
        ORDERS_URL,
        params={"page": 0, "size": 20},
    )
    return _find_unpaid_order(
        orders_data,
        venue=venue,
        target_date=target_date,
        selected_space=selected_space,
        begin_time=begin_time,
    )
