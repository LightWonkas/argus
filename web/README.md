# Argus Cockpit (web)

The Argus web cockpit: a React + Vite + TypeScript + Tailwind v4 single-page app.
It is a presentation layer only. All data comes from the Argus backend API
(`src/argus/api/`, FastAPI); this package collects nothing itself.

> All output is informational decision support. Not financial advice.

## Requirements

- Node.js 22 (the Docker build uses `node:22-alpine`)
- A running Argus backend API (default `http://127.0.0.1:8787`)

## Development

```bash
# 1) Start the backend API in a separate terminal (from the repository root)
python -m argus --dashboard            # http://127.0.0.1:8787

# 2) Start the cockpit dev server
cd web
npm ci
npm run dev                            # http://localhost:3000
```

The dev server listens on port 3000 (`strictPort`, so it fails instead of picking another
port when 3000 is taken).

### How the cockpit reaches the backend

The app only ever calls relative `/api/...` URLs. In development, Vite proxies `/api` to the
backend (see `vite.config.ts`), so the browser sees a single origin and the session cookie
and CSRF header work without CORS configuration. The proxy target defaults to
`http://127.0.0.1:8787`; override it with `VITE_API_TARGET`:

```bash
VITE_API_TARGET=http://127.0.0.1:9000 npm run dev
```

In the Docker image, nginx serves the built files and proxies `/api/` to the `app` service
on port 8787 (see `nginx.conf` and the repository's `docker-compose.yml`).

### Sign-in

Authentication is configured on the backend through `ARGUS_DASHBOARD_USERS`
(`user:password-hash` entries). Generate a hash with:

```bash
python -c "from argus.api.security import hash_password; print(hash_password('YOUR_PASSWORD'))"
```

If `ARGUS_DASHBOARD_USERS` is empty the backend runs without authentication, which is
intended for local development only.

## Commands

| Command | Description |
|---|---|
| `npm run dev` | Vite dev server with HMR (port 3000, `/api` proxy) |
| `npm run build` | Type check (`tsc -b`) and production build into `dist/` |
| `npm run preview` | Serve the production build locally |
| `npm run lint` | ESLint (TypeScript + React Hooks rules) |
| `npm test` | Run the Vitest suite once (jsdom, no network) |
| `npm run test:watch` | Vitest in watch mode |

## Layout

```
web/
├─ index.html
├─ vite.config.ts        # dev port 3000, /api proxy, Vitest config
├─ nginx.conf            # production: static files + /api proxy
└─ src/
   ├─ main.tsx           # entry point
   ├─ App.tsx            # session handling, polling, page routing
   ├─ api.ts             # typed API client (cookie session + CSRF header)
   ├─ format.ts          # shared formatting / colour helpers
   ├─ theme.ts           # semantic design tokens
   ├─ components/        # panels, pages and widgets' contents
   └─ dashboard/         # movable widget grid, tabs, command palette
```

The dashboard layout (tabs, widgets, positions) is stored in the browser's `localStorage`.

## Dependency note

`react-draggable` is pinned to 4.4.6 through `overrides` in `package.json`.
`react-grid-layout` 1.5.3 passes `nodeRef` to it, and `react-draggable` 4.6.0 dropped
`nodeRef` support, which breaks widget drag and resize.
