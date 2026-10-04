// Runs the pure RSVP helpers in Code.gs. Apps Script itself cannot run here;
// these functions do not touch SpreadsheetApp.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const code = fs.readFileSync(path.join(__dirname, "..", "Code.gs"), "utf8");
const context = { console };
vm.createContext(context);
vm.runInContext(code, context);

const {
  findGuestRowIn,
  namesReferToSameGuest,
  incomingPhoneReplaces,
  diffFields,
  stateAfterRsvp,
  assertSafeGuestWrite,
  RSVP_TRACKED_FIELDS,
  planGuestWrite,
  needsReviewValues,
  directoryAccessDecision,
  NEEDS_REVIEW_HEADERS,
} = context;

let failed = 0;
function check(name, cond) {
  if (!cond) {
    failed += 1;
    console.error("FAIL:", name);
  }
}

// Sheet column B, data starting at row 27. Index 0 is row 27.
const rows = [
  ["Robert Daly"],                  // 27
  ["Larissa Lima (Robert Daly)"],   // 28
  ["Corey Brennan"],                // 29
  ["Guest (Corey Brennan)"],        // 30
  ["Maria Silva"],                  // 31
  ["Maria Santos"],                 // 32
  ["Guest (Maria Santos)"],         // 33
  ["Cathy Cahill (Linda Cahill)"],  // 34
  ["Linda Cahill"],                 // 35
  ["Anna Laura"],                   // 36
  ["Anna Laura Teixeira"],          // 37
  ["Jane Doe (Alice)"],             // 38
  ["Jane Doe (Bob)"],               // 39
];

function rowOf(name) {
  return findGuestRowIn(rows, name);
}

check("exact Robert Daly is row 27", rowOf("Robert Daly") === 27);
check("Larissa with household suffix is her own row", rowOf("Larissa Lima (Robert Daly)") === 28);
check("Larissa without suffix still finds her row", rowOf("Larissa Lima") === 28);
check("Corey exact is his own row, not the plus-one", rowOf("Corey Brennan") === 29);
check("plus-one placeholder exact is row 30, not Corey", rowOf("Guest (Corey Brennan)") === 30);
check("Corey lookup does not land on Guest (Corey)", rowOf("Corey Brennan") !== 30);

check("short Maria does not guess between Silva and Santos", rowOf("Maria") === -1);
check("Maria Silva exact", rowOf("Maria Silva") === 31);
check("Maria Santos does not land on her plus-one", rowOf("Maria Santos") === 32);
check("Guest (Maria Santos) is the placeholder", rowOf("Guest (Maria Santos)") === 33);

check("Linda is not overwritten by Cathy's household suffix", rowOf("Linda Cahill") === 35);
check("Cathy exact row", rowOf("Cathy Cahill (Linda Cahill)") === 34);
check("Cathy outside-name finds Cathy, not Linda", rowOf("Cathy Cahill") === 34);

check("Anna Laura exact, not the longer similar name", rowOf("Anna Laura") === 36);
check("Anna Laura Teixeira exact", rowOf("Anna Laura Teixeira") === 37);
check("Anna does not fuzzy-match either Anna Laura", rowOf("Anna") === -1);

check("two Jane Does with different anchors are not guessed", rowOf("Jane Doe") === -1);
check("Jane Doe (Alice) exact", rowOf("Jane Doe (Alice)") === 38);
check("Jane Doe (Bob) does not fall through to Alice", rowOf("Jane Doe (Bob)") === 39);

check("missing guest writes nothing", rowOf("Nobody Here") === -1);
check("placeholder with no such row does not hit the anchor", rowOf("Guest (Robert Daly)") === -1);
check("renamed plus-one is found by the new outside name", findGuestRowIn([["Jane Doe (Corey Brennan)"]], "Jane Doe") === 27);

check("same guest helper rejects plus-one vs anchor", namesReferToSameGuest("guest (corey brennan)", "corey brennan") === false);
check("same guest helper rejects similar shorter name", namesReferToSameGuest("maria", "maria silva") === false);
check("same guest helper accepts outside name of a suffixed row", namesReferToSameGuest("larissa lima", "larissa lima (robert daly)") === true);

let threw = false;
try {
  assertSafeGuestWrite("Guest (Corey Brennan)", "Corey Brennan");
} catch (e) {
  threw = true;
}
check("refuses to write the anchor onto the plus-one row", threw);

threw = false;
try {
  assertSafeGuestWrite("Corey Brennan", "Guest (Corey Brennan)");
} catch (e) {
  threw = true;
}
check("refuses to write the plus-one lookup onto the anchor row", threw);

threw = false;
try {
  assertSafeGuestWrite("Corey Brennan", "Corey Brennan");
} catch (e) {
  threw = true;
}
check("exact same person is allowed", threw === false);

// Household-wide decline names each row on purpose. Both must still resolve.
const declineTargets = ["Corey Brennan", "Guest (Corey Brennan)"];
check(
  "household no still addresses the guest and the plus-one as two rows",
  declineTargets.map(rowOf).join(",") === "29,30"
);

check("blank phone does not clear an existing number", incomingPhoneReplaces("353831112222", "") === false);
check("first phone fills an empty cell", incomingPhoneReplaces("", "353831112222") === true);
check("same digits, different format, is not a change", incomingPhoneReplaces("087 123 4567", "0871234567") === false);
check("a different number replaces the old one", incomingPhoneReplaces("+353 83 111 2222", "+1 646 339 0886") === true);
check("missing new phone does not replace", incomingPhoneReplaces("+353 83 111 2222", "   ") === false);

const before = {
  name: "Corey Brennan",
  phone: "+353 83 111 2222",
  invitation_sent: "TRUE",
  attending: "FALSE",
  not_attending: "FALSE",
  dietary_vegetarian: "FALSE",
  dietary_vegan: "FALSE",
  dietary_nut_allergy: "FALSE",
  dietary_no_beef: "FALSE",
  dietary_no_pork: "FALSE",
  dietary_shellfish: "FALSE",
  day1_invited: "TRUE",
  day1_attending: "FALSE",
  day2_invited: "TRUE",
  day2_attending: "FALSE",
  day3_invited: "TRUE",
  day3_attending: "FALSE",
  needs_elevator: "FALSE",
};
const after = stateAfterRsvp(
  {
    name: "Corey Brennan",
    phone: "+1 646 000 0000",
    attending: "yes",
    days: ["day1", "day2"],
    dietary_vegetarian: true,
    needs_elevator: false,
  },
  "Corey Brennan",
  "+1 646 000 0000"
);
const changes = diffFields(before, after, RSVP_TRACKED_FIELDS);
const changedFields = changes.map((c) => c.field).sort();
check(
  "resubmit logs phone, status, diet and the days that flipped",
  changedFields.join(",") === ["attending", "day1_attending", "day2_attending", "dietary_vegetarian", "phone"].sort().join(",")
);
const phoneChange = changes.find((c) => c.field === "phone");
check("phone history is old -> new", phoneChange.oldValue === "+353 83 111 2222" && phoneChange.newValue === "+1 646 000 0000");

const same = stateAfterRsvp(
  { name: "Corey Brennan", phone: "+353 83 111 2222", attending: "no", days: [] },
  "Corey Brennan",
  "+353 83 111 2222"
);
// before is all FALSE attending. A decline changes attending? before.attending is FALSE,
// not_attending FALSE. Decline sets not_attending TRUE. That is a real change.
const declineChanges = diffFields(before, same, RSVP_TRACKED_FIELDS);
check(
  "a household decline still records not_attending",
  declineChanges.some((c) => c.field === "not_attending" && c.newValue === "TRUE")
);

const identical = diffFields(after, after, RSVP_TRACKED_FIELDS);
check("identical resubmission logs nothing", identical.length === 0);

const renamed = diffFields(
  { name: "Guest (Corey Brennan)" },
  { name: "Jane Doe (Corey Brennan)" },
  ["name"]
);
check(
  "plus-one rename is old Guest row -> new name",
  renamed.length === 1 &&
    renamed[0].oldValue === "Guest (Corey Brennan)" &&
    renamed[0].newValue === "Jane Doe (Corey Brennan)"
);

const ambiguous = planGuestWrite(rows, {
  name: "Maria",
  phone: "+353 83 000 0000",
  attending: "yes",
  days: ["day2"],
  dietary_vegetarian: true,
});
check("unclear Maria is parked, not written", ambiguous.action === "review" && ambiguous.reason === "unmatched");

const coreyPlan = planGuestWrite(rows, { name: "Corey Brennan", attending: "no", phone: "1" });
check("exact Corey is still a normal write", coreyPlan.action === "write" && coreyPlan.row === 29);

const plusOnePlan = planGuestWrite(rows, { name: "Guest (Corey Brennan)", attending: "no" });
check("exact plus-one row is still a normal write", plusOnePlan.action === "write" && plusOnePlan.row === 30);

const declinePlans = ["Corey Brennan", "Guest (Corey Brennan)"].map((name) =>
  planGuestWrite(rows, { name: name, attending: "no" })
);
check(
  "household no still plans a write for the guest and the plus-one",
  declinePlans.every((p) => p.action === "write")
);

const missingPlaceholder = planGuestWrite(rows, {
  name: "Someone New",
  placeholder_target: "Guest (Robert Daly)",
  attending: "yes",
  phone: "+1 646 339 0886",
});
check(
  "missing plus-one placeholder is review, not Robert's row",
  missingPlaceholder.action === "review" && missingPlaceholder.reason === "unmatched"
);

const reviewRow = needsReviewValues(
  "2026-10-04T00:00:00Z",
  {
    name: "Maria",
    phone: "+353 83 000 0000",
    attending: "yes",
    days: ["day1", "day2"],
    dietary_vegetarian: true,
    needs_elevator: false,
  },
  "unmatched",
  ["Maria", "Maria Silva"]
);
check("needs review has the timestamp", reviewRow[0] === "2026-10-04T00:00:00Z");
check("needs review has the submitted name", reviewRow[1] === "Maria");
check("needs review has the party names", reviewRow[3] === "Maria, Maria Silva");
check("needs review has the phone", reviewRow[4] === "+353 83 000 0000");
check("needs review has the answer", reviewRow[5] === "yes" && reviewRow[6] === "day1, day2");
check("needs review dietary and reason", reviewRow[7] === "vegetarian" && reviewRow[11] === "unmatched");
const raw = JSON.parse(reviewRow[12]);
check("needs review raw payload keeps phone and attending", raw.phone === "+353 83 000 0000" && raw.attending === "yes");
check("needs review header count matches the row", reviewRow.length === NEEDS_REVIEW_HEADERS.length);

check("missing directory secret stays open", directoryAccessDecision("", "anything").allow === true && directoryAccessDecision("", "anything").locked === false);
check("unset secret allows Aurora with no secret too", directoryAccessDecision(null, undefined).allow === true);
check("set secret allows the matching value", directoryAccessDecision("new-secret", "new-secret").allow === true && directoryAccessDecision("new-secret", "new-secret").locked === true);
check("set secret rejects a different value", directoryAccessDecision("new-secret", "old-secret").allow === false);
check("set secret rejects a missing value", directoryAccessDecision("new-secret", "").allow === false);

const page = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
check("RSVP page does not mention the directory secret", page.indexOf("DIRECTORY_SECRET") === -1);
check("RSVP page still treats only status error as failure", page.indexOf('body.status === "error"') !== -1);
check("RSVP page has no guest login or code field", !/login|access code|verification code/i.test(page));

if (failed) {
  console.error(failed + " failed");
  process.exit(1);
}
console.log("ok");
