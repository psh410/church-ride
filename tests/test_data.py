# Shared test fixtures and sample data for the test suite.
#
# This file contains realistic fake data for local testing, simulating
# what the agents would read from/write to Firestore. Using fixed,
# hand-crafted data (rather than randomly generated data) keeps tests
# deterministic and easy to reason about.

from __future__ import annotations

# The Sunday all fake data below is anchored to. Every driver/rider record
# uses this same date so agents can filter "today's" data consistently.
SUNDAY_DATE = "2026-08-23"

# --------------------------------------------------------------------------
# FAKE_ROUTES
# --------------------------------------------------------------------------
# Tests: basic route lookups (get_routes/get_route), and route-level
# capacity/stop data used when building assignments. All three routes
# start with driver_id=None so tests can exercise the "before a driver is
# assigned" state as well as "after assignment" once a test assigns one.
FAKE_ROUTES = [
    {
        "id": "route_north",
        "name": "North Route",
        "stops": [
            {"name": "Maple & 5th", "time": "9:15AM"},
            {"name": "Riverside Community Center", "time": "9:25AM"},
            {"name": "Oak Park & Church St", "time": "9:35AM"},
        ],
        "active": True,
        "driver_id": None,
    },
    {
        "id": "route_east",
        "name": "East Route",
        "stops": [
            {"name": "Pine Ave & 3rd", "time": "9:10AM"},
            {"name": "Eastside Library", "time": "9:20AM"},
            {"name": "Lincoln Park", "time": "9:30AM"},
        ],
        "active": True,
        "driver_id": None,
    },
    {
        "id": "route_south",
        "name": "South Route",
        "stops": [
            {"name": "South Mall", "time": "9:15AM"},
            {"name": "Grace Community Center", "time": "9:25AM"},
            {"name": "Elm & Broadway", "time": "9:35AM"},
        ],
        "active": True,
        "driver_id": None,
    },
]

# --------------------------------------------------------------------------
# FAKE_DRIVERS
# --------------------------------------------------------------------------
# Tests: get_available_drivers() and the assignment agent's matching logic
# (matching a driver's preferred_route to a route that needs one). Includes
# drivers preferring each specific route as well as drivers who are
# flexible ("either"), so tests can exercise both matching strategies.
FAKE_DRIVERS = [
    {
        "id": "driver_001",
        "name": "Marcus Johnson",
        "email": "marcus.johnson@example.com",
        "discord_username": "marcusj",
        "preferred_route": "North",
        "available": True,
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "driver_002",
        "name": "Aaliyah Washington",
        "email": "aaliyah.washington@example.com",
        "discord_username": "aaliyahw",
        "preferred_route": "East",
        "available": True,
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "driver_003",
        "name": "DeShawn Carter",
        "email": "deshawn.carter@example.com",
        "discord_username": "deshawnc",
        "preferred_route": "South",
        "available": True,
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "driver_004",
        "name": "Keisha Williams",
        "email": "keisha.williams@example.com",
        "discord_username": "keishaw",
        "preferred_route": "either",
        "available": True,
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "driver_005",
        "name": "James Rodriguez",
        "email": "james.rodriguez@example.com",
        "discord_username": "jrodriguez",
        "preferred_route": "North",
        "available": True,
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "driver_006",
        "name": "Fatima Ali",
        "email": "fatima.ali@example.com",
        "discord_username": "fatimaali",
        "preferred_route": "East",
        "available": True,
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "driver_007",
        "name": "Michael Thompson",
        "email": "michael.thompson@example.com",
        "discord_username": "mthompson",
        "preferred_route": "either",
        "available": True,
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "driver_008",
        "name": "Nia Brooks",
        "email": "nia.brooks@example.com",
        "discord_username": "niabrooks",
        "preferred_route": "North",
        "available": True,
        "sunday_date": SUNDAY_DATE,
    },
]

# --------------------------------------------------------------------------
# FAKE_RIDERS
# --------------------------------------------------------------------------
# Tests: get_riders()/get_riders_by_route(), grouping riders by stop within
# a route, the return-ride flow (return_ride=True riders need an evening
# pickup too), and status handling. 18 riders are "confirmed" and 2 are
# "pending" (rider_005 and rider_014) to test that pending riders are
# handled/flagged differently from confirmed ones.
FAKE_RIDERS = [
    # --- North Route riders ---
    {
        "id": "rider_001",
        "name": "Angela Davis",
        "email": "angela.davis@example.com",
        "route_id": "route_north",
        "stop": "Maple & 5th",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_002",
        "name": "Terrence Brown",
        "email": "terrence.brown@example.com",
        "route_id": "route_north",
        "stop": "Maple & 5th",
        "return_ride": False,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_003",
        "name": "Priya Patel",
        "email": "priya.patel@example.com",
        "route_id": "route_north",
        "stop": "Riverside Community Center",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_004",
        "name": "Malik Jefferson",
        "email": "malik.jefferson@example.com",
        "route_id": "route_north",
        "stop": "Riverside Community Center",
        "return_ride": False,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_005",
        "name": "Grace Kim",
        "email": "grace.kim@example.com",
        "route_id": "route_north",
        "stop": "Riverside Community Center",
        "return_ride": True,
        "status": "pending",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_006",
        "name": "Darnell Harris",
        "email": "darnell.harris@example.com",
        "route_id": "route_north",
        "stop": "Oak Park & Church St",
        "return_ride": False,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_007",
        "name": "Sofia Martinez",
        "email": "sofia.martinez@example.com",
        "route_id": "route_north",
        "stop": "Oak Park & Church St",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    # --- East Route riders ---
    {
        "id": "rider_008",
        "name": "Jamal Robinson",
        "email": "jamal.robinson@example.com",
        "route_id": "route_east",
        "stop": "Pine Ave & 3rd",
        "return_ride": False,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_009",
        "name": "Emily Chen",
        "email": "emily.chen@example.com",
        "route_id": "route_east",
        "stop": "Pine Ave & 3rd",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_010",
        "name": "Tyrone Mitchell",
        "email": "tyrone.mitchell@example.com",
        "route_id": "route_east",
        "stop": "Eastside Library",
        "return_ride": False,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_011",
        "name": "Latoya Green",
        "email": "latoya.green@example.com",
        "route_id": "route_east",
        "stop": "Eastside Library",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_012",
        "name": "Hiroshi Tanaka",
        "email": "hiroshi.tanaka@example.com",
        "route_id": "route_east",
        "stop": "Eastside Library",
        "return_ride": False,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_013",
        "name": "Andre Coleman",
        "email": "andre.coleman@example.com",
        "route_id": "route_east",
        "stop": "Lincoln Park",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_014",
        "name": "Maria Gonzalez",
        "email": "maria.gonzalez@example.com",
        "route_id": "route_east",
        "stop": "Lincoln Park",
        "return_ride": False,
        "status": "pending",
        "sunday_date": SUNDAY_DATE,
    },
    # --- South Route riders ---
    {
        "id": "rider_015",
        "name": "Brianna Scott",
        "email": "brianna.scott@example.com",
        "route_id": "route_south",
        "stop": "South Mall",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_016",
        "name": "Kevin Nguyen",
        "email": "kevin.nguyen@example.com",
        "route_id": "route_south",
        "stop": "South Mall",
        "return_ride": False,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_017",
        "name": "Jasmine Foster",
        "email": "jasmine.foster@example.com",
        "route_id": "route_south",
        "stop": "Grace Community Center",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_018",
        "name": "Isaiah Bell",
        "email": "isaiah.bell@example.com",
        "route_id": "route_south",
        "stop": "Grace Community Center",
        "return_ride": False,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_019",
        "name": "Camila Reyes",
        "email": "camila.reyes@example.com",
        "route_id": "route_south",
        "stop": "Elm & Broadway",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_020",
        "name": "Xavier Simmons",
        "email": "xavier.simmons@example.com",
        "route_id": "route_south",
        "stop": "Elm & Broadway",
        "return_ride": False,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
]

# --------------------------------------------------------------------------
# FAKE_RIDERS_NO_DRIVER
# --------------------------------------------------------------------------
# Tests: the edge case where a route has riders waiting but no driver has
# been assigned yet (route_south's driver_id is None in FAKE_ROUTES). This
# exercises the monitor/assignment agents' handling of "riders stuck
# without a driver" - e.g. raising an alert instead of silently dropping
# them.
FAKE_RIDERS_NO_DRIVER = [
    {
        "id": "rider_nd_001",
        "name": "Destiny Palmer",
        "email": "destiny.palmer@example.com",
        "route_id": "route_south",
        "stop": "South Mall",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_nd_002",
        "name": "Elijah Watkins",
        "email": "elijah.watkins@example.com",
        "route_id": "route_south",
        "stop": "South Mall",
        "return_ride": False,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_nd_003",
        "name": "Yolanda Price",
        "email": "yolanda.price@example.com",
        "route_id": "route_south",
        "stop": "Grace Community Center",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_nd_004",
        "name": "Carlos Fernandez",
        "email": "carlos.fernandez@example.com",
        "route_id": "route_south",
        "stop": "Grace Community Center",
        "return_ride": False,
        "status": "pending",
        "sunday_date": SUNDAY_DATE,
    },
    {
        "id": "rider_nd_005",
        "name": "Simone Walker",
        "email": "simone.walker@example.com",
        "route_id": "route_south",
        "stop": "Elm & Broadway",
        "return_ride": True,
        "status": "confirmed",
        "sunday_date": SUNDAY_DATE,
    },
]


def get_test_state() -> dict:
    """Return a snapshot of all fake data combined into one dict.

    This simulates what the agents would collectively read from Firestore
    for a given Sunday: the active routes, the available drivers, and the
    riders needing rides. Tests can pull individual pieces out of this
    dict, or pass the whole thing to a fake/mock Firestore layer.

    Returns:
        dict: A dict with keys "sunday_date", "routes", "drivers",
            "riders", and "riders_no_driver".
    """
    return {
        "sunday_date": SUNDAY_DATE,
        "routes": FAKE_ROUTES,
        "drivers": FAKE_DRIVERS,
        "riders": FAKE_RIDERS,
        "riders_no_driver": FAKE_RIDERS_NO_DRIVER,
    }
