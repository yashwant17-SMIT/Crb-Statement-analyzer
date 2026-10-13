/**
 * Ledger intake: assess the applications queued in this sheet.
 *
 * Bound to the intake sheet (Extensions → Apps Script). Runs as the person
 * who authorised it, so it can open exactly the Drive files they can. For
 * each row it downloads the bureau report and statements from Drive, posts
 * them to Ledger's /api/v1/intake/assess, and writes the outcome back:
 *
 *   this row          Ledger status, Ledger result, Ledger assessed at, Ledger report
 *   Ledger register   one row per assessment, never overwritten
 *   <customer> tab    the full report for the customer's latest assessment
 *
 * Columns are found by header, in any order (see FIELDS). A row is pending
 * while its Ledger status is empty; clear it to run the row again.
 *
 * Script properties (Project Settings → Script properties):
 *   LEDGER_URL     https://ledger.example.com   (required)
 *   LEDGER_TOKEN   the server's INTAKE_TOKEN    (required)
 *   INTAKE_TAB     tab holding the applications (default: the first tab)
 *   REPORT_TABS    "no" to skip the per-customer report tabs
 */

var LEDGER_COLS = ['Ledger status', 'Ledger result', 'Ledger assessed at', 'Ledger report'];
var FIELDS = {
  customer: ['customer', 'customername', 'name', 'applicant', 'applicantname'],
  crb: ['crb', 'crblink', 'crbreport', 'crbreportlink', 'crburl', 'crbpdf',
        'bureaureport', 'creditreport', 'creditbureaureport'],
  customer_type: ['customertype', 'type'],
  requested_amount: ['requestedamount', 'requestedamountrwf', 'amount', 'amountrwf',
                     'loanamount', 'loanamountrwf', 'amountrequested'],
  provider: ['provider', 'statementprovider'],
};
var REGISTER_TAB = 'Ledger register';
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
      if (n > 1 && n <= ctx.values.length && rows.indexOf(n) < 0) rows.push(n);
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

function checkConnection() {
  var cfg = config_();
  var ui = SpreadsheetApp.getUi();
  var res;
  try {
    // No files: a correct token gets "no bureau report", a wrong one 401.
    res = UrlFetchApp.fetch(cfg.url + '/api/v1/intake/assess', {
      method: 'post', payload: { probe: '1' }, headers: { 'X-Intake-Token': cfg.token },
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
  setStatus_(ctx, n, PROCESSING, '', '');
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

    var res;
    try {
      res = UrlFetchApp.fetch(ctx.cfg.url + '/api/v1/intake/assess', {
        method: 'post', payload: payload, headers: { 'X-Intake-Token': ctx.cfg.token },
        muteHttpExceptions: true,
      });
    } catch (e) {
      throw new Error('Could not reach Ledger at ' + ctx.cfg.url + ': ' + e.message);
    }
    var code = res.getResponseCode();
    if (code !== 200) throw new Error(errorMessage_(code, res));
    var body = JSON.parse(res.getContentText());

    var link = '';
    if (ctx.cfg.reportTabs) {
      link = '=HYPERLINK("#gid=' + writeReport_(body.report).getSheetId() + '","Open report")';
    } else if (body.saved && body.saved.url) {
      link = '=HYPERLINK("' + body.saved.url + '","Open report")';
    }
    appendRegister_(body, n, link);
    setStatus_(ctx, n, body.status, body.result, link);
    return true;
  } catch (e) {
    setStatus_(ctx, n, 'Error', e.message, '');
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
    reportTabs: String(p.getProperty('REPORT_TABS') || 'yes').trim().toLowerCase() !== 'no',
  };
}

function key_(s) { return String(s == null ? '' : s).toLowerCase().replace(/[^a-z0-9]/g, ''); }

function columns_(ctx) {
  var header = ctx.display[0] || [];
  var cols = { statement: [], out: {} };
  header.forEach(function (h, i) {
    var text = String(h).trim(), k = key_(text);
    if (!k) return;
    if (LEDGER_COLS.indexOf(text) >= 0) { cols.out[text] = i; return; }
    for (var f in FIELDS) {
      if (FIELDS[f].indexOf(k) >= 0 && !(f in cols)) { cols[f] = i; return; }
    }
    if (k.indexOf('statement') >= 0) cols.statement.push(i);
  });
  var missing = [];
  if (!('crb' in cols)) missing.push('CRB link');
  if (!cols.statement.length) missing.push('Statement link');
  if (!('customer_type' in cols)) missing.push('Customer type');
  if (!('requested_amount' in cols)) missing.push('Requested amount');
  if (missing.length) {
    throw new Error('The "' + ctx.sheet.getName() + '" tab has no ' + missing.join(', ') +
      ' column. Headers found: ' + header.filter(String).join(', ') + '.');
  }
  // The Ledger columns go after the last header, once.
  var next = header.length;
  LEDGER_COLS.forEach(function (name) {
    if (!(name in cols.out)) {
      ctx.sheet.getRange(1, next + 1).setValue(name).setFontWeight('bold');
      cols.out[name] = next++;
    }
  });
  return cols;
}

function pendingRows_(ctx) {
  var rows = [];
  var status = ctx.cols.out['Ledger status'], at = ctx.cols.out['Ledger assessed at'];
  for (var i = 1; i < ctx.values.length; i++) {
    var s = String(ctx.values[i][status] == null ? '' : ctx.values[i][status]).trim();
    var stale = s === PROCESSING && Date.now() - new Date(ctx.values[i][at]).getTime() > STALE_MS;
    if ((s === '' || stale) && !isBlank_(ctx, i)) rows.push(i + 1);
  }
  return rows;
}

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
  if (!crb.length) problems.push('No CRB link.');
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
  return found.filter(function (link, k) { return link && found.indexOf(link) === k; });
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
function setStatus_(ctx, n, status, result, link) {
  var out = ctx.cols.out;
  var values = {
    'Ledger status': status, 'Ledger result': result, 'Ledger assessed at': new Date(), 'Ledger report': link,
  };
  LEDGER_COLS.forEach(function (name) {
    var cell = ctx.sheet.getRange(n, out[name] + 1);
    cell.setValue(values[name]);
    if (name === 'Ledger assessed at') cell.setNumberFormat('yyyy-mm-dd hh:mm');
    ctx.values[n - 1][out[name]] = values[name];
  });
}

function writeReport_(report) {
  var ss = SpreadsheetApp.getActive();
  var sheet = ss.getSheetByName(report.title) || ss.insertSheet(report.title);
  sheet.clear();
  var width = report.values.reduce(function (w, r) { return Math.max(w, r.length); }, 1);
  var values = report.values.map(function (r) {
    var row = r.slice();
    while (row.length < width) row.push('');
    return row;
  });
  sheet.getRange(1, 1, values.length, width).setValues(values);

  var whole = [], byFormat = {};
  values.forEach(function (r, i) {
    r.forEach(function (v, j) {
      if (typeof v === 'number' && v % 1 === 0 && Math.abs(v) >= 1000) whole.push(a1_(i, j));
    });
  });
  report.formats.forEach(function (f) { (byFormat[f[2]] = byFormat[f[2]] || []).push(a1_(f[0], f[1])); });
  if (whole.length) sheet.getRangeList(whole).setNumberFormat('#,##0');
  Object.keys(byFormat).forEach(function (fmt) { sheet.getRangeList(byFormat[fmt]).setNumberFormat(fmt); });

  report.kinds.forEach(function (kind, i) {
    var row = sheet.getRange(i + 1, 1, 1, width);
    if (kind === 'banner') row.setFontWeight('bold').setFontSize(14);
    else if (kind === 'title') row.setFontWeight('bold').setFontSize(12);
    else if (kind === 'head' || kind === 'total' || kind === 'sub') row.setFontWeight('bold');
    else if (kind === 'meta' || kind === 'note') row.setFontStyle('italic').setFontColor('#5f6b78');
  });
  sheet.setColumnWidth(1, 280);
  if (width > 1) sheet.setColumnWidths(2, width - 1, 140);
  return sheet;
}

function appendRegister_(body, n, link) {
  var ss = SpreadsheetApp.getActive();
  var sheet = ss.getSheetByName(REGISTER_TAB);
  var headers = ['Intake row', 'Report'].concat(body.register.headers);
  if (!sheet) {
    sheet = ss.insertSheet(REGISTER_TAB);
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]).setFontWeight('bold');
    sheet.setFrozenRows(1);
  }
  sheet.appendRow([n, link].concat(body.register.values[0]));
  var last = sheet.getLastRow();
  body.register.formats.forEach(function (f) {
    sheet.getRange(last, f[1] + 3).setNumberFormat(f[2]);
  });
}

function a1_(i, j) {
  var col = '', k = j + 1;
  while (k) { var r = (k - 1) % 26; col = String.fromCharCode(65 + r) + col; k = Math.floor((k - 1) / 26); }
  return col + (i + 1);
}

function errorMessage_(code, res) {
  var body = null;
  try { body = JSON.parse(res.getContentText()); } catch (e) { /* not JSON */ }
  var detail = (body && (body.detail || body)) || {};
  var msg = typeof detail === 'string' ? detail : detail.message;
  return msg || ('Ledger answered ' + code + '.');
}
