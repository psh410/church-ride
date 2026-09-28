# Sets who drives (or backs up) on one Sunday, by hand. Use it when a
# change is a judgment call the automatic availability check would not
# make, e.g. evening out drives between people.
#
# Preview first (saves nothing):
#
#     python3 -m scripts.set_sunday_slots 2026-10-25 --shuttle-1 "Albert Lee" --backup "Dae Kang" --reason "..."
#
# Then save it by adding --apply.
#
# --reason is required to save. It is shown under that Sunday in the
# Monday schedule email, so a hand-made change always says why. If the
# automatic availability check later changes that Sunday, the reason is
# cleared since it no longer applies.
#
# --shuttle-1 / --shuttle-2 set the whole day (pickup and return) for that
# shuttle. --shuttle-1-pickup, --shuttle-1-return (and the same for
# shuttle 2) set one leg only. Anything not named is left exactly as it is.
#
# The automatic check keeps a hand-made slot as long as the driver is
# still listed as available and it does not put them past 2 Sundays in a
# row, so choose someone who is free that week.

from __future__ import annotations

import argparse


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("date", help="Sunday as YYYY-MM-DD")
    parser.add_argument("--shuttle-1")
    parser.add_argument("--shuttle-2")
    parser.add_argument("--backup")
    for n in ("1", "2"):
        for leg in ("pickup", "return"):
            parser.add_argument(f"--shuttle-{n}-{leg}")
    parser.add_argument("--reason", help="Why, in a sentence. Shown in the Monday email.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    from db.firestore_client import SEMESTER_SCHEDULE_COLLECTION, get_client

    client = get_client()
    docs = list(
        client.collection(SEMESTER_SCHEDULE_COLLECTION).where("date", "==", args.date).stream()
    )
    if len(docs) != 1:
        print(f"Expected one schedule entry for {args.date}, found {len(docs)}. Nothing changed.")
        return
    doc = docs[0]
    current = doc.to_dict() or {}

    fields: dict = {}
    for shuttle, name in (("shuttle_1", args.shuttle_1), ("shuttle_2", args.shuttle_2)):
        if name:
            fields[shuttle] = name
            fields[f"{shuttle}_pickup"] = name
            fields[f"{shuttle}_return"] = name
    for n in ("1", "2"):
        shuttle = f"shuttle_{n}"
        for leg in ("pickup", "return"):
            name = getattr(args, f"shuttle_{n}_{leg}")
            if name:
                fields[f"{shuttle}_{leg}"] = name
        if f"{shuttle}_pickup" in fields and shuttle not in fields:
            # The plain shuttle_N field follows the pickup driver, as the
            # automatic check does.
            fields[shuttle] = fields[f"{shuttle}_pickup"]
    if args.backup:
        fields["backup"] = args.backup
    if not fields:
        print("Nothing to change. Name a --shuttle-1, --shuttle-2 or --backup.")
        return
    if args.reason:
        fields["note"] = args.reason.strip()

    def show(entry: dict) -> None:
        for shuttle in ("shuttle_1", "shuttle_2"):
            p, r = entry.get(f"{shuttle}_pickup") or entry.get(shuttle), entry.get(f"{shuttle}_return") or entry.get(shuttle)
            print(f"  {shuttle}: {p if p == r else f'Pickup: {p}, Return: {r}'}")
        print(f"  backup: {entry.get('backup') or 'none'}")

    print(f"{args.date} now:")
    show(current)
    print("after:")
    show({**current, **fields})
    print(f"  why: {fields.get('note') or '(no reason given)'}")

    if args.apply and not args.reason:
        print("\nNot saved: add --reason so the email can say why.")
        return
    if not args.apply:
        print("\nPreview only, nothing saved. Add --apply to save.")
        return
    doc.reference.update(fields)
    print("\nSaved.")


if __name__ == "__main__":
    main()
