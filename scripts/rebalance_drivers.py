# Compares the semester driver schedule with the Available Drivers tab and
# fixes only the slots that no longer work.
#
# Run from the repo root with the venv on. The default is a preview that
# saves nothing:
#
#     python3 -m scripts.rebalance_drivers
#
# When the preview looks right, save it:
#
#     python3 -m scripts.rebalance_drivers --apply

from __future__ import annotations

import logging
import sys


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.WARNING)

    from functions.rebalance_drivers import run_rebalance

    apply = "--apply" in sys.argv[1:]
    result = run_rebalance(apply=apply)
    print(result["report"])
    for note in result["skipped"]:
        print("Skipped:", note)
    for err in result["errors"]:
        print("ERROR saving:", err)
    if not apply and result["changes"]:
        print("\nNothing was saved. Run again with --apply to save these changes.")


if __name__ == "__main__":
    main()
