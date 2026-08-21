# Writes rider-to-driver ride assignments to the database.
#
# Thin wrapper functions around db.firestore_client - no business logic
# here, just persisting the assignment agent's decisions and logging
# what was written.

from __future__ import annotations

import logging

from db.firestore_client import create_assignment, update_driver_assignment

logger = logging.getLogger(__name__)


def save_assignment(assignment: dict) -> str:
    """Persist a new rider-to-driver assignment document.

    Args:
        assignment: The assignment data to store (e.g. rider_id,
            driver_id, route_id, sunday_date).

    Returns:
        str: The Firestore document ID of the newly created assignment.
    """
    assignment_id = create_assignment(assignment)
    logger.info("Saved assignment %s: %s", assignment_id, assignment)
    return assignment_id


def assign_driver_to_route(driver_id: str, route_id: str) -> bool:
    """Record which route a driver is assigned to.

    Args:
        driver_id: The Firestore document ID of the driver.
        route_id: The Firestore document ID of the route being assigned.

    Returns:
        bool: True if the assignment was recorded successfully.
    """
    success = update_driver_assignment(driver_id, route_id)
    logger.info("Assigned driver_id=%s to route_id=%s.", driver_id, route_id)
    return success
