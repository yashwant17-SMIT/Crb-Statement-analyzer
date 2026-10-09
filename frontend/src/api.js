const BASE = import.meta.env.VITE_API_BASE || ''

export class ApiError extends Error {
  constructor(message, { code, hint, status } = {}) {
    super(message)
    this.code = code
    this.hint = hint
    this.status = status
  }
}

/**
 * Step 1 of the process. Send the credit bureau report.
 * The band decides whether a statement may be assessed at all.
 * @param {File} file
 * @param {{creditCap?:number, minBand?:string}} policy
 * @param {AbortSignal} [signal]
 */
export async function checkCreditReport(file, policy = {}, signal) {
  const form = new FormData()
  form.append('file', file)
  if (policy.creditCap != null) form.append('credit_cap', policy.creditCap)
  if (policy.minBand) form.append('min_band', policy.minBand)
  return post('/api/v1/crb/check', form, signal)
}

/**
 * The list of providers the API can parse, for the upload select.
 * @param {AbortSignal} [signal]
 */
export async function fetchProviders(signal) {
  const res = await fetch(`${BASE}/api/v1/providers`, { signal })
  if (!res.ok) throw new ApiError('Could not load the provider list.', { status: res.status })
  const payload = await res.json()
  return payload.providers ?? []
}

/**
 * Send one or more statements to the underwriting API. Several files come back
 * as one combined report: files of the same account are merged, different
 * accounts are analysed separately and added month by month.
 * @param {{file: File, provider: string}[]} items  provider e.g. 'mtn_momo'
 * @param {{largeTxnThreshold?:number, minInflowCount?:number, creditCap?:number}} policy
 * @param {AbortSignal} [signal]
 */
export async function analyzeStatement(items, policy = {}, signal) {
  const form = new FormData()
  for (const { file, provider } of items) {
    form.append('file', file)
    form.append('provider', provider || '')
  }
  if (policy.largeTxnThreshold != null) form.append('large_txn_threshold', policy.largeTxnThreshold)
  if (policy.minInflowCount != null) form.append('min_inflow_count', policy.minInflowCount)
  if (policy.creditCap != null) form.append('credit_cap', policy.creditCap)
  if (policy.monthlyEmi != null) form.append('monthly_emi', policy.monthlyEmi)
  if (policy.requestedAmount != null) form.append('requested_amount', policy.requestedAmount)
  if (policy.customerType) form.append('customer_type', policy.customerType)
  // The bureau applicant, so the engine can confirm the statement belongs to
  // the person whose credit file was just parsed.
  if (policy.bureauName) form.append('bureau_name', policy.bureauName)
  if (policy.bureauPhones?.length) form.append('bureau_phones', policy.bureauPhones.join(','))

  return post('/api/v1/analyze', form, signal)
}

/**
 * Loan amounts the sanction tables can pay out, largest first, per customer
 * type. Read from the server's policy so the dropdown can never offer an
 * amount the engine would not sanction.
 * @returns {Promise<{new:number[], existing:number[]}>}
 */
export async function fetchSanctionAmounts(signal) {
  const res = await fetch(`${BASE}/api/v1/policy`, { signal })
  if (!res.ok) throw new ApiError('Could not load the credit policy.', { status: res.status })
  const p = await res.json()
  const amounts = (bands) => [...new Set((bands || []).map((b) => b.loan_amount))].sort((a, b) => b - a)
  return { new: amounts(p.loan_eligibility_bands), existing: amounts(p.existing_loan_eligibility_bands) }
}

/**
 * Whether the server has a Google Sheet to save to. Any failure reads as
 * "not configured" so the console simply hides the export.
 */
export async function fetchSheetsStatus(signal) {
  try {
    const res = await fetch(`${BASE}/api/v1/sheets/status`, { signal })
    return res.ok ? await res.json() : { configured: false }
  } catch {
    return { configured: false }
  }
}

/**
 * Write the assessment to the customer's tab in the configured Google Sheet.
 * Either half may be null: a bureau-only file is saved as well.
 * @returns {Promise<{tab:string, url:string, created:boolean, rows:number}>}
 */
export async function saveToSheets(crb, report, files) {
  const body = JSON.stringify({ credit_bureau: crb ?? null, statement: report ?? null, files })
  let res
  try {
    res = await fetch(`${BASE}/api/v1/sheets/save`, {
      method: 'POST', body, headers: { 'Content-Type': 'application/json' },
    })
  } catch {
    throw new ApiError('The analysis service did not respond.', { code: 'network_unreachable' })
  }
  const payload = await res.json().catch(() => null)
  if (!res.ok) {
    const detail = payload?.detail ?? {}
    throw new ApiError(detail.message || 'Could not save to Google Sheets.',
      { code: detail.error, hint: detail.hint, status: res.status })
  }
  return payload
}

async function post(path, form, signal) {
  let res
  try {
    res = await fetch(`${BASE}${path}`, { method: 'POST', body: form, signal })
  } catch (err) {
    if (err.name === 'AbortError') throw err
    throw new ApiError(
      'The analysis service did not respond. Check that the API is running and reachable.',
      { code: 'network_unreachable' },
    )
  }

  const payload = await res.json().catch(() => null)

  if (!res.ok) {
    const detail = payload?.detail ?? payload ?? {}
    throw new ApiError(
      detail.message || 'The document could not be assessed.',
      { code: detail.error, hint: detail.hint, status: res.status },
    )
  }
  return payload
}

export const fmt = new Intl.NumberFormat('en-RW')
export const money = (n) => (n == null ? '—' : fmt.format(n))
export const pct = (n) => (n == null ? '—' : `${(n * 100).toFixed(1)}%`)
export const shortDate = (iso) =>
  iso ? new Date(iso).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—'

/**
 * Spell a whole number of Rwandan francs.
 *
 * A requested amount is typed by hand and an extra or missing zero is very
 * hard to see in a bare numeral: 1000000 and 10000000 look almost identical in
 * an input box. Words make the mistake obvious before the statement is parsed.
 * Grouped in thousands, millions and billions, which is how RWF is read.
 */
const ONES = ['', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
  'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
  'seventeen', 'eighteen', 'nineteen']
const TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']

function underThousand(n) {
  if (n < 20) return ONES[n]
  if (n < 100) {
    const t = TENS[Math.floor(n / 10)]
    const o = ONES[n % 10]
    return o ? `${t}-${o}` : t
  }
  const h = `${ONES[Math.floor(n / 100)]} hundred`
  const rest = n % 100
  return rest ? `${h} and ${underThousand(rest)}` : h
}

export function amountInWords(value) {
  const n = Math.floor(Number(value))
  if (!Number.isFinite(n) || n <= 0) return ''
  if (n >= 1e12) return 'a very large amount'
  const scales = [
    [1e9, 'billion'],
    [1e6, 'million'],
    [1e3, 'thousand'],
  ]
  let rest = n
  const parts = []
  for (const [size, name] of scales) {
    const count = Math.floor(rest / size)
    if (count) {
      parts.push(`${underThousand(count)} ${name}`)
      rest -= count * size
    }
  }
  if (rest) parts.push(underThousand(rest))
  const words = parts.join(' ')
  return words.charAt(0).toUpperCase() + words.slice(1) + ' Rwandan francs'
}
