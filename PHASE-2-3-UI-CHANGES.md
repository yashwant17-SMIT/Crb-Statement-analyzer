# UI Phases 2–3 — Changes

This build includes the Phase 1 UI work plus the remaining planned UI improvements.

## Phase 2 — Underwriter investigation

- Persistent report header with applicant/provider context and key metrics.
- Report section navigation in the main content and left rail.
- Evidence explorer built from evidence actually present in the API response.
- Search and category filtering for surfaced evidence.
- Red-flag evidence can jump directly to the evidence explorer.
- Human-readable investigation controls rather than exposing raw JSON as the primary path.
- Local underwriter review checklist.

## Phase 3 — Operational convenience

- Print-friendly report layout.
- Export the current API report as JSON.
- Processing-stage indicator during CRB and statement assessment.
- Responsive navigation and mobile-friendly evidence/review controls.

## Deliberate limitations

- The checklist is local to the current browser session and is not persisted server-side.
- No application/case ID was invented because the current API does not provide one.
- No server-side audit trail, save/resume, or workflow persistence was added because those require a backend/storage contract.
- The evidence explorer only shows transactions/evidence supplied by the current API response; it does not fabricate or fetch hidden raw transactions.
