# Keeps the semester shuttle schedule in step with driver availability,
# changing as little as possible.
#
# The semester schedule is planned well ahead and people build their lives
# around it, so when the "Available Drivers" tab changes (someone gets a
# conflict, or a new driver signs up) the schedule is NOT rebuilt. Every
# slot whose driver is still available stays exactly as it is. Only a slot
# whose driver is no longer listed as available for that Sunday is filled
# again, by the available driver with the fewest drives across the
# semester (drivers may drive week to week; adjacency only breaks ties).
# New drivers start at zero, so they are picked up first until their load
# matches everyone else's.
#
# Nothing here writes unless apply=True. plan_rebalance() is pure (no
# network, no Firestore) so the rules can be tested and previewed without
# touching real data.

from __future__ import annotations

import copy
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_SHUTTLES = ("shuttle_1", "shuttle_2")
_LEGS = ("pickup", "return")

# Elders do not drive or serve as backup on the first Sunday of the month
# (communion). Names are compared without case or extra spaces.
COMMUNION_ELDERS = ("Peter Hahn", "Albert Lee")

# Sundays with no shuttle service. Their rows are left alone.
NO_SERVICE_DATES = ("2026-11-22", "2026-11-29")

# Which Shift answers (from the driver form) can cover which leg.
_LEG_SHIFTS = {
    "pickup": {"both", "pickup"},
    "return": {"both", "drop-off", "dropoff", "drop off", "return"},
}


def is_first_sunday(day: str) -> bool:
    """True for the first Sunday of a month (ISO date in, Sunday assumed)."""
    return date.fromisoformat(day).day <= 7


def _norm(name: str | None) -> str:
    """Compare names without caring about case or extra spaces."""
    return " ".join(str(name or "").split()).casefold()


def _slot_driver(entry: dict, shuttle: str, leg: str) -> str | None:
    """Who is scheduled for one leg of one shuttle (falls back to the base name)."""
    return entry.get(f"{shuttle}_{leg}") or entry.get(shuttle) or None


def _drivers_on(entry: dict) -> set[str]:
    """Normalised names of everyone scheduled on a Sunday, backup included."""
    names = {
        _norm(_slot_driver(entry, s, leg)) for s in _SHUTTLES for leg in _LEGS
    }
    names.add(_norm(entry.get("backup")))
    names.discard("")
    return names


def _shuttle_drivers_on(entry: dict) -> set[str]:
    names = {
        _norm(_slot_driver(entry, s, leg)) for s in _SHUTTLES for leg in _LEGS
    }
    names.discard("")
    return names


def _count_drives(schedule: list[dict]) -> dict[str, int]:
    """How many Sundays each driver is on a shuttle (past and scheduled)."""
    counts: dict[str, int] = {}
    for entry in schedule:
        if entry.get("date") in NO_SERVICE_DATES:
            continue
        for name in _shuttle_drivers_on(entry):
            counts[name] = counts.get(name, 0) + 1
    return counts


def _days_since_last_drive(name: str, day: str, schedule: list[dict]) -> int:
    """Days between this driver's most recent earlier drive and `day`.

    Bigger means it has been longer, so they are more due. A driver with
    no earlier drive is treated as the most due.
    """
    target = date.fromisoformat(day)
    best = None
    for entry in schedule:
        if entry["date"] >= day:
            continue
        if name in _shuttle_drivers_on(entry):
            gap = (target - date.fromisoformat(entry["date"])).days
            best = gap if best is None else min(best, gap)
    return 10_000 if best is None else best


# Driving the Sunday right before or after another one is fine: drivers can
# go week to week. It only breaks a tie. When two drivers have the same
# number of drives, the one who is not driving an adjacent Sunday goes
# first. It is worth less than a single drive, so it never outweighs
# fairness.
BACK_TO_BACK_PENALTY = 0.5


def _drives_adjacent(name: str, day: str, schedule: list[dict]) -> bool:
    """True if `name` is on a shuttle the Sunday before or after `day`.

    Sundays with no shuttle service are ignored, since nobody drives them.
    """
    target = date.fromisoformat(day)
    for delta in (-7, 7):
        neighbour = (target + timedelta(days=delta)).isoformat()
        if neighbour in NO_SERVICE_DATES:
            continue
        for entry in schedule:
            if entry["date"] == neighbour and name in _shuttle_drivers_on(entry):
                return True
    return False


def plan_rebalance(
    schedule: list[dict],
    availability: dict[str, list[str]],
    shifts: dict[str, str] | None = None,
    today: str | None = None,
) -> dict:
    """Work out which slots must change, and who should take them.

    Args:
        schedule: semester_schedule documents (each with "date" in ISO
            form, "shuttle_1"/"shuttle_2", optional "_pickup"/"_return"
            variants, "backup", "past", "id"), any order.
        availability: ISO date -> driver names listed as available that
            Sunday (display spelling). A Sunday missing from this map, or
            listed with nobody, is left completely alone: no information
            is not the same as everyone being unavailable.
        shifts: driver name -> "Both" / "Pickup" / "Drop-off". Unknown
            drivers are treated as "Both".
        today: ISO date; Sundays before it are never changed. Defaults to
            treating only entries not flagged "past" as changeable.

    Returns:
        dict: {"changes": [...], "unfilled": [...], "updates":
            {doc_id: {field: value}}, "schedule": the new schedule,
            "drives_before": {...}, "drives_after": {...},
            "skipped": [...]}. schedule is a deep copy; the input is not
            modified.
    """
    shifts = {_norm(k): (v or "Both") for k, v in (shifts or {}).items()}
    work = sorted(copy.deepcopy(schedule), key=lambda e: e["date"])
    display = {}  # normalised -> spelling to write
    for names in availability.values():
        for name in names:
            display.setdefault(_norm(name), " ".join(name.split()))

    drives = _count_drives(work)
    drives_before = dict(drives)
    changes: list[dict] = []
    unfilled: list[dict] = []
    skipped: list[str] = []
    touched: dict[str, set[str]] = {}

    for entry in work:
        day = entry["date"]
        if entry.get("past") or (today and day < today) or day in NO_SERVICE_DATES:
            continue
        listed = availability.get(day)
        if not listed:
            skipped.append(f"{day}: no availability row, left unchanged")
            continue
        avail = {_norm(n) for n in listed}
        elders_out = set()
        if is_first_sunday(day):
            elders_out = {_norm(n) for n in COMMUNION_ELDERS}
            avail -= elders_out

        def why(old: str) -> str:
            if _norm(old) in elders_out:
                return "elder, first Sunday (communion)"
            return "no longer available"

        def candidates(leg: str, need_both: bool = False) -> list[str]:
            already = _drivers_on(entry)
            out = []
            for name in avail:
                if name in already:
                    continue
                shift = _norm(shifts.get(name, "Both"))
                if need_both:
                    if shift != "both":
                        continue
                elif shift not in _LEG_SHIFTS[leg]:
                    continue
                out.append(name)
            return out

        def pick(pool: list[str]) -> str | None:
            if not pool:
                return None
            return sorted(
                pool,
                key=lambda n: (
                    drives.get(n, 0)
                    + (BACK_TO_BACK_PENALTY if _drives_adjacent(n, day, work) else 0),
                    drives.get(n, 0),
                    -_days_since_last_drive(n, day, work),
                    n,
                ),
            )[0]

        def assign(shuttle: str, leg: str, new: str | None) -> None:
            entry[f"{shuttle}_{leg}"] = display.get(new, new) if new else None

        def note_change(slot: str, old: str, new: str | None, why: str) -> None:
            changes.append({"date": day, "slot": slot, "old": old,
                            "new": display.get(new, new) if new else None,
                            "reason": why})

        def move_count(old: str, new: str | None, before_on_day: set[str]) -> None:
            # A driver only counts once per Sunday, however many legs.
            still = _shuttle_drivers_on(entry)
            if _norm(old) not in still and _norm(old) in before_on_day:
                drives[_norm(old)] = max(0, drives.get(_norm(old), 0) - 1)
            if new and new not in before_on_day:
                drives[new] = drives.get(new, 0) + 1

        for shuttle in _SHUTTLES:
            pickup = _slot_driver(entry, shuttle, "pickup")
            ret = _slot_driver(entry, shuttle, "return")
            gone = {leg: d for leg, d in (("pickup", pickup), ("return", ret))
                    if d and _norm(d) not in avail}
            if not gone:
                continue
            before_on_day = _shuttle_drivers_on(entry)
            label = shuttle.replace("_", " ").title()

            # One person covering the whole shuttle: replace with one person
            # who can do both legs if there is one.
            if len(gone) == 2 and _norm(pickup) == _norm(ret):
                whole = pick(candidates("pickup", need_both=True))
                if whole:
                    for leg in _LEGS:
                        assign(shuttle, leg, whole)
                    entry[shuttle] = display.get(whole, whole)
                    note_change(f"{label} (whole day)", pickup, whole, why(pickup))
                    move_count(pickup, whole, before_on_day)
                    touched.setdefault(entry["id"], set()).add(shuttle)
                    continue

            missed: list[dict] = []
            for leg in _LEGS:
                old = gone.get(leg)
                if not old:
                    continue
                new = pick(candidates(leg))
                if new is None:
                    missed.append({"date": day, "slot": f"{label} {leg}",
                                   "driver": old,
                                   "reason": why(old) + "; no available driver can take it"})
                    continue
                assign(shuttle, leg, new)
                note_change(f"{label} {leg}", old, new, why(old))
                move_count(old, new, before_on_day)
                touched.setdefault(entry["id"], set()).add(shuttle)
                before_on_day = _shuttle_drivers_on(entry) | before_on_day
            if len(missed) == 2 and _norm(pickup) == _norm(ret):
                # Same person had the whole day and nobody can take it:
                # one line in the report, not two.
                missed = [{**missed[0], "slot": f"{label} (whole day)"}]
            unfilled.extend(missed)
            if entry["id"] in touched and shuttle in touched[entry["id"]]:
                entry[shuttle] = _slot_driver(entry, shuttle, "pickup")

        backup = entry.get("backup")
        if backup and _norm(backup) not in avail:
            pool = [n for n in avail if n not in _drivers_on(entry)]
            new = pick(pool)
            entry["backup"] = display.get(new, new) if new else None
            note_change("Backup", backup, new, why(backup))
            touched.setdefault(entry["id"], set()).add("backup")

    updates: dict[str, dict] = {}
    for entry in work:
        parts = touched.get(entry.get("id"))
        if not parts:
            continue
        fields: dict = {}
        for shuttle in _SHUTTLES:
            if shuttle in parts:
                fields[shuttle] = entry.get(shuttle)
                fields[f"{shuttle}_pickup"] = entry.get(f"{shuttle}_pickup")
                fields[f"{shuttle}_return"] = entry.get(f"{shuttle}_return")
        if "backup" in parts:
            fields["backup"] = entry.get("backup")
        updates[entry["id"]] = fields

    return {
        "changes": changes,
        "unfilled": unfilled,
        "updates": updates,
        "schedule": work,
        "drives_before": drives_before,
        "drives_after": _count_drives(work),
        "skipped": skipped,
    }


def format_report(plan: dict, applied: bool) -> str:
    """Plain-text summary of a plan, for the admin."""
    lines = []
    if not plan["changes"] and not plan["unfilled"]:
        return "No changes: every scheduled driver is still available."
    lines.append(
        "Driver schedule changes (applied)" if applied
        else "Proposed driver schedule changes (preview only, nothing saved)"
    )
    lines.append("Everything not listed here is unchanged.")
    lines.append("")
    for c in plan["changes"]:
        lines.append(f"{c['date']}  {c['slot']}: {c['old']} -> {c['new'] or 'nobody'}")
    if plan["unfilled"]:
        lines.append("")
        lines.append("STILL NEEDS A DRIVER (nobody available can take it):")
        for u in plan["unfilled"]:
            lines.append(f"{u['date']}  {u['slot']}: {u['driver']} is unavailable")
    lines.append("")
    lines.append("Drives per driver across the semester (before -> after):")
    names = sorted(set(plan["drives_before"]) | set(plan["drives_after"]))
    for n in names:
        b, a = plan["drives_before"].get(n, 0), plan["drives_after"].get(n, 0)
        if b != a:
            lines.append(f"  {n}: {b} -> {a}")
    return "\n".join(lines)


def load_inputs() -> tuple[list[dict], dict[str, list[str]], dict[str, str]]:
    """Read the live schedule, availability tab and driver shifts."""
    from db.firestore_client import get_semester_schedule
    from functions.read_sheets import (
        _get_available_drivers_by_sunday,
        _get_form_responses,
        _sheet_date_to_iso,
    )

    availability: dict[str, list[str]] = {}
    for sheet_date, names in _get_available_drivers_by_sunday().items():
        try:
            availability[_sheet_date_to_iso(sheet_date)] = sorted(names)
        except ValueError:
            continue
    shifts = {n: d.get("shift") or "Both" for n, d in _get_form_responses().items()}
    return get_semester_schedule(), availability, shifts


def run_rebalance(apply: bool = False) -> dict:
    """Plan changes from live data, and save them only when apply=True.

    Returns:
        dict: {"applied": bool, "report": str, "changes": [...],
            "unfilled": [...], "skipped": [...], "errors": [...]}.
    """
    from config.clock import church_today

    schedule, availability, shifts = load_inputs()
    plan = plan_rebalance(schedule, availability, shifts, today=church_today().isoformat())

    errors: list[dict] = []
    if apply and plan["updates"]:
        from db.firestore_client import SEMESTER_SCHEDULE_COLLECTION, get_client

        client = get_client()
        for doc_id, fields in plan["updates"].items():
            try:
                client.collection(SEMESTER_SCHEDULE_COLLECTION).document(doc_id).update(fields)
            except Exception as exc:
                logger.error("Failed to update schedule %s: %s", doc_id, exc)
                errors.append({"id": doc_id, "error": str(exc)})

    return {
        "applied": bool(apply and not errors),
        "report": format_report(plan, applied=bool(apply and not errors)),
        "changes": plan["changes"],
        "unfilled": plan["unfilled"],
        "skipped": plan["skipped"],
        "errors": errors,
    }
