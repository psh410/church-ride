// Google Apps Script — Rider Signup Duplicate & Capacity Flagging
// Runs automatically when someone submits the CFC Need-A-Ride Form.
// This trigger is attached to the FORM itself, so the event object
// gives us e.response (a FormResponse), not e.range like a
// spreadsheet-attached trigger would.

const CAMPUS_ADDRESS_COL = 4;  // Column D
const EMAIL_COL = 6;           // Column F
const PHONE_COL = 5;           // Column E
const TIMESTAMP_COL = 1;       // Column A

const ROUTES_TAB = "Routes";
const SHUTTLES_TAB = "Shuttles";
const RESPONSES_TAB = "Form Responses 1";

// Signup confirmation text. The real secret lives in the live Apps
// Script project and in Secret Manager, never in this repo copy.
const CONFIRMATION_URL =
  "https://church-rides-app-607355372763.us-central1.run.app/confirm-rider-signup";
const CONFIRMATION_SECRET = "SET-IN-THE-LIVE-SCRIPT";

function onFormSubmit(e) {
  try {
    const form = FormApp.getActiveForm();
    const spreadsheetId = form.getDestinationId();
    const ss = SpreadsheetApp.openById(spreadsheetId);
    const sheet = ss.getSheetByName(RESPONSES_TAB);

    // Find the row that was just submitted by matching the
    // FormResponse's timestamp against column A.
    const responseTimestamp = e.response.getTimestamp();
    const allData = sheet.getDataRange().getValues();

    let thisRowIndex = -1;
    for (let i = allData.length - 1; i >= 1; i--) {
      const rowTimestamp = new Date(allData[i][TIMESTAMP_COL - 1]);
      if (Math.abs(rowTimestamp.getTime() - responseTimestamp.getTime()) < 5000) {
        thisRowIndex = i;
        break;
      }
    }

    if (thisRowIndex === -1) {
      console.error("Could not find matching row for this submission");
      return;
    }

    const row = thisRowIndex + 1; // convert back to 1-indexed sheet row
    const thisRow = allData[thisRowIndex];

    const thisTimestamp = new Date(thisRow[TIMESTAMP_COL - 1]);
    const thisEmail = normalizeEmail(thisRow[EMAIL_COL - 1]);
    const thisPhone = normalizePhone(thisRow[PHONE_COL - 1]);
    const thisStop = String(thisRow[CAMPUS_ADDRESS_COL - 1]).trim();

    const windowStart = getSignupWindowStart(thisTimestamp);

    const priorRowsThisWeek = [];
    for (let i = 1; i < allData.length; i++) {
      if (i === thisRowIndex) continue;
      const rowTimestamp = new Date(allData[i][TIMESTAMP_COL - 1]);
      if (rowTimestamp >= windowStart && rowTimestamp < thisTimestamp) {
        priorRowsThisWeek.push(allData[i]);
      }
    }

    // ── Check 1: Duplicate ──────────────────────────────────────
    // A prior row only makes this one a duplicate if that prior signup
    // is still standing. Rows flagged "cancelled" are exactly the case
    // where it is not: the rider gave up their seat by texting SKIP and
    // is now signing up again on purpose. Treating that as a duplicate
    // would drop them out of every shuttle list and text them that
    // they're already signed up, leaving them with no ride and a
    // message saying they have one.
    //
    // Rows already flagged "duplicate" are skipped for the same reason
    // one step removed: if the original signup was cancelled, the
    // duplicate of it is not a live signup either, and matching against
    // it would recreate the bug through the back door. When the
    // original IS still standing it is unflagged, so it still matches
    // and repeat submissions are still caught.
    const isDuplicate = priorRowsThisWeek.some(function (priorRow) {
      const priorFlags = rowFlags(priorRow[CAMPUS_ADDRESS_COL - 1]);
      if (priorFlags.indexOf("cancelled") !== -1 ||
          priorFlags.indexOf("duplicate") !== -1) {
        return false;
      }
      const priorEmail = normalizeEmail(priorRow[EMAIL_COL - 1]);
      const priorPhone = normalizePhone(priorRow[PHONE_COL - 1]);
      return (
        (thisEmail && priorEmail && thisEmail === priorEmail) ||
        (thisPhone && priorPhone && thisPhone === priorPhone)
      );
    });

    if (isDuplicate) {
      appendFlagToAddress(sheet, row, thisStop, "duplicate");
      sendSignupConfirmation(row);
      return;
    }

    // ── Check 2: Shuttle capacity ────────────────────────────────
    const stopToShuttle = getStopToShuttleMap(ss);
    const shuttleId = stopToShuttle[thisStop];
    if (!shuttleId) {
      // Off-route address. No shuttle, but they still get a text
      // saying a personal driver is being arranged. This is roughly
      // 40% of signups, so do NOT let this path return silently.
      sendSignupConfirmation(row);
      return;
    }

    // Count only rows actually occupying a shuttle seat, which means
    // rows with no flag at all.
    //
    // This used to count every row whose address mapped to the shuttle,
    // flags and all, which held seats that nobody was sitting in. A
    // "cancelled" row kept a seat reserved for someone who had
    // explicitly given it up, defeating the point of having a
    // cancellation path. A "duplicate" row counted one person twice. A
    // "driver" row counted someone who by definition did not get a
    // shuttle seat.
    //
    // Known limitation: riders already flagged "driver" are not
    // promoted when a seat frees up later. Re-flagging existing rows is
    // a bigger change than this, and Dae and Sarah are arranging those
    // rides by hand anyway. What this does guarantee is that the next
    // person to sign up gets the freed seat instead of being turned
    // away from an empty one.
    const currentShuttleCount = priorRowsThisWeek.filter(function (priorRow) {
      if (hasKnownFlag(priorRow[CAMPUS_ADDRESS_COL - 1])) return false;
      const priorStop = String(priorRow[CAMPUS_ADDRESS_COL - 1]).trim();
      return stopToShuttle[priorStop.split("/")[0].trim()] === shuttleId;
    }).length;

    const capacity = getShuttleCapacity(ss, shuttleId);
    if (currentShuttleCount >= capacity) {
      appendFlagToAddress(sheet, row, thisStop, "driver");
    }

    // Always last: the backend reads the flags off the row to decide
    // which of the four messages to send, so the sheet must be written
    // before this fires. Deciding who gets a text is the backend's job,
    // not this script's - it is called on every path and may well
    // answer "skipped".
    sendSignupConfirmation(row);
  } catch (err) {
    console.error("onFormSubmit error: " + err.toString());
  }
}

// ── Signup confirmation ─────────────────────────────────────────
function sendSignupConfirmation(row) {
  try {
    const response = UrlFetchApp.fetch(CONFIRMATION_URL, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify({ row: row, secret: CONFIRMATION_SECRET }),
      muteHttpExceptions: true,
    });
    console.log("Confirmation for row " + row + ": " + response.getContentText());
  } catch (err) {
    // Never let a texting problem break the flagging above.
    console.error("Confirmation failed for row " + row + ": " + err);
  }
}

// ── Helpers ─────────────────────────────────────────────────────
function getStopToShuttleMap(ss) {
  const routesSheet = ss.getSheetByName(ROUTES_TAB);
  if (!routesSheet) {
    console.error("Routes tab not found");
    return {};
  }
  const data = routesSheet.getDataRange().getValues();
  const map = {};
  for (let i = 1; i < data.length; i++) {
    const shuttleId = String(data[i][0]).trim();
    const stopName = String(data[i][3]).trim();
    if (shuttleId && stopName) {
      map[stopName] = shuttleId;
    }
  }
  return map;
}

function getShuttleCapacity(ss, shuttleId) {
  const shuttlesSheet = ss.getSheetByName(SHUTTLES_TAB);
  if (!shuttlesSheet) {
    console.error("Shuttles tab not found - defaulting to 14");
    return 14;
  }
  const data = shuttlesSheet.getDataRange().getValues();
  const headerRow = data[0];
  let shuttleColIndex = -1;
  for (let c = 0; c < headerRow.length; c++) {
    if (String(headerRow[c]).trim() === shuttleId) {
      shuttleColIndex = c;
      break;
    }
  }
  if (shuttleColIndex === -1) return 14;

  for (let r = 1; r < data.length; r++) {
    if (String(data[r][0]).trim() === "Capacity") {
      return Number(data[r][shuttleColIndex]) || 14;
    }
  }
  return 14;
}

// The complete flag vocabulary. Matched explicitly rather than treating
// any "/" as a flag, because riders do type addresses containing
// slashes ("1002 S Lincoln Apt 1/2") and a stray one must not be read
// as a flag that quietly changes how their row is counted.
const KNOWN_FLAGS = ["duplicate", "driver", "cancelled"];


function hasKnownFlag(addressValue) {
  const flags = rowFlags(addressValue);
  for (let i = 0; i < flags.length; i++) {
    if (KNOWN_FLAGS.indexOf(flags[i]) !== -1) return true;
  }
  return false;
}


function rowFlags(addressValue) {
  // Return the flags appended to a campus address cell, lowercased.
  // "FAR" -> [], "FAR/duplicate" -> ["duplicate"],
  // "FAR/cancelled" -> ["cancelled"]. Written by this script
  // (duplicate, driver) and by the backend's SKIP handler (cancelled),
  // which writes to the sheet directly through the Sheets API rather
  // than through this script.
  const parts = String(addressValue).split("/");
  const flags = [];
  for (let i = 1; i < parts.length; i++) {
    const flag = parts[i].trim().toLowerCase();
    if (flag) flags.push(flag);
  }
  return flags;
}


function appendFlagToAddress(sheet, row, currentStop, flag) {
  const newValue = currentStop + "/" + flag;
  sheet.getRange(row, CAMPUS_ADDRESS_COL).setValue(newValue);
}

function normalizeEmail(email) {
  return String(email || "").trim().toLowerCase();
}

function normalizePhone(phone) {
  return String(phone || "").replace(/\D/g, "");
}

function getSignupWindowStart(submissionTime) {
  // Find the most recent Sunday 9:00 AM before this submission.
  // That's when the previous week's ride happened, and signups
  // for the NEXT Sunday open right after that moment.
  const date = new Date(submissionTime);
  const dayOfWeek = date.getDay(); // 0 = Sunday, 1 = Monday, etc.

  const lastSunday = new Date(date);
  lastSunday.setDate(date.getDate() - dayOfWeek);
  lastSunday.setHours(9, 0, 0, 0);

  // If today IS Sunday and it's before 9 AM, the correct window
  // boundary is actually the Sunday one week before that.
  if (dayOfWeek === 0 && date.getHours() < 9) {
    lastSunday.setDate(lastSunday.getDate() - 7);
  }

  return lastSunday;
}