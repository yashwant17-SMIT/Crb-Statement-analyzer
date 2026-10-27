/**
 * Ledger intake: assess the applications queued in this sheet.
 *
 * Bound to the intake sheet (Extensions → Apps Script). Runs as the person
 * who authorised it, so it can open exactly the Drive files they can. For
 * each row it downloads the bureau report and statements from Drive and
 * posts them to Ledger's /api/v1/intake/assess. Ledger answers with the
 * write that records the assessment in the results workbook, which this
 * script applies:
 *
 *   results workbook   Register, Monthly revenue, Bureau accounts, Findings,
 *                      and the customer's formatted report tab, exactly as
 *                      Ledger's own Sheets export lays them out
 *   this row           the outcome in the sheet's own columns when it has
 *                      them (CRB band, decision status, sanctioned amount,
 *                      reason), plus Ledger assessment (what happened, when)
 *                      and Ledger report (a link to the customer's tab)
 *
 * Columns are found by header, in any order (see FIELDS and LOOSE). A row is
 * pending while both its Ledger assessment and its decision status are empty;
 * clear them to run the row again, or select it and use Assess selected rows.
 *
 * Script properties (Project Settings → Script properties):
 *   LEDGER_URL         https://ledger.example.com   (required)
 *   LEDGER_TOKEN       the server's INTAKE_TOKEN    (required)
 *   RESULTS_SHEET_ID   the results workbook (default: this spreadsheet)
 *   INTAKE_TAB         tab holding the applications (default: the first tab)
 *
 * Needs the Google Sheets advanced service (appsscript.json enables it).
 */

var ASSESSMENT = 'Ledger assessment', REPORT = 'Ledger report';
var LEDGER_COLS = [ASSESSMENT, REPORT];
// Exact header names, compared without case, spaces or punctuation.
var FIELDS = {
  customer: ['customer', 'customername', 'name', 'applicant', 'applicantname'],
  crb: ['crb', 'crblink', 'crbreport', 'crbreportlink', 'crburl', 'crbpdf',
        'bureaureport', 'creditreport', 'creditbureaureport'],
  customer_type: ['customertype', 'type'],
  requested_amount: ['requestedamount', 'requestedamountrwf', 'amount', 'amountrwf',
                     'loanamount', 'loanamountrwf', 'amountrequested'],
  provider: ['provider', 'statementprovider'],
  // Where the outcome goes, when the sheet has these columns.
  band: ['band', 'crbband'],
  decision: ['decision', 'status', 'creditdecision'],
  sanctioned: ['sanctionedamount', 'approvedamount'],
  reason: ['reason', 'rejectionreason', 'basisofrejection'],
};
// Looser matches for a field no header names exactly, so "TYPE of customer"
// or "Status Signed off by Credit risk team" work without renaming. "PA Open
// Total Value (from CRB report)" is not the CRB link.
var LOOSE = {
  crb: function (k) { return /crb|bureau|credit/.test(k) && /link|report|url|pdf|file/.test(k) &&
                             !/from|band|value|total|number|score/.test(k); },
  customer_type: function (k) { return /type/.test(k) && /customer|client|borrower|applicant/.test(k); },
  requested_amount: function (k) { return /amount/.test(k) && /request|loan|applied/.test(k) &&
                                          !/approv|sanction|disburs|current/.test(k); },
  band: function (k) { return /band/.test(k); },
  decision: function (k) { return /status|decision/.test(k) && /sign|credit|risk|underwrit/.test(k); },
  sanctioned: function (k) { return /sanction/.test(k); },
  reason: function (k) { return /reason|basis|reject/.test(k); },
};
// The decision as written when the status column has no dropdown, and how to
// find the matching option when it does.
var DECISIONS = {
  approved: { label: 'Approved', match: /approv|accept|pass/i },
  rejected: { label: 'Rejected', match: /reject|declin|fail/i },
  on_hold: { label: 'On hold', match: /hold|pend|refer|review/i },
};
var HEADER_SCAN_ROWS = 15;               // the header row may sit under a title
var PROCESSING = 'Processing…';
var MAX_BYTES = 25 * 1024 * 1024;        // the server's MAX_UPLOAD_MB
var TIME_BUDGET_MS = 4.5 * 60 * 1000;    // Apps Script stops a run at 6 minutes
var STALE_MS = 15 * 60 * 1000;           // a "Processing…" row this old was interrupted

/* ------------------------------------------------------------------ menu */
function onOpen() {
  SpreadsheetApp.getUi().createMenu('Ledger')
    .addItem('Assess selected rows', 'assessSelected')
    .addItem('Assess all pending rows', 'assessPending')
    .addSeparator()
    .addItem('Check connection', 'checkConnection')
    .addItem('Turn on automatic runs (every 10 minutes)', 'enableAutomaticRuns')
    .addItem('Turn off automatic runs', 'disableAutomaticRuns')
    .addToUi();
}

function assessSelected() {
  var ctx = context_();
  if (SpreadsheetApp.getActiveSheet().getSheetId() !== ctx.sheet.getSheetId()) {
    throw new Error('Select rows on the "' + ctx.sheet.getName() + '" tab first.');
  }
  var rows = [];
  SpreadsheetApp.getActiveRangeList().getRanges().forEach(function (range) {
    for (var n = range.getRow(); n < range.getRow() + range.getNumRows(); n++) {
      if (n > ctx.headerRow + 1 && n <= ctx.values.length && rows.indexOf(n) < 0) rows.push(n);
    }
  });
  if (!rows.length) throw new Error('Select one or more application rows (not the header).');
  run_(ctx, rows, true);
}

function assessPending() {
  var ctx = context_();
  run_(ctx, pendingRows_(ctx), true);
}

/** The timer entry point: no dialogs, nothing to click. */
function runPendingOnTimer() {
  var ctx = context_();
  run_(ctx, pendingRows_(ctx), false);
}

function enableAutomaticRuns() {
  disableAutomaticRuns(true);
  ScriptApp.newTrigger('runPendingOnTimer').timeBased().everyMinutes(10).create();
  SpreadsheetApp.getActive().toast('Pending rows will be assessed every 10 minutes.', 'Ledger');
}

function disableAutomaticRuns(quiet) {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'runPendingOnTimer') ScriptApp.deleteTrigger(t);
  });
  if (quiet !== true) SpreadsheetApp.getActive().toast('Automatic runs are off.', 'Ledger');
}

// ngrok's free plan shows a warning page to browser-like callers, which
// includes Apps Script; this header skips it. Other hosts ignore it.
function headers_(cfg) {
  return { 'X-Intake-Token': cfg.token, 'ngrok-skip-browser-warning': '1' };
}

function checkConnection() {
  var cfg = config_();
  var ui = SpreadsheetApp.getUi();
  var res;
  try {
    // No files: a correct token gets "no bureau report", a wrong one 401.
    res = UrlFetchApp.fetch(cfg.url + '/api/v1/intake/assess', {
      method: 'post', payload: { probe: '1' }, headers: headers_(cfg),
      muteHttpExceptions: true,
    });
  } catch (e) {
    ui.alert('Ledger', 'Could not reach ' + cfg.url + ': ' + e.message, ui.ButtonSet.OK);
    return;
  }
  var code = res.getResponseCode();
  var msg = code === 422 ? 'Connected. The token is accepted.'
    : code === 401 ? 'Ledger answered, but refused the token. Check LEDGER_TOKEN.'
    : code === 503 ? 'Ledger answered, but its intake route is off. Set INTAKE_TOKEN on the server.'
    : 'Unexpected answer (' + code + '): ' + errorMessage_(code, res);
  ui.alert('Ledger', msg, ui.ButtonSet.OK);
}

/* ------------------------------------------------------------- the run */
function run_(ctx, rows, interactive) {
  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(5000)) {
    if (interactive) SpreadsheetApp.getActive().toast('Another run is in progress. Try again shortly.', 'Ledger');
    return;
  }
  try {
    var started = Date.now();
    var done = 0, failed = 0;
    for (var i = 0; i < rows.length; i++) {
      if (Date.now() - started > TIME_BUDGET_MS) break;
      if (assessRow_(ctx, rows[i])) done++; else failed++;
    }
    var left = rows.length - done - failed;
    var summary = done + ' assessed' + (failed ? ', ' + failed + ' failed' : '') +
      (left ? '. Stopped for time with ' + left + ' left; run again to continue.' : '.');
    if (interactive) SpreadsheetApp.getActive().toast(rows.length ? summary : 'Nothing pending.', 'Ledger', 8);
    return { done: done, failed: failed, left: left };
  } finally {
    lock.releaseLock();
  }
}

/** Assess sheet row n (1-based). True when Ledger returned an assessment. */
function assessRow_(ctx, n) {
  var row = readRow_(ctx, n);
  setLedger_(ctx, n, PROCESSING + ' ' + new Date().toISOString(), '');
  SpreadsheetApp.flush();
  try {
    if (row.problems.length) throw new Error(row.problems.join(' '));
    var payload = {
      customer_type: row.customerType,
      requested_amount: row.amount,
      provider: row.provider,
      crb_file: pdf_(row.crb),
    };
    row.statements.forEach(function (link, i) { payload['statement_file_' + (i + 1)] = pdf_(link); });
    payload.results_state = JSON.stringify(resultsState_(ctx.cfg));

    var res;
    try {
      res = UrlFetchApp.fetch(ctx.cfg.url + '/api/v1/intake/assess', {
        method: 'post', payload: payload, headers: headers_(ctx.cfg),
        muteHttpExceptions: true,
      });
    } catch (e) {
      throw new Error('Could not reach Ledger at ' + ctx.cfg.url + ': ' + e.message);
    }
    var code = res.getResponseCode();
    if (code !== 200) throw new Error(errorMessage_(code, res));
    var body = JSON.parse(res.getContentText());

    try {
      Sheets.Spreadsheets.batchUpdate({ requests: body.results.ops }, ctx.cfg.resultsId);
    } catch (e) {
      throw new Error('Assessed, but could not write to the results workbook: ' + e.message);
    }
    var link = '=HYPERLINK("https://docs.google.com/spreadsheets/d/' + ctx.cfg.resultsId +
      '/edit#gid=' + body.results.gid + '","Open report")';
    var note = setOutcome_(ctx, n, body.row || {});
    setLedger_(ctx, n, body.status + ' ' + stamp_() + ' · ' + body.result + note, link);
    return true;
  } catch (e) {
    setLedger_(ctx, n, 'Error: ' + e.message, '');
    return false;
  }
}

/* ------------------------------------------------------------- reading */
function context_() {
  var cfg = config_();
  var ss = SpreadsheetApp.getActive();
  var sheet = cfg.tab ? ss.getSheetByName(cfg.tab) : ss.getSheets()[0];
  if (!sheet) throw new Error('There is no tab named "' + cfg.tab + '". Check INTAKE_TAB.');
  var range = sheet.getDataRange();
  var ctx = {
    cfg: cfg, sheet: sheet,
    values: range.getValues(),
    display: range.getDisplayValues(),
    rich: range.getRichTextValues(),
    formulas: range.getFormulas(),
  };
  ctx.cols = columns_(ctx);
  return ctx;
}

function config_() {
  var p = PropertiesService.getScriptProperties();
  var url = String(p.getProperty('LEDGER_URL') || '').trim().replace(/\/+$/, '');
  var token = String(p.getProperty('LEDGER_TOKEN') || '').trim();
  if (!url || !token) {
    throw new Error('Set LEDGER_URL and LEDGER_TOKEN under Project Settings → Script properties.');
  }
  if (!/^https:\/\//.test(url)) throw new Error('LEDGER_URL must start with https://.');
  return {
    url: url, token: token,
    tab: String(p.getProperty('INTAKE_TAB') || '').trim(),
    resultsId: String(p.getProperty('RESULTS_SHEET_ID') || '').trim() || SpreadsheetApp.getActive().getId(),
  };
}

function key_(s) { return String(s == null ? '' : s).toLowerCase().replace(/[^a-z0-9]/g, ''); }

// Exact names win over loose ones, and each column is used once: the first
// header that names a field, left to right. Other Ledger columns, such as
// those an older version of this script added, are left alone.
function headerColumns_(header) {
  var cols = { statement: [], out: {} }, used = {};
  var keys = header.map(function (h, i) {
    var text = String(h).trim();
    if (LEDGER_COLS.indexOf(text) >= 0) { cols.out[text] = i; used[i] = true; }
    else if (/^ledger/i.test(text)) used[i] = true;
    return key_(text);
  });
  function claim(f, test) {
    for (var i = 0; i < keys.length && !(f in cols); i++) {
      if (keys[i] && !used[i] && test(keys[i])) { cols[f] = i; used[i] = true; }
    }
  }
  Object.keys(FIELDS).forEach(function (f) {
    claim(f, function (k) { return FIELDS[f].indexOf(k) >= 0; });
  });
  Object.keys(LOOSE).forEach(function (f) { claim(f, LOOSE[f]); });
  keys.forEach(function (k, i) {
    if (!used[i] && k.indexOf('statement') >= 0 && !/type|provider/.test(k)) cols.statement.push(i);
  });
  cols.missing = [];
  if (!('crb' in cols)) cols.missing.push('CRB link');
  if (!cols.statement.length) cols.missing.push('Statement link');
  if (!('customer_type' in cols)) cols.missing.push('Customer type');
  if (!('requested_amount' in cols)) cols.missing.push('Requested amount');
  return cols;
}

// The header is the first of the top rows that names the most fields.
function columns_(ctx) {
  var best = null;
  for (var r = 0; r < Math.min(HEADER_SCAN_ROWS, ctx.display.length); r++) {
    var cols = headerColumns_(ctx.display[r]);
    if (!best || cols.missing.length < best.cols.missing.length) best = { row: r, cols: cols };
    if (!cols.missing.length) break;
  }
  if (!best || best.cols.missing.length === 4) {
    var tabs = SpreadsheetApp.getActive().getSheets().map(function (s) { return s.getName(); });
    throw new Error('No header row in the top ' + HEADER_SCAN_ROWS + ' rows of the "' +
      ctx.sheet.getName() + '" tab. If the applications are on another tab, set INTAKE_TAB ' +
      'to its name. Tabs: ' + tabs.join(', ') + '.');
  }
  var header = ctx.display[best.row];
  cols = best.cols;
  if (cols.missing.length) {
    throw new Error('The "' + ctx.sheet.getName() + '" tab has no ' + cols.missing.join(', ') +
      ' column (header row ' + (best.row + 1) + '). Headers found: ' +
      header.filter(String).join(', ') + '.');
  }
  ctx.headerRow = best.row;
  // The Ledger columns go after the last header, once.
  var next = header.length;
  LEDGER_COLS.forEach(function (name) {
    if (!(name in cols.out)) {
      ctx.sheet.getRange(best.row + 1, next + 1).setValue(name).setFontWeight('bold');
      cols.out[name] = next++;
    }
  });
  return cols;
}

// Pending: not yet assessed by Ledger, not already decided by a person, and
// not blank. A run interrupted mid-row is retried once it is STALE_MS old.
function pendingRows_(ctx) {
  var rows = [], c = ctx.cols;
  for (var i = ctx.headerRow + 1; i < ctx.values.length; i++) {
    var s = text_(ctx.values[i][c.out[ASSESSMENT]]);
    var stale = s.indexOf(PROCESSING) === 0 &&
      !(Date.now() - Date.parse(s.slice(PROCESSING.length).trim()) <= STALE_MS);
    var decided = c.decision != null && text_(ctx.values[i][c.decision]) !== '';
    if ((s === '' || stale) && !decided && !isBlank_(ctx, i)) rows.push(i + 1);
  }
  return rows;
}

function text_(v) { return String(v == null ? '' : v).trim(); }

function isBlank_(ctx, i) {
  var c = ctx.cols;
  return [c.crb, c.customer_type, c.requested_amount].concat(c.statement).every(function (j) {
    return j == null || String(ctx.display[i][j]).trim() === '';
  });
}

function readRow_(ctx, n) {
  var i = n - 1, c = ctx.cols, problems = [];
  var crb = linksIn_(ctx, i, c.crb);
  var statements = [];
  c.statement.forEach(function (j) { statements = statements.concat(linksIn_(ctx, i, j)); });
  if (!crb.length) {
    var shown = String(ctx.display[i][c.crb]).trim();
    problems.push('No CRB link in ' + ctx.sheet.getRange(n, c.crb + 1).getA1Notation() +
      (shown ? ' (it shows "' + shown + '"; paste the Drive link itself).' : ' (the cell is empty).'));
  }
  else if (crb.length > 1) problems.push('The CRB cell has more than one link.');
  crb.slice(0, 1).concat(statements).forEach(function (link) {
    if (!driveId_(link)) problems.push('Not a Google Drive file link: ' + link);
  });
  var amount = ctx.values[i][c.requested_amount];
  return {
    crb: crb[0], statements: statements, problems: problems,
    // Sent as typed; the server reads them strictly and names a bad cell.
    customerType: String(ctx.display[i][c.customer_type]).trim(),
    amount: typeof amount === 'number' ? String(amount) : String(ctx.display[i][c.requested_amount]).trim(),
    provider: c.provider == null ? '' : String(ctx.display[i][c.provider]).trim(),
  };
}

/** Every URL in a cell: typed, hyperlinked, partly hyperlinked, a chip, or =HYPERLINK. */
function linksIn_(ctx, i, j) {
  if (j == null) return [];
  var found = [];
  var rich = ctx.rich[i] && ctx.rich[i][j];
  if (rich) {
    found.push(rich.getLinkUrl());
    rich.getRuns().forEach(function (run) { found.push(run.getLinkUrl()); });
  }
  var f = /HYPERLINK\(\s*"([^"]+)"/i.exec(ctx.formulas[i][j] || '');
  if (f) found.push(f[1]);
  found = found.concat(String(ctx.display[i][j]).match(/https?:\/\/[^\s,;"'<>]+/g) || []);
  found = found.filter(function (link, k) { return link && found.indexOf(link) === k; });
  // A Drive smart chip shows the file name; only the Sheets API has its link.
  if (!found.length && String(ctx.display[i][j]).trim()) found = chipLinks_(ctx, i + 1)[j] || [];
  return found;
}

/** The links in row n's smart chips and cell hyperlinks, by column; one API call per row. */
function chipLinks_(ctx, n) {
  ctx.chips = ctx.chips || {};
  if (ctx.chips[n]) return ctx.chips[n];
  var out = {};
  try {
    var range = "'" + ctx.sheet.getName().replace(/'/g, "''") + "'!" + n + ':' + n;
    var res = Sheets.Spreadsheets.get(SpreadsheetApp.getActive().getId(),
      { ranges: [range], fields: 'sheets.data.rowData.values(chipRuns,hyperlink)' });
    var data = ((res.sheets || [])[0] || {}).data || [];
    var row = ((data[0] || {}).rowData || [])[0] || {};
    (row.values || []).forEach(function (v, j) {
      var links = (v.chipRuns || []).map(function (r) {
        return r.chip && r.chip.richLinkProperties && r.chip.richLinkProperties.uri;
      }).filter(Boolean);
      if (v.hyperlink && links.indexOf(v.hyperlink) < 0) links.push(v.hyperlink);
      if (links.length) out[j] = links;
    });
  } catch (e) {
    // Leave it empty: the row's error names the cell and what it shows.
  }
  return (ctx.chips[n] = out);
}

/** The file ID of a Drive or Docs link; null for anything else, folders included. */
function driveId_(link) {
  if (!/^https:\/\/(drive|docs)\.google\.com\//.test(link) || /\/folders\//.test(link)) return null;
  var m = /(?:\/d\/|[?&]id=)([A-Za-z0-9_-]{20,})/.exec(link);
  return m ? m[1] : null;
}

function pdf_(link) {
  var file;
  try {
    file = DriveApp.getFileById(driveId_(link));
  } catch (e) {
    throw new Error('Cannot open ' + link + ' — check it exists and is shared with ' +
      Session.getEffectiveUser().getEmail() + '.');
  }
  var name = file.getName();
  if (file.getMimeType() !== 'application/pdf') {
    throw new Error(name + ' is ' + file.getMimeType() + ', not a PDF. Link the original PDF.');
  }
  if (file.getSize() > MAX_BYTES) throw new Error(name + ' is larger than 25 MB.');
  return file.getBlob().setName(name);
}

/* ------------------------------------------------------------- writing */
function setLedger_(ctx, n, assessment, link) {
  setCell_(ctx, n, ctx.cols.out[ASSESSMENT], assessment);
  setCell_(ctx, n, ctx.cols.out[REPORT], link);
}

function setCell_(ctx, n, j, value) {
  ctx.sheet.getRange(n, j + 1).setValue(value);
  ctx.values[n - 1][j] = value;
}

function stamp_() {
  return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm');
}

/**
 * The outcome into the sheet's own columns, those it has. Returns a note for
 * Ledger assessment when the status dropdown has no matching option, in which
 * case the status cell is left for a person.
 */
function setOutcome_(ctx, n, row) {
  var c = ctx.cols, note = '';
  if (c.band != null) setCell_(ctx, n, c.band, row.band || '');
  if (c.sanctioned != null) setCell_(ctx, n, c.sanctioned, row.sanctioned_amount == null ? '' : row.sanctioned_amount);
  if (c.reason != null) setCell_(ctx, n, c.reason, row.reason || '');
  var d = DECISIONS[row.decision];
  if (c.decision != null && d) {
    var label = decisionLabel_(ctx.sheet.getRange(n, c.decision + 1), d);
    if (label) setCell_(ctx, n, c.decision, label);
    else note = ' · Status not set: its dropdown has no option for "' + d.label + '".';
  }
  return note;
}

/** The status to write: the dropdown's matching option, or the plain label without one. */
function decisionLabel_(cell, d) {
  var rule = cell.getDataValidation && cell.getDataValidation();
  if (!rule) return d.label;
  var type = rule.getCriteriaType(), args = rule.getCriteriaValues();
  var options;
  if (type === SpreadsheetApp.DataValidationCriteria.VALUE_IN_LIST) options = args[0];
  else if (type === SpreadsheetApp.DataValidationCriteria.VALUE_IN_RANGE) {
    options = [].concat.apply([], args[0].getDisplayValues());
  } else return d.label;
  return options.map(String).filter(function (o) { return d.match.test(o); })[0] || null;
}

/**
 * What Ledger needs to plan the write: the results workbook's tabs, and the
 * Register's Customer and Current columns so a re-assessed customer's earlier
 * rows can be marked superseded. Read inside the run's lock, just before the
 * write, so the two cannot drift apart.
 */
function resultsState_(cfg) {
  var meta;
  try {
    meta = Sheets.Spreadsheets.get(cfg.resultsId, { fields: 'sheets.properties(sheetId,title)' });
  } catch (e) {
    throw new Error('Cannot open the results workbook (' + cfg.resultsId + '): ' + e.message +
      ' Check RESULTS_SHEET_ID and that ' + Session.getEffectiveUser().getEmail() + ' can edit it.');
  }
  var tabs = {}, register = [];
  (meta.sheets || []).forEach(function (s) { tabs[s.properties.title] = s.properties.sheetId; });
  var title = Object.keys(tabs).filter(function (t) { return t.toLowerCase() === 'register'; })[0];
  if (title) {
    var q = "'" + title.replace(/'/g, "''") + "'!";
    var header = (Sheets.Spreadsheets.Values.get(cfg.resultsId, q + '1:1').values || [[]])[0];
    var c = header.indexOf('Customer'), x = header.indexOf('Current');
    if (c >= 0 && x >= 0) {
      var got = Sheets.Spreadsheets.Values.batchGet(cfg.resultsId, {
        ranges: [q + col_(c) + '2:' + col_(c), q + col_(x) + '2:' + col_(x)],
        valueRenderOption: 'FORMATTED_VALUE',
      }).valueRanges;
      var names = got[0].values || [], current = got[1].values || [];
      for (var i = 0; i < Math.max(names.length, current.length); i++) {
        register.push([(names[i] || [])[0] || '', (current[i] || [])[0] || '']);
      }
    }
  }
  return { tabs: tabs, register: register };
}

function col_(j) {
  var col = '', k = j + 1;
  while (k) { col = String.fromCharCode(65 + (k - 1) % 26) + col; k = Math.floor((k - 1) / 26); }
  return col;
}

function errorMessage_(code, res) {
  var body = null;
  try { body = JSON.parse(res.getContentText()); } catch (e) { /* not JSON */ }
  var detail = (body && (body.detail || body)) || {};
  var msg = typeof detail === 'string' ? detail : detail.message;
  return msg || ('Ledger answered ' + code + '.');
}
