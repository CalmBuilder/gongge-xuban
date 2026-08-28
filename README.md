# 共格·序伴

<p align="center">
  <img src="packaging/assets/gongge-xuban-mark.svg" width="112" alt="共格·序伴标识" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/Node.js-20%2B-339933" alt="Node.js 20+" />
  <img src="https://img.shields.io/badge/FastAPI-0.139%2B-009688" alt="FastAPI" />
  <img src="https://img.shields.io/badge/React%2018-TypeScript%20strict-3178C6" alt="React 18 + TypeScript strict" />
  <img src="https://img.shields.io/badge/DB-SQLite%20%C2%B7%20MySQL%208.4-4479A1" alt="SQLite / MySQL 8.4" />
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh.md">中文</a>
</p>

共格·序伴 (Gongge Xuban) is an enterprise platform for building, running, and
governing digital employees. It turns professional knowledge, procedures, tools,
and decision criteria into reusable organizational capabilities — with every
execution recorded, auditable, and reproducible.

## Highlights

- **Unified Agent Loop protocol** — every conversation is driven by one
  cross-module contract: routing → SOP → tools → knowledge → general skills →
  reflection → reply, streamed over SSE with cancellation and failure-repair
  paths, and covered end to end by regression tests on both the backend
  producers and the frontend consumer.
- **Unified SOP metamodel** — state-machine execution, versioned definitions,
  an idempotent execution store, a condition DSL, explicit confirmation, and
  work items, plus bulk-migration tooling. All 20 built-in SOPs compile against
  this single metamodel.
- **Auditable tool and skill execution** — HTTP tools, MCP client and built-in
  servers, and a generated-code skill runner (Python/Bash) guarded by timeouts,
  package allowlists, permission checks, and path/shell protections.
- **Governed knowledge grounding** — managed knowledge bases with PDF/DOCX/HTML
  parsing, citation tracking, and access governance, so answers stay traceable
  to their sources.
- **Enterprise governance loop** — tenant isolation and role checks at the API
  layer, human approvals for high-risk actions, management audit, execution
  traces and event logs, scheduled tasks, and long-term memory.
- **Dual-dialect persistence** — SQLite for zero-configuration development and
  desktop, MySQL 8.4 (`utf8mb4`, UTC) for server deployments, with
  Alembic-managed schema versions and dialect differences isolated behind an
  adapter boundary.
- **One codebase, three runtime forms** — browser application, single-port
  service, and a packaged desktop app (PyInstaller) for macOS, Windows, and
  Linux, with signing and notarization pipelines.

## Architecture

```text
React 18 workbench (chat · management · audit)          frontend-enterprise/
        │  HTTP + SSE
FastAPI API (auth · tenant isolation · role checks)     backend/app/api/
        │
Agent Loop: routing → SOP → tools → knowledge           backend/app/llm/ + app/sop_runtime/
            → general skills → reflection → reply
        │
┌───────────────┬────────────────┬──────────────┬───────────────────┐
SOP runtime      Tool execution   Knowledge      Scheduling · approvals
state machine ·  HTTP · MCP ·     bases ·        · audit · traces ·
versions ·       generated code   citations      memory
idempotency      (sandboxed)
        │
SQLite (dev/desktop)  ·  MySQL 8.4 (server, Alembic migrations)
```

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer
- npm
- Optional: Docker Compose and MySQL 8.4 for server-style deployments

## Quick start

### 1. Install dependencies

macOS, Linux, or WSL:

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

cd ../frontend-enterprise
npm ci
cd ..
```

Windows PowerShell:

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env

cd ..\frontend-enterprise
npm ci
cd ..
```

### 2. Start and manage the application

| Operation | macOS, Linux, or WSL | Windows PowerShell |
| --- | --- | --- |
| Development mode | `./app.sh dev` | `.\app.ps1 dev` |
| Detached production mode | `./app.sh` | `.\app.ps1` |
| Show status | `./app.sh status` | `.\app.ps1 status` |
| Stop services | `./app.sh stop` | `.\app.ps1 stop` |

Open `http://localhost:5137`. Swagger UI is available at
`http://localhost:5137/docs`. The default development administrator is
`admin` / `admin`; change the password after the first login.

Both wrappers call the shared `scripts/app.py` lifecycle entry point. You can
also invoke it directly through the project virtual environment:

| Platform | Detached production example |
| --- | --- |
| macOS, Linux, or WSL | `backend/.venv/bin/python scripts/app.py up --detach` |
| Windows PowerShell | `.\backend\.venv\Scripts\python.exe scripts\app.py up --detach` |

Replace `up --detach` with `up --mode development`, `status`, or `down` for the
other lifecycle operations.

## Database

SQLite is the default for desktop and zero-configuration development. To use the
repository MySQL service, configure the root `.env`, then run:

```bash
docker compose up -d mysql
cd backend
.venv/bin/python -m alembic -c alembic.ini upgrade head
```

Set the application connection in `backend/.env`:

```dotenv
DATABASE_URL="mysql+pymysql://gongge_xuban:URL_ENCODED_PASSWORD@127.0.0.1:3306/gongge_xuban?charset=utf8mb4"
```

Operating-system environment variables take precedence over `backend/.env`,
which takes precedence over defaults in `backend/app/config.py`. MySQL
deployments always upgrade through Alembic (`alembic -c alembic.ini upgrade
head`); schema creation shortcuts are not a substitute for migrations.

## Development checks

```bash
cd frontend-enterprise
npm run brand:check:test
npm run brand:check
npm run i18n:check
npm run test:run
npm run test:e2e
npm run test:e2e:fullstack
npm run build

cd ../backend
.venv/bin/ruff check .
.venv/bin/pytest
```

The quality gates include 135+ backend pytest modules, 40+ frontend Vitest files
with Testing Library behavior assertions, brand-literal and i18n-key scanners,
Ruff (Python 3.11, line width 100), and a strict TypeScript build.

Browser regressions run in Playwright Chromium. Before the first run, execute
`npm run test:e2e:install` in `frontend-enterprise/`; minimal Linux environments
also need the Chromium system dependencies reported by Playwright. The full-stack
suite builds the frontend, starts FastAPI, and uses an automatically removed temporary
SQLite database instead of the development database.

## Packaging

Platform build entry points live under `packaging/`: `build_macos.sh`,
`build_windows.ps1`, and `build_linux.sh`. Generated desktop assets use the
`gongge-xuban` slug and the application identifier `cn.gongge.xuban.desktop`.

On a native macOS builder, run `bash packaging/build_macos.sh`. The script aligns
the packaging environment with `backend/pyproject.toml`, signs the `.app`, starts
that packaged executable on the runner's native architecture, verifies the
product-specific health response, and only then creates the DMG. Developer ID
signing and notarization remain controlled by `MAC_SIGN_ID` and
`NOTARY_PROFILE`. Windows signing is handled by `packaging/sign_windows.ps1`;
see [packaging/WINDOWS_SIGNING.md](packaging/WINDOWS_SIGNING.md).
Native Windows installation and frozen-runtime validation is enforced by
`packaging/smoke_windows.ps1`; see
[packaging/WINDOWS_NATIVE_VALIDATION.md](packaging/WINDOWS_NATIVE_VALIDATION.md).

## Project structure

```text
backend/                 FastAPI application, workers, persistence, and tests
frontend-enterprise/     React and TypeScript workspace
packaging/               Desktop build definitions and brand assets
scripts/                 Local process management
```

Product manuals and reference sources are maintained outside the git tree.

## Documentation

- [Backend README](backend/README.md) — backend run and development entry points
- [Frontend README](frontend-enterprise/README.md) — frontend workspace entry points
- [Contributor conventions (AGENTS.md)](AGENTS.md) — architecture rules, quality gates, delivery checklist
- [Windows signing guide](packaging/WINDOWS_SIGNING.md) — certificate and signing pipeline
- [Windows/native validation](packaging/WINDOWS_NATIVE_VALIDATION.md) — install, smoke, and uninstall gate

## Security and limitations

Model output can be incomplete or incorrect. External tools and generated runners
may have real side effects, so use least-privilege credentials and human approval
for high-risk actions. This platform does not replace qualified review in legal,
medical, financial, security, or other regulated fields.
