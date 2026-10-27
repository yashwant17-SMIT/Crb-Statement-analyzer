/**
 * Runs Code.gs against in-memory fakes of SpreadsheetApp, DriveApp,
 * UrlFetchApp and the Sheets advanced service. No Google account needed.
 *
 *   node apps-script/test/run.mjs
 *
 * With LEDGER_LIVE=http://127.0.0.1:8000 and LEDGER_TOKEN set, the fake
 * UrlFetchApp posts to that server with curl instead of answering itself, so
 * the multipart the script builds is parsed by the real FastAPI route.
 */
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const CODE = readFileSync(join(HERE, '..', 'Code.gs'), 'utf8')
const LIVE = process.env.LEDGER_LIVE
const PDF = '%PDF-1.4\n% test\n'

const CRB = 'https://drive.google.com/file/d/1CRBcrbCRBcrbCRBcrbCRBcrb/view'
const ST1 = 'https://drive.google.com/open?id=1STAstaSTAstaSTAstaSTA001'
const ST2 = 'https://drive.google.com/file/d/1STAstaSTAstaSTAstaSTA002/view'

/* ----------------------------------------------------------------- fakes */
class Cell {
  constructor(v = '') { this.value = v; this.link = null; this.formula = ''; this.format = null; this.bold = false }
  get display() { return this.value instanceof Date ? this.value.toISOString() : String(this.value ?? '') }
}

class Sheet {
  constructor(name, id, rows = []) {
    this.name = name; this.id = id; this.cells = []; this.frozen = 0
    rows.forEach((r, i) => r.forEach((v, j) => {
      const c = this.cell(i, j)
      if (v && typeof v === 'object' && !(v instanceof Date)) Object.assign(c, v)
      else c.value = v
    }))
  }
  cell(i, j) {
    while (this.cells.length <= i) this.cells.push([])
    while (this.cells[i].length <= j) this.cells[i].push(new Cell())
    return this.cells[i][j]
  }
  width() { return Math.max(0, ...this.cells.map((r) => r.length)) }
  getName() { return this.name }
  getSheetId() { return this.id }
  getLastRow() { return this.cells.length }
  getDataRange() { return this.getRange(1, 1, this.cells.length, this.width()) }
  getRange(r, c, nr = 1, nc = 1) {
    if (typeof r === 'string') return this.a1(r)
    return new Range(this, r - 1, c - 1, nr, nc)
  }
  a1(ref) {
    const [, col, row] = /^([A-Z]+)(\d+)$/.exec(ref)
    const c = [...col].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0)
    return this.getRange(+row, c)
  }
  getRangeList(refs) {
    const ranges = refs.map((r) => this.a1(r))
    return { setNumberFormat: (f) => ranges.forEach((r) => r.setNumberFormat(f)) }
  }
  appendRow(values) { this.getRange(this.cells.length + 1, 1, 1, values.length).setValues([values]) }
  clear() { this.cells = [] }
  setColumnWidth() {}
  setColumnWidths() {}
  setFrozenRows(n) { this.frozen = n }
}

class Range {
  constructor(sheet, r, c, nr, nc) { Object.assign(this, { sheet, r, c, nr, nc }) }
  each(fn) { for (let i = 0; i < this.nr; i++) for (let j = 0; j < this.nc; j++) fn(this.sheet.cell(this.r + i, this.c + j), i, j) }
  grid(fn) { return Array.from({ length: this.nr }, (_, i) => Array.from({ length: this.nc }, (_, j) => fn(this.sheet.cell(this.r + i, this.c + j)))) }
  getRow() { return this.r + 1 }
  getDataValidation() {
    const o = this.sheet.cell(this.r, this.c).options
    return o ? { getCriteriaType: () => 'VALUE_IN_LIST', getCriteriaValues: () => [o] } : null
  }
  getA1Notation() { let n = this.c + 1, s = ''; while (n) { s = String.fromCharCode(65 + (n - 1) % 26) + s; n = Math.floor((n - 1) / 26) } return s + (this.r + 1) }
  getNumRows() { return this.nr }
  getValues() { return this.grid((c) => c.value) }
  getDisplayValues() { return this.grid((c) => c.display) }
  getFormulas() { return this.grid((c) => c.formula) }
  getRichTextValues() {
    return this.grid((c) => ({ getLinkUrl: () => c.link, getRuns: () => (c.runs || []).map((u) => ({ getLinkUrl: () => u })) }))
  }
  setValue(v) { return this.setValues([[v]]) }
  setValues(rows) {
    this.each((c, i, j) => {
      const v = rows[i][j]
      if (typeof v === 'string' && v.startsWith('=')) { c.formula = v; c.value = '' } else { c.value = v; c.formula = '' }
    })
    return this
  }
  setNumberFormat(f) { this.each((c) => { c.format = f }); return this }
  setFontWeight(w) { this.each((c) => { c.bold = w === 'bold' }); return this }
  setFontSize() { return this }
  setFontStyle() { return this }
  setFontColor() { return this }
}

const REGISTER_GID = 910001

/** The results workbook, as far as Code.gs can see it through Sheets.*. */
function workbook(fail) {
  const wb = { tabs: { Sheet1: 0 }, register: [], batches: [], chipRows: {}, fail }
  wb.api = {
    get: (id, opts) => {
      // A row's smart chips, read from the intake sheet.
      if (opts?.ranges) {
        const n = Number(/!(\d+):/.exec(opts.ranges[0])[1])
        return { sheets: [{ data: [{ rowData: wb.chipRows[n] ? [{ values: wb.chipRows[n] }] : [] }] }] }
      }
      return { sheets: Object.entries(wb.tabs).map(([title, sheetId]) => ({ properties: { title, sheetId } })) }
    },
    batchUpdate(body, id) {
      if (wb.fail) throw new Error(wb.fail)
      wb.batches.push({ id, ops: body.requests })
      for (const op of body.requests) {
        if (op.addSheet) wb.tabs[op.addSheet.properties.title] = op.addSheet.properties.sheetId
        if (op.appendCells?.sheetId === REGISTER_GID) {
          for (const row of op.appendCells.rows) {
            const f = row.values.map((v) => v.userEnteredValue?.formulaValue).find(Boolean)
            wb.register.push([/,"(.*)"\)$/.exec(f)[1], 'yes'])
          }
        }
        const u = op.updateCells
        if (u?.range.sheetId === REGISTER_GID && u.range.startRowIndex > 0) wb.register[u.range.startRowIndex - 1][1] = u.rows[0].values[0].userEnteredValue.stringValue
      }
      return {}
    },
    Values: {
      get: () => ({ values: [['Assessment ID', 'Assessed at', 'Customer', 'Current']] }),
      batchGet: () => ({ valueRanges: [{ values: wb.register.map((r) => [r[0]]) }, { values: wb.register.map((r) => [r[1]]) }] }),
    },
  }
  return wb
}

function harness({ rows, files, respond, props = {}, failWrite }) {
  const wb = workbook(failWrite)
  const intake = new Sheet('Applications', 1, rows)
  const sheets = [intake]
  const fetches = []
  const toasts = []
  const ss = {
    getSheets: () => sheets,
    getSheetByName: (n) => sheets.find((s) => s.name === n) || null,
    insertSheet: (n) => { const s = new Sheet(n, 100 + sheets.length); sheets.push(s); return s },
    toast: (m) => toasts.push(m),
    getId: () => 'intake-id',
  }
  const blob = (name, data) => ({ name, data, setName(n) { this.name = n; return this } })
  const ctx = {
    SpreadsheetApp: {
      getActive: () => ss, getActiveSheet: () => intake, flush() {},
      getActiveRangeList: () => ({ getRanges: () => [intake.getRange(2, 1, 2, 1)] }),
      getUi: () => ({ alert() {}, ButtonSet: {} }),
      DataValidationCriteria: { VALUE_IN_LIST: 'VALUE_IN_LIST', VALUE_IN_RANGE: 'VALUE_IN_RANGE' },
    },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: (k) => ({ LEDGER_URL: 'https://ledger.example', LEDGER_TOKEN: process.env.LEDGER_TOKEN || 'tok', ...props })[k] ?? null,
      }),
    },
    DriveApp: {
      getFileById(id) {
        const f = files[id]
        if (!f) throw new Error('No item with the given ID could be found')
        return { getName: () => f.name, getMimeType: () => f.mime, getSize: () => f.data.length, getBlob: () => blob(f.name, f.data) }
      },
    },
    UrlFetchApp: {
      fetch(url, opts) {
        fetches.push({ url, opts })
        const { code, body } = LIVE ? curl(url, opts) : respond(opts.payload)
        return { getResponseCode: () => code, getContentText: () => (typeof body === 'string' ? body : JSON.stringify(body)) }
      },
    },
    Sheets: { Spreadsheets: wb.api },
    LockService: { getDocumentLock: () => ({ tryLock: () => true, releaseLock() {} }) },
    Session: { getEffectiveUser: () => ({ getEmail: () => 'owner@example.com' }), getScriptTimeZone: () => 'Africa/Kigali' },
    Utilities: { formatDate: (d) => d.toISOString().slice(0, 16).replace('T', ' ') },
    ScriptApp: { getProjectTriggers: () => [] },
    Date,
  }
  vm.createContext(ctx)
  vm.runInContext(CODE, ctx)
  return { ctx, intake, sheets, fetches, toasts, wb }
}

/** Posts the script's payload to a real server, the way UrlFetchApp would. */
function curl(url, opts) {
  const dir = mkdtempSync(join(tmpdir(), 'ledger-'))
  const args = ['-s', '-o', '-', '-w', '\n%{http_code}', '-X', 'POST', url.replace('https://ledger.example', LIVE)]
  for (const [k, v] of Object.entries(opts.headers || {})) args.push('-H', `${k}: ${v}`)
  for (const [k, v] of Object.entries(opts.payload)) {
    if (v && typeof v === 'object') {
      const p = join(dir, v.name)
      writeFileSync(p, v.data)
      args.push('-F', `${k}=@${p};type=application/pdf;filename=${v.name}`)
    } else {
      args.push('-F', `${k}=${v}`)
    }
  }
  const out = execFileSync('curl', args, { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 })
  const at = out.lastIndexOf('\n')
  return { code: Number(out.slice(at + 1)), body: out.slice(0, at) }
}

/* --------------------------------------------------------------- fixtures */
const HEADER = ['Requested Amount (RWF)', 'Customer Name', 'CRB Report', 'Notes', 'Statement link 1', 'Statement link 2', 'Customer type']
const FILES = {
  '1CRBcrbCRBcrbCRBcrbCRBcrb': { name: 'crb.pdf', mime: 'application/pdf', data: PDF },
  '1STAstaSTAstaSTAstaSTA001': { name: 'jan.pdf', mime: 'application/pdf', data: PDF },
  '1STAstaSTAstaSTAstaSTA002': { name: 'feb.pdf', mime: 'application/pdf', data: PDF },
}
const row = (over = {}) => {
  const r = {
    amount: 1000000, name: 'Benie N', crb: { value: 'crb.pdf', link: CRB }, notes: 'called twice',
    s1: ST1, s2: { value: '=HYPERLINK("…")', formula: `=HYPERLINK("${ST2}","feb")` }, type: 'Existing', ...over,
  }
  return [r.amount, r.name, r.crb, r.notes, r.s1, r.s2, r.type]
}
const OK = {
  status: 'Done', result: 'Band BB · Assessed — no findings',
  results: { ops: [{ addSheet: { properties: { sheetId: 5, title: 'BENIE N · 1199' } } }], gid: 5, tab: 'BENIE N · 1199' },
}
const col = (h, name) => h.intake.cells[0].findIndex((c) => c.value === name)
// Ledger assessment reads "<status> <when> · <result>" or "Error: <message>".
const assessment = (h, n) => String(h.intake.cells[n - 1][col(h, 'Ledger assessment')].value)
const status = (h, n) => /^(Done|Blocked at CRB|Awaiting statement|Error|Processing…)/.exec(assessment(h, n))?.[1]
const result = (h, n) => assessment(h, n).replace(/^Error: /, '')

/* ------------------------------------------------------------------ tests */
const tests = {
  'pending rows are posted, written to the results workbook and linked'() {
    const h = harness({ rows: [HEADER, row(), [], row({ name: 'Second', type: 'new' })], files: FILES, respond: () => ({ code: 200, body: OK }) })
    h.ctx.assessPending()
    assert.equal(h.fetches.length, 2, 'the blank row is skipped')
    const { url, opts } = h.fetches[0]
    assert.equal(url, 'https://ledger.example/api/v1/intake/assess')
    assert.equal(opts.headers['X-Intake-Token'], 'tok')
    assert.equal(opts.payload.crb_file.name, 'crb.pdf')
    assert.equal(opts.payload.statement_file_1.name, 'jan.pdf')
    assert.equal(opts.payload.statement_file_2.name, 'feb.pdf', '=HYPERLINK formulas are read')
    assert.equal(opts.payload.customer_type, 'Existing')
    assert.equal(opts.payload.requested_amount, '1000000')
    assert.deepEqual(JSON.parse(opts.payload.results_state), { tabs: { Sheet1: 0 }, register: [] })
    // The second row sees the tab the first one created.
    assert.equal(JSON.parse(h.fetches[1].opts.payload.results_state).tabs['BENIE N · 1199'], 5)
    // Ledger columns are added after the last header (G), once.
    assert.deepEqual(h.intake.cells[0].slice(7).map((c) => c.value), ['Ledger assessment', 'Ledger report'])
    assert.equal(status(h, 2), 'Done')
    assert.equal(status(h, 4), 'Done')
    assert.deepEqual(h.wb.batches.map((b) => b.id), ['intake-id', 'intake-id'], 'default: this spreadsheet')
    assert.equal(h.intake.cells[1][col(h, 'Ledger report')].formula,
      '=HYPERLINK("https://docs.google.com/spreadsheets/d/intake-id/edit#gid=5","Open report")')
    assert.equal(h.sheets.length, 1, 'nothing is written into the intake spreadsheet by hand')
    assert.match(h.toasts.at(-1), /^2 assessed\.$/)
  },

  'RESULTS_SHEET_ID sends the output to another workbook'() {
    const h = harness({ rows: [HEADER, row()], files: FILES, respond: () => ({ code: 200, body: OK }), props: { RESULTS_SHEET_ID: 'results-id' } })
    h.ctx.assessPending()
    assert.equal(h.wb.batches[0].id, 'results-id')
    assert.match(h.intake.cells[1][col(h, 'Ledger report')].formula, /\/d\/results-id\/edit#gid=5/)
  },

  'a failed write to the results workbook marks the row'() {
    const h = harness({ rows: [HEADER, row()], files: FILES, respond: () => ({ code: 200, body: OK }), failWrite: 'The caller does not have permission' })
    h.ctx.assessPending()
    assert.equal(status(h, 2), 'Error')
    assert.match(result(h, 2), /could not write to the results workbook: The caller does not have permission/)
  },

  'assessed rows are not run again; a cleared status is'() {
    const h = harness({ rows: [HEADER, row(), row()], files: FILES, respond: () => ({ code: 200, body: OK }) })
    h.ctx.assessPending()
    h.ctx.assessPending()
    assert.equal(h.fetches.length, 2)
    h.intake.cells[1][col(h, 'Ledger assessment')].value = ''
    h.ctx.assessPending()
    assert.equal(h.fetches.length, 3)
  },

  'a row that was interrupted mid-run is picked up again'() {
    const h = harness({ rows: [HEADER, row()], files: FILES, respond: () => ({ code: 200, body: OK }) })
    h.ctx.assessPending()
    h.intake.cells[1][col(h, 'Ledger assessment')].value = 'Processing… ' + new Date(Date.now() - 5 * 60 * 1000).toISOString()
    h.ctx.assessPending()
    assert.equal(h.fetches.length, 1, 'a run in progress is left alone')
    h.intake.cells[1][col(h, 'Ledger assessment')].value = 'Processing… ' + new Date(Date.now() - 60 * 60 * 1000).toISOString()
    h.ctx.assessPending()
    assert.equal(h.fetches.length, 2)
  },

  'a non-Drive link is refused without a call'() {
    const h = harness({ rows: [HEADER, row({ s1: 'https://dropbox.com/s/x/statement.pdf' })], files: FILES, respond: () => assert.fail('posted') })
    h.ctx.assessPending()
    assert.equal(status(h, 2), 'Error')
    assert.match(result(h, 2), /Not a Google Drive file link: https:\/\/dropbox\.com/)
  },

  'a file the owner cannot open names the owner'() {
    const h = harness({ rows: [HEADER, row()], files: {}, respond: () => assert.fail('posted') })
    h.ctx.assessPending()
    assert.match(result(h, 2), /shared with owner@example\.com/)
  },

  'a Google Doc is not sent as a PDF'() {
    const files = { ...FILES, '1CRBcrbCRBcrbCRBcrbCRBcrb': { name: 'crb', mime: 'application/vnd.google-apps.document', data: 'x' } }
    const h = harness({ rows: [HEADER, row()], files, respond: () => assert.fail('posted') })
    h.ctx.assessPending()
    assert.match(result(h, 2), /not a PDF/)
  },

  "the server's message lands in the row"() {
    const h = harness({
      rows: [HEADER, row()], files: FILES,
      respond: () => ({ code: 422, body: { detail: { error: 'intake_row_invalid', message: 'Customer type is empty. Enter New or Existing.' } } }),
    })
    h.ctx.assessPending()
    assert.equal(status(h, 2), 'Error')
    assert.equal(assessment(h, 2), 'Error: Customer type is empty. Enter New or Existing.')
  },

  'a sheet without the required columns is refused by name'() {
    const h = () => harness({ rows: [['Customer', 'CRB', 'Amount']], files: FILES, respond: () => ({}) })
    assert.throws(() => h().ctx.assessPending(), /no Statement link, Customer type column/)
  },

  'an ops sheet with a title row and its own header names'() {
    const header = ['Customer Name', 'TYPE of customer', 'Loan amount requested', 'ID Copy', 'MoMo/BoK Statement', 'CRB Link', 'CRB band (AA, BB etc)']
    const app = ['Benie N', 'New', '1,500,000', 'id.pdf', ST1, { value: 'crb', link: CRB }, 'BB']
    const h = harness({ rows: [['Loan applications — October'], [], header, app], files: FILES, respond: () => ({ code: 200, body: OK }) })
    h.ctx.assessPending()
    assert.equal(h.fetches.length, 1, 'the title and blank rows are not applications')
    const p = h.fetches[0].opts.payload
    assert.equal(p.crb_file.name, 'crb.pdf', '"CRB Link" is the report, "CRB band" is not')
    assert.equal(p.statement_file_1.name, 'jan.pdf')
    assert.equal(p.customer_type, 'New')
    assert.equal(p.requested_amount, '1,500,000')
    const at = h.intake.cells[2].findIndex((c) => c.value === 'Ledger assessment')
    assert.equal(at, 7, 'Ledger columns go on the header row')
    assert.match(h.intake.cells[3][at].value, /^Done /)
  },

  'a Drive smart chip is read through the Sheets API'() {
    const h = harness({ rows: [HEADER, row({ crb: 'crb.pdf' })], files: FILES, respond: () => ({ code: 200, body: OK }) })
    const chips = []
    chips[2] = { chipRuns: [{ startIndex: 0, chip: { richLinkProperties: { uri: CRB, mimeType: 'application/pdf' } } }] }
    h.wb.chipRows[2] = chips
    h.ctx.assessPending()
    assert.equal(status(h, 2), 'Done', result(h, 2))
    assert.equal(h.fetches[0].opts.payload.crb_file.name, 'crb.pdf')
  },

  'a CRB cell without a link says what it shows'() {
    const h = harness({ rows: [HEADER, row({ crb: 'crb report.pdf' })], files: FILES, respond: () => ({ code: 200, body: OK }) })
    h.ctx.assessPending()
    assert.equal(h.fetches.length, 0)
    assert.match(result(h, 2), /No CRB link in C2 \(it shows "crb report.pdf"; paste the Drive link itself\)/)
  },

  'the ops sheet: outcome in its own columns, signed-off rows left alone'() {
    const header = ['S.No', 'Application ID', 'RM/Sales Rep', 'Cust ID', 'Customer Name', 'TYPE of customer', 'Date Applied',
      'Existing Customer check', 'Current FA Amount', 'Visit date (along with GPS check)', 'Business vintage (from Business registration form)',
      'Business photos (atleast 4)', 'Loan Purpose', 'Description of purpose', 'Business Type', 'Loan amount requested', 'Phone Number',
      'ID Copy', 'MoMo/BoK Statement', 'Type of statement', 'Cash Flow Analysis Link', 'CRB Link', 'CRB band (AA, BB etc)',
      'PA Open Total Value(RWF) (from CRB report)', 'Number of active loans (Open PA from CRB Report)', 'Comments',
      'Signed off by Vivianne (Link)', 'Status Signed off by Credit risk team', 'Sanctioned Amount', 'Basis of Rejection/ on-hold']
    const at = (name) => header.indexOf(name)
    const app = (over = {}) => {
      const r = header.map(() => '')
      Object.assign(r, { [at('Customer Name')]: 'Benie N', [at('TYPE of customer')]: 'Existing customer (25%)',
        [at('Loan amount requested')]: 1500000, [at('MoMo/BoK Statement')]: ST1, [at('Type of statement')]: 'MoMo',
        [at('CRB Link')]: { value: 'crb', link: CRB }, [at('PA Open Total Value(RWF) (from CRB report)')]: 250000,
        [at('Status Signed off by Credit risk team')]: { value: '', options: ['Approved', 'Rejected', 'On Hold'] } })
      for (const [k, v] of Object.entries(over)) r[at(k)] = v
      return r
    }
    const decided = app({ 'Status Signed off by Credit risk team': 'Approved' })
    const row = { band: 'BB', decision: 'on_hold', sanctioned_amount: 800000, reason: 'CRB: Written-off account.' }
    const h = harness({ rows: [header, app(), decided], files: FILES, respond: () => ({ code: 200, body: { ...OK, row } }) })
    h.ctx.assessPending()
    assert.equal(h.fetches.length, 1, 'a row the credit risk team already signed off is not pending')
    const p = h.fetches[0].opts.payload
    assert.equal(p.crb_file.name, 'crb.pdf')
    assert.deepEqual(Object.keys(p).filter((k) => k.startsWith('statement_file')), ['statement_file_1'])
    assert.equal(p.customer_type, 'Existing customer (25%)')
    const cell = (name) => h.intake.cells[1][at(name)].value
    assert.equal(cell('CRB band (AA, BB etc)'), 'BB')
    assert.equal(cell('Status Signed off by Credit risk team'), 'On Hold', "the dropdown's own spelling")
    assert.equal(cell('Sanctioned Amount'), 800000)
    assert.equal(cell('Basis of Rejection/ on-hold'), 'CRB: Written-off account.')
    assert.deepEqual(h.intake.cells[0].slice(header.length).map((c) => c.value), ['Ledger assessment', 'Ledger report'])
    assert.match(String(h.intake.cells[1][header.length].value), /^Done \d{4}-\d\d-\d\d \d\d:\d\d · /)
  },

  'a status dropdown without the option is left for a person'() {
    const header = ['Customer type', 'Amount', 'CRB', 'Statement', 'Status', 'Reason']
    const h = harness({
      rows: [header, ['New', 500000, { value: 'c', link: CRB }, ST1, { value: '', options: ['Yes', 'No'] }, '']],
      files: FILES, respond: () => ({ code: 200, body: { ...OK, row: { band: 'AA', decision: 'approved', sanctioned_amount: 500000, reason: '' } } }),
    })
    h.ctx.assessPending()
    assert.equal(h.intake.cells[1][4].value, '')
    assert.match(String(h.intake.cells[1][6].value), /Status not set: its dropdown has no option for "Approved"/)
  },

  'a tab with no header row names the tabs'() {
    const h = harness({ rows: [[], ['x']], files: FILES, respond: () => ({}) })
    assert.throws(() => h.ctx.assessPending(), /No header row .* "Applications" tab.*INTAKE_TAB.*Tabs: Applications/)
  },

  'LEDGER_URL must be https'() {
    const h = harness({ rows: [HEADER, row()], files: FILES, respond: () => ({}), props: { LEDGER_URL: 'http://1.2.3.4' } })
    assert.throws(() => h.ctx.assessPending(), /must start with https/)
  },
}

const liveTests = {
  'the real route plans the write and a re-run supersedes'() {
    const h = harness({ rows: [HEADER, row(), row(), row({ type: '' })], files: FILES })
    h.ctx.assessPending()
    assert.equal(status(h, 2), 'Done', result(h, 2))
    assert.equal(status(h, 3), 'Done', result(h, 3))
    for (const t of ['Register', 'Monthly revenue', 'Bureau accounts', 'Findings']) assert.ok(t in h.wb.tabs, t)
    const customer = Object.keys(h.wb.tabs).find((t) => !['Sheet1', 'Register', 'Monthly revenue', 'Bureau accounts', 'Findings'].includes(t))
    assert.ok(customer, 'customer tab created')
    assert.deepEqual(h.wb.register, [[customer, 'no'], [customer, 'yes']], 'second assessment supersedes the first')
    assert.ok(h.intake.cells[1][col(h, 'Ledger report')].formula.endsWith(`#gid=${h.wb.tabs[customer]}","Open report")`))
    assert.equal(status(h, 4), 'Error')
    assert.match(result(h, 4), /Customer type is empty/)
  },
}

let failed = 0
for (const [name, fn] of Object.entries(LIVE ? liveTests : tests)) {
  try { fn(); console.log(`  ok    ${name}`) } catch (e) { failed++; console.log(`  FAIL  ${name}\n        ${e.message}`) }
}
process.exit(failed ? 1 : 0)
