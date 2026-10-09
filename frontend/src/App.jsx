import { useEffect, useRef, useState } from 'react'
import { amountInWords, analyzeStatement, checkCreditReport, fetchSanctionAmounts, fetchSheetsStatus, money, saveToSheets } from './api'
import {
  Assumptions, CapabilityNotes, Chrome, DetailTables, Disclosures, EmptyState,
  FlowRibbon, Rail, RedFlags, SummaryTable, Verdict, RevenueWaterfall, StatementHealth,
  EdgeCases, Reconciliation, IdentityCheck,
  ReviewChecklist, TransactionExplorer, Tier, LoanEligibility, CombinedAccounts, NameCheck,
} from './components'
import {
  CrbExposure, CrbFindings, CrbNotes, CrbPortfolio, CrbProfile, CrbUpload, GateBanner,
} from './components/crb'

/**
 * The process is deliberately sequential.
 *
 *   crb -> gate -> statement -> done
 *
 * The statement upload does not exist in the DOM until the bureau gate opens,
 * so an underwriter cannot skip the credit check by scrolling past it. The
 * underwriter may still opt in to a statement-only analysis (`crbOverride`):
 * 'blocked' after the bureau blocks, or 'skipped' when there is no bureau
 * report at all. The report then carries a banner saying the CRB did not pass.
 */
export default function App() {
  const [stage, setStage] = useState('crb')
  const [crb, setCrb] = useState(null)
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)
  const [files, setFiles] = useState({ crb: '', statement: '' })
  const [review, setReview] = useState({})
  const [customerType, setCustomerType] = useState('new')
  const [requestedAmount, setRequestedAmount] = useState('')
  const [crbOverride, setCrbOverride] = useState(null)
  const [sheetsEnabled, setSheetsEnabled] = useState(false)
  const [sheet, setSheet] = useState({ state: 'idle' })
  const abort = useRef(null)

  const busy = stage === 'crbLoading' || stage === 'stmtLoading'

  // Sanctioned loan amounts per customer type, for the requested-amount
  // dropdown. Null until loaded; if the policy cannot be fetched the form
  // falls back to a free number field rather than blocking the application.
  const [sanctionAmounts, setSanctionAmounts] = useState(null)

  useEffect(() => {
    fetchSheetsStatus().then((s) => setSheetsEnabled(!!s.configured))
    fetchSanctionAmounts().then(setSanctionAmounts).catch(() => setSanctionAmounts(null))
  }, [])

  const amountOptions = sanctionAmounts?.[customerType] ?? []

  function changeCustomerType(type) {
    setCustomerType(type)
    // An amount from the other table may not exist in this one.
    if (sanctionAmounts && !sanctionAmounts[type]?.includes(Number(requestedAmount))) setRequestedAmount('')
  }

  async function saveSheet() {
    setSheet({ state: 'saving' })
    try {
      const out = await saveToSheets(crb, report, files)
      setSheet({ state: 'saved', ...out })
    } catch (err) {
      setSheet({ state: 'error', message: err.message })
    }
  }

  // Every finished assessment lands in the sheet without the underwriter
  // having to remember: a full report, or a bureau file that stopped at the
  // gate. Each save is a new Register row; the customer's earlier rows are
  // marked superseded.
  useEffect(() => {
    if (sheetsEnabled && (stage === 'done' || stage === 'blocked')) saveSheet()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, sheetsEnabled])

  async function runCrb(file) {
    setFiles((f) => ({ ...f, crb: file.name }))
    setError(null)
    setCrbOverride(null)
    setStage('crbLoading')
    abort.current?.abort()
    abort.current = new AbortController()
    try {
      const data = await checkCreditReport(file, {}, abort.current.signal)
      setCrb(data)
      setStage(data.gate.statement_parsing_allowed ? 'gate' : 'blocked')
      window.scrollTo({ top: 0, behavior: 'smooth' })
    } catch (err) {
      if (err.name === 'AbortError') return
      setError(err)
      setStage('crb')
    }
  }

  async function runStatement(items) {
    setFiles((f) => ({ ...f, statement: items.map((i) => i.file.name).join(', ') }))
    setError(null)
    setStage('stmtLoading')
    abort.current?.abort()
    abort.current = new AbortController()
    try {
      const monthlyEmi = crb?.exposure?.scheduled_monthly_payments ?? 0
      const data = await analyzeStatement(items, {
        monthlyEmi,
        requestedAmount: Number(requestedAmount),
        customerType,
        bureauName: crb?.applicant?.names,
        bureauPhones: (crb?.applicant?.phones || []).map((p) => p.number),
      }, abort.current.signal)
      setReport(data)
      setStage('done')
      // Land on the statement verdict rather than jumping to the top of the
      // page: the bureau evidence above has already been read, and scrolling
      // back to it makes the report feel like it restarted.
      requestAnimationFrame(() => {
        document.getElementById('overview')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      })
    } catch (err) {
      if (err.name === 'AbortError') return
      setError(err)
      setStage('gate')
    }
  }

  function skipCrb() {
    abort.current?.abort()
    setCrb(null); setError(null)
    setCrbOverride('skipped')
    setStage('gate')
  }

  function reset() {
    abort.current?.abort()
    setCrb(null); setReport(null); setError(null)
    setFiles({ crb: '', statement: '' }); setReview({}); setCustomerType('new'); setRequestedAmount('')
    setCrbOverride(null)
    setSheet({ state: 'idle' })
    setStage('crb')
  }

  const heading = {
    crb: 'New application',
    crbLoading: 'Reading the bureau report',
    gate: crbOverride === 'skipped' ? 'Statement analysis — no bureau check'
      : crbOverride ? 'Statement analysis — CRB not cleared' : 'Credit bureau cleared',
    blocked: 'Application blocked',
    stmtLoading: 'Assessing the statement',
    done: 'Underwriting report',
  }[stage]

  // A block can come from the band or from the report's age; name the one that fired.
  const gate = crb?.gate
  const bandClears = !!gate && (gate.bands_accepted || []).includes(gate.band)
  const adverse = gate?.adverse_records || []
  const blockedLede = !gate
    ? 'No statement will be processed.'
    : adverse.length
      ? 'Adverse record on the bureau file. No statement will be processed.'
    : !bandClears
      ? 'The band is below policy. No statement will be processed.'
      : gate.report_is_current === false
        ? 'The bureau report is too old. No statement will be processed.'
        : 'The bureau report needs review. No statement will be processed.'

  const lede = {
    crb: 'The credit bureau check runs first. The statement is assessed only if the band clears.',
    gate: crbOverride === 'skipped'
      ? 'The credit bureau check was skipped. The statement is analysed on its own; this is not an approval.'
      : crbOverride
      ? 'The bureau check did not pass. The statement is analysed for reference only; this is not an approval.'
      : 'The band clears policy. Upload the statement to complete the assessment.',
    blocked: blockedLede,
    done: null,
  }[stage]

  return (
    <div className="app">
      <Chrome
        crb={crb} report={report} files={files} onReset={reset} showReset={!!(crb || report || crbOverride)}
        sheet={sheetsEnabled && !busy ? { ...sheet, onSave: saveSheet } : null}
      />
      <Rail stage={stage} crb={crb} report={report} files={files} />

      <main className={stage === 'crb' ? 'main main--start' : 'main'}>
        {stage !== 'done' && (
          <header className="pagehead">
            <span className="eyebrow">Credit risk · Rwanda · RWF</span>
            <h2>{heading}</h2>
            {lede && <p>{lede}</p>}
          </header>
        )}

        {busy && (
          <div className="processing">
            <div className="bar"><i /></div>
            <div className="processing__steps">
              {(stage === 'crbLoading'
                ? ['Opening bureau report', 'Reading profile and accounts', 'Checking arrears and score band']
                : ['Opening statements', 'Detecting statement format', 'Extracting transactions', 'Reconciling balances', 'Applying exclusions and risk checks']
              ).map((item, i) => (
                <span key={item} className={i === 0 ? 'is-active' : ''}><i />{item}</span>
              ))}
            </div>
            <p className="muted">{stage === 'crbLoading' ? files.crb : files.statement}</p>
          </div>
        )}

        {error && (
          <div className="error">
            <h4>{error.code === 'network_unreachable' ? 'No response from the service' : 'Document could not be assessed'}</h4>
            <p>{error.message}</p>
            {error.hint && <p style={{ marginTop: 6 }}>{error.hint}</p>}
          </div>
        )}

        {stage === 'crb' && <CrbUpload onFile={runCrb} onSkip={skipCrb} busy={busy} />}

        {/* ---- the bureau decision, and its evidence ---- */}
        {crb && stage !== 'crb' && (
          <>
            <GateBanner
              crb={crb}
              busy={busy}
              onContinue={() => document.getElementById('statement-step')?.scrollIntoView({ behavior: 'smooth' })}
            />

            {stage === 'blocked' && (
              <div className="blocked">
                <h4>The statement will not be assessed</h4>
                <p>
                  {adverse.length
                    ? `Credit policy declines any applicant with a bounced cheque or fraud case: ${adverse.join('; ')}.${!bandClears ? ` Band ${crb.gate.band} is also below the minimum of ${crb.gate.minimum_band}.` : ''}`
                    : !bandClears
                    ? `Band ${crb.gate.band} is below the minimum of ${crb.gate.minimum_band}.`
                    : crb.gate.report_is_current === false
                      ? `Band ${crb.gate.band} clears policy, but the bureau report is ${crb.gate.report_age_days} days old (maximum ${crb.gate.max_report_age_days}). Pull a fresh report and start a new application.`
                      : crb.gate.reason}
                  {' '}Under current credit policy the application stops here. The bureau detail
                  below is retained for the file.
                </p>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 12, flexWrap: 'wrap' }}>
                  <button
                    className="btn btn--ghost"
                    disabled={busy}
                    onClick={() => {
                      setCrbOverride('blocked')
                      setStage('gate')
                      requestAnimationFrame(() => {
                        document.getElementById('statement-step')?.scrollIntoView({ behavior: 'smooth' })
                      })
                    }}
                  >
                    Run statement analysis anyway
                  </button>
                  <span className="muted" style={{ fontSize: 12.5 }}>
                    Optional. For reference only; the bureau decision stands.
                  </span>
                </div>
              </div>
            )}

            <Tier id="bureau" label="Bureau evidence" note="From the TransUnion report">
              <CrbExposure crb={crb} />
              <CrbPortfolio crb={crb} />
              <CrbFindings findings={crb.findings} />
              <CrbProfile crb={crb} identity={report?.identity_check} />
              <CrbNotes crb={crb} />
            </Tier>
          </>
        )}

        {stage === 'gate' && (
          <div id="statement-step">
            <section className="panel application-inputs">
              <div className="panel__head">
                <div>
                  <span className="eyebrow">Application details</span>
                  <h3 style={{ marginTop: 6 }}>Customer type & requested amount</h3>
                  <p>Set these before the statement is parsed. New and existing customers go through the same steps; existing customers are priced on their own rates and sanction table.</p>
                </div>
              </div>
              <div className="panel__body application-inputs__body">
                <label className="field field--inline">
                  <span>Customer type</span>
                  <select value={customerType} onChange={(e) => changeCustomerType(e.target.value)} disabled={busy}>
                    <option value="new">New customer</option>
                    <option value="existing">Existing customer</option>
                  </select>
                </label>
                <label className="field field--inline">
                  <span>Requested amount (RWF)</span>
                  {amountOptions.length > 0 ? (
                    <select value={requestedAmount} onChange={(e) => setRequestedAmount(e.target.value)} disabled={busy}>
                      <option value="" disabled>Choose an amount</option>
                      {amountOptions.map((a) => (
                        <option key={a} value={a}>{money(a)}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      type="number"
                      min="1"
                      step="1"
                      value={requestedAmount}
                      onChange={(e) => setRequestedAmount(e.target.value)}
                      placeholder="e.g. 1,000,000"
                      disabled={busy}
                    />
                  )}
                  <span className="field__words">
                    {amountInWords(requestedAmount) ||
                      (amountOptions.length > 0
                        ? `${customerType === 'existing' ? 'Existing' : 'New'}-customer sanction amounts`
                        : 'Type the amount the customer asked for')}
                  </span>
                </label>
              </div>
              {customerType === 'existing' && (
                <div className="health-note" style={{ margin: '0 20px 18px' }}>
                  <b>Existing customer.</b> Net revenue is 18% of net inflow and the fee cap is 20% of net revenue, against the existing-customer sanction table (up to 3,000,000 RWF).
                </div>
              )}
            </section>
            <EmptyState
              onFile={runStatement}
              busy={busy}
              step="Step 2 of 2"
              disabled={!requestedAmount || Number(requestedAmount) <= 0}
            />
          </div>
        )}

        {/* ---- the finished report, in three tiers ---- */}
        {stage === 'done' && report && (
          <>
            <div id="overview" className="report-anchor">
              {crbOverride && (
                <div className="blocked">
                  {crbOverride === 'skipped' ? (
                    <>
                      <h4>No credit bureau check</h4>
                      <p>
                        This statement was analysed without a bureau report. Bureau EMIs are not
                        deducted and identity is not checked against the bureau file. The figures
                        below are for reference only and are not an approval.
                      </p>
                    </>
                  ) : (
                    <>
                      <h4>Credit bureau check did not pass</h4>
                      <p>
                        This statement was analysed at the underwriter's request after the bureau
                        blocked the application ({crb?.gate?.reason}). The figures below are for
                        reference only and are not an approval.
                      </p>
                    </>
                  )}
                </div>
              )}
              <NameCheck
                report={report}
                value={review.names}
                onChange={(names) => setReview((r) => ({ ...r, names }))}
              />
              <IdentityCheck report={report} />
              <Verdict report={report} />
              <EdgeCases report={report} />
            </div>

            <Tier id="findings" label="Findings" note="What the statement supports">
              <div id="revenue" className="report-anchor">
                <SummaryTable summary={report.summary} />
                <CombinedAccounts report={report} />
                <RevenueWaterfall report={report} />
                <LoanEligibility report={report} />
              </div>
              <div id="disclosures" className="report-anchor">
                <Disclosures d={report.disclosures} cap={report.policy.credit_cap} />
                <StatementHealth report={report} />
              </div>
              <div id="monthly" className="report-anchor">
                <FlowRibbon flow={report.monthly_flow} />
              </div>
              <div id="risk" className="report-anchor"><RedFlags flags={report.red_flags} /></div>
              <Reconciliation report={report} />
            </Tier>

            <Tier id="review" label="Underwriter review" note="Local to this session">
              <ReviewChecklist report={report} value={review} onChange={setReview} />
            </Tier>

            <Tier id="audit" label="Audit trail" note="Every figure, traced to a source row" recessed>
              <div id="exclusions" className="report-anchor"><DetailTables details={report.details} /></div>
              <TransactionExplorer report={report} />
              <CapabilityNotes notes={report.disclosures?.capability_notes} account={report.account} />
              <div id="assumptions" className="report-anchor"><Assumptions items={report.assumptions} /></div>
            </Tier>
          </>
        )}
      </main>
    </div>
  )
}
