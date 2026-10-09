import { useEffect, useRef, useState } from 'react'
import { fetchProviders, money, pct, shortDate } from '../api'

const BANDS = ['AA', 'BB', 'CC', 'DD', 'EE', 'FF', 'GG', 'HH', 'II', 'JJ']

/* ------------------------------------------------------------------ chrome
   Applicant identity lives here and nowhere else. The old build repeated the
   name in a page header and again in a sticky bar 90px below it. */
export function Chrome({ crb, report, files = {}, onReset, showReset, sheet }) {
  return (
    <header className="chrome">
      <div className="chrome__mark">
        <h1>Ledger</h1>
        <span>v1.1</span>
      </div>

      <div className="chrome__subject">
        {crb ? (
          <>
            <b>{crb.applicant.names}</b>
            <dl>
              <div>
                <dt>National ID</dt>
                <dd>{crb.applicant.national_id}</dd>
              </div>
              <div>
                <dt>Band</dt>
                <dd>{crb.gate.band} · {crb.gate.score}</dd>
              </div>
              {report && (
                <div>
                  <dt>Statement</dt>
                  <dd>{report.account?.provider ?? files.statement ?? '—'}</dd>
                </div>
              )}
            </dl>
          </>
        ) : report ? (
          /* Bureau skipped: the statement header is the only identity source. */
          <>
            <b>{report.account?.customer || 'Name not on statement'}</b>
            <dl>
              {report.account?.msisdn && (
                <div>
                  <dt>Account</dt>
                  <dd>{report.account.msisdn}</dd>
                </div>
              )}
              <div>
                <dt>Statement</dt>
                <dd>{report.account?.provider ?? files.statement ?? '—'}</dd>
              </div>
            </dl>
          </>
        ) : (
          <b style={{ color: '#63707E', fontWeight: 400 }}>No application loaded</b>
        )}
      </div>

      <div className="chrome__actions">
        {(crb || report) && <ReportActions report={report} crb={crb} sheet={sheet} />}
        {showReset && (
          <button className="btn btn--ghost btn--small" onClick={onReset}>New application</button>
        )}
      </div>
    </header>
  )
}

/* ----------------------------------------------------- signature: the gauge
   AA→JJ is the instrument the whole product turns on: it decides whether a
   statement is ever read. It stays on screen for the length of the session. */
export function BandGauge({ crb }) {
  if (!crb) return null
  const current = crb.gate.band
  const floor = crb.gate.minimum_band
  const floorAt = BANDS.indexOf(floor)
  const currentAt = BANDS.indexOf(current)
  const passes = currentAt >= 0 && floorAt >= 0 && currentAt <= floorAt

  return (
    <div className="gauge">
      <div className="gauge__head">
        <span className="eyebrow">Bureau band</span>
        <span className="gauge__value">{current}</span>
      </div>
      <ol className="gauge__scale">
        {BANDS.map((b, i) => (
          <li
            key={b}
            data-pass={floorAt >= 0 && i <= floorAt}
            data-floor={b === floor}
            data-current={b === current}
            title={`${b}${b === floor ? ' — policy floor' : ''}${b === current ? ' — this applicant' : ''}`}
          >
            <i />
          </li>
        ))}
      </ol>
      <div className="gauge__foot">
        <span>AA</span>
        <span>floor <b>{floor}</b> · {passes ? 'clears' : 'below'}</span>
        <span>JJ</span>
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------- rail
   The only navigation in the product. The previous build shipped this rail
   AND a horizontal nav listing the identical seven sections. */
export function Rail({ stage, crb, report, files = {} }) {
  const steps = [
    ['01', 'Credit bureau check'],
    ['02', 'Band gate'],
    ['03', 'Statement analysis'],
    ['04', 'Review decision'],
  ]
  const order = { crb: 0, crbLoading: 0, gate: 2, blocked: 1, stmtLoading: 2, done: 3 }
  const at = order[stage] ?? 0

  /* Grouped, because the groups are the hierarchy: what you decide on,
     versus what you check when you want to challenge the decision. */
  const groups = [
    ['Findings', [
      ...(report?.name_check?.needs_review ? [['name-check', '⚠ Name check']] : []),
      ['overview', 'Decision'], ['revenue', 'Revenue'], ['monthly', 'Monthly flow'], ['risk', 'Risk flags']]],
    ['Audit trail', [['exclusions', 'Exclusions'], ['disclosures', 'Disclosures'], ['bureau', 'Bureau detail'], ['assumptions', 'Assumptions']]],
  ]

  return (
    <aside className="rail">
      <div className="rail__group">
        <span className="eyebrow">Process</span>
        <ol className="steps">
          {steps.map(([n, label], i) => (
            <li key={n} data-state={stage === 'blocked' && i > 1 ? 'todo'
                                    : i < at ? 'done' : i === at ? 'active' : 'todo'}>
              <b>{i < at ? '✓' : n}</b>
              <span>{label}</span>
            </li>
          ))}
        </ol>
      </div>

      {report && stage === 'done' && groups.map(([label, links]) => (
        <nav className="rail__group" key={label} aria-label={label}>
          <span className="eyebrow">{label}</span>
          <div className="railnav">
            {links.map(([id, text]) => <a key={id} href={`#${id}`}>{text}</a>)}
          </div>
        </nav>
      ))}

      {crb && <BandGauge crb={crb} />}

      {(crb || report) && (
        <div className="rail__meta">
          {crb && (
            <div>
              <dt>Existing debt</dt>
              <dd>{money(crb.exposure.total_open_balance)} RWF</dd>
            </div>
          )}
          {report && (
            <>
              <div>
                <dt>Statement period</dt>
                <dd>
                  {report.account.period_start?.slice(0, 10)} → {report.account.period_end?.slice(0, 10)}
                  {report.account.freshness?.period_days != null &&
                    ` (${report.account.freshness.period_days}d)`}
                </dd>
              </div>
              {report.account.freshness?.generated_age_days != null && (
                <div>
                  <dt>Generated</dt>
                  <dd data-tone={report.account.freshness.generated_ok === false ? 'flag' : undefined}>
                    {report.account.freshness.generated_age_days}d ago
                  </dd>
                </div>
              )}
              <div>
                <dt>Source</dt>
                <dd>{files.statement} · {report.meta?.pages} pp</dd>
              </div>
            </>
          )}
        </div>
      )}
    </aside>
  )
}

/* -------------------------------------------------------------------- tier
   Three weights on the page: the decision, the findings, the audit trail.
   Previously all nine sections were identical white cards. */
export function Tier({ id, label, note, recessed, children }) {
  return (
    <section className={recessed ? 'tier tier--audit' : 'tier'} id={id}>
      <div className="tier__bar">
        <h3>{label}</h3>
        {note && <span>{note}</span>}
      </div>
      {children}
    </section>
  )
}

/* -------------------------------------------------------------- empty state
   Several statements may be assessed together: more than one export of the
   same MoMo wallet, a MoMo wallet and a bank account, or both. Files are
   staged with the provider that issued each, then sent as one request and
   come back as one combined outcome. */
export function EmptyState({ onFile, busy, step, disabled = false }) {
  const input = useRef(null)
  const [over, setOver] = useState(false)
  const [providers, setProviders] = useState([])
  const [provider, setProvider] = useState('')
  const [staged, setStaged] = useState([])

  // The provider is chosen, not guessed. An operator knows which institution
  // issued the file; they cannot know which export version it is, so the
  // format within a provider is still detected server-side.
  useEffect(() => {
    let live = true
    fetchProviders()
      .then((list) => {
        if (!live) return
        setProviders(list)
        if (list.length === 1) setProvider(list[0].value)
      })
      .catch(() => { /* select stays empty; the API reports the real error */ })
    return () => { live = false }
  }, [])

  const labelOf = (v) => providers.find((p) => p.value === v)?.label ?? v

  const stage = (list) => {
    if (!provider) return
    const pdfs = [...(list || [])]
    if (!pdfs.length) return
    setStaged((s) => {
      const seen = new Set(s.map((x) => `${x.file.name}|${x.file.size}`))
      return [...s, ...pdfs.filter((f) => !seen.has(`${f.name}|${f.size}`)).map((file) => ({ file, provider }))]
    })
  }

  const run = () => !disabled && staged.length && onFile(staged)

  return (
    <>
      <div
        className="drop"
        data-over={over}
        onDragOver={(e) => { e.preventDefault(); setOver(true) }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); stage(e.dataTransfer.files) }}
      >
        {step && <span className="eyebrow">{step}</span>}
        <h3 style={{ marginTop: step ? 8 : 0 }}>Drop the statements</h3>
        <p>
          The original PDFs from the provider. Add one or more MTN MoMo files, Bank of Kigali
          files, or both — they are assessed together into one outcome. Scans and screenshots
          have no text layer and cannot be read.
        </p>

        <label className="field">
          <span>Which provider issued the next file(s)?</span>
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            disabled={busy || !providers.length}
          >
            <option value="">Select a provider…</option>
            {providers.map((p) => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
          </select>
        </label>

        <button
          className={staged.length ? 'btn btn--ghost' : 'btn'}
          onClick={() => input.current?.click()}
          disabled={busy || !provider}
        >
          {!provider ? 'Select a provider first' : staged.length ? 'Add more statements' : 'Choose statements'}
        </button>
        <input
          ref={input}
          type="file"
          accept="application/pdf"
          multiple
          hidden
          onChange={(e) => { stage(e.target.files); e.target.value = '' }}
        />

        {staged.length > 0 && (
          <div className="staged">
            <ul>
              {staged.map((s, i) => (
                <li key={`${s.file.name}|${s.file.size}`}>
                  <span className="staged__name">{s.file.name}</span>
                  <select
                    aria-label={`Provider for ${s.file.name}`}
                    value={s.provider}
                    disabled={busy}
                    onChange={(e) => setStaged((all) => all.map((x, j) => (j === i ? { ...x, provider: e.target.value } : x)))}
                  >
                    {providers.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
                  </select>
                  <button
                    className="btn btn--ghost btn--small"
                    disabled={busy}
                    onClick={() => setStaged((all) => all.filter((_, j) => j !== i))}
                    aria-label={`Remove ${s.file.name}`}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            <p className="muted staged__note">
              {new Set(staged.map((s) => s.provider)).size > 1
                ? `${[...new Set(staged.map((s) => labelOf(s.provider)))].join(' + ')}: each account is analysed on its own and the monthly figures are combined. Transfers between the applicant's own accounts are counted once.`
                : staged.length > 1
                  ? 'Files from the same account are merged and overlapping rows are counted once. Files from different accounts are combined.'
                  : 'Add more files to combine them, or assess this one on its own.'}
            </p>
            <button className="btn" onClick={run} disabled={busy || disabled}>
              {staged.length === 1 ? 'Assess statement' : `Assess ${staged.length} statements together`}
            </button>
          </div>
        )}
      </div>

      <div className="specs">
        <div>
          <h4>What gets deducted</h4>
          <p>Self-transfers, confirmed lender disbursements, and any single credit above the large-transaction threshold.</p>
        </div>
        <div>
          <h4>What gets flagged</h4>
          <p>Same-day pass-through, agent float, round-number income, existing borrowing, and concentration.</p>
        </div>
        <div>
          <h4>What never happens</h4>
          <p>No figure is inferred. Anything the statement cannot prove is reported as unresolved, not guessed.</p>
        </div>
      </div>

      <div className="panel ghost-table">
        <div className="panel__head">
          <div>
            <span className="eyebrow">Awaiting statement</span>
            <h3 style={{ marginTop: 6 }}>Net revenue summary</h3>
            <p>Month, gross inflows, each exclusion, and the net figure will appear here.</p>
          </div>
        </div>
        <div className="panel__body">
          <table>
            <thead>
              <tr>
                <th>Month</th><th className="r">Gross</th><th className="r">Self-transfers</th>
                <th className="r">Loans</th><th className="r">Large txns</th><th className="r">Net</th>
              </tr>
            </thead>
            <tbody>
              {[0, 1, 2, 3].map((i) => (
                <tr key={i}>
                  <td><div className="ghost-bar ghost-bar--thin" /></td>
                  <td><div className="ghost-bar ghost-bar--wide" /></td>
                  <td><div className="ghost-bar ghost-bar--mid" /></td>
                  <td><div className="ghost-bar ghost-bar--mid" /></td>
                  <td><div className="ghost-bar ghost-bar--mid" /></td>
                  <td><div className="ghost-bar ghost-bar--wide" /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ verdict */
export function AssessmentStatus({ report }) {
  const flags = report.red_flags ?? []
  const severe = flags.filter((f) => f.severity === 'severe').length
  const elevated = flags.filter((f) => f.severity === 'elevated').length
  const assessed = report.summary?.revenue_assessed !== false
  const capabilityLimits = report.disclosures?.capability_notes?.length ?? 0
  const incomplete = report.meta?.parse_complete === false || report.disclosures?.parse_complete === false

  let tone = 'clean'
  let title = 'ASSESSED — CLEAN'
  let copy = 'The statement was parsed and no severe or elevated risk findings were raised.'
  if (!assessed) {
    tone = 'severe'
    title = 'NOT ASSESSED'
    copy = report.summary?.not_assessed_reason || 'The statement does not provide enough evidence to assess revenue.'
  } else if (severe > 0) {
    tone = 'severe'
    title = 'REVIEW REQUIRED'
    copy = `${severe} severe finding${severe === 1 ? '' : 's'} require review before using the revenue figure for a credit decision.`
  } else if (elevated > 0) {
    tone = 'elevated'
    title = 'ASSESSED — REVIEW'
    copy = `${elevated} elevated finding${elevated === 1 ? '' : 's'} should be reviewed before relying on the assessment.`
  } else if (incomplete) {
    tone = 'elevated'
    title = 'REVIEW REQUIRED'
    copy = 'The statement could not be fully reconciled. Review the data-quality notes before relying on the result.'
  }

  /* Sentence case, not shouted caps — the tone is carried by the tint and the
     mark, so the words don't also have to raise their voice. */
  const label = { 'ASSESSED — CLEAN': 'Assessed — no findings', 'NOT ASSESSED': 'Not assessed',
    'REVIEW REQUIRED': 'Review required', 'ASSESSED — REVIEW': 'Assessed — review advised' }[title] ?? title

  return (
    <section className="status" data-tone={tone}>
      <div className="status__mark" aria-hidden="true">{tone === 'clean' ? '✓' : '!'}</div>
      <div className="status__copy">
        <b>{label}</b>
        <p>{copy}</p>
      </div>
      <div className="status__meta">
        {capabilityLimits > 0 ? `${capabilityLimits} format limit${capabilityLimits === 1 ? '' : 's'}` : ''}
      </div>
    </section>
  )
}

export function Verdict({ report }) {
  const s = report.summary
  const d = report.disclosures
  const severe = report.red_flags.filter((f) => f.severity === 'severe').length
  const elevated = report.red_flags.filter((f) => f.severity === 'elevated').length
  const exclusions = Math.max((s.totals.gross_inflows ?? 0) - (s.totals.net_revenue ?? 0), 0)

  return (
    <>
      <AssessmentStatus report={report} />
      <section className="decision">
        <div>
          <span className="eyebrow">Net revenue · period total</span>
          {s.revenue_assessed === false ? (
            <>
              <div className="decision__figure" data-tone="flag">Not assessed</div>
              <p className="decision__sub">
                {s.not_assessed_reason} Gross credits were {money(s.totals.gross_inflows)} RWF.
              </p>
            </>
          ) : (
            <>
              <div className="decision__figure">{money(s.totals.net_revenue)}<small>RWF</small></div>
              <p className="decision__sub">
                {money(exclusions)} excluded from {money(s.totals.gross_inflows)} gross inflows.
              </p>
            </>
          )}
        </div>

        <hr />

        <div>
          <span className="eyebrow">Full-month run rate</span>
          <div className="decision__second">{money(s.full_month_run_rate)}<small>RWF</small></div>
          <p className="decision__sub">
            {s.full_months.length === 0
              ? 'No complete month in this period.'
              : `Based on ${s.full_months.length} full month${s.full_months.length === 1 ? '' : 's'} only.${s.partial_months.length ? ` ${s.partial_months.length} partial month${s.partial_months.length === 1 ? '' : 's'} excluded.` : ''}`}
          </p>
        </div>

        <hr />

        <div>
          <span className="eyebrow">Risk findings</span>
          <div className="decision__counts">
            <span>
              <b data-sev="severe" data-on={severe > 0}>{severe}</b>
              <small>severe</small>
            </span>
            <span>
              <b data-sev="elevated" data-on={elevated > 0}>{elevated}</b>
              <small>elevated</small>
            </span>
          </div>
          <p className="decision__sub">
            {d.total_transaction_count} transactions read · {d.counted_inflow_count} inflows counted
          </p>
        </div>
      </section>
    </>
  )
}

export function RevenueWaterfall({ report }) {
  const t = report.summary.totals
  const gross = t.gross_inflows || 0
  const items = [
    ['Self-transfers', t.self_transfers || 0, 'self'],
    ['Loan inflows', t.loan_inflows || 0, 'loan'],
    ['Bank / e-wallet credits', t.institutional_credits || 0, 'institutional'],
    ['Large transactions', t.large_transactions || 0, 'large'],
  ]
  const max = Math.max(gross, 1)
  return (
    <section className="waterfall panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Why this number?</span>
          <h3 style={{ marginTop: 6 }}>Revenue calculation</h3>
          <p>Every deduction is shown separately. Net revenue is the gross inflow that remains after the configured exclusions.</p>
        </div>
        <span className="formula">Gross − exclusions = net</span>
      </div>
      <div className="panel__body">
        <div className="waterfall__gross">
          <div><span>Gross inflows</span><b className="num">{money(gross)} RWF</b></div>
          <div className="waterfall__track"><i style={{ width: '100%' }} /></div>
        </div>
        <div className="waterfall__deductions">
          {items.map(([label, amount, key]) => (
            <div className="waterfall__item" key={key}>
              <div className="waterfall__label">
                <span>− {label}</span>
                <b className={amount ? 'num' : 'num zero'}>{money(amount)} RWF</b>
              </div>
              {/* a zero deduction gets no bar at all; a small one gets a small
                  bar, never the 4px dot that read as a rendering fault */}
              <div className="waterfall__track">
                {amount > 0 && <i data-kind={key} style={{ width: `${Math.min((amount / max) * 100, 100)}%` }} />}
              </div>
            </div>
          ))}
        </div>
        <div className="waterfall__net">
          <div><span>Net revenue</span><b className="num">{safelyMoney(t.net_revenue)} RWF</b></div>
          <span className="waterfall__share">{gross ? `${((t.net_revenue / gross) * 100).toFixed(1)}% retained` : '—'}</span>
        </div>
      </div>
    </section>
  )
}

function safelyMoney(value) { return money(value) }

export function StatementHealth({ report }) {
  const d = report.disclosures || {}
  const meta = report.meta || {}
  const rejected = Math.max((d.total_transaction_count ?? 0) - (meta.rows_parsed ?? d.total_transaction_count ?? 0), 0)
  const checks = [
    ['Parsing', meta.parse_complete !== false && d.parse_complete !== false, meta.parse_complete !== false ? `${meta.rows_parsed ?? d.total_transaction_count ?? 0} rows parsed` : 'Incomplete'],
    ['Transaction coverage', d.declared_transaction_count == null || d.total_transaction_count === d.declared_transaction_count, d.declared_transaction_count != null ? `${d.total_transaction_count} / ${d.declared_transaction_count} declared` : `${d.total_transaction_count} transactions read`],
    ['Rejected rows', rejected === 0, `${rejected} rejected`],
    ['Counterparty data', d.distinct_senders_total != null, d.distinct_senders_total != null ? `${d.distinct_senders_total} distinct senders` : 'Not available'],
  ]
  const limitations = report.disclosures?.capability_notes?.length ?? 0
  const quality = !checks[0][1] || !checks[1][1] ? 'LIMITED' : limitations >= 2 ? 'MODERATE' : 'HIGH'
  const tone = quality === 'HIGH' ? 'good' : quality === 'MODERATE' ? 'warn' : 'bad'
  return (
    <section className="panel health-panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Data quality</span>
          <h3 style={{ marginTop: 6 }}>Statement health</h3>
          <p>Evidence quality describes what the document can support; it is not a risk score.</p>
        </div>
        <span className="quality-badge" data-tone={tone}>{quality} EVIDENCE QUALITY</span>
      </div>
      <div className="panel__body">
        <div className="health-grid">
          {checks.map(([label, ok, note]) => (
            <div className="health-item" key={label} data-ok={ok}>
              <span className="health-icon">{ok ? '✓' : '!'}</span>
              <div><b>{label}</b><span>{note}</span></div>
            </div>
          ))}
        </div>
        {limitations > 0 && <div className="health-note"><b>{limitations} format limitation{limitations === 1 ? '' : 's'}.</b> Some checks were unavailable because the statement format does not carry the required evidence. See “Format limits” below.</div>}
      </div>
    </section>
  )
}

/* ------------------------------------------- signature: pass-through ribbon */
export function FlowRibbon({ flow }) {
  const max = Math.max(...flow.map((f) => Math.max(f.inflow, f.outflow)), 1)
  return (
    <section className="ribbon">
      <div className="ribbon__head">
        <div>
          <span className="eyebrow">Retained vs moved on</span>
          <h3 style={{ fontSize: 16, marginTop: 6 }}>Where the money went</h3>
        </div>
      </div>
      <p className="ribbon__lede">
        Each month draws inflow above outflow on the same scale. The green segment is what
        stayed. If the two bars match, the account is moving money rather than earning it.
      </p>

      {flow.map((f) => {
        const inW = (f.inflow / max) * 100
        const outW = (f.outflow / max) * 100
        const keepW = (Math.max(f.retained, 0) / max) * 100
        return (
          <div className="ribbon__row" key={f.month} data-heavy={f.passthrough_ratio >= 0.9}>
            <span className="ribbon__month">{f.month}</span>
            <div className="ribbon__track">
              <div className="ribbon__in" style={{ width: `${inW}%` }} />
              <div className="ribbon__keep" style={{ width: `${keepW}%` }} />
              <div className="ribbon__out" style={{ width: `${outW}%` }} />
            </div>
            <span className="ribbon__pct">{pct(f.passthrough_ratio)} out</span>
          </div>
        )
      })}

      <div className="ribbon__legend">
        <span><i style={{ background: '#C6CDD5' }} />Inflow</span>
        <span><i style={{ background: 'var(--clear)' }} />Retained</span>
        <span><i style={{ background: '#7C8894' }} />Outflow</span>
      </div>
    </section>
  )
}

/* ------------------------------------------------------ loan eligibility */
const pctLabel = (rate, fallback) =>
  `${((rate ?? fallback / 100) * 100).toFixed(0)}%`

export function LoanEligibility({ report }) {
  const e = report.summary?.loan_eligibility
  if (!e) return null
  const fmt = (n) => n == null ? '—' : `${money(n)} RWF`
  const statusLabel = {
    eligible: 'Eligible',
    not_eligible: 'Not eligible',
    not_assessed: 'Not assessed',
  }[e.status] || 'Not assessed'
  const tone = e.status === 'eligible' ? 'good' : e.status === 'not_eligible' ? 'warn' : 'bad'

  return (
    <section className="panel eligibility-panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Product eligibility</span>
          <h3 style={{ marginTop: 6 }}>How much can be disbursed?</h3>
          <p>
            Each month is scaled to a 30-day equivalent, averaged over {e.months_required ?? 3} months,
            then reduced by the open-PA monthly EMI. Statement exclusions are applied before the
            EMI deduction.
          </p>
        </div>
        <span className="quality-badge" data-tone={tone}>{statusLabel.toUpperCase()}</span>
      </div>

      <div className="panel__body">
        <div className="metrics">
          <div>
            <span className="eyebrow">Average monthly statement net revenue</span>
            <div className="num metric__figure">{fmt(e.average_monthly_inflow)}</div>
            <div className="metric__sub">After statement exclusions · {e.months_used?.join(' · ') || '—'}</div>
          </div>
          <div>
            <span className="eyebrow">Monthly EMI</span>
            <div className="num metric__figure" data-tone={e.monthly_emi == null ? 'flag' : undefined}>{fmt(e.monthly_emi)}</div>
            <div className="metric__sub">From open performing (PA) bureau accounts only</div>
          </div>
          <div>
            <span className="eyebrow">Net inflow</span>
            <div className="num metric__figure">{fmt(e.net_inflow)}</div>
            <div className="metric__sub">Average statement net revenue − open PA monthly EMI</div>
          </div>
          <div>
            <span className="eyebrow">Net revenue</span>
            <div className="num metric__figure">{fmt(e.net_revenue)}</div>
            <div className="metric__sub">{pctLabel(e.net_revenue_rate, 15)} of net inflow · {e.customer_type === 'existing' ? 'existing' : 'new'} customer</div>
          </div>
          <div>
            <span className="eyebrow">Fee cap</span>
            <div className="num metric__figure">{fmt(e.fee_cap)}</div>
            <div className="metric__sub">{pctLabel(e.fee_cap_rate, 15)} of net revenue · {e.customer_type === 'existing' ? 'existing' : 'new'} customer</div>
          </div>
          <div>
            <span className="eyebrow">Requested amount</span>
            <div className="num metric__figure" data-tone={e.requested_amount == null ? 'flag' : undefined}>{e.requested_amount == null ? '—' : fmt(e.requested_amount)}</div>
            <div className="metric__sub">Customer request entered before statement parsing</div>
          </div>
          <div>
            <span className="eyebrow">Eligible amount</span>
            <div className="num metric__figure" data-tone={e.loan_amount_eligibility == null ? 'flag' : undefined}>
              {e.loan_amount_eligibility == null ? '—' : fmt(e.loan_amount_eligibility)}
            </div>
            <div className="metric__sub">What the fee-cap band supports</div>
          </div>
          <div className="metric--sanction">
            <span className="eyebrow">Sanctioned amount</span>
            <div className="num metric__figure" data-tone={e.sanctioned_amount == null ? 'flag' : undefined}>
              {e.sanctioned_amount == null ? '—' : fmt(e.sanctioned_amount)}
            </div>
            <div className="metric__sub">
              {e.sanctioned_amount == null
                ? 'Nothing is sanctioned unless the applicant is eligible'
                : e.sanction_basis === 'requested_amount'
                  ? 'The request is below the eligible amount, so the request stands'
                  : e.sanction_basis === 'eligible_amount_capped'
                    ? 'The request exceeds the eligible amount, so the band caps it'
                    : 'No request entered · the full eligible amount'}
            </div>
          </div>
        </div>

        {e.status === 'not_assessed' && (
          <div className="health-note" style={{ marginTop: 16 }}>
            <b>{e.reason}</b>
          </div>
        )}
        {e.status === 'not_eligible' && (
          <div className="health-note" style={{ marginTop: 16 }}>
            <b>{e.reason}</b>
          </div>
        )}
        {e.band_overlap_resolved && (
          <div className="health-note" style={{ marginTop: 16 }}>
            <b>Overlapping policy bands.</b> {e.band_overlap_note}
          </div>
        )}
        {e.requested_amount_cap_applied && (
          <div className="health-note" style={{ marginTop: 16 }}>
            <b>Requested amount cap applied.</b> The configured eligibility amount is higher than the customer's request, so the final eligible amount does not exceed the requested amount.
          </div>
        )}

        {e.monthly_inflows?.length > 0 && (
          <>
            <div className="eyebrow" style={{ marginTop: 22, marginBottom: 8 }}>
              Months considered
            </div>
            <p className="muted" style={{ fontSize: 12.5, margin: '0 0 10px', maxWidth: '84ch' }}>
              Every month in the statement and what became of it. A month is scaled to a
              30-day equivalent by multiplying by 30 ÷ days covered. The floor is{' '}
              <b>{e.min_days_for_run_rate ?? 15} days</b>, and it is set by that
              multiplier rather than by the day count: at{' '}
              {e.min_days_for_run_rate ?? 15} days it is{' '}
              <b>{e.max_scale_factor ?? 2}×</b>, so no month is averaged on the strength
              of more than doubling it — the observed period is still at least as long as
              the part being inferred. At 10 days it would be 3.0× and at 5 days 6.0×,
              where the figure is mostly arithmetic rather than observed trade.
            </p>
            <div className="table-scroll">
              <table>
                <thead><tr>
                  <th>Month</th><th className="r">Days</th><th className="r">Net revenue</th>
                  <th className="r">Scale</th><th className="r">Per 30 days</th><th>Status</th>
                </tr></thead>
                <tbody>
                  {(e.months_considered || e.monthly_inflows || []).map((m) => {
                    const used = m.status === 'averaged' || !m.status
                    return (
                      <tr key={m.month} data-excluded={!used || undefined}>
                        <td className="num">
                          {m.month}
                          {m.is_full_month === false && <span className="tag-partial" style={{ marginLeft: 8 }}>partial</span>}
                        </td>
                        <td className={`num r ${used ? '' : 'neg'}`}>{m.days_covered ?? '—'}</td>
                        <td className="num r muted">{fmt(m.net_revenue)}</td>
                        <td className="num r muted">{m.scale_factor ? `×${m.scale_factor}` : '—'}</td>
                        <td className="num r">{used ? <b>{fmt(m.net_revenue_30d)}</b> : <span className="muted">{fmt(m.net_revenue_30d)}</span>}</td>
                        <td className={used ? 'muted' : 'neg'}>
                          {used ? 'averaged' : m.status === 'excluded_too_short' ? 'excluded · too short' : 'outside window'}
                          {m.reason && <div className="metric__sub">{m.reason}</div>}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
                <tfoot>
                  <tr>
                    <td colSpan={4}>
                      <b>Average of the {e.months_averaged ?? (e.monthly_inflows || []).length} months averaged</b>
                    </td>
                    <td className="num r"><b>{fmt(e.average_monthly_net_revenue)}</b></td>
                    <td />
                  </tr>
                </tfoot>
              </table>
            </div>
            <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>{e.basis_note}</p>
          </>
        )}

        {(
          <>
            <div className="eyebrow" style={{ marginTop: 22, marginBottom: 8 }}>Eligibility bands</div>
            <table>
              <thead><tr><th>Min fee cap</th><th>Max fee cap</th><th className="r">Loan amount</th></tr></thead>
              <tbody>
                {(e.bands || []).map((b) => {
                  const matched = e.matched_band && b.min_fee_cap === e.matched_band.min_fee_cap && b.max_fee_cap === e.matched_band.max_fee_cap
                  return (
                    <tr key={`${b.min_fee_cap}-${b.max_fee_cap}`} data-current={matched}>
                      <td className="num">{fmt(b.min_fee_cap)}</td>
                      <td className="num">{b.max_fee_cap == null ? 'and above' : fmt(b.max_fee_cap)}</td>
                      <td className="num r"><b>{fmt(b.loan_amount)}</b>{matched && <span className="tag-partial" style={{ marginLeft: 8 }}>selected</span>}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            <p className="muted" style={{ fontSize: 12.5, marginTop: 10 }}>
              {e.customer_type === 'existing' ? 'Existing' : 'New'}-customer sanction table. Both ends of each range are inclusive. A fee cap
              below {money(e.fee_cap_floor ?? 50000)} RWF
              {e.fee_cap_ceiling != null && <> or above {money(e.fee_cap_ceiling)} RWF</>} is
              outside policy and refers to a credit officer rather than being assigned the
              nearest band.
            </p>
          </>
        )}
      </div>
    </section>
  )
}

/* ------------------------------------------------------------ summary table */
export function SummaryTable({ summary }) {
  const t = summary.totals
  const cols = [
    ['self_transfers', '(−) Self-transfers'],
    ['loan_inflows', '(−) Loan inflows'],
    ['institutional_credits', '(−) Bank / e-wallet'],
    ['large_transactions', '(−) Large txns'],
  ]
  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Per month · RWF</span>
          <h3 style={{ marginTop: 6 }}>Net revenue summary</h3>
          <p>
            Net = gross inflows less self-transfers, confirmed loan disbursements,
            bank and e-wallet credits, and large transactions.
          </p>
        </div>
      </div>
      <div className="panel__body">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Month</th>
                <th className="r">Gross inflows</th>
                {cols.map(([, label]) => <th key={label} className="r">{label}</th>)}
                <th className="r">Net revenue</th>
                <th className="r">Inflow count</th>
              </tr>
            </thead>
            <tbody>
              {summary.rows.map((r) => (
                <tr key={r.month}>
                  <td className="num">
                    {r.month}
                    {!r.is_full_month && <span className="tag-partial">partial · {r.days_covered}d</span>}
                  </td>
                  <td className="num r">{money(r.gross_inflows)}</td>
                  {cols.map(([key]) => (
                    <td key={key} className={`num r ${r[key] ? 'neg' : 'muted'}`}>{money(r[key])}</td>
                  ))}
                  <td className="num r"><b>{money(r.net_revenue)}</b></td>
                  <td className="num r muted">{r.inflow_count}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td>Total</td>
                <td className="num r">{money(t.gross_inflows)}</td>
                {cols.map(([key]) => <td key={key} className="num r">{money(t[key])}</td>)}
                <td className="num r">{money(t.net_revenue)}</td>
                <td className="num r">{t.inflow_count}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
    </section>
  )
}


/* -------------------------------------------------------- large / loan lists */
export function DetailTables({ details }) {
  const large = details.large_transactions
  const loans = details.loan_inflows
  const inst = details.institutional_credits
  return (
    <>
      {inst?.items?.length > 0 && (
        <section className="panel">
          <div className="panel__head">
            <div>
              <span className="eyebrow">Deduction · bank / e-wallet</span>
              <h3 style={{ marginTop: 6 }}>Institutional credits excluded</h3>
              <p>{inst.note}</p>
            </div>
          </div>
          <div className="panel__body">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Date</th><th>Txn ID</th><th>Counterparty</th><th>Number</th><th>Type</th>
                    <th className="r">Amount</th><th className="r">Fee</th><th className="r">Balance after</th>
                  </tr>
                </thead>
                <tbody>
                  {inst.items.map((r) => (
                    <tr key={`${r.account ?? ''}|${r.txn_id}`}>
                      <td className="num">{(r.timestamp || '').slice(0, 10)}</td>
                      <td className="num muted">{r.txn_id}</td>
                      <td>{r.counterparty}</td>
                      <td className="num muted">{r.msisdn}</td>
                      <td className="muted">{r.type}</td>
                      <td className="num r neg">{money(r.amount)}</td>
                      <td className="num r muted">{r.fee != null ? money(r.fee) : '—'}</td>
                      <td className="num r muted">{r.balance_after != null ? money(r.balance_after) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}
      {large.inflows.length > 0 && (
        <section className="panel">
          <div className="panel__head">
            <div>
              <span className="eyebrow">Deduction · above threshold</span>
              <h3 style={{ marginTop: 6 }}>Large transactions excluded in full</h3>
              <p>Every inflow above the threshold, itemised. Outflows above it are listed for context.</p>
            </div>
          </div>
          <div className="panel__body">
            <table>
              <thead>
                <tr><th>Date</th><th>Counterparty</th><th>Number</th><th>Type</th><th className="r">Amount</th></tr>
              </thead>
              <tbody>
                {large.inflows.map((r) => (
                  <tr key={`${r.account ?? ''}|${r.txn_id}`}>
                    <td className="num">{shortDate(r.timestamp)}</td>
                    <td>{r.counterparty}</td>
                    <td className="num muted">{r.msisdn}</td>
                    <td className="muted">{r.type}</td>
                    <td className="num r neg">{money(r.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {loans.ledger.length > 0 && (
        <section className="panel">
          <div className="panel__head">
            <div>
              <span className="eyebrow">Deduction · borrowing</span>
              <h3 style={{ marginTop: 6 }}>Lender ledger</h3>
              <p>Disbursements are deducted from revenue. Repayments are shown because they prove the cycle and reveal borrowing outside the period.</p>
            </div>
          </div>
          <div className="panel__body">
            <table>
              <thead>
                <tr><th>Date</th><th>Direction</th><th>Counterparty</th><th className="r">Amount</th></tr>
              </thead>
              <tbody>
                {loans.ledger.map((r) => (
                  <tr key={`${r.account ?? ''}|${r.txn_id}`}>
                    <td className="num">{shortDate(r.timestamp)}</td>
                    <td style={{ color: r.direction === 'disbursement' ? 'var(--passthru)' : 'var(--ink-3)' }}>
                      {r.direction}
                    </td>
                    <td>{r.counterparty}</td>
                    <td className="num r">{money(r.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {loans.pattern_matches?.length > 0 && (
        <section className="panel">
          <div className="panel__head">
            <div>
              <span className="eyebrow">Deduction · inferred</span>
              <h3 style={{ marginTop: 6 }}>Pattern-based loan cycles</h3>
              <p>Pattern matches are deducted as loan inflows and shown with the repayment evidence that triggered the classification.</p>
            </div>
          </div>
          <div className="panel__body">
            <table>
              <thead>
                <tr><th>Date</th><th>Counterparty</th><th>Basis</th><th className="r">Principal</th><th className="r">Repayment</th><th className="r">n</th></tr>
              </thead>
              <tbody>
                {loans.pattern_matches.map((r) => (
                  <tr key={`cycle-${r.account ?? ''}|${r.txn_id}`}>
                    <td className="num">{shortDate(r.timestamp)}</td>
                    <td>{r.counterparty}</td>
                    <td className="muted">{r.basis}</td>
                    <td className="num r neg">{money(r.principal)}</td>
                    <td className="num r">{money(r.repayment_total_within_window)}</td>
                    <td className="num r muted">{r.repayment_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {details.self_transfers.possible_for_review.length > 0 && (
        <section className="panel">
          <div className="panel__head">
            <div>
              <span className="eyebrow">Deduction · own accounts</span>
              <h3 style={{ marginTop: 6 }}>Possible self-transfers — for review</h3>
              <p>{details.self_transfers.note}</p>
            </div>
          </div>
          <div className="panel__body">
            <table>
              <thead>
                <tr>
                  <th>Counterparty</th><th className="r">In</th><th className="r">n</th>
                  <th>Basis</th><th>Why</th>
                </tr>
              </thead>
              <tbody>
                {details.self_transfers.possible_for_review.slice(0, 14).map((r) => (
                  <tr key={`${r.account ?? ''}|${r.txn_id}`}>
                    <td>{r.counterparty}<div className="num muted" style={{ fontSize: 11 }}>{r.msisdn}</div></td>
                    <td className="num r">{money(r.amount)}</td>
                    <td className="num r muted">1</td>
                    <td className="muted">—</td>
                    <td className="muted">{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  )
}

/* ------------------------------------------------------------- disclosures */
export function Disclosures({ d, cap }) {
  const check = d.minimum_transaction_check
  const items = [
    ['Total transactions read', `${d.total_transaction_count}`, d.parse_complete
      ? `Matches the ${d.declared_transaction_count} the statement declares.`
      : `Statement declares ${d.declared_transaction_count} — reconcile before use.`],
    ['Inflows counted toward revenue', `${d.counted_inflow_count}`, `${d.inflow_count} inflows less ${d.inflow_count - d.counted_inflow_count} excluded.`],
    ['Transactions above threshold', `${d.large_transaction_count}`, `${d.large_inflow_count} inflows, ${d.large_outflow_count} outflows.`],
    ['Distinct individuals sending money', `${d.distinct_individual_senders}`, `Of ${d.distinct_senders_total} senders; ${d.distinct_institution_senders} are institutions or merchants.`],
    ['Minimum-transaction check', check.status === 'not_assessed' ? 'Not assessed' : check.status.toUpperCase(),
      check.note || `Observed ${check.observed_inflow_count} against a threshold of ${check.threshold}.`],
    ['Active days with inflow', `${check.active_days}`, `Credit cap in force: ${money(cap)} RWF.`],
  ]

  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Policy checks</span>
          <h3 style={{ marginTop: 6 }}>Mandatory disclosures</h3>
          <p>Reported whether or not they change the outcome.</p>
        </div>
      </div>
      <div className="panel__body">
        <table>
          <tbody>
            {items.map(([k, v, note]) => (
              <tr key={k}>
                <td style={{ width: '30%' }}><b>{k}</b></td>
                <td className="num" style={{ width: '14%' }}>{v}</td>
                <td className="muted">{note}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <h4 style={{ margin: '22px 0 2px', fontSize: 13 }}>Month-concentration check</h4>
        <table>
          <thead>
            <tr>
              <th>Month</th><th className="r">Net</th><th>Largest counterparty</th>
              <th className="r">Share</th><th className="r">Top 3</th><th>Verdict</th>
            </tr>
          </thead>
          <tbody>
            {d.concentration.map((c) => (
              <tr key={c.month}>
                <td className="num">{c.month}</td>
                <td className="num r muted">{money(c.net_revenue)}</td>
                <td>{c.top_counterparties[0]?.counterparty ?? '—'}</td>
                <td className={`num r ${c.top1_share >= 0.2 ? 'neg' : ''}`}>{pct(c.top1_share)}</td>
                <td className="num r muted">{pct(c.top3_share)}</td>
                <td>
                  {c.flagged
                    ? <span className="pill" data-sev="elevated">Concentrated</span>
                    : <span className="muted">Broad</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {d.concentration.filter((c) => c.flagged).map((c) => (
          <p key={c.month} style={{ fontSize: 12.5, color: 'var(--ink-3)', margin: '10px 0 0' }}>
            <b className="num">{c.month}</b> — {c.reasons.join(' ')}
          </p>
        ))}
      </div>
    </section>
  )
}

/* ---------------------------------------------------------------- red flags */
export function RedFlags({ flags }) {
  const [open, setOpen] = useState(flags[0]?.code ?? null)
  return (
    <section style={{ marginBottom: 26 }}>
      <div style={{ marginBottom: 12 }}>
        <span className="eyebrow">Findings</span>
        <h3 style={{ fontSize: 16, marginTop: 6 }}>Risk findings</h3>
      </div>

      {flags.length === 0 && (
        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel__body" style={{ padding: '18px 20px' }}>
            <b>None observed.</b>{' '}
            <span className="muted">
              No pass-through, float, round-number, borrowing, concentration or profile
              deviation pattern crossed its threshold on this statement.
            </span>
          </div>
        </div>
      )}

      {flags.map((f) => (
        <article className="flag" key={`${f.code}|${f.account ?? ''}`} data-sev={f.severity}>
          <button
            className="flag__btn"
            aria-expanded={open === `${f.code}|${f.account ?? ''}`}
            onClick={() => setOpen(open === `${f.code}|${f.account ?? ''}` ? null : `${f.code}|${f.account ?? ''}`)}
          >
            <span>
              <span className="flag__title">{f.title}</span>
              <span className="flag__metric">{f.metric}</span>
            </span>
            <span className="pill" data-sev={f.severity}>{f.severity}</span>
          </button>

          {open === `${f.code}|${f.account ?? ''}` && (
            <div className="flag__body">
              <p>{f.summary}</p>
              <Evidence evidence={f.evidence} />
              <button className="btn btn--ghost btn--small flag__evidence-btn" onClick={() => document.getElementById('evidence-explorer')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>Open evidence explorer</button>
            </div>
          )}
        </article>
      ))}
    </section>
  )
}

const label = (k) => k.replace(/_/g, ' ')

/**
 * Render one evidence value. Shares arrive as floats below 1, amounts and
 * counts as integers, and some fields are plain sentences — passing a string
 * through a number formatter renders "NaN", so type is checked first.
 */
function value(v) {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'string') return v
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  if (typeof v === 'number') return Number.isInteger(v) ? money(v) : pct(v)
  // Nested structures used to fall through to String(v) and render as
  // "[object Object]". A cell should always say something a human can read.
  if (Array.isArray(v)) {
    if (!v.length) return '—'
    if (v.every((x) => x === null || typeof x !== 'object')) return v.map(value).join(' · ')
    return v.map(value).join(' · ')
  }
  if (typeof v === 'object') {
    const parts = Object.entries(v)
      .filter(([, x]) => x !== null && x !== undefined && x !== '' && typeof x !== 'object')
      .slice(0, 4)
      .map(([k, x]) => `${label(k)} ${value(x)}`)
    return parts.length ? parts.join(', ') : `${Object.keys(v).length} fields`
  }
  return String(v)
}

/** Columns whose every value is empty carry no information; drop them so a
 *  wide evidence table does not push the panel off the page. */
function usefulColumns(rows) {
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))]
  return keys.filter((k) => rows.some((r) => {
    const v = r[k]
    if (v === null || v === undefined || v === '' || v === false) return false
    if (Array.isArray(v) && !v.length) return false
    return true
  }))
}

/* ------------------------------------------------------------ identity gate */
export function IdentityCheck({ report }) {
  const id = report?.identity_check
  if (!id || id.status === 'match') return null
  // A name problem is shown, and checked off, in the name check panel.
  if (report?.name_check?.needs_review && id.status !== 'match_name_only') return null
  const bad = id.status === 'mismatch'
  return (
    <div className="notebox" data-tone={bad ? 'bad' : 'warn'} style={{ marginBottom: 14 }}>
      <b>
        {bad
          ? 'This statement is not the bureau applicant\u2019s.'
          : 'Statement holder only partly reconciles to the bureau file.'}
      </b>{' '}
      {id.note}
      <div className="metric__sub" style={{ marginTop: 8 }}>
        Bureau: <b>{id.bureau_name || '—'}</b> · Statement: <b>{id.statement_name || '—'}</b>
        {id.statement_msisdn && <> · {id.statement_msisdn}</>}
        {id.name_tokens_matched?.length > 0 && <> · matched on {id.name_tokens_matched.join(', ')}</>}
      </div>
    </div>
  )
}

/* ------------------------------------------------- edge cases & arithmetic */
export function EdgeCases({ report }) {
  const items = report?.edge_cases || []
  const [open, setOpen] = useState(null)
  if (!items.length) return null

  const counts = items.reduce((a, i) => ({ ...a, [i.severity]: (a[i.severity] || 0) + 1 }), {})
  const tone = counts.high ? 'bad' : counts.medium ? 'warn' : 'ok'

  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Edge cases</span>
          <h3 style={{ marginTop: 6 }}>Everything that qualifies the numbers above</h3>
          <p>
            Each of these also appears beside the figure it affects. Collected here so
            none of them can be missed by scrolling past the section that carries it.
          </p>
        </div>
        <span className="quality-badge" data-tone={tone}>
          {counts.high ? `${counts.high} MUST READ` : counts.medium ? `${counts.medium} TO REVIEW` : 'MINOR ONLY'}
        </span>
      </div>
      <div className="panel__body">
        {items.map((i) => {
          const sev = i.severity === 'high' ? 'severe' : i.severity === 'medium' ? 'elevated' : 'informational'
          const hasEvidence = Object.keys(i.evidence || {}).length > 0
          const id = `${i.code}|${i.account ?? ''}|${i.title}`
          return (
            <div key={id} className="flag" data-sev={sev}>
              <button className="flag__btn" onClick={() => hasEvidence && setOpen(open === id ? null : id)}>
                <span>
                  <span className="flag__title">{i.title}</span>
                  <span className="flag__metric">{i.detail}</span>
                </span>
                <span className="pill" data-sev={sev}>
                  {i.severity === 'high' ? 'must read' : i.severity === 'medium' ? 'review' : 'note'}
                </span>
              </button>
              {open === id && hasEvidence && (
                <div className="flag__body"><Evidence evidence={i.evidence} /></div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

export function Reconciliation({ report }) {
  const r = report?.reconciliation
  const [open, setOpen] = useState(false)
  if (!r) return null
  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Arithmetic</span>
          <h3 style={{ marginTop: 6 }}>Every figure re-derived from its own rows</h3>
          <p>{r.note}</p>
        </div>
        <span className="quality-badge" data-tone={r.all_figures_reconcile ? 'ok' : 'bad'}>
          {r.all_figures_reconcile ? `${r.checks_run}/${r.checks_run} RECONCILE` : `${r.checks_failed} FAILED`}
        </span>
      </div>
      <div className="panel__body">
        {!r.all_figures_reconcile && (
          <div className="notebox" data-tone="warn">
            <b>Do not use these numbers.</b> A published figure disagrees with the rows
            beneath it, which means a calculation bug rather than a data problem.
          </div>
        )}
        <button className="flag__evidence-btn linkish" onClick={() => setOpen(!open)}>
          {open ? 'Hide' : 'Show'} all {r.checks_run} checks
        </button>
        {open && (
          <table style={{ marginTop: 10 }}>
            <thead>
              <tr><th>Check</th><th className="r">Computed</th><th className="r">Re-derived</th><th>Result</th></tr>
            </thead>
            <tbody>
              {r.checks.map((c) => (
                <tr key={c.check}>
                  <td>{c.check}{c.detail && <div className="metric__sub">{c.detail}</div>}</td>
                  <td className="num r">{money(c.computed)}</td>
                  <td className="num r">{money(c.expected)}</td>
                  <td className={c.passes ? 'muted' : 'neg'}>{c.passes ? 'matches' : 'DIFFERS'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  )
}

export function Evidence({ evidence }) {
  if (!evidence) return null
  const entries = Object.entries(evidence)
  const tables = entries.filter(([, v]) => Array.isArray(v) && v.length && typeof v[0] === 'object')
  const lists = entries.filter(([, v]) => Array.isArray(v) && v.length && typeof v[0] !== 'object')
  const notes = entries.filter(([, v]) => typeof v === 'string')
  const scalars = entries.filter(([, v]) => typeof v === 'number' || typeof v === 'boolean')

  return (
    <>
      {(scalars.length > 0 || lists.length > 0) && (
        <p className="num" style={{ fontSize: 12, color: 'var(--ink-3)' }}>
          {[
            ...scalars.map(([k, v]) => `${label(k)}: ${value(v)}`),
            ...lists.map(([k, v]) => `${label(k)}: ${v.map(value).join(' – ')}`),
          ].join('   ·   ')}
        </p>
      )}

      {notes.map(([k, v]) => (
        <p key={k} style={{ fontSize: 12.5, color: 'var(--ink-3)', margin: '8px 0 0' }}>{v}</p>
      ))}
      {tables.map(([key, rows]) => (
        <div key={key} style={{ marginTop: 12 }}>
          <span className="eyebrow">{key.replace(/_/g, ' ')}</span>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>{usefulColumns(rows).map((c) => (
                  <th key={c} className={typeof rows[0][c] === 'number' ? 'r' : ''}>{label(c)}</th>
                ))}</tr>
              </thead>
              <tbody>
                {rows.slice(0, 8).map((row, i) => (
                  <tr key={i}>
                    {usefulColumns(rows).map((c) => (
                      <td key={c} className={typeof row[c] === 'number' ? 'num r' : 'num'}>{value(row[c])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {rows.length > 8 && (
            <p className="muted" style={{ fontSize: 12, margin: '6px 0 0' }}>
              {rows.length - 8} further rows in the API response.
            </p>
          )}
        </div>
      ))}
    </>
  )
}

/* ReportNav is gone. It listed the same seven sections as the rail, one below
   the other, with no signal about which was authoritative. The rail is now the
   only navigation in the product. */

/* ----------------------------------------------------------- report actions */
/**
 * Print and export work from the moment there is anything to print. They used
 * to be gated on the statement report, so a bureau-only file - including one
 * declined at the band gate, which never gets a statement - could not be
 * printed or exported at all.
 */
export function ReportActions({ report, crb, sheet }) {
  const payload = report ? { credit_bureau: crb, statement: report } : { credit_bureau: crb }
  const kind = report ? 'underwriting-report' : 'bureau-check'
  function downloadJson() {
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${kind}-${new Date().toISOString().slice(0, 10)}.json`
    a.click()
    URL.revokeObjectURL(url)
  }
  return (
    <div className="report-actions">
      <button className="btn btn--ghost btn--small" onClick={() => window.print()}>Print</button>
      <button className="btn btn--ghost btn--small" onClick={downloadJson}>Export JSON</button>
      {sheet && <SheetsAction sheet={sheet} />}
    </div>
  )
}

/**
 * Save-to-Sheets control. Each save records a new assessment in the Register,
 * so once one has succeeded the button gives way to a link rather than
 * inviting a duplicate row; it returns only as a retry after a failure.
 */
function SheetsAction({ sheet }) {
  if (sheet.state === 'saved') {
    return (
      <a className="sheets-status" href={sheet.url} target="_blank" rel="noreferrer"
         title={`Assessment ${sheet.assessment_id}`}>
        Saved to Sheets ↗
      </a>
    )
  }
  const saving = sheet.state === 'saving'
  return (
    <>
      <button className="btn btn--ghost btn--small" onClick={sheet.onSave} disabled={saving}
              title="Record this assessment in the Google Sheets register">
        {saving ? 'Saving…' : sheet.state === 'error' ? 'Retry save' : 'Save to Sheets'}
      </button>
      {sheet.state === 'error' && (
        <span className="sheets-status" data-tone="bad" title={sheet.message}>Not saved: {sheet.message}</span>
      )}
    </>
  )
}

/* ------------------------------------------------------- transaction explorer */
function evidenceRows(report) {
  const rows = []
  const details = report.details || {}
  const add = (items, category, direction = 'inflow') => (items || []).forEach((r) => rows.push({
    id: `${category}-${r.account ?? ''}-${r.txn_id || r.timestamp || Math.random()}`,
    date: r.timestamp || r.date,
    counterparty: (r.counterparty || r.msisdn || '—') + (r.account ? ` · ${r.account}` : ''),
    amount: r.amount ?? r.principal ?? r.inflow ?? r.outflow_within_window ?? 0,
    type: r.type || category.replace(/_/g, ' '),
    category,
    direction,
    reason: r.reason || r.basis || '',
  }))
  add(details.large_transactions?.inflows, 'large_transaction')
  add(details.self_transfers?.possible_for_review, 'possible_self_transfer')
  add(details.loan_inflows?.ledger?.filter((r) => r.direction === 'disbursement'), 'loan_disbursement')
  add(details.loan_inflows?.pattern_matches, 'loan_pattern')
  ;(report.red_flags || []).forEach((f) => {
    const e = f.evidence || {}
    add(e.rapid_round_trips, 'rapid_round_trip')
    add(e.passthrough_days, 'pass_through_day')
    add(e.outlier_transactions, 'profile_deviation')
    add(e.outlier_days, 'profile_deviation')
    add(e.items, f.code.toLowerCase())
  })
  return rows
}

export function TransactionExplorer({ report }) {
  const all = evidenceRows(report)
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('all')
  const [open, setOpen] = useState(false)
  const categories = [...new Set(all.map((r) => r.category))]
  const filtered = all.filter((r) => {
    const q = query.trim().toLowerCase()
    return (category === 'all' || r.category === category) && (!q || `${r.counterparty} ${r.type} ${r.date}`.toLowerCase().includes(q))
  })
  if (!all.length) return null
  return (
    <section className="panel report-anchor" id="evidence-explorer">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Evidence explorer</span>
          <h3 style={{ marginTop: 6 }}>Investigate flagged transactions</h3>
          <p>These are source records surfaced by exclusions and risk findings. The explorer does not invent transactions that are absent from the API response.</p>
        </div>
        <button className="btn btn--ghost btn--small" onClick={() => setOpen(!open)}>{open ? 'Hide evidence' : `View ${all.length} evidence items`}</button>
      </div>
      {open && <div className="panel__body"> 
        <div className="explorer-controls">
          <input aria-label="Search evidence" placeholder="Search counterparty, type or date…" value={query} onChange={(e) => setQuery(e.target.value)} />
          <select value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Evidence category">
            <option value="all">All evidence</option>
            {categories.map((c) => <option key={c} value={c}>{c.replace(/_/g, ' ')}</option>)}
          </select>
          <span className="muted explorer-count">{filtered.length} shown</span>
        </div>
        <div className="table-scroll">
          <table>
            <thead><tr><th>Date</th><th>Counterparty</th><th>Category</th><th>Reason</th><th className="r">Amount</th></tr></thead>
            <tbody>{filtered.map((r) => <tr key={r.id}>
              <td className="num">{shortDate(r.date)}</td><td>{r.counterparty}</td><td><span className="tag">{r.category.replace(/_/g, ' ')}</span></td><td className="muted">{r.reason || r.type}</td><td className="num r">{money(r.amount)}</td>
            </tr>)}</tbody>
          </table>
        </div>
      </div>}
    </section>
  )
}

/* ----------------------------------------------------------- review checklist */
export function ReviewChecklist({ report, value, onChange }) {
  const flags = report.red_flags || []
  const nc = report.name_check
  const namesOpen = nc?.needs_review
    ? nc.items.filter((i) => i.status !== 'match' && !value.names?.[i.account_id || i.account]?.checked).length
    : 0
  const hasLoans = (report.details?.loan_inflows?.ledger?.length || 0) + (report.details?.loan_inflows?.pattern_matches?.length || 0) > 0
  const hasConcentration = (report.disclosures?.concentration || []).some((c) => c.flagged)
  const items = [
    ['crb', 'Credit bureau gate reviewed', true],
    ['identity', 'Applicant / statement identity reviewed', !!report.identity_check || true],
    ['loan', 'Loan inflows and repayment evidence reviewed', hasLoans],
    ['concentration', 'Month concentration reviewed', hasConcentration],
    ['float', 'Agent / float classification reviewed', flags.some((f) => f.code === 'AGENT_FLOAT')],
    ['limitations', 'Format limitations reviewed', (report.disclosures?.capability_notes?.length || 0) > 0],
  ]
  const applicable = items.filter(([, , relevant]) => relevant).length
  const completed = items.filter(([id, , relevant]) => relevant && value[id]).length
  return (
    <section className="panel review-panel">
      <div className="panel__head">
        <div><span className="eyebrow">Underwriter workflow</span><h3 style={{ marginTop: 6 }}>Review checklist</h3><p>Local checklist for the current assessment. It is not a credit decision and is not persisted to the server.</p></div>
        <span className="review-progress">{completed}/{applicable} reviewed</span>
      </div>
      <div className="panel__body">
        <div className="checklist">{items.map(([id, label, relevant]) => <label key={id} className={!relevant ? 'checklist__item is-muted' : 'checklist__item'}>
          <input type="checkbox" checked={!!value[id] && !(id === 'identity' && namesOpen)} disabled={!relevant || (id === 'identity' && namesOpen > 0)} onChange={(e) => onChange({ ...value, [id]: e.target.checked })} />
          <span><b>{label}</b>{!relevant && <small>Not applicable — no relevant evidence was surfaced.</small>}
            {id === 'identity' && namesOpen > 0 && <small>{namesOpen} name mismatch{namesOpen > 1 ? 'es' : ''} still to check in the name check panel.</small>}</span>
        </label>)}</div>
      </div>
    </section>
  )
}

/* -------------------------------------------------------------- assumptions */
/* --------------------------------------------------- what could not be run */
const STATUS_LABEL = {
  not_assessed: 'Not assessed',
  substituted: 'Different test used',
  degraded: 'Reduced confidence',
}

export function CapabilityNotes({ notes, account }) {
  if (!notes?.length) return null
  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Format limits</span>
          <h3 style={{ marginTop: 6 }}>Checks this statement format cannot support</h3>
          <p>
            A check that could never have fired must not read as a check that passed.
            Everything below was skipped or weakened because {account?.source_format} does
            not carry the field it needs — not because the account came back clean.
          </p>
        </div>
      </div>
      <div className="panel__body">
        <table className="table">
          <thead>
            <tr><th>Check</th><th>Status</th><th>Why</th></tr>
          </thead>
          <tbody>
            {notes.map((n) => (
              <tr key={n.check}>
                <td><code>{n.check}</code></td>
                <td><span className="tag">{STATUS_LABEL[n.status] ?? n.status}</span></td>
                <td>{n.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export function Assumptions({ items }) {
  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Confidence</span>
          <h3 style={{ marginTop: 6 }}>Assumptions &amp; data-quality notes</h3>
          <p>What the engine took as given, and what limits confidence in the figures.</p>
        </div>
      </div>
      <div className="panel__body">
        <ul className="notes">{items.map((a, i) => <li key={i}>{a}</li>)}</ul>
      </div>
    </section>
  )
}

/* ---------------------------------------------------------- combined accounts
   Only present when more than one account was assessed. Laid out exactly like
   the net revenue summary, with each month broken out by account and then
   combined, so every franc of the combined figure traces to an account. */
const SUMMARY_COLS = [
  ['self_transfers', '(−) Self-transfers'],
  ['loan_inflows', '(−) Loan inflows'],
  ['institutional_credits', '(−) Bank / e-wallet'],
  ['large_transactions', '(−) Large txns'],
]

export function CombinedAccounts({ report }) {
  if (!report?.combined) return null
  const accounts = report.accounts || []
  const moved = report.details?.inter_account_transfers || []
  const rows = report.summary?.rows || []
  const t = report.summary?.totals || {}
  const short = (a) => a.customer ? `${a.customer} · ${a.account_id ?? ''}` : a.label

  const cells = (r, bold) => (
    <>
      <td className="num r">{bold ? <b>{money(r.gross_inflows)}</b> : money(r.gross_inflows)}</td>
      {SUMMARY_COLS.map(([key]) => (
        <td key={key} className={`num r ${r[key] ? 'neg' : 'muted'}`}>{money(r[key])}</td>
      ))}
      <td className="num r"><b>{money(r.net_revenue)}</b></td>
      <td className="num r muted">{r.inflow_count}</td>
    </>
  )

  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Combined assessment · {accounts.length} accounts · RWF</span>
          <h3 style={{ marginTop: 6 }}>Net revenue by account</h3>
          <p>
            Each account is analysed under its own format's rules, then the months are added.
            Money moved between the applicant's own accounts is deducted as a self-transfer so it
            is counted once.
          </p>
        </div>
      </div>
      <div className="panel__body">
        <div className="table-scroll">
          <table className="combined-table">
            <thead>
              <tr>
                <th>Month</th>
                <th>Account</th>
                <th className="r">Gross inflows</th>
                {SUMMARY_COLS.map(([, label]) => <th key={label} className="r">{label}</th>)}
                <th className="r">Net revenue</th>
                <th className="r">Inflow count</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => {
                const parts = accounts
                  .filter((a) => a.included_in_combined)
                  .map((a) => [a, (a.rows || []).find((r) => r.month === m.month)])
                  .filter(([, r]) => r)
                return [
                  ...parts.map(([a, r], i) => (
                    <tr key={`${m.month}|${a.label}`}>
                      {i === 0 && (
                        <td className="num" rowSpan={parts.length + 1}>
                          {m.month}
                          {!m.is_full_month && <span className="tag-partial">partial · {m.days_covered}d</span>}
                        </td>
                      )}
                      <td className="muted">
                        {short(a)}
                        {!r.is_full_month && <span className="tag-partial">{r.days_covered}d</span>}
                      </td>
                      {cells(r)}
                    </tr>
                  )),
                  <tr key={`${m.month}|combined`} className="combined-table__sub">
                    {parts.length === 0 && <td className="num">{m.month}</td>}
                    <td>Combined</td>
                    {cells(m, true)}
                  </tr>,
                ]
              })}
            </tbody>
            <tfoot>
              {accounts.map((a) => (
                <tr key={a.label} data-excluded={!a.included_in_combined || undefined}>
                  <td colSpan={2}>
                    {short(a)}
                    {!a.included_in_combined && (
                      <span className="tag-partial">not included · {a.not_included_reason}</span>
                    )}
                  </td>
                  <td className="num r">{money(a.totals?.gross_inflows)}</td>
                  {SUMMARY_COLS.map(([key]) => <td key={key} className="num r">{money(a.totals?.[key])}</td>)}
                  <td className="num r">{a.included_in_combined ? money(a.totals?.net_revenue) : '—'}</td>
                  <td className="num r">{a.totals?.inflow_count}</td>
                </tr>
              ))}
              <tr className="combined-table__total">
                <td colSpan={2}>Combined total</td>
                <td className="num r">{money(t.gross_inflows)}</td>
                {SUMMARY_COLS.map(([key]) => <td key={key} className="num r">{money(t[key])}</td>)}
                <td className="num r">{money(t.net_revenue)}</td>
                <td className="num r">{t.inflow_count}</td>
              </tr>
            </tfoot>
          </table>
        </div>

        {moved.length > 0 && (
          <>
            <h4 style={{ margin: '18px 0 8px' }}>
              Transfers between own accounts ({moved.length} · {money(moved.reduce((n, x) => n + x.amount, 0))} RWF, included in self-transfers above)
            </h4>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr><th>Date</th><th>Received on</th><th>Sent from</th><th className="r">Amount</th></tr>
                </thead>
                <tbody>
                  {moved.slice(0, 30).map((x) => (
                    <tr key={`${x.account}|${x.txn_id}`}>
                      <td className="num">{shortDate(x.timestamp)}</td>
                      <td>{x.account}</td>
                      <td>{x.matched_outflow?.account}</td>
                      <td className="num r">{money(x.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </section>
  )
}

/* --------------------------------------------------------------- name check
   A statement in someone else's name is not a footnote: every figure on the
   page assumes the documents describe one person. Shown first, in full, and
   each mismatch stays open until an underwriter ticks it off with a reason. */
const NAME_REASONS = [
  ['same_person', 'Same person — spelling / name order differs'],
  ['spouse', 'Spouse or family member, accepted by policy'],
  ['business', 'Business partner / co-owner, accepted by policy'],
  ['other', 'Other — see note'],
]

export function NameCheck({ report, value = {}, onChange }) {
  const nc = report?.name_check
  if (!nc?.needs_review) return null
  const open = nc.items.filter((i) => i.status !== 'match')
  const key = (i) => i.account_id || i.account
  const done = open.filter((i) => value[key(i)]?.checked).length
  const resolved = done === open.length
  const set = (i, patch) => onChange({ ...value, [key(i)]: { ...value[key(i)], ...patch } })
  const refLabel = nc.reference === 'bureau' ? 'Bureau applicant' : 'First statement holder'

  return (
    <section className="panel namecheck" data-resolved={resolved} id="name-check">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Important · name check</span>
          <h3 style={{ marginTop: 6 }}>
            {nc.mismatch_count
              ? `${nc.mismatch_count} statement${nc.mismatch_count > 1 ? 's are' : ' is'} not in ${nc.reference_name ? `${nc.reference_name}’s` : 'the applicant’s'} name`
              : 'A statement holder name could not be confirmed'}
          </h3>
          <p>
            Every figure below assumes these documents belong to one applicant. Check each
            name and record why it is acceptable, or stop and request the right statement.
          </p>
        </div>
        <span className="quality-badge" data-tone={resolved ? 'ok' : 'bad'}>
          {resolved ? 'CHECKED' : `${open.length - done} TO CHECK`}
        </span>
      </div>
      <div className="panel__body">
        <div className="table-scroll">
          <table>
            <thead>
              <tr><th>Statement</th><th>Holder name</th><th>{refLabel}</th><th>Result</th><th>Underwriter check</th></tr>
            </thead>
            <tbody>
              {nc.items.map((i) => {
                const v = value[key(i)] || {}
                const needs = i.status !== 'match'
                return (
                  <tr key={key(i)}>
                    <td className="muted">{i.account}</td>
                    <td><b>{i.holder || 'Name not on statement'}</b></td>
                    <td>{nc.reference_name || '—'}</td>
                    <td>
                      <span className="pill" data-sev={i.status === 'match' ? 'informational' : i.status === 'mismatch' ? 'severe' : 'elevated'}>
                        {i.status === 'match' ? 'match' : i.status === 'mismatch' ? 'name differs' : 'cannot compare'}
                      </span>
                    </td>
                    <td>
                      {needs ? (
                        <div className="namecheck__check">
                          <select
                            value={v.reason || ''}
                            onChange={(e) => set(i, { reason: e.target.value, checked: v.checked && !!e.target.value })}
                            aria-label={`Reason for ${i.holder}`}
                          >
                            <option value="">Why is this acceptable?…</option>
                            {NAME_REASONS.map(([r, label]) => <option key={r} value={r}>{label}</option>)}
                          </select>
                          {v.reason === 'other' && (
                            <input
                              placeholder="Note"
                              value={v.note || ''}
                              onChange={(e) => set(i, { note: e.target.value })}
                            />
                          )}
                          <label>
                            <input
                              type="checkbox"
                              checked={!!v.checked}
                              disabled={!v.reason || (v.reason === 'other' && !v.note?.trim())}
                              onChange={(e) => set(i, { checked: e.target.checked })}
                            />
                            Checked
                          </label>
                        </div>
                      ) : <span className="muted">—</span>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        {!resolved && (
          <p className="namecheck__warn">
            Not checked yet. Do not rely on the revenue or sanction until every name above is checked.
          </p>
        )}
      </div>
    </section>
  )
}
