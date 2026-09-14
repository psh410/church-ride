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
    const isDuplicate = priorRowsThisWeek.some(function (priorRow) {
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

    const currentShuttleCount = priorRowsThisWeek.filter(function (priorRow) {
      const priorStop = String(priorRow[CAMPUS_ADDRESS_COL - 1]).trim();
      const cleanStop = priorStop.split("/")[0].trim();
      return stopToShuttle[cleanStop] === shuttleId;
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