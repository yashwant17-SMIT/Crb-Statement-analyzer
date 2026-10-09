import { renderToString } from 'react-dom/server'
import App from '../src/App.jsx'
import { Verdict, FlowRibbon, SummaryTable, DetailTables, Disclosures, RedFlags, Assumptions, Rail, EmptyState, Chrome, Tier, BandGauge, RevenueWaterfall, StatementHealth, TransactionExplorer, ReviewChecklist, CapabilityNotes, LoanEligibility, EdgeCases, Reconciliation } from '../src/components/index.jsx'
import report from '../report.sample.json' with { type: 'json' }
import crb from '../crb.sample.json' with { type: 'json' }
import { CrbUpload, GateBanner, CrbExposure, CrbFindings, CrbPortfolio, CrbProfile, CrbNotes } from '../src/components/crb.jsx'

const cases = {
  'App (crb step)':  <App />,
  'Chrome (empty)':  <Chrome crb={null} report={null} files={{}} onReset={() => {}} showReset={false} />,
  'Chrome (loaded)': <Chrome crb={crb} report={report} files={{ statement: 's.pdf' }} onReset={() => {}} showReset />,
  'BandGauge':       <BandGauge crb={crb} />,
  'Tier':            <Tier id="t" label="Audit trail" note="note" recessed><p>body</p></Tier>,
  'CrbUpload':       <CrbUpload onFile={() => {}} busy={false} />,
  'GateBanner':      <GateBanner crb={crb} onContinue={() => {}} busy={false} />,
  'CrbExposure':     <CrbExposure crb={crb} />,
  'CrbFindings':     <CrbFindings findings={crb.findings} />,
  'CrbPortfolio':    <CrbPortfolio crb={crb} />,
  'CrbProfile':      <CrbProfile crb={crb} />,
  'CrbNotes':        <CrbNotes crb={crb} />,
  'Rail (done)':     <Rail stage="done" crb={crb} report={report} files={{ statement: 'statement.pdf' }} />,
  'EmptyState':      <EmptyState onFile={() => {}} busy={false} step="Step 2 of 2" />,
  'Verdict':         <Verdict report={report} />,
  'FlowRibbon':      <FlowRibbon flow={report.monthly_flow} />,
  'SummaryTable':    <SummaryTable summary={report.summary} sensitivity={report.sensitivity} />,
  'DetailTables':    <DetailTables details={report.details} />,
  'Disclosures':     <Disclosures d={report.disclosures} cap={report.policy.credit_cap} />,
  'RedFlags':        <RedFlags flags={report.red_flags} />,
  'Assumptions':     <Assumptions items={report.assumptions} />,
  'RevenueWaterfall':<RevenueWaterfall report={report} />,
  'StatementHealth': <StatementHealth report={report} />,
  'TransactionExpl': <TransactionExplorer report={report} />,
  'ReviewChecklist': <ReviewChecklist report={report} value={{}} onChange={() => {}} />,
  'CapabilityNotes': <CapabilityNotes notes={report.disclosures?.capability_notes} account={report.account} />,
  'LoanEligibility': <LoanEligibility report={report} />,
  'EdgeCases':       <EdgeCases report={report} />,
  'Reconciliation':  <Reconciliation report={report} />,
}

let fails = 0
const out = {}
for (const [name, el] of Object.entries(cases)) {
  try {
    const html = renderToString(el)
    out[name] = html
    const nan = (html.match(/NaN/g) || []).length
    const undef = (html.match(/undefined/g) || []).length
    console.log(`  ${nan || undef ? 'WARN ' : 'ok   '} ${name.padEnd(18)} ${String(html.length).padStart(6)} chars` +
      (nan ? `  NaN x${nan}` : '') + (undef ? `  undefined x${undef}` : ''))
    if (nan || undef) fails++
  } catch (e) {
    console.log(`  FAIL  ${name.padEnd(18)} ${e.message}`)
    fails++
  }
}
process.exit(fails ? 1 : 0)
