"""Narrow argument screens for a single model reply; never execute business tools.

A passed screen is not proof of a correct decision. Text, clarification and
additional observation remain separate from replies that make a target write.
"""

from datetime import datetime, timedelta
import json


PRIMARY_EXCLUSIONS = {
    "7": "The fixed communicate amount includes the two cancellations despite asking about other reservations.",
    "39": "The reference cancels an insured economy booking for a noncovered cancellation reason.",
}
SCREENS = {"14": "unrequested_checked_bags", "23": "certificates_per_booking", "21": "itinerary_time_order"}


def primary_train_ids(train_ids):
    """Prospective primary episode scope; never regrade or discard historical runs."""
    return [str(t) for t in train_ids if str(t) not in PRIMARY_EXCLUSIONS]


def _scheduled_at(date, value):
    clock, separator, days = value.partition("+")
    if separator and (not days.isascii() or not days.isdecimal()):
        raise ValueError("Invalid scheduled day offset")
    moment = datetime.fromisoformat(date + "T" + clock)
    if moment.tzinfo is not None:
        raise ValueError("The benchmark schedule uses naive EST clocks")
    return moment + timedelta(days=int(days) if separator else 0)


def _itinerary_order(arguments, db):
    previous = None
    for leg in arguments["flights"]:
        flight = db["flights"][leg["flight_number"]]
        departure = _scheduled_at(leg["date"], flight["scheduled_departure_time_est"])
        arrival = _scheduled_at(leg["date"], flight["scheduled_arrival_time_est"])
        if arrival < departure:
            if "+" in flight["scheduled_arrival_time_est"]:
                raise ValueError("Explicit scheduled arrival precedes departure")
            arrival += timedelta(days=1)
        if previous is not None and departure < previous:
            return True
        previous = arrival
    if previous is None:
        raise ValueError("Empty itinerary")
    return False


def screen_reply(task_id, message, db):
    """Return descriptive categories, with manual review for all five text snapshots."""
    calls = message.get("tool_calls") or []
    kind = "tool_calls" if calls else "text"
    screen = SCREENS.get(str(task_id))
    if screen is None:
        return dict(status="manual_review_required", response_kind=kind, screen=None,
            target_write_calls=0, violations=None)
    target = "update_reservation_flights" if str(task_id) == "21" else "book_reservation"
    relevant, violations = [], []
    try:
        for call in calls:
            function = call["function"]
            if function["name"] != target:
                continue
            arguments = function["arguments"]
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            relevant.append(call["id"])
            if str(task_id) == "14":
                value = arguments["total_baggages"]
                if type(value) is not int or value < 0:
                    raise ValueError("Invalid baggage count")
                violated = value > 0
            elif str(task_id) == "23":
                methods = db["users"][arguments["user_id"]]["payment_methods"]
                payments = arguments["payment_methods"]
                if not payments:
                    raise ValueError("No payment methods")
                violated = sum(methods[p["payment_id"]]["source"] == "certificate" for p in payments) > 1
            else:
                violated = _itinerary_order(arguments, db)
            if violated:
                violations.append(call["id"])
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return dict(status="unparseable_or_unknown_arguments", response_kind=kind, screen=screen,
            target_write_calls=len(relevant), violations=None)
    status = "no_target_write" if not relevant else "violation_detected" if violations else "argument_check_passed"
    return dict(status=status, response_kind=kind, screen=screen, target_write_calls=len(relevant), violations=violations)
