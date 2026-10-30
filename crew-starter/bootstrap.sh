#!/usr/bin/env bash
# =============================================================================
# bootstrap.sh: turn an EMPTY folder into a repo the agent crew can build in.
#
#   mkdir claims-portal && cd claims-portal
#   bash ~/Downloads/crew-starter/bootstrap.sh      (instructions: crew-starter/README.md)
#
# It sets up, in this order:
#   1. git                      6. packages/contracts (shared API types) + acceptance tests
#   2. the agent crew kit       7. PRD, backlog, ADR-0001, CLAUDE.md, CI, Makefile, git hooks
#   3. root files (.gitignore…) 8. dependencies (exact versions)
#   4. apps/web  (React + TS)   9. lint, typecheck, tests, guard self-test, first commit
#   5. apps/api  (Node + TS + PostgreSQL)
#
# Why a human runs this: the crew is not allowed to edit TypeScript, lint, test or
# CI config, git hooks, deploy scripts or the PRD. Those must exist before it starts.
# =============================================================================
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/kit"
PROJECT="$(basename "$PWD")"
PKG="$(printf %s "$PROJECT" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9._-' '-')"
TODAY="$(date +%F)"
TS_VERSION="6.0.3"   # highest TypeScript that typescript-eslint supports today
export PROJECT PKG TODAY TS_VERSION

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
# Replace __PROJECT__ / __PKG__ / __OWNER__ / __DATE__ placeholders in the given files.
fill() {
  node -e '
    const fs = require("fs");
    const map = { __PROJECT__: process.env.PROJECT, __PKG__: process.env.PKG,
                  __OWNER__: process.env.OWNER, __DATE__: process.env.TODAY };
    for (const f of process.argv.slice(1)) {
      let s = fs.readFileSync(f, "utf8");
      for (const [k, v] of Object.entries(map)) s = s.replaceAll(k, v);
      fs.writeFileSync(f, s);
    }' "$@"
}

# -----------------------------------------------------------------------------
step "0/9 Checking prerequisites"
[ -d "$KIT_DIR/.claude/agents" ] || die "kit/ folder not found next to bootstrap.sh"
if [ -n "$(ls -A | grep -v -x -E '\.DS_Store|\.git' || true)" ]; then die "run this in an empty folder"; fi
for c in node npm git python3 gitleaks; do
  command -v "$c" >/dev/null 2>&1 || die "'$c' is required. See the prerequisites in the instructions."
done
node -e 'process.exit(+process.versions.node.split(".")[0] >= 22 ? 0 : 1)' || die "Node 22 or newer is required (24 LTS recommended)"
command -v docker      >/dev/null 2>&1 || warn "docker not found: you need it for the local PostgreSQL database"
command -v semgrep     >/dev/null 2>&1 || warn "semgrep not found: the Security Engineer will report INCOMPLETE without it"
command -v osv-scanner >/dev/null 2>&1 || warn "osv-scanner not found: dependency scans will fall back to npm audit"
command -v gh          >/dev/null 2>&1 || warn "gh not found: you need it to create the GitHub repo and pull requests"
OWNER="@$(gh api user --jq .login 2>/dev/null || echo your-github-username)"; export OWNER

# -----------------------------------------------------------------------------
step "1/9 git"
[ -d .git ] || git init -q -b main

# -----------------------------------------------------------------------------
step "2/9 Agent crew kit"
cp -R "$KIT_DIR"/. .
chmod +x .claude/crew/*.py

# -----------------------------------------------------------------------------
step "3/9 Root files"
cat > .gitignore <<'EOF'
# ---- Dependencies
node_modules/

# ---- Build and test output
dist/
build/
coverage/
*.tsbuildinfo
.vite/
playwright-report/
test-results/
blob-report/
playwright/.cache/

# ---- Secrets: never commit. The *.example files stay in git.
.env
.env.*
!.env.example
*.pem
*.key
*.p8
*.p12
*.pfx
.pgpass
credentials*.json
service-account*.json

# ---- Database dumps hold real data
*.dump
*.backup
*.sql.gz
backups/

# ---- Logs
logs/
*.log
npm-debug.log*

# ---- Agent crew: local audit log, agent worktrees, personal settings
.crew/
.claude/worktrees/
.claude/settings.local.json

# ---- OS and editors
.DS_Store
Thumbs.db
.idea/
.vscode/*
!.vscode/extensions.json
!.vscode/settings.json
*.swp
EOF

cat > .gitattributes <<'EOF'
* text=auto eol=lf
*.png binary
*.jpg binary
*.ico binary
EOF

cat > .editorconfig <<'EOF'
root = true

[*]
charset = utf-8
end_of_line = lf
indent_style = space
indent_size = 2
insert_final_newline = true
trim_trailing_whitespace = true

[*.md]
trim_trailing_whitespace = false

[Makefile]
indent_style = tab
EOF

echo "24" > .nvmrc

cat > .npmrc <<'EOF'
# Exact versions only, so a dependency can't change underneath you (supply-chain safety).
save-exact=true
engine-strict=true
fund=false
EOF

cat > .dockerignore <<'EOF'
.git
node_modules
**/node_modules
.env
**/.env
**/.env.*
!**/.env.example
.crew
.claude
coverage
**/dist
playwright-report
test-results
EOF

cat > package.json <<'EOF'
{
  "name": "__PKG__",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "workspaces": ["packages/*", "apps/*"],
  "engines": { "node": ">=22" },
  "scripts": {
    "dev": "concurrently -k -n api,web -c blue,magenta \"npm run dev -w apps/api\" \"npm run dev -w apps/web\"",
    "build": "npm run build --workspaces --if-present",
    "lint": "npm run lint --workspaces --if-present",
    "typecheck": "npm run typecheck --workspaces --if-present",
    "test": "npm run test --workspaces --if-present",
    "test:unit": "npm run test:unit --workspaces --if-present",
    "test:acceptance": "playwright test",
    "check": "npm run lint && npm run typecheck && npm test",
    "crew:check": "python3 .claude/crew/test_guard_fullstack.py"
  }
}
EOF

cat > docker-compose.yml <<'EOF'
# Local development only. Postgres listens on 127.0.0.1 and uses trust auth (no password),
# so there is no secret to leak. Never load shared or production data into it.
services:
  db:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_DB: app
      POSTGRES_HOST_AUTH_METHOD: trust
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./docker/postgres/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app"]
      interval: 2s
      timeout: 3s
      retries: 30

volumes:
  pgdata:
EOF
mkdir -p docker/postgres/init
cat > docker/postgres/init/01-create-test-db.sql <<'EOF'
-- A separate database for tests. Integration tests refuse any database whose name doesn't end in _test.
CREATE DATABASE app_test;
EOF

# -----------------------------------------------------------------------------
step "4/9 apps/web (React + Vite + TypeScript)"
npx --yes create-vite@9.3.3 apps/web --template react-ts --eslint --no-interactive --no-immediate >/dev/null
rm -rf apps/web/README.md apps/web/src/App.css apps/web/src/assets apps/web/public/icons.svg

node <<'EOF'
const fs = require('fs')
const edit = (file, fn) => fs.writeFileSync(file, fn(fs.readFileSync(file, 'utf8')))
const must = (s, anchor, file) => { if (!s.includes(anchor)) { console.error(`bootstrap: '${anchor}' not found in ${file}`); process.exit(1) } return s }

edit('apps/web/package.json', (s) => {
  const pkg = JSON.parse(s)
  pkg.name = '@app/web'
  pkg.scripts = { ...pkg.scripts, typecheck: 'tsc -b', test: 'vitest run', 'test:unit': 'vitest run' }
  pkg.dependencies = { ...pkg.dependencies, '@app/contracts': '0.0.0' }
  // typescript-eslint supports TypeScript < 6.1, but the template ships 7.x. Pin 6.0 so lint works.
  pkg.devDependencies = { ...pkg.devDependencies, typescript: process.env.TS_VERSION }
  return JSON.stringify(pkg, null, 2) + '\n'
})
// Strict TypeScript, stated explicitly so nobody can quietly relax it (tsconfig is human-owned).
edit('apps/web/tsconfig.app.json', (s) => must(s, '"compilerOptions": {', 'tsconfig.app.json')
  .replace('"compilerOptions": {', '"compilerOptions": {\n    "strict": true,\n    "noUncheckedIndexedAccess": true,'))
edit('apps/web/tsconfig.node.json', (s) => must(s, '"include": ["vite.config.ts"]', 'tsconfig.node.json')
  .replace('"include": ["vite.config.ts"]', '"include": ["vite.config.ts", "vitest.config.ts"]'))
EOF

cat > apps/web/vite.config.ts <<'EOF'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development, /api/* goes to the API on :3000, so the browser never needs its address.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://localhost:3000', rewrite: (path) => path.replace(/^\/api/, '') },
    },
  },
})
EOF

cat > apps/web/vitest.config.ts <<'EOF'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
EOF

cat > apps/web/src/test-setup.ts <<'EOF'
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => cleanup())
EOF

cat > apps/web/src/App.tsx <<'EOF'
import { useEffect, useState } from 'react'
import type { HealthResponse } from '@app/contracts'

type ApiStatus = HealthResponse | 'checking' | 'unreachable'

function describe(status: ApiStatus): string {
  if (status === 'checking') return 'checking…'
  if (status === 'unreachable') return 'unreachable'
  return `${status.status} (database ${status.db})`
}

export default function App() {
  const [status, setStatus] = useState<ApiStatus>('checking')

  useEffect(() => {
    fetch('/api/health')
      .then((res) => res.json() as Promise<HealthResponse>)
      .then(setStatus)
      .catch(() => setStatus('unreachable'))
  }, [])

  return (
    <main>
      <h1>__PROJECT__</h1>
      <p>API: {describe(status)}</p>
    </main>
  )
}
EOF

cat > apps/web/src/App.test.tsx <<'EOF'
import { render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import App from './App'

afterEach(() => vi.unstubAllGlobals())

it('shows the API and database status', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ json: async () => ({ status: 'ok', db: 'up' }) }))
  render(<App />)
  expect(await screen.findByText('API: ok (database up)')).toBeInTheDocument()
})

it('says when the API cannot be reached', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
  render(<App />)
  expect(await screen.findByText('API: unreachable')).toBeInTheDocument()
})
EOF

cat > apps/web/.env.example <<'EOF'
# Vite bundles every VITE_* variable into the JavaScript sent to browsers.
# Never put a secret, key or token here.
# VITE_FEATURE_FLAG_EXAMPLE=true
EOF
fill apps/web/src/App.tsx

# -----------------------------------------------------------------------------
step "5/9 apps/api (Node.js + TypeScript + PostgreSQL)"
mkdir -p apps/api/src/db apps/api/tests apps/api/db/migrations
touch apps/api/db/migrations/.gitkeep

cat > apps/api/package.json <<'EOF'
{
  "name": "@app/api",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "dev": "tsx watch --env-file-if-exists=.env src/server.ts",
    "build": "tsc -p tsconfig.build.json",
    "start": "node --env-file-if-exists=.env dist/server.js",
    "typecheck": "tsc --noEmit",
    "lint": "eslint .",
    "test": "vitest run",
    "test:unit": "vitest run --project unit",
    "migrate:create": "node-pg-migrate create --migrations-dir db/migrations --migration-file-language sql",
    "migrate:up": "node-pg-migrate up --migrations-dir db/migrations",
    "migrate:down": "node-pg-migrate down --migrations-dir db/migrations"
  },
  "dependencies": {
    "@app/contracts": "0.0.0"
  }
}
EOF

cat > apps/api/tsconfig.json <<'EOF'
{
  "compilerOptions": {
    "target": "es2023",
    "lib": ["ES2023"],
    "module": "nodenext",
    "moduleResolution": "nodenext",
    "types": ["node"],
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "verbatimModuleSyntax": true,
    "skipLibCheck": true,
    "noEmit": true
  },
  "include": ["src", "tests", "vitest.config.ts"]
}
EOF

cat > apps/api/tsconfig.build.json <<'EOF'
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "noEmit": false,
    "rootDir": "src",
    "outDir": "dist",
    "sourceMap": true
  },
  "include": ["src"],
  "exclude": ["src/**/*.test.ts"]
}
EOF

cat > apps/api/eslint.config.js <<'EOF'
import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.ts'],
    extends: [js.configs.recommended, tseslint.configs.recommended],
    languageOptions: { globals: globals.node },
  },
])
EOF

cat > apps/api/vitest.config.ts <<'EOF'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    projects: [
      { test: { name: 'unit', environment: 'node', include: ['src/**/*.test.ts'] } },
      {
        test: {
          name: 'integration',
          environment: 'node',
          include: ['tests/**/*.int.test.ts'],
          globalSetup: ['tests/global-setup.ts'],
        },
      },
    ],
  },
})
EOF

cat > apps/api/src/config.ts <<'EOF'
import { z } from 'zod'

// Parse configuration once, at startup. Everything after this trusts the types.
const ConfigSchema = z.object({
  PORT: z.coerce.number().int().min(1).max(65535).default(3000),
  DATABASE_URL: z.string().min(1, 'DATABASE_URL is required'),
})

export type Config = z.infer<typeof ConfigSchema>

export function loadConfig(env: NodeJS.ProcessEnv): Config {
  return ConfigSchema.parse(env)
}
EOF

cat > apps/api/src/db/pool.ts <<'EOF'
import pg from 'pg'

export interface Db {
  query(sql: string, values?: unknown[]): Promise<unknown>
}

export function createPool(connectionString: string): pg.Pool {
  return new pg.Pool({
    connectionString,
    max: 10,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 5_000,
  })
}
EOF

cat > apps/api/src/app.ts <<'EOF'
import express from 'express'
import helmet from 'helmet'
import type { HealthResponse } from '@app/contracts'
import type { Db } from './db/pool.js'

export interface AppDeps {
  db: Db
}

export function createApp({ db }: AppDeps): express.Express {
  const app = express()
  app.disable('x-powered-by')
  app.use(helmet())
  app.use(express.json({ limit: '100kb' }))

  app.get('/health', async (_req, res) => {
    try {
      await db.query('select 1')
      res.json({ status: 'ok', db: 'up' } satisfies HealthResponse)
    } catch {
      res.status(503).json({ status: 'degraded', db: 'down' } satisfies HealthResponse)
    }
  })

  return app
}
EOF

cat > apps/api/src/app.test.ts <<'EOF'
import request from 'supertest'
import { describe, expect, it } from 'vitest'
import { createApp } from './app.js'

describe('GET /health', () => {
  it('returns 200 when the database answers', async () => {
    const app = createApp({ db: { query: async () => ({ rows: [] }) } })
    const res = await request(app).get('/health')
    expect(res.status).toBe(200)
    expect(res.body).toEqual({ status: 'ok', db: 'up' })
  })

  it('returns 503 when the database is down', async () => {
    const app = createApp({
      db: {
        query: async () => {
          throw new Error('connection refused')
        },
      },
    })
    const res = await request(app).get('/health')
    expect(res.status).toBe(503)
    expect(res.body).toEqual({ status: 'degraded', db: 'down' })
  })
})
EOF

cat > apps/api/src/server.ts <<'EOF'
import { createApp } from './app.js'
import { loadConfig } from './config.js'
import { createPool } from './db/pool.js'

const config = loadConfig(process.env)
const pool = createPool(config.DATABASE_URL)

const server = createApp({ db: pool }).listen(config.PORT, () => {
  console.log(`api listening on http://localhost:${config.PORT}`)
})

function shutdown(): void {
  server.close(() => void pool.end())
}
process.on('SIGINT', shutdown)
process.on('SIGTERM', shutdown)
EOF

cat > apps/api/tests/test-db.ts <<'EOF'
// Integration tests only ever touch a database whose name ends in _test.
// A mis-set variable can't point the suite at real data.
export function testDatabaseUrl(): string {
  const url = process.env.TEST_DATABASE_URL ?? 'postgres://app@localhost:5432/app_test'
  const name = new URL(url).pathname.slice(1)
  if (!name.endsWith('_test')) {
    const message = `Refusing to run tests against database "${name}": its name must end in _test`
    console.error(message)
    throw new Error(message)
  }
  return url
}
EOF

cat > apps/api/tests/global-setup.ts <<'EOF'
import { runner } from 'node-pg-migrate'
import { testDatabaseUrl } from './test-db.js'

// Bring the throwaway test database up to the latest migration before integration tests run.
export default async function setup(): Promise<void> {
  await runner({
    databaseUrl: testDatabaseUrl(),
    dir: 'db/migrations',
    direction: 'up',
    migrationsTable: 'pgmigrations',
    log: () => {},
  })
}
EOF

cat > apps/api/tests/health.int.test.ts <<'EOF'
import request from 'supertest'
import { afterAll, describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import { createPool } from '../src/db/pool.js'
import { testDatabaseUrl } from './test-db.js'

const pool = createPool(testDatabaseUrl())
afterAll(() => pool.end())

describe('GET /health against a real PostgreSQL database', () => {
  it('reports the database as up', async () => {
    const res = await request(createApp({ db: pool })).get('/health')
    expect(res.status).toBe(200)
    expect(res.body).toEqual({ status: 'ok', db: 'up' })
  })
})
EOF

cat > apps/api/.env.example <<'EOF'
# Copy to apps/api/.env for local development (bootstrap.sh does this for you).
# Local Docker Postgres uses trust auth on 127.0.0.1, so there is no password here.
# Real environments get DATABASE_URL from a secrets manager, never from a file in this repo.
PORT=3000
DATABASE_URL=postgres://app@localhost:5432/app
TEST_DATABASE_URL=postgres://app@localhost:5432/app_test
EOF

# -----------------------------------------------------------------------------
step "6/9 packages/contracts and acceptance tests"
mkdir -p packages/contracts/src tests/acceptance/_smoke
cat > packages/contracts/package.json <<'EOF'
{
  "name": "@app/contracts",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "exports": {
    ".": { "types": "./src/index.ts", "default": "./src/index.ts" }
  }
}
EOF
cat > packages/contracts/src/index.ts <<'EOF'
// API request and response types shared by apps/web and apps/api.
// Types only: import them with `import type` so nothing from here runs at runtime.

export interface HealthResponse {
  status: 'ok' | 'degraded'
  db: 'up' | 'down'
}
EOF

cat > playwright.config.ts <<'EOF'
import { defineConfig, devices } from '@playwright/test'

// Acceptance tests live in tests/acceptance/<REQ>/ and belong to the Test Engineer.
//   *.api.spec.ts  call the API over HTTP (no browser)
//   *.web.spec.ts  drive the web app in Chromium
const API_URL = process.env.API_URL ?? 'http://localhost:3000'
const WEB_URL = process.env.WEB_URL ?? 'http://localhost:5173'

export default defineConfig({
  testDir: './tests/acceptance',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0, // a flaky test is a failing test
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { trace: 'retain-on-failure' },
  projects: [
    { name: 'api', testMatch: /.*\.api\.spec\.ts/, use: { baseURL: API_URL } },
    { name: 'web', testMatch: /.*\.web\.spec\.ts/, use: { ...devices['Desktop Chrome'], baseURL: WEB_URL } },
  ],
  webServer: [
    { command: 'npm run dev -w apps/api', url: `${API_URL}/health`, reuseExistingServer: !process.env.CI, timeout: 60_000 },
    { command: 'npm run dev -w apps/web', url: WEB_URL, reuseExistingServer: !process.env.CI, timeout: 60_000 },
  ],
})
EOF

cat > tests/acceptance/_smoke/health.api.spec.ts <<'EOF'
import { expect, test } from '@playwright/test'

test('API reports healthy with the database up', async ({ request }) => {
  const res = await request.get('/health')
  expect(res.status()).toBe(200)
  expect(await res.json()).toEqual({ status: 'ok', db: 'up' })
})
EOF
cat > tests/acceptance/_smoke/home.web.spec.ts <<'EOF'
import { expect, test } from '@playwright/test'

test('home page shows the API and database status', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('API: ok (database up)')).toBeVisible()
})
EOF

# -----------------------------------------------------------------------------
step "7/9 Product docs, CLAUDE.md, CI, Makefile, git hooks"
mkdir -p docs/product docs/adr .github/workflows scripts/deploy .githooks

cat > docs/product/PRD.md <<'EOF'
# __PROJECT__: Product Requirements Document

> Human-owned. Every agent reads this file; no crew agent can edit it (the guard blocks it).
> Keep it short and testable. The Chief Engineer challenges requirements in each request's
> design.md, not here.

| Field | Value |
|---|---|
| Owner | <your name> |
| Status | Draft |
| Last updated | __DATE__ |

## 1. Problem
What is broken or missing today, for whom, and what it costs. 3-5 lines.

## 2. Users
| User | What they need to get done |
|---|---|
| <e.g. Claims officer> | <e.g. Review high-value claims before they are paid> |

## 3. Goals and how we measure them
| Goal | Measure | Target |
|---|---|---|
| <e.g. No unreviewed high-value payouts> | <Claims over $10k paid without review> | <0> |

## 4. Scope
In:
Out (and why):

## 5. Requirements
Give each one an ID. Backlog items and the crew's acceptance criteria point back to these.

| ID | Requirement | Priority | Notes |
|---|---|---|---|
| FR-1 | <e.g. A claim over $10,000 is held for manual review before payment> | Must | |

## 6. Non-functional requirements
- Security: authentication, permissions per role, audit trail (who did what, when)
- Privacy and data: personal data held, its classification, where it is stored, how long
- Compliance: the regulations and internal standards that apply
- Performance and availability: e.g. p95 under 300 ms, 99.9% in business hours
- Accessibility: WCAG 2.2 AA

## 7. AI in the product (delete if not used)
What the AI may do on its own, what needs a person to confirm, and what data it may see.

## 8. Constraints and assumptions

## 9. Risks and open questions
EOF

cat > docs/product/backlog.md <<'EOF'
# Backlog

> Human-owned. You set the order; the crew takes one item at a time.
> Start an item:  `claude --agent chief-of-staff`  then  "Take BL-002 from docs/product/backlog.md."
> The Chief of Staff turns it into REQ-nnn and tracks its tasks in docs/crew/REQ-nnn/board.md.
> When it ships, set Status to done and fill in the REQ column.

Status: idea → ready → in-crew → done (or dropped)

| ID | Request (the outcome a user will see) | PRD | Priority | Status | REQ |
|---|---|---|---|---|---|
| BL-001 | Project skeleton: web, API, PostgreSQL, CI, agent crew | - | P0 | done | - |
| BL-002 | <first real feature, written as an outcome> | FR-1 | P1 | idea | |

## An item is ready when
- [ ] It links to a PRD requirement
- [ ] It describes an outcome you can test ("a claim over $10k appears in the review queue"), not a task ("add a table")
- [ ] Any data, security or compliance constraint is written in the PRD
EOF

cat > docs/adr/0001-tech-stack.md <<'EOF'
# ADR-0001: Tech stack and repository layout
Status: accepted
Date: __DATE__   Request: bootstrap

Context: Greenfield product built by an AI agent crew with human gates. We need typed contracts
between front end and back end, boundaries that are easy to test, and database changes that a
reviewer can read.

Options: 1) Two repos (web, api)  2) One repo with npm workspaces  3) Full-stack framework (e.g. Next.js)

Decision: Option 2.
- apps/web: React + Vite + TypeScript. apps/api: Node.js + Express 5 + TypeScript.
- packages/contracts: API types shared by both, so contract drift fails the build.
- PostgreSQL 17, plain SQL migrations with node-pg-migrate (easy to review line by line).
- Vitest for unit and integration tests, Playwright for acceptance tests.
- Config parsed with zod at startup; secrets only from the environment.

Consequences: + one CI pipeline and one place for the crew's rules; + compile-time contract checks.
- Both apps share one dependency tree; - a separate API deploy needs its own build step later.
EOF

cat > CLAUDE.md <<'EOF'
# __PROJECT__

@AGENTS.md

## Product
- PRD: `docs/product/PRD.md`. Read it before designing or building anything.
- Backlog: `docs/product/backlog.md`. The ordered list of requests. Each one becomes `REQ-nnn` in `docs/crew/`.
- Both are human-owned. Agents read them and never edit them.

## Stack
React + Vite + TypeScript (apps/web), Node.js + Express 5 + TypeScript (apps/api), PostgreSQL 17,
npm workspaces. Why: `docs/adr/0001-tech-stack.md`.

## Where things go
| Path | What | Owner |
|---|---|---|
| apps/web/src | UI. Unit tests next to the code: `*.test.tsx` | responsible-engineer |
| apps/api/src | API. Unit tests next to the code: `*.test.ts` | responsible-engineer |
| apps/api/tests | Integration tests `*.int.test.ts`, run against the `app_test` database | responsible-engineer |
| apps/api/db/migrations | SQL migrations | responsible-engineer |
| packages/contracts | API request and response types shared by web and api | responsible-engineer |
| tests/acceptance/REQ-nnn | Playwright: `*.api.spec.ts` (HTTP) and `*.web.spec.ts` (browser) | test-engineer |
| docs/product, configs, CI, hooks, deploy scripts | | human |

## Commands
| Task | Command |
|---|---|
| Start PostgreSQL | `docker compose up -d --wait db` |
| Run web + api | `npm run dev` (api on :3000, web on :5173, `/api` proxied to the API) |
| Lint, typecheck, all tests | `npm run check` |
| Unit tests only (no database) | `npm run test:unit` |
| Acceptance tests for a request | `npm run test:acceptance -- tests/acceptance/REQ-nnn` |
| New migration | `npm run migrate:create -w apps/api -- <name>` |
| Guard self-test | `npm run crew:check` |

## Conventions
- TypeScript strict. Parse outside input with zod at the boundary (HTTP bodies, env, external APIs); trust the types inside.
- Share API shapes through `packages/contracts` and import them with `import type`.
- Migrations are SQL. Tests apply them to `app_test` automatically, and integration tests refuse any
  database whose name doesn't end in `_test`. The Launch Director applies migrations to staging; a human applies them to production.
- Never put secrets in `VITE_*` variables: Vite bundles them into the browser.
- Local PostgreSQL uses trust auth on 127.0.0.1 only. Real environments get `DATABASE_URL` from a secrets manager.
EOF

cat > README.md <<'EOF'
# __PROJECT__

Built by an agent crew with human gates. Start with:
- `docs/product/PRD.md`: what we are building and why
- `docs/product/backlog.md`: what's next
- `CLAUDE.md`: stack, layout, commands
- `AGENTS.md` and `docs/crew/README.md`: how the crew works

Quick start: `make setup`, then `npm run dev`.
EOF

cat > .github/workflows/ci.yml <<'EOF'
name: ci
on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  ci:
    name: ci
    runs-on: ubuntu-latest
    timeout-minutes: 20
    services:
      postgres:
        image: postgres:17-alpine
        env:
          POSTGRES_USER: app
          POSTGRES_DB: app_test
          POSTGRES_HOST_AUTH_METHOD: trust
        ports: ['5432:5432']
        options: >-
          --health-cmd "pg_isready -U app"
          --health-interval 2s --health-timeout 5s --health-retries 30
    env:
      TEST_DATABASE_URL: postgres://app@localhost:5432/app_test
    steps:
      # For regulated work, pin each action to a full commit SHA instead of a tag.
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version-file: .nvmrc
          cache: npm
      - run: npm ci
      - run: npm run lint
      - run: npm run typecheck
      - run: npm test
      - run: npm run build
      - run: npm audit --omit=dev --audit-level=high
      - name: Crew guard self-test
        run: python3 .claude/crew/test_guard_fullstack.py
EOF

cat > .github/pull_request_template.md <<'EOF'
## Why
<!-- The user outcome in one or two lines. Link the request: docs/crew/REQ-nnn/ -->

## What changed

## Evidence
- Go/no-go: `python3 .claude/crew/poll.py REQ-nnn` → GO
- Release pack: docs/crew/REQ-nnn/release-pack.md

## Human checks (Gate B)
- [ ] Read the release pack: what ships, security, rollback
- [ ] Reviewed any database migrations line by line
EOF

cat > .github/CODEOWNERS <<'EOF'
# Changes to the crew's rules, CI, configs and product docs need the owner's review.
*               __OWNER__
/.claude/       __OWNER__
/.github/       __OWNER__
/docs/product/  __OWNER__
EOF

cat > Makefile <<'EOF'
# Human-owned. The crew can run these targets but can't edit this file.
.PHONY: setup hooks db dev check crew-check deploy-staging

setup: hooks      ## after a fresh clone: git hooks, dependencies, PostgreSQL
	npm ci
	[ -f apps/api/.env ] || cp apps/api/.env.example apps/api/.env
	docker compose up -d --wait db

hooks:            ## point git at .githooks (absolute path, so agent worktrees use it too)
	git config core.hooksPath "$(CURDIR)/.githooks"

db:               ## start PostgreSQL
	docker compose up -d --wait db

dev:              ## run api and web
	npm run dev

check:            ## lint, typecheck, all tests
	npm run check

crew-check:       ## guard self-test
	python3 .claude/crew/test_guard_fullstack.py

deploy-staging:   ## Launch Director only (the guard enforces this)
	./scripts/deploy/staging.sh
EOF

cat > scripts/deploy/staging.sh <<'EOF'
#!/usr/bin/env bash
# Human-owned. Called by `make deploy-staging`, which only the Launch Director may run.
# Fill in for your platform: build, run `npm run migrate:up -w apps/api` against the STAGING
# database, then roll out. This script must never be able to reach production.
set -euo pipefail
echo "scripts/deploy/staging.sh is not set up yet. Ask the project owner to write it." >&2
exit 1
EOF
chmod +x scripts/deploy/staging.sh

cat > .githooks/pre-commit <<'EOF'
#!/bin/sh
# Human-owned. Agents can't edit .githooks/ and can't skip hooks (--no-verify is blocked).
# git finds this hook through an ABSOLUTE core.hooksPath, so it also runs in agent worktrees.
if ! command -v gitleaks >/dev/null 2>&1; then
  echo "pre-commit: gitleaks is not installed (brew install gitleaks). Commit blocked so no secret slips through." >&2
  exit 1
fi
gitleaks git --pre-commit --redact --staged --verbose
EOF
chmod +x .githooks/pre-commit
git config core.hooksPath "$PWD/.githooks"

fill package.json docs/product/PRD.md docs/product/backlog.md docs/adr/0001-tech-stack.md CLAUDE.md README.md .github/CODEOWNERS

# -----------------------------------------------------------------------------
step "8/9 Installing dependencies (exact versions)"
npm install --no-audit --loglevel=error -D @playwright/test@1 concurrently@10
npm install --no-audit --loglevel=error -w apps/api express@5 helmet@8 pg@8 zod@4
npm install --no-audit --loglevel=error -w apps/api -D "typescript@$TS_VERSION" tsx@4 vitest@5 supertest@7 \
  @types/supertest@7 @types/express@5 @types/pg@8 @types/node@24 eslint@10 @eslint/js@10 \
  typescript-eslint@8 globals@17 node-pg-migrate@9 dotenv@18
npm install --no-audit --loglevel=error -w apps/web -D vitest@5 jsdom@30 @testing-library/react@16 @testing-library/jest-dom@7
cp apps/api/.env.example apps/api/.env

# -----------------------------------------------------------------------------
step "9/9 Checks and first commit"
npm run lint --silent
npm run typecheck --silent
npm run test:unit --silent
python3 .claude/crew/test_guard_fullstack.py | tail -1
if docker info >/dev/null 2>&1; then
  docker compose up -d --wait db
  npm test --silent
else
  warn "Docker is not running, so the API integration tests were skipped. Later: make db && npm test"
fi
git add -A
git commit -q -m "chore: bootstrap $PROJECT with agent crew"
git log --oneline -1

cat <<EOF

Done. Next (details in crew-starter/README.md):
  1. npx playwright install chromium
  2. Write docs/product/PRD.md and docs/product/backlog.md, then commit them.
  3. Create the GitHub repo and protect main.
  4. Start Claude Code here, trust the folder, and check /permissions and /hooks.
  5. claude --agent chief-of-staff   then: "Take BL-002 from docs/product/backlog.md."
  Later: write scripts/deploy/staging.sh when you have a staging environment.
EOF
