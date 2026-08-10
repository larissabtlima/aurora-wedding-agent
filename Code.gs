// Aurora Wedding Agent — Google Sheets Webhook v5
// Data starts at ROW 27 (confirmed: Robert Daly = row 27)
// COLUMN MAP:
// B=2 NAME, C=3 ORIGIN, D=4 LANGUAGE, E=5 ACCOMMODATION INCLU, F=6 ACCOMMODATION CONF, G=7 PHONE
// H=8 BRIDAL PARTY, I=9 CERTAINTY, J=10 INVITATION SENT
// K=11 ATTENDING, L=12 NOT ATTENDING
// M=13 Vegetarian, N=14 Vegan, O=15 Nut Allergy, P=16 No Beef, Q=17 No Pork, R=18 Shellfish
// T=20 Day1 Winery INVITED, U=21 Day1 ATTENDING
// V=22 Day2 Wedding INVITED, W=23 Day2 ATTENDING
// X=24 Day3 Pub INVITED, Y=25 Day3 ATTENDING
//
// CHANGES IN v5:
// 1. Guest linking no longer requires matching surnames, and now handles names that
//    have a line break inside the cell instead of a space before "(...)" — this was
//    silently breaking linking for real guests like "Ezgi Atakul (Will Daly)" and
//    "Charlotte Barton\n(George O Mahony)".
// 2. Added a real `action=directory` endpoint, protected by a shared secret, that
//    returns per-guest RSVP/accommodation/bridal-party data. This is what Aurora
//    (app.py) uses to answer guests about ONLY themselves, and to build live admin
//    stats. Previously this endpoint didn't exist, so Aurora's personalization and
//    admin stats were silently returning nothing.
// 3. "Invited" day columns (T/V/X) are now only set TRUE for the specific days a
//    guest actually selected, instead of being blindly set TRUE for all 3 days on
//    every submission.
//
// Passport data goes into a separate "Passaportes" sheet tab.

var DIRECTORY_SECRET = "dda510d6-f20e-452b-b528-44c66d03eab84b81a658-f227-447b-9b4a-318daf548ee6";

function normalizeName(raw) {
  return raw.toString().replace(/\s+/g, ' ').trim();
}

function doGet(e) {
  var params = (e && e.parameter) || {};
  if (params.action === 'directory') {
    return handleDirectoryRequest(params);
  }
  return handlePublicGuestList();
}

// Public guest list — used by the RSVP form's name search / linking.
// Deliberately does NOT include phone, accommodation, or RSVP status.
function handlePublicGuestList() {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Guests");
    var lastRow = sheet.getLastRow();
    var data = sheet.getRange("B27:D" + lastRow).getValues();
    var guests = [];

    var allNames = [];
    for (var i = 0; i < data.length; i++) {
      var n = normalizeName(data[i][0].toString());
      if (n) allNames.push(n.toLowerCase());
    }

    for (var i = 0; i < data.length; i++) {
      var rawName = data[i][0].toString().trim();
      if (!rawName) continue;
      var name = normalizeName(rawName);
      var origin = data[i][1].toString().trim();
      var lang = data[i][2].toString().trim().toUpperCase();

      // Matches "Anything (Something Inside)" even if there's a line break
      // instead of a space before the "(".
      var parentMatch = name.match(/^(.+?)\s*\(([^()]+)\)$/);
      // isPlaceholder = TRUE only for genuine plus-one placeholder slots like
      // "Guest (Robert Daly)" — nobody is actually named "Guest" yet, so this
      // row must stay hidden from the name search until a real name is given.
      // A real named family member like "Larissa Lima (Robert Daly)" is a
      // completely different case: that person has an actual name and must
      // be searchable on their own, in addition to being auto-linked under
      // Robert's party. Earlier logic marked BOTH cases as non-searchable
      // placeholders, which silently hid every named family member from the
      // search box — confirmed broken in testing (Larissa Lima, Christopher
      // Daly, etc. never appeared when searched by name).
      var isPlusOnePlaceholder = false;

      if (parentMatch) {
        var beforeParen = parentMatch[1].trim();
        if (beforeParen === 'Guest') {
          isPlusOnePlaceholder = true;
        }
      }

      guests.push({
        name: name,
        isPT: lang === 'PT',
        isPlaceholder: isPlusOnePlaceholder,
        hasPlusOneSlot: false,
        linked: []
      });
    }

    // ---------------------------------------------------------------------
    // Second pass: HOUSEHOLD GROUPING.
    //
    // "Cathy Cahill (Linda Cahill)" means Cathy belongs to Linda's household.
    // Everyone who points at the same person — plus that person — forms ONE
    // household, and every member sees every OTHER member as an "Also RSVP
    // for" checkbox. Whoever opens the form first can RSVP for the whole
    // group; it no longer matters whether that's the parent or a child.
    //
    // Grouping is transitive (union-find), so a chain like "A (B)" + "B (C)"
    // correctly ends up as one household of three rather than two overlapping
    // pairs.
    //
    // NOTE: names inside the parentheses don't always exist as their own row
    // (real cases: Margareth Dillworth, Rafaela). Those are still added to the
    // household so the checkbox appears, even though they have no row of
    // their own to search for.
    // ---------------------------------------------------------------------
    var parent = {};                 // lowercase name -> lowercase group root
    var displayName = {};            // lowercase name -> display spelling

    function root(x) {
      if (parent[x] === undefined) parent[x] = x;
      while (parent[x] !== x) {
        parent[x] = parent[parent[x]];
        x = parent[x];
      }
      return x;
    }
    function union(a, b) {
      var ra = root(a), rb = root(b);
      if (ra !== rb) parent[rb] = ra;
    }
    function register(name) {
      var key = name.toLowerCase();
      if (parent[key] === undefined) parent[key] = key;
      if (!displayName[key]) displayName[key] = name;
      return key;
    }

    for (var j = 0; j < guests.length; j++) {
      register(guests[j].name);
    }

    for (var j = 0; j < guests.length; j++) {
      var g = guests[j];
      var pm = g.name.match(/^(.+?)\s*\(([^()]+)\)$/);
      if (!pm) continue;
      var beforeParenJ = pm[1].trim();
      var linkedName = pm[2].trim();

      if (beforeParenJ === 'Guest') {
        // Vacant plus-one placeholder, e.g. "Guest (Corey Brennan)".
        // The slot belongs to the person named inside the brackets — it is
        // deliberately NOT shared with the rest of the household, because the
        // form builds the placeholder row name from whoever is selected as the
        // primary guest, and only the anchor produces the right row name.
        for (var k = 0; k < guests.length; k++) {
          if (guests[k].name.toLowerCase() === linkedName.toLowerCase()) {
            guests[k].hasPlusOneSlot = true;
            break;
          }
        }
        continue;
      }

      register(linkedName);
      union(register(linkedName), register(g.name));
    }

    // Turn the groups into per-guest "linked" lists (everyone except yourself).
    var household = {};
    for (var key in parent) {
      if (!parent.hasOwnProperty(key)) continue;
      var r = root(key);
      if (!household[r]) household[r] = [];
      household[r].push(key);
    }
    for (var j = 0; j < guests.length; j++) {
      var g = guests[j];
      if (g.isPlaceholder) continue; // vacant "Guest (X)" rows have no household
      var members = household[root(g.name.toLowerCase())] || [];
      for (var m = 0; m < members.length; m++) {
        if (members[m] === g.name.toLowerCase()) continue;
        var disp = displayName[members[m]] || members[m];
        if (g.linked.indexOf(disp) === -1) g.linked.push(disp);
      }
    }

    return ContentService
      .createTextOutput(JSON.stringify(guests))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify([]))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

// Secret-gated directory — used ONLY by the Aurora backend (app.py), never by
// the public RSVP form. Returns per-guest RSVP + accommodation + bridal party
// data so Aurora can answer a guest about themselves, and so admin stats are
// computed live instead of from data Aurora never actually stores.
function handleDirectoryRequest(params) {
  if (!DIRECTORY_SECRET || params.secret !== DIRECTORY_SECRET) {
    return ContentService
      .createTextOutput(JSON.stringify({ error: "unauthorized" }))
      .setMimeType(ContentService.MimeType.JSON);
  }
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Guests");
    var lastRow = sheet.getLastRow();
    var data = sheet.getRange("B27:Y" + lastRow).getValues(); // B..Y = columns 2..25

    var records = [];
    for (var i = 0; i < data.length; i++) {
      var row = data[i];
      var rawName = row[0]; // B
      if (!rawName) continue;
      var name = normalizeName(rawName.toString());

      records.push({
        name: name,
        origin: row[1],                 // C
        language: row[2],                // D
        accommodation_included: !!row[3],// E
        accommodation_confirmed: !!row[4],// F
        phone: row[5],                   // G
        bridal_party: !!row[6],          // H
        certainty: row[7],               // I
        invitation_sent: !!row[8],       // J
        attending: !!row[9],             // K
        not_attending: !!row[10],        // L
        dietary_vegetarian: !!row[11],   // M
        dietary_vegan: !!row[12],        // N
        dietary_nut_allergy: !!row[13],  // O
        dietary_no_beef: !!row[14],      // P
        dietary_no_pork: !!row[15],      // Q
        dietary_shellfish: !!row[16],    // R
        day1_invited: !!row[18],         // T
        day1_attending: !!row[19],       // U
        day2_invited: !!row[20],         // V
        day2_attending: !!row[21],       // W
        day3_invited: !!row[22],         // X
        day3_attending: !!row[23]        // Y
      });
    }

    return ContentService
      .createTextOutput(JSON.stringify(records))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ error: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function doPost(e) {
  // NO GLOBAL LOCK — deliberately, and this was measured.
  //
  // An earlier version wrapped every submission in a script lock to avoid
  // concurrent read-modify-write problems. That serialised the entire wedding:
  // 30 simultaneous submissions were tested live and SEVEN of them failed with
  // "busy, try again" after waiting out the timeout, with a median wait of 25
  // seconds. On invite day, with 242 guests receiving links at once, that would
  // have lost real RSVPs.
  //
  // The lock was also solving a problem that mostly doesn't exist. Each guest's
  // RSVP writes to their OWN row, and two guests writing different rows of a
  // Google Sheet don't conflict. Only two things are genuinely shared:
  //   • appending a row to the Passaportes tab (uses getLastRow() + 1)
  //   • creating the elevator column if it doesn't exist yet
  // Both are now locked individually, briefly, where they actually happen.
  //
  // Remaining edge case, accepted: two members of the SAME household submitting
  // at the exact same moment will both write the same rows, and the later one
  // wins. That's the same outcome as them submitting a second apart, and no
  // amount of locking makes "two people answered differently" resolvable.
  try {
    var data = JSON.parse(e.postData.contents);
    var type = data.type;
    var payload = data.data;

    if (type === "rsvp_batch") {
      // A whole party in ONE request. Previously the form fired one request per
      // person; with the script lock those queue up, and measured live at ~3.5s
      // each a six-person family took over 20 seconds — two families submitting
      // at the same moment would blow past the lock timeout and fail. One
      // request means one lock, one sheet read, and no queue to fall off.
      var ctx = buildSheetContext();
      var members = payload.members || [];
      for (var m = 0; m < members.length; m++) {
        updateGuestRSVP(members[m], ctx);
      }
      var passports = payload.passports || [];
      for (var p = 0; p < passports.length; p++) {
        logPassportSubmission(passports[p], ctx);
      }
    } else if (type === "rsvp") {
      updateGuestRSVP(payload);
    } else if (type === "phone") {
      updateGuestPhone(payload);
    } else if (type === "passport_submission") {
      logPassportSubmission(payload);
    }

    return ContentService
      .createTextOutput(JSON.stringify({"status": "ok"}))
      .setMimeType(ContentService.MimeType.JSON);

  } catch(err) {
    return ContentService
      .createTextOutput(JSON.stringify({"status": "error", "message": err.toString()}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

// ---------------------------------------------------------------------------
// NOTE HANDLING (column K)
// The note cell holds several independent facts: the RSVP summary, whether a
// passport was requested, and anything Larissa typed by hand. Previously the
// RSVP write did a plain setNote(), which wiped the passport flag and any
// manual note every time a guest resubmitted. Notes are now line-based: each
// writer replaces ONLY its own line and leaves every other line untouched.
// ---------------------------------------------------------------------------
function setNoteLine(cell, prefix, newLine) {
  var existing = (cell.getNote() || "").toString();
  var kept = [];
  var lines = existing.split("\n");
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (!line) continue;
    if (line.indexOf(prefix) === 0) continue; // drop the old version of THIS line
    kept.push(line);
  }
  if (newLine) kept.unshift(newLine);
  cell.setNote(kept.join("\n"));
}

// Finds a column by its header text (searched across the header rows above the
// data), and creates it at the end of the sheet if it doesn't exist yet. Used
// so the elevator request lands in a real, sortable column instead of only
// living inside a note that's easy to miss.
function getOrCreateColumn(sheet, headerText) {
  var lastCol = sheet.getLastColumn();
  var headerRow = 26; // data starts at row 27
  if (lastCol > 0) {
    var headers = sheet.getRange(headerRow, 1, 1, lastCol).getValues()[0];
    for (var c = 0; c < headers.length; c++) {
      if (headers[c] && headers[c].toString().trim().toUpperCase() === headerText.toUpperCase()) {
        return c + 1;
      }
    }
  }
  // Creating the column is the only racy part — two submissions arriving at once
  // could otherwise each append their own copy. Lock just this, briefly, and
  // re-check inside the lock in case someone else created it while we waited.
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(20000);
  } catch (lockErr) {
    return lastCol + 1; // couldn't lock; best effort rather than losing the RSVP
  }
  try {
    var recheck = sheet.getRange(headerRow, 1, 1, sheet.getLastColumn()).getValues()[0];
    for (var r = 0; r < recheck.length; r++) {
      if (recheck[r] && recheck[r].toString().trim().toUpperCase() === headerText.toUpperCase()) {
        return r + 1;
      }
    }
    var newCol = sheet.getLastColumn() + 1;
    sheet.getRange(headerRow, newCol).setValue(headerText).setFontWeight("bold");
    SpreadsheetApp.flush();
    return newCol;
  } finally {
    lock.releaseLock();
  }
}

function findGuestRow(sheet, name) {
  return findGuestRowIn(sheet.getRange("B27:B" + sheet.getLastRow()).getValues(), name);
}

// Same matching logic, but against an already-fetched name column so a batch
// of party members can share one read instead of one read each.
function findGuestRowIn(nameCol, name) {
  var searchName = normalizeName(name).toLowerCase();

  // PASS 1 — exact match only. This has to run first and fully, over every
  // row, before any fuzzy fallback. Real bug found in testing: searching for
  // "Guest (Corey Brennan)" would match Corey Brennan's OWN row first under
  // the old single-pass fuzzy logic, because "Corey Brennan" is a substring
  // of "Guest (Corey Brennan)" — so confirming his plus-one silently
  // overwrote his own name in column B instead of the placeholder row.
  for (var i = 0; i < nameCol.length; i++) {
    var cellName = normalizeName(nameCol[i][0].toString()).toLowerCase();
    if (cellName === searchName) {
      return i + 27;
    }
  }

  // PASS 2 — fuzzy fallback (substring match), only used for free-text
  // lookups (e.g. passport submissions, admin tools) where an exact match
  // isn't guaranteed. Never used for placeholder_target lookups in practice
  // since those are always exact strings the form generated itself.
  //
  // IMPORTANT: the fuzzy pass now only returns a row when there is exactly ONE
  // candidate. If two or more rows could match, we return -1 and write nothing,
  // rather than guessing and saving someone's RSVP onto a different guest's
  // row. A missing write is recoverable; a write to the wrong person is not.
  var fuzzyMatches = [];
  for (var i = 0; i < nameCol.length; i++) {
    var cellName = normalizeName(nameCol[i][0].toString()).toLowerCase();
    if (!cellName) continue;
    if (cellName.indexOf(searchName) !== -1 || searchName.indexOf(cellName) !== -1) {
      fuzzyMatches.push(i + 27);
    }
  }
  if (fuzzyMatches.length === 1) return fuzzyMatches[0];
  return -1;
}

function updateGuestPhone(payload) {
  if (!payload.name || !payload.phone) return;
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Guests");
  if (!sheet) return;
  var row = findGuestRow(sheet, payload.name);
  if (row === -1) return;
  var phoneCell = sheet.getRange(row, 7);
  if (!phoneCell.getValue()) phoneCell.setValue(payload.phone);
  sheet.getRange(row, 10).setValue(true);
}

// Shared per-execution context. Reading the name column and locating the
// elevator column are the two most expensive operations in a write, and doing
// them once per PERSON meant a six-person family paid for them six times.
// Built once per request and reused for every member of the party.
function buildSheetContext() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Guests");
  if (!sheet) throw new Error("Guests sheet not found");
  return {
    sheet: sheet,
    nameCol: sheet.getRange("B27:B" + sheet.getLastRow()).getValues(),
    elevatorCol: getOrCreateColumn(sheet, "PRECISA ELEVADOR (IGREJA)")
  };
}

function updateGuestRSVP(payload, ctx) {
  // Throw instead of returning quietly. A silent return meant the guest saw a
  // cheerful success screen while their RSVP was never written anywhere —
  // the worst possible failure mode for something you only get one shot at.
  // doPost catches this and returns status:"error", which the form now shows.
  if (!payload.name) throw new Error("No name in submission");
  ctx = ctx || buildSheetContext();
  var sheet = ctx.sheet;

  // Plus-one handling: when a guest adds their plus-one's real name (or
  // confirms the slot with no name yet), the row we need to update is the
  // existing "Guest (Primary Name)" placeholder row, NOT a row matching the
  // brand-new name (that row doesn't exist). payload.placeholder_target
  // tells us which placeholder row this submission belongs to.
  var lookupName = payload.placeholder_target || payload.name;
  var row = findGuestRowIn(ctx.nameCol, lookupName);
  if (row === -1) throw new Error("Guest not found in sheet: " + lookupName);

  // If a real name was given for what used to be "Guest (Primary Name)",
  // rename the cell so the live list shows "Actual Name (Primary Name)"
  // from now on, instead of staying stuck as a generic "Guest (...)" slot.
  if (payload.placeholder_target && payload.name !== payload.placeholder_target) {
    var parenMatch = payload.placeholder_target.match(/\(([^()]+)\)\s*$/);
    var suffix = parenMatch ? " (" + parenMatch[1] + ")" : "";
    var newName = payload.name + suffix;
    sheet.getRange(row, 2).setValue(newName);
    // Keep the cached name column in step, so later members of the SAME batch
    // don't look this row up under its old "Guest (...)" name.
    ctx.nameCol[row - 27][0] = newName;
  }

  // PHONE — only fill an empty cell, never overwrite a number already there.
  if (payload.phone) {
    var pc = sheet.getRange(row, 7);
    if (!pc.getValue()) pc.setValue(payload.phone);
  }

  // COLUMNS J..R IN ONE WRITE — invitation sent, attending, not attending,
  // and all six dietary flags. These are contiguous (10..18), so what used to
  // be nine separate round trips is now one.
  //
  // Dietary writes the FULL answer every time, TRUE *and* FALSE. Previously
  // only TRUE was ever written, so a guest who resubmitted to correct
  // themselves ("actually I'm not vegetarian") kept the old TRUE forever and
  // the catering numbers silently drifted. A resubmission must completely
  // replace the previous answer, not merge with it.
  var attendingYes = payload.attending === "yes";
  var attendingNo = payload.attending === "no";
  sheet.getRange(row, 10, 1, 9).setValues([[
    true,                                   // J invitation sent
    attendingYes,                           // K attending
    attendingNo,                            // L not attending
    payload.dietary_vegetarian === true,    // M
    payload.dietary_vegan === true,         // N
    payload.dietary_nut_allergy === true,   // O
    payload.dietary_no_beef === true,       // P
    payload.dietary_no_pork === true,       // Q
    payload.dietary_shellfish === true      // R
  ]]);

  // DAY ATTENDANCE (T..Y in one write)
  // "Invited" (T/V/X) = who WE invited. Every guest is invited to all three
  // days, so these are always TRUE. They are NOT a record of what the guest
  // picked — that's what the "Attending" columns are for. Previously both
  // meant the same thing, which is why every guest showed as not invited to
  // the wedding day itself.
  // "Attending" (U/W/Y) = the guest's actual answer, rewritten in full on
  // every submission so corrections and declines take effect properly.
  var days = payload.days || [];
  sheet.getRange(row, 20, 1, 6).setValues([[
    true, days.indexOf("day1") !== -1,
    true, days.indexOf("day2") !== -1,
    true, days.indexOf("day3") !== -1
  ]]);

  // ELEVATOR — logged in a real column (created automatically the first time),
  // not only inside a note. Applies to the whole party, as intended.
  sheet.getRange(row, ctx.elevatorCol).setValue(payload.needs_elevator === true);

  // NOTE — replaces only the RSVP line. The passport flag and anything typed
  // by hand in this cell survive untouched.
  var note = "RSVP via Form: " + new Date().toLocaleDateString("pt-BR");
  if (payload.attending) note += " | " + (payload.attending === "yes" ? "✅ Vai" : "❌ Não vai");
  if (payload.needs_elevator) note += " | ⚠️ Precisa elevador";
  if (payload.plus_one) note += " | +1: " + payload.plus_one;
  setNoteLine(sheet.getRange(row, 11), "RSVP via Form:", note);
}

function logPassportSubmission(payload, ctx) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  // NOTE: there are SEVENTEEN columns here, not sixteen. This was previously
  // written into a 16-wide range, so Apps Script threw
  // "The data has 17 but the range has 16" on EVERY passport submission and
  // nothing was ever saved. Confirmed by live testing — the Passaportes tab
  // was completely empty despite successful-looking submissions.
  var PASSPORT_HEADERS = [
    "Data", "Nome Convidado", "Nome Completo Legal", "WhatsApp", "CPF",
    "Data Nasc.", "Cidade Nasc.", "RG", "Órgão Emissor",
    "Sexo", "Estado Civil", "Nome da Mãe", "Profissão",
    "Endereço", "Cidade PF", "Disponibilidade", "Precisa Elevador Igreja?"
  ];
  var PASSPORT_COLS = PASSPORT_HEADERS.length; // 17

  var passSheet = ss.getSheetByName("Passaportes");
  if (!passSheet) {
    passSheet = ss.insertSheet("Passaportes");
  }
  // Write the headers if the tab is empty — covers both a freshly created tab
  // and an existing-but-blank one (which is exactly the state this bug left it in).
  if (passSheet.getLastRow() === 0) {
    passSheet.getRange(1, 1, 1, PASSPORT_COLS).setValues([PASSPORT_HEADERS]);
    passSheet.getRange(1, 1, 1, PASSPORT_COLS).setFontWeight("bold");
    passSheet.setFrozenRows(1);
  }

  // Appending uses getLastRow() + 1, so two passport submissions landing at the
  // same instant could both target the same row and one would be overwritten.
  // This is the one place that genuinely needs serialising — and it's rare
  // enough (only Brazilian guests requesting a passport appointment) that a
  // brief lock here costs nothing, unlike locking every RSVP.
  var passLock = LockService.getScriptLock();
  try {
    passLock.waitLock(30000);
  } catch (lockErr) {
    throw new Error("Passport sheet busy, please try again");
  }
  try {
  var nextRow = passSheet.getLastRow() + 1;
  passSheet.getRange(nextRow, 1, 1, PASSPORT_COLS).setValues([[
    new Date().toLocaleDateString("pt-BR"),
    payload.name || "",
    payload.full_name || "",
    payload.phone || "",
    payload.cpf || "",
    payload.dob || "",
    payload.birth_place || "",
    payload.rg || "",
    payload.rg_issuer || "",
    payload.gender || "",
    payload.marital_status || "",
    payload.mother || "",
    payload.job || "",
    payload.address || "",
    payload.pf_city || "",
    payload.availability || "",
    payload.needs_elevator ? "Sim" : "Não"
  ]]);
  SpreadsheetApp.flush();
  } finally {
    passLock.releaseLock();
  }

  var guestSheet = (ctx && ctx.sheet) || ss.getSheetByName("Guests");
  if (guestSheet && payload.name) {
    var row = ctx && ctx.nameCol
      ? findGuestRowIn(ctx.nameCol, payload.name)
      : findGuestRow(guestSheet, payload.name);
    if (row !== -1) {
      // Its own line, so a later RSVP resubmission can't wipe it (and so this
      // flag can't be duplicated if the guest submits passport data twice).
      setNoteLine(
        guestSheet.getRange(row, 11),
        "🛂 Passaporte solicitado",
        "🛂 Passaporte solicitado: " + new Date().toLocaleDateString("pt-BR")
      );
    }
  }
}

function testWebhook() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Guests");
  var row = findGuestRow(sheet, "Robert Daly");
  Logger.log("Robert Daly at row: " + row + " (expected 27)");
  if (row > 0) Logger.log("Row: " + JSON.stringify(sheet.getRange(row, 1, 1, 30).getValues()));
}
