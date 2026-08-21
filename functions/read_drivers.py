# Reads and returns driver records (those available to give rides) from the database.
#
# Thin wrapper function around db.firestore_client - no business logic
# here, just fetching data and logging how many records came back so
# agents have visibility into what they're working with.

from __future__ import annotations

import logging

from db.firestore_client import get_available_drivers as _get_available_drivers

logger = logging.getLogger(__name__)


def get_available_drivers(sunday_date: str) -> list[dict]:
    """Fetch all available drivers for a given Sunday.

    Args:
        sunday_date: The Sunday date to fetch drivers for, e.g.
            "2026-08-23".

    Returns:
        list[dict]: The driver records found for that date. Empty list if
            none were found.
    """
    drivers = _get_available_drivers(sunday_date)
    logger.info(
        "Found %d available driver(s) for sunday_date=%s.",
        len(drivers),
        sunday_date,
    )
    return drivers
