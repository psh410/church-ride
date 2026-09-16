# Shows the Morning Prayer email for a given week without sending it,
# and shows who the old name matching would have emailed instead.
#
# Written after the reminder went to the wrong people. The failure was
# silent in both directions: a wrong guess looked exactly like a right
# one, and a name that matched nobody just quietly missed their
# reminder. This makes both visible before anything is sent.
#
# Usage, from the repo root with the venv active and a .env present:
#
#   python -m scripts.preview_morning_prayer
#   python -m scripts.preview_morning_prayer --week 2026-09-14
#   python -m scripts.preview_morning_prayer --no-compare
#
# With no arguments it previews the week the job would pick right now,
# which through midweek is the current week - the one already sent.
#
# Reads Sheets. Sends nothing, ever.

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from functions.prayer.morning import (
    ALWAYS_BCC,
    CHICAGO,
    EMAIL_SUBJECT,
    SCHEDULE_DAYS,
    build_morning_prayer_body,
    get_morning_prayer_schedule,
    get_roster_emails,
    get_schedule_monday,
    resolve_recipients_for_week,
    _bcc_addresses,
)


def _old_tokens(name: str) -> set[str]:
    return {p for p in name.replace("-", " ").lower().split() if p}


def _old_lookup(name: str, roster: dict[str, str]) -> tuple[str | None, list[str]]:
    """Reproduce the matching that was replaced, plus who else it matched.

    Exact match first, then the first roster entry sharing ANY token,
    in dict (sheet row) order. Returned alongside the full candidate
    list, because the count of candidates is the whole story: anything
    above one was a coin flip decided by row order.
    """
    if not name:
        return None, []
    for roster_name, email in roster.items():
        if roster_name.lower() == name.lower():
            return email, [roster_name]

    tokens = _old_tokens(name)
    candidates = [rn for rn in roster if tokens & _old_tokens(rn)]
    if candidates:
        return roster[candidates[0]], candidates
    return None, []


def _monday_from_arg(raw: str | None) -> tuple[date, datetime | None]:
    if raw is None:
        return get_schedule_monday(), None
    monday = datetime.strptime(raw, "%Y-%m-%d").date()
    # Hand the module the Saturday 6pm before that Monday, which is when
    # the job actually fires (Cloud Scheduler "0 18 * * 6"), so the
    # preview selects the week the real send would have.
    when = datetime.combine(
        monday - timedelta(days=2), datetime.min.time()
    ).replace(hour=18, tzinfo=CHICAGO)
    return monday, when


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="Monday of the week, YYYY-MM-DD")
    parser.add_argument(
        "--no-compare",
        action="store_true",
        help="skip the comparison against the old matching",
    )
    args = parser.parse_args()

    monday, when = _monday_from_arg(args.week)
    print(f"Morning Prayer preview for the week of {monday} (Mon-Fri)")
    print("NOTHING IS SENT BY THIS SCRIPT.")
    print("=" * 72)

    schedule = get_morning_prayer_schedule(when)
    roster = get_roster_emails()
    recipients, problems = resolve_recipients_for_week(schedule, roster)

    print("\nSCHEDULE AS READ FROM THE SHEETS")
    print("-" * 72)
    for key, label in SCHEDULE_DAYS:
        day = schedule.get(key) or {}
        dev = day.get("devotional") or "(none)"
        wor = day.get("worship") or "(none)"
        marks = []
        if day.get("devotional_absent"):
            marks.append("devotional marked ABSENT")
        if day.get("worship_absent"):
            marks.append("worship marked ABSENT")
        note = ("   [" + "; ".join(marks) + "]") if marks else ""
        print(f"  {label:<10} devotional: {dev:<22} worship: {wor}{note}")

    print(f"\nRECIPIENTS ({len(recipients)})")
    print("-" * 72)
    for email in recipients:
        print(f"  {email}")
    print(f"  bcc: {', '.join(_bcc_addresses())}")

    print(f"\nUNRESOLVED NAMES ({len(problems)})")
    print("-" * 72)
    if problems:
        for problem in problems:
            print(f"  {problem}")
        print(f"\n  These are skipped, never guessed at. {ALWAYS_BCC} gets an")
        print("  alert naming them. The fix is in the sheet, not the code.")
    else:
        print("  none - every name resolved to exactly one person")

    if not args.no_compare:
        print("\nWHAT THE OLD MATCHING WOULD HAVE DONE")
        print("-" * 72)
        differences = 0
        for key, label in SCHEDULE_DAYS:
            day = schedule.get(key) or {}
            for role in ("devotional", "worship"):
                name = day.get(role)
                if not name:
                    continue
                old_email, candidates = _old_lookup(name, roster)
                new_email = None
                for roster_name, email in roster.items():
                    from functions.prayer.morning import _same_person

                    if roster_name.lower() == name.lower():
                        new_email = email
                        break
                else:
                    matches = [
                        em for rn, em in roster.items() if _same_person(name, rn)
                    ]
                    new_email = matches[0] if len(matches) == 1 else None

                if old_email == new_email:
                    continue
                differences += 1
                print(f"  {label} {role}: {name!r}")
                print(f"      old -> {old_email}")
                print(f"      now -> {new_email or '(skipped, reported to you)'}")
                if len(candidates) > 1:
                    print(f"      it matched {len(candidates)}: {candidates}")
                    print("      and picked the first by sheet row order")
        if differences == 0:
            print("  no difference this week - every name was unambiguous")
        else:
            print(f"\n  {differences} name(s) would have been routed differently.")

    print(f"\nEMAIL BODY  (subject: {EMAIL_SUBJECT})")
    print("=" * 72)
    print(build_morning_prayer_body(schedule))
    return 0


if __name__ == "__main__":
    sys.exit(main())
