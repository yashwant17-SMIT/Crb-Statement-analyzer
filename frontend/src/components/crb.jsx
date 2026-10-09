import { useRef, useState } from 'react'
import { money, pct } from '../api'
import { Evidence } from './index.jsx'

/* ------------------------------------------------------------ upload step */
export function CrbUpload({ onFile, onSkip, busy }) {
  const input = useRef(null)
  const [over, setOver] = useState(false)
  const take = (f) => f && onFile(f)

  return (
    <>
      <div
        className="drop"
        data-over={over}
        onDragOver={(e) => { e.preventDefault(); setOver(true) }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); take(e.dataTransfer.files?.[0]) }}
      >
        <span className="eyebrow">Step 1 of 2</span>
        <h3 style={{ marginTop: 8 }}>Start with the credit bureau report</h3>
        <p>
          The TransUnion PDF for this applicant. The statement is assessed only if
          the bureau band clears the minimum.
        </p>
        <button className="btn" onClick={() => input.current?.click()} disabled={busy}>
          Choose the bureau report
        </button>
        <input ref={input} type="file" accept="application/pdf" hidden
               onChange={(e) => take(e.target.files?.[0])} />
        {onSkip && (
          <p style={{ marginTop: 14 }}>
            <button className="btn btn--ghost btn--small" onClick={onSkip} disabled={busy}>
              No bureau report? Analyse a statement only
            </button>
          </p>
        )}
      </div>

      <div className="specs">
        <div>
          <h4>What opens the gate</h4>
          <p>The bureau band only. AA, BB, CC and DD proceed; EE and below stop here.</p>
        </div>
        <div>
          <h4>What the band will not tell you</h4>
          <p>Outstanding balances, arrears, and how many lenders are already owed. Those are read from the accounts.</p>
        </div>
        <div>
          <h4>Order is enforced</h4>
          <p>The statement is never parsed before the bureau decision. A blocked applicant costs no processing time.</p>
        </div>
      </div>
    </>
  )
}

/* ------------------------------------------------------------ gate banner */
export function GateBanner({ crb, onContinue, busy }) {
  const g = crb.gate
  const open = g.statement_parsing_allowed
  const refer = g.recommendation === 'refer'
  const tone = !open ? 'flag' : refer ? 'warn' : 'ok'

  return (
    <section className="gate" data-tone={tone}>
      <div className="gate__head">
        <div>
          <span className="eyebrow">Step 1 · credit bureau decision</span>
          <h3 className="gate__title">
            {!open ? 'Blocked at the bureau' : refer ? 'Gate open — refer to a credit officer' : 'Gate open'}
          </h3>
          <p className="gate__reason">{g.reason}</p>
          {refer && <p className="gate__reason">{g.recommendation_reason}</p>}
        </div>
        <div className="gate__score">
          <div className="num gate__band">{g.band ?? '—'}</div>
          <div className="num gate__num">{g.score ?? '—'}</div>
          <div className="gate__pd">{g.probability_of_default != null ? `${g.probability_of_default}% PD` : ''}</div>
          {g.report_age_days != null && (
            <div className="gate__pd" data-tone={g.report_is_current === false ? 'flag' : undefined}>
              Report {g.report_age_days}d old (max {g.max_report_age_days}d)
            </div>
          )}
        </div>
      </div>

      <BandLadder ladder={g.band_ladder} accepted={g.bands_accepted} current={g.band}
                  ranges={g.band_score_ranges} score={g.score} />

      {open && (
        <div className="gate__foot">
          <button className="btn" onClick={onContinue} disabled={busy}>
            Continue to the statement
          </button>
          <span className="muted" style={{ fontSize: 12.5 }}>
            {refer
              ? 'Proceeding gathers evidence for the committee. It is not an approval.'
              : 'Band clears the minimum.'}
          </span>
        </div>
      )}
    </section>
  )
}

function BandLadder({ ladder = [], accepted = [], current, ranges = [], score }) {
  const byBand = Object.fromEntries(ranges.map((r) => [r.band, r]))
  return (
    <div className="ladder">
      {ladder.map((b) => {
        const ok = accepted.includes(b)
        const r = byBand[b]
        return (
          <span key={b} className="ladder__cell" data-ok={ok} data-current={b === current}
                title={r ? `${b}: score ${r.range}` : b}>
            {b}
            {r && <small className="ladder__range">{r.range}</small>}
          </span>
        )
      })}
      <span className="ladder__legend">
        {accepted.join(', ')} proceed · {ladder.filter((b) => !accepted.includes(b)).join(', ')} stop
        {score != null && byBand[current] && (
          <> · score {score} sits in {current} ({byBand[current].range})</>
        )}
      </span>
    </div>
  )
}

/* --------------------------------------------------------------- exposure */
export function CrbExposure({ crb }) {
  const e = crb.exposure
  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Existing debt</span>
          <h3 style={{ marginTop: 6 }}>What the applicant already owes</h3>
          <p>Open accounts only. Closed facilities are in the track record below.</p>
        </div>
      </div>
      <div className="panel__body">
        <div className="metrics">
          <div>
            <span className="eyebrow">Active loans</span>
            <div className="num metric__figure">{e.ongoing_loan_count}</div>
            <div className="metric__sub">
              Open and performing · {e.open_account_count} open in total
              {e.portfolio?.non_performing?.open_count > 0 &&
                ` (${e.portfolio.non_performing.open_count} non-performing)`} · {e.closed_account_count} closed
            </div>
          </div>
          <div>
            <span className="eyebrow">Total monthly EMI</span>
            <div className="num metric__figure" data-tone={['partial', 'not_available'].includes(e.scheduled_monthly_payments_status) ? 'flag' : undefined}>
              {e.scheduled_monthly_payments_status === 'not_available'
                ? 'N/A'
                : money(e.scheduled_monthly_payments)}
            </div>
            <div className="metric__sub">
              <b>Open performing (PA) accounts only.</b>{' '}
              {e.scheduled_monthly_payments_status === 'complete'
                ? `Reported or estimated across ${e.ongoing_emi_account_count} account${e.ongoing_emi_account_count === 1 ? '' : 's'}`
                : e.scheduled_monthly_payments_status === 'calculated'
                  ? `Estimated for ${e.emi_accounts_with_calculated_payment} account${e.emi_accounts_with_calculated_payment === 1 ? '' : 's'} from principal ÷ term`
                  : e.scheduled_monthly_payments_status === 'partial'
                    ? `${e.emi_accounts_with_reported_payment} reported + ${e.emi_accounts_with_calculated_payment || 0} estimated of ${e.ongoing_emi_account_count} PA account${e.ongoing_emi_account_count === 1 ? '' : 's'}`
                    : e.scheduled_monthly_payments_status === 'no_ongoing_emi'
                      ? 'No open performing account reports or implies a monthly payment'
                      : e.scheduled_monthly_payments_status === 'no_open_loans'
                        ? 'No open credit accounts'
                        : 'Not reported for open performing accounts'}
              {e.emi_accounts_excluded_from_eligibility > 0 && (
                <span> · {e.emi_accounts_excluded_from_eligibility} account{e.emi_accounts_excluded_from_eligibility === 1 ? '' : 's'} excluded</span>
              )}
            </div>
          </div>
          <div>
            <span className="eyebrow">Total outstanding</span>
            <div className="num metric__figure" data-tone={e.cap_breached ? 'flag' : undefined}>{money(e.total_open_balance)}</div>
            <div className="metric__sub">
              Cap {money(e.credit_cap)} — {e.cap_breached
                ? `exceeded by ${money(-e.cap_headroom)}`
                : `${money(e.cap_headroom)} headroom`}
            </div>
          </div>
          <div>
            <span className="eyebrow">In arrears</span>
            <div className="num metric__figure" data-tone={e.total_arrears > 0 ? 'flag' : undefined}>{money(e.total_arrears)}</div>
            <div className="metric__sub">{pct(e.arrears_share_of_balance)} of the open balance</div>
          </div>
          <div>
            <span className="eyebrow">Worst account</span>
            {e.worst_days_in_arrears == null ? (
              <>
                <div className="num metric__figure" data-tone="flag">Not reported</div>
                <div className="metric__sub">
                  No open account prints a days-in-arrears figure. This is not the same
                  as nothing being overdue — use arrears and classification instead.
                </div>
              </>
            ) : (
              <>
                <div className="num metric__figure" data-tone={e.worst_days_in_arrears >= 30 ? 'flag' : undefined}>
                  {e.worst_days_in_arrears}<small>days late</small>
                </div>
                <div className="metric__sub">
                  Maximum days in arrears
                  {e.days_in_arrears_missing_count > 0 &&
                    ` · ${e.days_in_arrears_missing_count} open account${e.days_in_arrears_missing_count === 1 ? '' : 's'} do not report this field`}
                </div>
              </>
            )}
          </div>
        </div>

        <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Sector</th><th>Type</th><th>Class</th>
              <th>Listing date</th><th>Repayment terms</th>
              <th className="r">Principal</th><th className="r">Balance</th>
              <th className="r">Monthly EMI</th><th className="r">Arrears</th><th className="r">Days</th>
              <th>Last 12 months</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {e.accounts.map((a) => {
              const rb = a.recent_repayment_behavior
              return (
              <tr key={a.ref || `${a.index}-${a.type}-${a.balance}`}
                  data-excluded={a.counted_in_emi === false || undefined}>
                <td>{a.trade_sector}</td>
                <td>{a.type}</td>
                <td className={a.classification && a.classification.toUpperCase() !== 'NORMAL RISK' ? 'neg' : 'muted'}>
                  {a.classification || '—'}
                  {a.non_performing && <div className="metric__sub">Not serviced · no EMI deducted</div>}
                </td>
                <td className="num muted">{a.listing_date || '—'}</td>
                <td className="muted">{a.repayment_terms || '—'}</td>
                <td className="num r muted">{money(a.principal)}</td>
                <td className="num r">{money(a.balance)}</td>
                <td className={`num r ${!a.scheduled_payment_available && !a.calculated_emi_available ? 'muted' : ''}`}>
                  {a.counted_in_emi === false
                    ? <span className="muted" title={`Bureau/term figure ${money(a.scheduled_payment)} — not counted`}>Excluded</span>
                    : a.scheduled_payment_available ? money(a.emi_contribution)
                      : a.calculated_emi_available ? money(a.emi_contribution)
                        : 'Not reported'}
                  {a.counted_in_emi !== false && a.calculated_emi_available && !a.scheduled_payment_available && (
                    <div className="metric__sub">
                      Estimated · {a.repayment_term_months} mo
                      {a.term_basis && a.term_basis !== 'exact_months' && ` from ${a.repayment_terms}`}
                    </div>
                  )}
                  {a.scheduled_payment_printed_zero && a.counted_in_emi !== false && !a.calculated_emi_available && (
                    <div className="metric__sub">Bureau printed 0.00 against a live balance</div>
                  )}
                  {a.scheduled_payment_period_assumed_monthly && (
                    <div className="metric__sub">Period not stated · assumed monthly ({a.repayment_terms})</div>
                  )}
                </td>
                <td className={`num r ${a.arrears > 0 ? 'neg' : 'muted'}`}>{money(a.arrears)}</td>
                <td className={`num r ${a.days_in_arrears >= 30 ? 'neg' : 'muted'}`}>
                  {a.days_in_arrears_reported ? a.days_in_arrears : '—'}
                </td>
                <td>
                  {rb && rb.months_recorded > 0 ? (
                    <>
                      <span className={rb.months_delayed > 0 ? 'neg' : 'muted'}>
                        {rb.months_recorded - rb.months_delayed}/{rb.months_recorded} on time
                      </span>
                      {rb.months_delayed > 0 && (
                        <div className="metric__sub">
                          {rb.months_delayed} delayed · worst {rb.worst_days_late}d
                          {' — '}
                          {rb.delayed_detail.map((d) => `${d.month} (${d.days_late}d)`).join(', ')}
                        </div>
                      )}
                    </>
                  ) : <span className="muted">No data</span>}
                </td>
                <td className="muted">{a.status}</td>
              </tr>
            )})}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan={7}><b>Counted toward monthly EMI</b></td>
              <td className="num r"><b>{money(e.scheduled_monthly_payments)}</b></td>
              <td className="num r neg"><b>{money(e.total_arrears)}</b></td>
              <td colSpan={3} />
            </tr>
          </tfoot>
        </table>
        </div>
        {e.unserviced_obligations?.count > 0 && !e.unserviced_obligations.deducted_from_eligibility && (
          <div className="notebox" data-tone="warn" style={{ marginTop: 14 }}>
            <b>
              Credit policy: only open <u>performing</u> (PA) accounts contribute to the
              monthly EMI deduction.
            </b>{' '}
            {e.unserviced_obligations.count} non-performing facility
            {e.unserviced_obligations.count === 1 ? '' : 'ies'} holding{' '}
            {money(e.unserviced_obligations.balance)} RWF outstanding and{' '}
            {money(e.unserviced_obligations.arrears)} RWF in arrears {e.unserviced_obligations.count === 1 ? 'is' : 'are'} excluded
            from the deduction. Note these accounts print <b>Account Status: CURRENT</b>,
            which is a billing state, not a sign of health — they are excluded on
            classification. Excluding them leaves <b>more</b> revenue apparently
            available, which is why they also raise a blocking finding.
          </div>
        )}

        <p className="muted" style={{ fontSize: 12.5, marginTop: 10 }}>
          Monthly EMI uses Scheduled Payments printed by the bureau. A payment printed as
          0.00 against a live balance is treated as unreported, not as an obligation of
          zero. Where the bureau gives a repayment term instead, a principal-only estimate
          (principal ÷ term) is shown and labelled; daily and weekly schedules are
          converted at the mean calendar month, and a term under one month is refused
          rather than converted. No interest or fees are invented. A status of CURRENT is
          a billing state, not a health check.
        </p>
      </div>
    </section>
  )
}

/* --------------------------------------------------------------- findings */
export function CrbFindings({ findings }) {
  const [open, setOpen] = useState(findings[0]?.code ?? null)
  const map = { blocking: 'severe', warning: 'elevated', informational: 'informational' }

  return (
    <section style={{ marginBottom: 26 }}>
      <div style={{ marginBottom: 12 }}>
        <span className="eyebrow">Bureau findings</span>
        <h3 style={{ fontSize: 16, marginTop: 6 }}>What the band does not show</h3>
      </div>

      {findings.length === 0 && (
        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel__body" style={{ padding: '18px 20px' }}>
            <b>None observed.</b>{' '}
            <span className="muted">No adverse credit events, arrears or cap pressure on the bureau file.</span>
          </div>
        </div>
      )}

      {findings.map((f) => (
        <article className="flag" key={f.code} data-sev={map[f.severity]}>
          <button className="flag__btn" aria-expanded={open === f.code}
                  onClick={() => setOpen(open === f.code ? null : f.code)}>
            <span>
              <span className="flag__title">{f.title}</span>
              <span className="flag__metric">{f.detail}</span>
            </span>
            <span className="pill" data-sev={map[f.severity]}>{f.severity}</span>
          </button>
          {open === f.code && Object.keys(f.evidence || {}).length > 0 && (
            <div className="flag__body">
              <Evidence evidence={f.evidence} />
            </div>
          )}
        </article>
      ))}
    </section>
  )
}

/* ------------------------------------------------- portfolio / reconciliation */
export function CrbPortfolio({ crb }) {
  const p = crb.exposure?.portfolio
  const cheques = crb.bounced_cheques || []
  if (!p) return null

  const side = (s, label, tone) => (
    <div>
      <span className="eyebrow">{label}</span>
      <div className="num metric__figure" data-tone={tone}>{s.open_count}</div>
      <div className="metric__sub">
        {money(s.open_value)} RWF outstanding
        {s.bureau_open_count != null && ` · bureau states ${s.bureau_open_count}`}
      </div>
    </div>
  )

  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Portfolio</span>
          <h3 style={{ marginTop: 6 }}>Performing and non-performing, as the bureau counts them</h3>
          <p>
            Open accounts split by classification, checked against the figures the report
            states in its own summary block on page one.
          </p>
        </div>
        <span className="quality-badge" data-tone={p.reconciles ? 'good' : 'bad'}>
          {p.reconciles ? 'RECONCILES' : 'MISMATCH'}
        </span>
      </div>

      <div className="panel__body">
        <div className="metrics">
          {side(p.performing, 'Open performing (PA)')}
          {side(p.non_performing, 'Open non-performing (NPA)', p.non_performing.open_count > 0 ? 'flag' : undefined)}
        </div>

        <div className="eyebrow" style={{ marginTop: 22, marginBottom: 8 }}>By sector, as printed</div>
        <table>
          <thead>
            <tr>
              <th>Sector</th>
              <th className="r">Open PA</th><th className="r">PA value</th>
              <th className="r">Open NPA</th><th className="r">NPA value</th>
            </tr>
          </thead>
          <tbody>
            {['your_sector', 'other_sectors'].map((k) => (
              <tr key={k}>
                <td>{k === 'your_sector' ? 'Your sector' : 'Other sectors'}</td>
                <td className="num r">{p.performing.by_sector?.[k]?.open_count ?? '—'}</td>
                <td className="num r">{money(p.performing.by_sector?.[k]?.open_value)}</td>
                <td className="num r">{p.non_performing.by_sector?.[k]?.open_count ?? '—'}</td>
                <td className="num r">{money(p.non_performing.by_sector?.[k]?.open_value)}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="eyebrow" style={{ marginTop: 22, marginBottom: 8 }}>Reconciliation</div>
        <table>
          <thead>
            <tr><th>Check</th><th className="r">Parsed</th><th className="r">Bureau summary</th><th>Result</th></tr>
          </thead>
          <tbody>
            {p.checks.map((c) => (
              <tr key={c.check}>
                <td>{c.check.replace(/_/g, ' ')}</td>
                <td className="num r">{money(c.parsed)}</td>
                <td className="num r">{c.bureau_summary == null ? '—' : money(c.bureau_summary)}</td>
                <td className={c.matches === false ? 'neg' : 'muted'}>
                  {c.matches == null ? 'not stated' : c.matches ? 'matches' : 'differs'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>{p.note}</p>

        {cheques.length > 0 && (
          <>
            <div className="eyebrow" style={{ marginTop: 22, marginBottom: 8 }}>Bounced cheques</div>
            <table>
              <thead>
                <tr><th>Date bounced</th><th>Cheque no.</th><th>Sector</th><th>Reason</th><th className="r">Amount</th></tr>
              </thead>
              <tbody>
                {cheques.map((c, i) => (
                  <tr key={c.cheque_no || i}>
                    <td className="num">{c.date_bounced || c.cheque_date || '—'}</td>
                    <td className="num muted">{c.cheque_no || '—'}</td>
                    <td>{c.trade_sector || '—'}</td>
                    <td className="neg">{c.reason || '—'}</td>
                    <td className="num r neg"><b>{money(c.amount)}</b></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
              A dishonoured cheque is a failure to fund a payment the applicant had already
              committed to. It carries no balance and appears in no exposure figure.
            </p>
          </>
        )}
      </div>
    </section>
  )
}

/* --------------------------------------------- applicant / track / notes */
export function CrbProfile({ crb, identity }) {
  const a = crb.applicant
  const t = crb.repayment_track_record
  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Applicant</span>
          <h3 style={{ marginTop: 6 }}>Profile and track record</h3>
        </div>
      </div>
      <div className="panel__body">
        <table>
          <tbody>
            <tr><td style={{ width: '26%' }}><b>Name</b></td><td>{a.names}</td></tr>
            <tr><td><b>National ID</b></td><td className="num">{a.national_id}</td></tr>
            <tr><td><b>Date of birth</b></td><td className="num">{a.date_of_birth}</td></tr>
            <tr><td><b>Employment</b></td><td>{a.employment || '—'} <span className="muted">— bureau employment fields are often placeholder text, not verified income</span></td></tr>
            {a.directorships?.length > 0 && (
              <tr><td><b>Directorships</b></td><td>{a.directorships.map((d) => `${d.company} (${d.title})`).join(', ')}</td></tr>
            )}
            <tr><td><b>Phones on file</b></td><td className="num">{a.phones?.length ?? 0}</td></tr>
          </tbody>
        </table>

        {identity && (
          <div className="notebox" data-tone={identity.msisdn_on_bureau_profile ? 'ok' : 'warn'}>
            <b>Identity linkage.</b> {identity.note}
            <div className="num muted" style={{ fontSize: 12, marginTop: 6 }}>
              bureau: {identity.bureau_name} · statement: {identity.statement_name} ·
              matched on {identity.name_tokens_matched.join(', ') || 'nothing'}
            </div>
          </div>
        )}

        <h4 style={{ margin: '20px 0 2px', fontSize: 13 }}>Closed-facility track record</h4>
        <p className="muted" style={{ fontSize: 12.5, margin: '4px 0 10px' }}>{t.note}</p>
        <table>
          <thead><tr><th>Period</th><th className="r">Facilities</th><th className="r">Average principal</th></tr></thead>
          <tbody>
            {t.principal_escalation.map((r) => (
              <tr key={r.period}>
                <td className="num">{r.period}</td>
                <td className="num r muted">{r.count}</td>
                <td className="num r">{money(r.average_principal)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td>{t.closed_small_facilities} small facilities</td>
              <td className="num r">{t.closed_accounts}</td>
              <td className="num r">{money(t.total_small_advanced)} advanced</td>
            </tr>
          </tfoot>
        </table>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
          {t.all_repaid_without_arrears
            ? 'Every closed facility was repaid without arrears.'
            : 'Some closed facilities carried arrears.'}
        </p>
      </div>
    </section>
  )
}

export function CrbNotes({ crb }) {
  const items = [...(crb.notes || [])]
  if (crb.collateral?.note) items.push(crb.collateral.note)
  return (
    <section className="panel">
      <div className="panel__head">
        <div>
          <span className="eyebrow">Reading the report</span>
          <h3 style={{ marginTop: 6 }}>Notes and caveats</h3>
        </div>
      </div>
      <div className="panel__body">
        <ul className="notes">{items.map((n, i) => <li key={i}>{n}</li>)}</ul>
      </div>
    </section>
  )
}
