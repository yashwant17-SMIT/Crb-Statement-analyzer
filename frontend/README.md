# Console

React 18 + Vite. No component library, no CSS framework — the design tokens in
`src/styles.css` are the whole system.

- `src/api.js` — the only place that talks to the API. `ApiError` carries
  `code`, `hint` and `status` so the UI can say what actually went wrong.
- `src/components/index.jsx` — presentational only, every component takes plain
  data and renders it.
- `src/App.jsx` — the state machine: `idle → loading → done | error`.

All numerics use `.num` (IBM Plex Mono, tabular figures) so columns align down
the page. Do not render a figure without it.
