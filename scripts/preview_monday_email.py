# Prints tonight's Monday schedule email exactly as Dae would get it,
# using live Firestore and sheet data. Sends nothing and saves nothing:
# the availability check runs as a preview, and send_email is replaced
# with a print. Also writes the result to "Claude outputs/".
#
# Run from the repo root with the venv on:
#
#     python3 -m scripts.preview_monday_email
#
# To also email the preview to Peter only (no Cc, no Bcc, subject marked
# TEST), add --email-me. The recipient is fixed here on purpose so this
# can never reach Dae, Sarah or Ellie:
#
#     python3 -m scripts.preview_monday_email --email-me

from __future__ import annotations

import logging
import os
import sys

TEST_RECIPIENT = "peterhahn410@gmail.com"


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.WARNING)

    from config.clock import church_today
    import functions.rebalance_drivers as rb
    import functions.send_semester_schedule as ss

    schedule, availability, shifts = rb.load_inputs()
    plan = rb.plan_rebalance(schedule, availability, shifts, today=church_today().isoformat())

    # Same result the real job would get, but nothing is written to Firestore.
    rb.run_rebalance = lambda apply=False: {
        "applied": False, "changes": plan["changes"], "unfilled": plan["unfilled"],
        "skipped": plan["skipped"], "errors": [], "report": "",
    }
    # The email reads the schedule as it will be after tonight's changes.
    ss.get_semester_schedule = lambda: sorted(plan["schedule"], key=lambda e: e["date"])

    captured: dict = {}

    def fake_send_email(to, subject, body, cc=None, bcc=None, html=False):
        captured.update(to=to, cc=cc, bcc=bcc, subject=subject, body=body)
        return True

    from functions.send_email import send_email as real_send_email

    ss.send_email = fake_send_email
    ss.send_monday_schedule()

    text = (
        f"To: {captured.get('to')}\nCc: {captured.get('cc')}\nBcc: {captured.get('bcc')}\n"
        f"Subject: {captured.get('subject')}\n\n{captured.get('body')}\n"
    )
    print(text)
    os.makedirs("Claude outputs", exist_ok=True)
    with open("Claude outputs/monday_email_preview.txt", "w") as fh:
        fh.write(text)
    if "--email-me" in sys.argv[1:]:
        note = (
            "TEST PREVIEW. Only you received this. The real email goes to "
            f"{captured.get('to')} (Cc {captured.get('cc')}) at 6 PM.\n\n"
        )
        ok = real_send_email(
            to=TEST_RECIPIENT,
            subject=f"TEST - {captured.get('subject')}",
            body=note + str(captured.get("body")),
        )
        print(f"Test email to {TEST_RECIPIENT}: {'sent' if ok else 'FAILED'}")
        print("(Nothing was sent to anyone else, and nothing was saved to the schedule.)")
    else:
        print("(Preview only. Nothing was sent or saved to the schedule.)")


if __name__ == "__main__":
    main()
