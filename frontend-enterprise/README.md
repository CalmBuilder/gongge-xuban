# Enterprise Console

```bash
npm install
npm run dev
```

Environment:

- `VITE_API_BASE_URL`, default same origin.
- `VITE_PROXY_TARGET`, split-mode `/api` proxy target, default `http://127.0.0.1:8000`.
- `VITE_TENANT_ID`, default `tenant_demo`

From the repository root, use `./app.sh dev` for foreground development or
`./app.sh` for detached production; both build this frontend
and serve `/enterprise` from the same port as `/api`. The Vite dev server is
only for legacy split-mode debugging, where it proxies `/api` to
`http://127.0.0.1:8000`.

Real-browser regressions use Playwright Chromium with deterministic API mocks:

```bash
npm run test:e2e:install  # first run only
npm run test:e2e
npm run test:e2e:fullstack
```

The Playwright runner starts and stops its own Vite server on port `4174`.
The full-stack suite builds the frontend, starts FastAPI on port `5148`, and uses a temporary
SQLite database that is removed when the test server exits.
