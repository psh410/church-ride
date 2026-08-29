# Generates shuttle stop map visuals for the church ride coordination
# system: a Google Static Maps image showing all 5 stops (colored by
# which shuttle they belong to, labeled with their rider count) plus
# the church, and a companion Google Maps link riders/drivers can
# click to open an interactive, turn-by-turn view of the same stops.

from __future__ import annotations

import logging
import urllib.parse

from config import settings

logger = logging.getLogger(__name__)

# Verified lat/lng coordinates for each shuttle stop.
STOP_COORDINATES: dict[str, tuple[float, float]] = {
    "FAR": (40.099143, -88.221030),
    "Allen": (40.104128, -88.220973),
    "SDRP": (40.104067, -88.234689),
    "ISR": (40.110113, -88.221735),
    "Icon": (40.112279, -88.234493),
}

# Covenant Fellowship Church's location - always shown on both maps.
CHURCH_COORDINATES: tuple[float, float] = (40.084797, -88.291652)

# Which shuttle each stop belongs to - drives marker color (and the
# grouping shown in build_map_legend()).
SHUTTLE_1_STOPS = ["FAR", "Allen", "SDRP"]
SHUTTLE_2_STOPS = ["ISR", "Icon"]

_STATIC_MAP_BASE_URL = "https://maps.googleapis.com/maps/api/staticmap"
_DIRECTIONS_BASE_URL = "https://www.google.com/maps/dir"

# Reasonable fixed zoom level to fit the church + all 5 stops in one frame.
_MAP_ZOOM = 12
_MAP_SIZE = "600x400"


def build_static_map_url(stop_counts: dict[str, int]) -> str:
    """Build a Google Static Maps image URL showing every stop's rider count.

    Each stop's marker is colored by which shuttle it belongs to -
    orange for SHUTTLE_1_STOPS, blue for SHUTTLE_2_STOPS - and labeled
    with that stop's rider count (pair with build_map_legend() to spell
    out which shuttle each color represents). The church is always
    included as an unlabeled green marker (it has no rider count of its
    own), giving the map a fixed reference point.

    Args:
        stop_counts: Rider count per stop, e.g.
            {"FAR": 0, "Allen": 3, "SDRP": 11, "ISR": 5, "Icon": 8}.
            Stops not present in STOP_COORDINATES are ignored.

    Returns:
        str: The full Static Maps API URL, ready to embed as an <img>
            src or fetch directly.

    Raises:
        RuntimeError: If the URL can't be built (e.g. missing API key).
    """
    try:
        markers = []
        all_points = []

        for stop_name, (lat, lng) in STOP_COORDINATES.items():
            count = stop_counts.get(stop_name, 0)
            color = "orange" if stop_name in SHUTTLE_1_STOPS else "blue"
            label = _marker_count_label(count)
            markers.append(f"color:{color}|label:{label}|{lat},{lng}")
            all_points.append((lat, lng))

        church_lat, church_lng = CHURCH_COORDINATES
        markers.append(f"color:green|{church_lat},{church_lng}")
        all_points.append((church_lat, church_lng))

        center_lat = sum(p[0] for p in all_points) / len(all_points)
        center_lng = sum(p[1] for p in all_points) / len(all_points)

        params = {
            "center": f"{center_lat},{center_lng}",
            "zoom": _MAP_ZOOM,
            "size": _MAP_SIZE,
            "markers": markers,
            "key": settings.GOOGLE_MAPS_API_KEY,
        }
        query = urllib.parse.urlencode(params, doseq=True)

        return f"{_STATIC_MAP_BASE_URL}?{query}"
    except Exception as exc:
        raise RuntimeError(f"Failed to build static map URL: {exc}") from exc


def _marker_count_label(count: int) -> str:
    """Return a single-character marker label for a rider count.

    The Static Maps API only accepts a single alphanumeric character
    per marker label, so counts of 9 or more are capped and shown as
    "9" rather than the exact number.

    Args:
        count: A stop's (or the church's) rider count.

    Returns:
        str: A single character, "0"-"9".
    """
    return str(min(count, 9))


def build_interactive_map_link(stop_counts: dict[str, int]) -> str:
    """Build a clickable Google Maps directions link through the active stops.

    Only stops with at least one rider are included, in
    STOP_COORDINATES order, so the route stays relevant to that week's
    actual pickups. The church is always the final destination.

    Args:
        stop_counts: Rider count per stop, e.g.
            {"FAR": 0, "Allen": 3, "SDRP": 11, "ISR": 5, "Icon": 8}.
            Stops not present in STOP_COORDINATES are ignored.

    Returns:
        str: A "https://www.google.com/maps/dir/..." URL through every
            active stop and ending at the church. If no stop has any
            riders, this is just a direct link to the church's location.

    Raises:
        RuntimeError: If the link can't be built.
    """
    try:
        waypoints = [
            (lat, lng)
            for stop_name, (lat, lng) in STOP_COORDINATES.items()
            if stop_counts.get(stop_name, 0) > 0
        ]
        waypoints.append(CHURCH_COORDINATES)

        encoded_points = [
            urllib.parse.quote(f"{lat},{lng}", safe=",-.")
            for lat, lng in waypoints
        ]

        return f"{_DIRECTIONS_BASE_URL}/{'/'.join(encoded_points)}"
    except Exception as exc:
        raise RuntimeError(f"Failed to build interactive map link: {exc}") from exc


def build_map_legend(stop_counts: dict[str, int], non_shuttle_total: int) -> str:
    """Build a plain-text legend explaining each map marker's color group.

    Meant to be included alongside build_static_map_url()'s image so
    readers can tell which color corresponds to which shuttle (and how
    many riders are at each of that shuttle's stops) without guessing.
    Also notes how many riders need personal driver coordination
    outside the shuttle system.

    Args:
        stop_counts: Rider count per stop, e.g.
            {"FAR": 0, "Allen": 3, "SDRP": 11, "ISR": 5, "Icon": 8}.
            Stops not present in STOP_COORDINATES are ignored.
        non_shuttle_total: How many riders this week need a personal
            driver rather than a shuttle stop (e.g. from
            get_all_riders_for_sunday()'s "non_shuttle_total").

    Returns:
        str: A multi-line legend grouped by shuttle color, ending with
            the church and the non-shuttle rider count.
    """
    lines = ["\U0001f5fa\ufe0f Map Legend:"]

    lines.append("\U0001f7e0 Shuttle 1 (Gray Van)")
    for stop_name in SHUTTLE_1_STOPS:
        count = stop_counts.get(stop_name, 0)
        lines.append(f"   {stop_name}: {count} riders")

    lines.append("\U0001f535 Shuttle 2 (Silver Van)")
    for stop_name in SHUTTLE_2_STOPS:
        count = stop_counts.get(stop_name, 0)
        lines.append(f"   {stop_name}: {count} riders")

    lines.append("\U0001f7e2 Church (destination)")
    lines.append("")
    lines.append(
        f"\U0001f464 Non-shuttle riders: {non_shuttle_total} "
        "(need personal driver coordination)"
    )

    return "\n".join(lines)
