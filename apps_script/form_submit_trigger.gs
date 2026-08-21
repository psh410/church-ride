// Google Apps Script form-submit trigger: on new ride signups, writes a
// rider document to Firestore and publishes a Pub/Sub event so the
// monitor agent can decide whether coordinators need to be alerted.

var PROJECT_ID = "church-rides";
var FIRESTORE_RIDERS_URL =
  "https://firestore.googleapis.com/v1/projects/" +
  PROJECT_ID +
  "/databases/(default)/documents/riders";
var PUBSUB_PUBLISH_URL =
  "https://pubsub.googleapis.com/v1/projects/" +
  PROJECT_ID +
  "/topics/church-rides-events:publish";

var ROUTE_NAME_TO_ID = {
  "North Route": "route_north",
  "East Route": "route_east",
  "South Route": "route_south",
};

function onFormSubmit(e) {
  try {
    var namedValues = e.namedValues;

    var fullName = getFormValue(namedValues, "Full Name");
    var email = getFormValue(namedValues, "Email Address");
    var discordUsername = getFormValue(namedValues, "Discord Username");
    var routeName = getFormValue(namedValues, "Route");
    var pickupStop = getFormValue(namedValues, "Pickup Stop");
    var returnRideAnswer = getFormValue(
      namedValues,
      "Do you need a return ride?"
    );

    var routeId = ROUTE_NAME_TO_ID[routeName] || null;
    var returnRide = /^y(es)?$/i.test(String(returnRideAnswer).trim());
    var sundayDate = getNextSunday();

    var riderData = {
      name: fullName,
      email: email,
      discord_username: discordUsername,
      route_id: routeId,
      stop: pickupStop,
      return_ride: returnRide,
      status: "pending",
      sunday_date: sundayDate,
    };

    createFirestoreRider(riderData);

    publishPubSubEvent({
      event_type: "new_rider_signup",
      event_data: {
        rider_name: fullName,
        sunday_date: sundayDate,
        route_id: routeId,
      },
    });
  } catch (err) {
    notifyAdminOfError(err);
  }
}

function getFormValue(namedValues, fieldName) {
  var values = namedValues ? namedValues[fieldName] : null;
  return values && values.length > 0 ? values[0] : "";
}

function createFirestoreRider(riderData) {
  var token = ScriptApp.getOAuthToken();
  var payload = { fields: toFirestoreDoc(riderData) };

  var response = UrlFetchApp.fetch(FIRESTORE_RIDERS_URL, {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + token },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });

  var code = response.getResponseCode();
  if (code < 200 || code >= 300) {
    throw new Error(
      "Firestore write failed (" + code + "): " + response.getContentText()
    );
  }
}

function publishPubSubEvent(message) {
  var token = ScriptApp.getOAuthToken();
  var encodedData = Utilities.base64Encode(
    JSON.stringify(message),
    Utilities.Charset.UTF_8
  );

  var payload = {
    messages: [{ data: encodedData }],
  };

  var response = UrlFetchApp.fetch(PUBSUB_PUBLISH_URL, {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + token },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });

  var code = response.getResponseCode();
  if (code < 200 || code >= 300) {
    throw new Error(
      "Pub/Sub publish failed (" + code + "): " + response.getContentText()
    );
  }
}

function getNextSunday() {
  var today = new Date();
  var dayOfWeek = today.getDay(); // Sunday = 0 ... Saturday = 6
  var daysUntilSunday = (7 - dayOfWeek) % 7; // 0 if today is already Sunday

  var nextSunday = new Date(
    today.getFullYear(),
    today.getMonth(),
    today.getDate() + daysUntilSunday
  );

  return Utilities.formatDate(
    nextSunday,
    Session.getScriptTimeZone(),
    "yyyy-MM-dd"
  );
}

function toFirestoreDoc(data) {
  var fields = {};
  Object.keys(data).forEach(function (key) {
    fields[key] = toFirestoreValue(data[key]);
  });
  return fields;
}

function toFirestoreValue(value) {
  if (value === null || value === undefined) {
    return { nullValue: null };
  }
  if (typeof value === "boolean") {
    return { booleanValue: value };
  }
  if (typeof value === "number") {
    return { doubleValue: value };
  }
  return { stringValue: String(value) };
}

function notifyAdminOfError(err) {
  var adminEmail = PropertiesService.getScriptProperties().getProperty(
    "ADMIN_EMAIL"
  );
  if (!adminEmail) {
    return;
  }

  var subject = "Church Rides Form Trigger Error";
  var body =
    "onFormSubmit failed with error:\n\n" +
    (err && err.stack ? err.stack : String(err));

  try {
    MailApp.sendEmail(adminEmail, subject, body);
  } catch (mailErr) {
    console.error("Failed to send admin error email: " + mailErr);
  }
}
