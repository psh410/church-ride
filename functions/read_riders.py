# Reads and returns rider records (those needing rides) from the database.
#
# Thin wrapper functions around db.firestore_client - no business logic
# here, just fetching data and logging how many records came back so
# agents have visibility into what they're working with.

from __future__ import annotations

import logging

from db.firestore_client import get_riders as _get_riders
from db.firestore_client import get_riders_by_route as _get_riders_by_route

logger = logging.getLogger(__name__)


def get_riders(sunday_date: str) -> list[dict]:
    """Fetch all riders for a given Sunday date.

    Args:
        sunday_date: The Sunday date to fetch riders for, e.g.
            "2026-08-23".

    Returns:
        list[dict]: The rider records found for that date. Empty list if
            none were found.
    """
    riders = _get_riders(sunday_date)
    logger.info("Found %d rider(s) for sunday_date=%s.", len(riders), sunday_date)
    return riders


def get_riders_by_route(sunday_date: str, route_id: str) -> list[dict]:
    """Fetch riders for a specific route on a given Sunday.

    Args:
        sunday_date: The Sunday date to fetch riders for, e.g.
            "2026-08-23".
        route_id: The Firestore document ID of the route.

    Returns:
        list[dict]: The rider records found for that route/date. Empty
            list if none were found.
    """
    riders = _get_riders_by_route(sunday_date, route_id)
    logger.info(
        "Found %d rider(s) for sunday_date=%s, route_id=%s.",
        len(riders),
        sunday_date,
        route_id,
    )
    return riders
