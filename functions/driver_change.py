# Last minute driver changes by text, for the admin allowlist.
#
# Three texts. The admin sends CHANGE, gets this Sunday's slots back
# numbered, replies with a number, gets the driver roster back numbered,
# replies with a number. Then everyone involved is told.
#
# Guided rather than a command with arguments, because this gets used
# on a Saturday night when somebody has just dropped out, and nobody
# should be recalling syntax at that moment. It also means the admin
# sees who is currently assigned before changing anything, which a
# one-shot command would skip.
#
# The numbered lists live in Firestore between texts (see
# db.firestore_client.get_driver_change_session) rather than being
# rebuilt on each reply. Rebuilding would let a roster that changed
# mid-flow silently point "3" at a different person than the one the
# admin read.
#
# The whole roster is offered, not just drivers marked available, with
# "(not available)" against the ones who are not. Peter's reason: people
# who said no in the sheet can often still do it when actually asked,
# and a last minute change is exactly when you ask.

from __future__ import annotations

import logging

from config.clock import next_sunday
from db.firestore_client import (
    clear_driver_change_session,
    get_driver_change_session,
    get_semester_schedule,
    set_driver_change_session,
)
from functions.send_sms import BRAND_PREFIX, OPT_OUT_NOTICE, normalize_to_e164, send_sms

logger = logging.getLogger(__name__)

# What starts the flow. Phrases are matched whole, so "CHANGE" alone and
# "DRIVER CHANGE" both work and nothing else does.
CHANGE_KEYWORDS = {"CHANGE", "DRIVER CHANGE", "DRIVERCHANGE", "CHANGE DRIVER"}

# Replying 0 backs out. Not CANCEL, which Twilio intercepts as an
# opt-out and never delivers to us.
CANCEL_REPLY = "0"

_SHUTTLES = ("shuttle_1", "shuttle_2")
_LEGS = ("pickup", "return")

_LABELS = {
    "shuttle_1": "S1",
    "shuttle_2": "S2",
    "backup": "Backup",
}


def matches_change_keyword(body: str) -> bool:
    """Whether this body starts a driver change."""
    return body.strip() in CHANGE_KEYWORDS


def is_numeric_reply(body: str) -> bool:
    """Whether this body is a bare number, i.e. an answer to a menu.

    Only digits count. An admin mid-flow can still text UPDATE or
    REQUESTS and have it work normally, because those are not numbers.
    """
    return body.strip().isdigit()


# --------------------------------------------------------------------------
# Step 1: which slot
# --------------------------------------------------------------------------
def build_slot_menu(phone: str, sunday_date: str | None = None) -> str:
    """Start a change: list this Sunday's slots, numbered, and store them.

    Args:
        phone: The admin's number, E.164 preferred.
        sunday_date: Optional ISO override. Defaults to the coming Sunday.

    Returns:
        str: The SMS body to reply with.
    """
    if sunday_date is None:
        sunday_date = next_sunday().isoformat()

    entry = _schedule_entry(sunday_date)
    if entry is None:
        return (
            f"{BRAND_PREFIX} No driver schedule found for "
            f"{_short(sunday_date)}. Nothing to change."
        )

    slots = _slots_for(entry)
    lines = [f"{BRAND_PREFIX} Change a driver for {_short(sunday_date)}:"]
    for index, slot in enumerate(slots, start=1):
        who = slot["current"] or "unassigned"
        lines.append(f"{index}. {slot['label']} {who}")
    lines.append(f"Reply with a number, or {CANCEL_REPLY} to cancel.")

    try:
        set_driver_change_session(
            phone,
            {
                "step": "slot",
                "sunday_date": sunday_date,
                "schedule_id": entry.get("id"),
                "slots": slots,
            },
        )
    except RuntimeError as exc:
        logger.error("Could not start driver change for %s: %s", phone, exc)
        return f"{BRAND_PREFIX} Couldn't start that just now. Please try again."

    return "\n".join(lines)


def _slots_for(entry: dict) -> list[dict]:
    """The changeable slots on one Sunday, as numbered-menu rows.

    A shuttle is one row when both legs have the same driver and two
    when they differ. Collapsing a split week into one row would mean
    picking "S1" silently replaced a driver the admin never looked at.

    Each row carries the Firestore field it writes, so applying a choice
    never has to re-derive it.
    """
    slots: list[dict] = []

    for shuttle in _SHUTTLES:
        pickup = entry.get(f"{shuttle}_pickup") or entry.get(shuttle)
        ret = entry.get(f"{shuttle}_return") or entry.get(shuttle)
        label = _LABELS[shuttle]

        if _same(pickup, ret):
            slots.append(
                {
                    "label": label,
                    "current": pickup,
                    # Writing the base field and clearing the leg fields
                    # keeps a whole-day assignment whole, rather than
                    # leaving a stale override behind it.
                    "fields": [shuttle],
                    "clear": [f"{shuttle}_pickup", f"{shuttle}_return"],
                }
            )
        else:
            for leg, who in (("pickup", pickup), ("return", ret)):
                slots.append(
                    {
                        "label": f"{label} {leg}",
                        "current": who,
                        "fields": [f"{shuttle}_{leg}"],
                        "clear": [],
                    }
                )

    slots.append(
        {
            "label": _LABELS["backup"],
            "current": entry.get("backup"),
            "fields": ["backup"],
            "clear": [],
        }
    )
    return slots


# --------------------------------------------------------------------------
# Step 2: which driver
# --------------------------------------------------------------------------
def build_driver_menu(phone: str, session: dict, choice: int) -> str:
    """Record the chosen slot and list every driver, numbered.

    The whole roster is offered, with "(not available)" against anyone
    not marked available that Sunday, because a driver who said no in
    the sheet can often still say yes when asked directly, and this is
    used precisely when somebody is being asked directly.
    """
    slots = session.get("slots") or []
    if not 1 <= choice <= len(slots):
        return (
            f"{BRAND_PREFIX} Pick a number between 1 and {len(slots)}, "
            f"or {CANCEL_REPLY} to cancel."
        )

    slot = slots[choice - 1]
    sunday_date = session["sunday_date"]

    try:
        drivers = _roster_for(sunday_date)
    except RuntimeError as exc:
        logger.error("Could not read the driver roster: %s", exc)
        return f"{BRAND_PREFIX} Couldn't read the driver list just now."

    if not drivers:
        clear_driver_change_session(phone)
        return f"{BRAND_PREFIX} No drivers on the roster. Nothing to pick from."

    lines = [
        f"{BRAND_PREFIX} {slot['label']} for {_short(sunday_date)} "
        f"is {slot['current'] or 'unassigned'}. Replace with:"
    ]
    for index, driver in enumerate(drivers, start=1):
        mark = "" if driver["available"] else " (not available)"
        lines.append(f"{index}. {driver['name']}{mark}")
    lines.append(f"Reply with a number, or {CANCEL_REPLY} to cancel.")

    try:
        set_driver_change_session(
            phone,
            {
                **session,
                "step": "driver",
                "slot": slot,
                "drivers": drivers,
            },
        )
    except RuntimeError as exc:
        logger.error("Could not store driver menu for %s: %s", phone, exc)
        return f"{BRAND_PREFIX} Couldn't continue just now. Text CHANGE to retry."

    return "\n".join(lines)


def _roster_for(sunday_date: str) -> list[dict]:
    """Every driver, flagged with whether they are available that Sunday."""
    from functions.read_sheets import (
        _get_available_drivers_by_sunday,
        _get_form_responses,
    )

    roster = _get_form_responses()

    available: set[str] = set()
    try:
        by_sunday = _get_available_drivers_by_sunday()
        for key, names in by_sunday.items():
            if _iso_matches(key, sunday_date):
                available = set(names)
                break
    except Exception as exc:
        # Everyone shows as not available rather than losing the list.
        # An admin reading "(not available)" against the whole roster
        # will know something is off; an empty menu tells them nothing.
        logger.warning("Could not read availability for %s: %s", sunday_date, exc)

    return [
        {"name": name, "phone": details.get("phone", ""), "available": name in available}
        for name, details in sorted(roster.items())
    ]


# --------------------------------------------------------------------------
# Step 3: apply
# --------------------------------------------------------------------------
def apply_driver_change(phone: str, session: dict, choice: int) -> str:
    """Write the change, tell everyone involved, and confirm to the admin."""
    drivers = session.get("drivers") or []
    if not 1 <= choice <= len(drivers):
        return (
            f"{BRAND_PREFIX} Pick a number between 1 and {len(drivers)}, "
            f"or {CANCEL_REPLY} to cancel."
        )

    slot = session["slot"]
    sunday_date = session["sunday_date"]
    new_driver = drivers[choice - 1]["name"]
    old_driver = slot.get("current")

    if _same(old_driver, new_driver):
        clear_driver_change_session(phone)
        return (
            f"{BRAND_PREFIX} {new_driver} is already on {slot['label']} "
            f"for {_short(sunday_date)}. Nothing changed."
        )

    fields = {field: new_driver for field in slot["fields"]}
    for field in slot.get("clear", []):
        fields[field] = ""

    try:
        _write_schedule(session["schedule_id"], fields)
    except RuntimeError as exc:
        logger.error("Could not apply driver change: %s", exc)
        return (
            f"{BRAND_PREFIX} Couldn't save that change. Nothing was "
            f"altered. Please try again."
        )

    clear_driver_change_session(phone)
    logger.info(
        "Driver change for %s: %s -> %s on %s by %s.",
        sunday_date,
        old_driver,
        new_driver,
        slot["label"],
        phone,
    )

    notified = _notify_everyone(sunday_date, slot["label"], old_driver, new_driver, phone)

    return (
        f"{BRAND_PREFIX} Done. {slot['label']} for {_short(sunday_date)} is "
        f"now {new_driver}"
        + (f", was {old_driver}" if old_driver else "")
        + f". Told {notified} driver(s)."
    )


def _write_schedule(schedule_id: str, fields: dict) -> None:
    """Update one semester_schedule document."""
    from db.firestore_client import SEMESTER_SCHEDULE_COLLECTION, get_client

    try:
        get_client().collection(SEMESTER_SCHEDULE_COLLECTION).document(
            schedule_id
        ).update(fields)
    except Exception as exc:
        raise RuntimeError(f"Failed to update schedule {schedule_id}: {exc}") from exc


def _notify_everyone(
    sunday_date: str, label: str, old_driver: str | None, new_driver: str, admin_phone: str
) -> int:
    """Text the driver coming off, the one going on, and everyone else on that day.

    Read fresh from the schedule after the write, so the "also driving"
    list reflects the change rather than the state before it.

    Never raises. The change is already saved, and a texting failure must
    not report it as having failed.
    """
    try:
        entry = _schedule_entry(sunday_date)
        roster = {d["name"]: d for d in _roster_for(sunday_date)}
    except Exception as exc:
        logger.error("Driver change saved but could not build notifications: %s", exc)
        return 0

    when = _short(sunday_date)
    others = sorted(_everyone_on(entry) - {_key(new_driver), _key(old_driver)})

    messages: list[tuple[str, str]] = []
    if old_driver:
        messages.append(
            (
                old_driver,
                f"{BRAND_PREFIX} You're off {label} for {when}. "
                f"{new_driver} is taking it. Thanks for being on the list. "
                f"{OPT_OUT_NOTICE}",
            )
        )
    messages.append(
        (
            new_driver,
            f"{BRAND_PREFIX} You're now on {label} for {when}"
            + (f", covering for {old_driver}" if old_driver else "")
            + f". Reply ROUTE for your stops. {OPT_OUT_NOTICE}",
        )
    )
    for name in others:
        actual = _display_name(entry, name)
        messages.append(
            (
                actual,
                f"{BRAND_PREFIX} Driver change for {when}: {label} is now "
                f"{new_driver}"
                + (f", was {old_driver}" if old_driver else "")
                + f". Your own slot is unchanged. {OPT_OUT_NOTICE}",
            )
        )

    sent = 0
    for name, body in messages:
        driver = roster.get(name)
        if not driver or not driver.get("phone"):
            logger.warning("No phone for %r; not telling them about the change.", name)
            continue
        try:
            number = normalize_to_e164(driver["phone"])
        except ValueError:
            logger.warning("Unusable phone for %r.", name)
            continue
        if send_sms(number, body):
            sent += 1
        else:
            logger.error("Could not text %r about the driver change.", name)

    return sent


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------
def _schedule_entry(sunday_date: str) -> dict | None:
    for entry in get_semester_schedule():
        if (entry.get("date") or "").strip() == sunday_date:
            return entry
    return None


def _everyone_on(entry: dict | None) -> set[str]:
    if not entry:
        return set()
    names = {
        _key(entry.get(f"{s}_{leg}") or entry.get(s))
        for s in _SHUTTLES
        for leg in _LEGS
    }
    names.add(_key(entry.get("backup")))
    names.discard("")
    return names


def _display_name(entry: dict | None, key: str) -> str:
    """Recover the name as written on the schedule from its normalised key."""
    if not entry:
        return key
    for field in list(_SHUTTLES) + [
        f"{s}_{leg}" for s in _SHUTTLES for leg in _LEGS
    ] + ["backup"]:
        value = entry.get(field)
        if value and _key(value) == key:
            return value
    return key


def _key(name: str | None) -> str:
    return " ".join(str(name or "").split()).casefold()


def _same(a: str | None, b: str | None) -> bool:
    return _key(a) == _key(b) and _key(a) != ""


def _iso_matches(sheet_date: str, iso_date: str) -> bool:
    """Whether a sheet date cell refers to the same day as an ISO date."""
    from functions.read_sheets import _sheet_date_to_iso

    try:
        return _sheet_date_to_iso(str(sheet_date).strip()) == iso_date
    except Exception:
        return False


def _short(iso_date: str) -> str:
    from datetime import datetime

    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"
