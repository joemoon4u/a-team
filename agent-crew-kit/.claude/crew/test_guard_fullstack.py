#!/usr/bin/env python3
"""
Self-test for the full-stack TypeScript config (React web + Node.js API + PostgreSQL).
Use this instead of test_guard.py, which checks the default Python layout.

    python3 .claude/crew/test_guard_fullstack.py      # expect: 0 failures

Builds a throwaway repo with the apps/web + apps/api layout (and a worktree, like a
Responsible Engineer gets) and checks each role is allowed or blocked as designed.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "guard.py"
A, B = "ALLOW", "BLOCK"

FILES = [
    "apps/web/src/App.tsx", "apps/web/src/components/ClaimForm.tsx", "apps/web/tsconfig.json",
    "apps/web/package.json", "apps/api/src/routes/claims.ts", "apps/api/src/db/pool.ts",
    "apps/api/db/migrations/001_init.sql", "apps/api/tests/claims.int.test.ts", "apps/api/package.json",
    "apps/api/.env", "apps/api/.env.example", "packages/contracts/src/index.ts",
    "docs/crew/REQ-012/design.md", "docs/crew/REQ-012/build-claims-api.md",
    "package.json", "package-lock.json", "Makefile", "scripts/deploy/staging.sh", "docker-compose.yml",
]


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def make_repo(base):
    repo = Path(base) / "repo"
    repo.mkdir()
    for f in FILES:
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text("x\n")
    g = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com"]
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, *g, "add", "-A")
    sh(repo, *g, "commit", "-q", "-m", "init")
    wt = repo / ".claude" / "worktrees" / "re1"
    sh(repo, "git", "worktree", "add", "-q", "-b", "feature/REQ-012-claims-api", str(wt))
    return repo, wt


def run(role, tool, tool_input, cwd):
    event = {"session_id": "test", "hook_event_name": "PreToolUse", "cwd": str(cwd),
             "tool_name": tool, "tool_input": tool_input}
    r = subprocess.run([sys.executable, str(GUARD), role], input=json.dumps(event),
                       capture_output=True, text=True, cwd=str(cwd))
    reason = r.stderr.strip().splitlines()[0] if r.stderr.strip() else ""
    return (A if r.returncode == 0 else B), reason


def main():
    with tempfile.TemporaryDirectory() as tmp:
        repo, wt = make_repo(tmp)
        home = os.path.expanduser("~")
        bash = lambda c: ("Bash", {"command": c})
        read = lambda p: ("Read", {"file_path": str(p)})
        write = lambda p: ("Write", {"file_path": str(p), "content": "x"})

        cases = [
            # --- every session, including yours ---
            ("baseline", read(repo / "apps/api/.env"), repo, B),
            ("baseline", read(repo / "apps/web/.env.production.local"), repo, B),
            ("baseline", read(repo / "apps/api/.env.example"), repo, A),
            ("baseline", read(home + "/.pgpass"), repo, B),
            ("baseline", read(repo / "backups/claims-2026-10-01.dump"), repo, B),
            ("baseline", bash("pg_dump -h db.prod.internal claims > claims.sql"), repo, B),
            ("baseline", bash("DATABASE_URL=$PROD_DATABASE_URL npx prisma migrate deploy"), repo, B),
            ("baseline", bash("make deploy-staging"), repo, A),

            # --- Responsible Engineer, in its worktree ---
            ("responsible-engineer", write(wt / "apps/api/src/routes/claims.ts"), wt, A),
            ("responsible-engineer", write(wt / "apps/web/src/components/ReviewQueue.tsx"), wt, A),
            ("responsible-engineer", write(wt / "apps/web/src/components/ReviewQueue.test.tsx"), wt, A),
            ("responsible-engineer", write(wt / "apps/api/db/migrations/002_review_queue.sql"), wt, A),
            ("responsible-engineer", write(wt / "apps/api/package.json"), wt, A),
            ("responsible-engineer", write(wt / "package-lock.json"), wt, A),
            ("responsible-engineer", write(wt / "apps/api/.env.example"), wt, A),
            ("responsible-engineer", write(wt / "apps/web/vite.config.ts"), wt, A),
            ("responsible-engineer", write(wt / "apps/api/.env"), wt, B),
            ("responsible-engineer", write(wt / "apps/web/tsconfig.json"), wt, B),
            ("responsible-engineer", write(wt / "eslint.config.js"), wt, B),
            ("responsible-engineer", write(wt / "apps/api/vitest.config.ts"), wt, B),
            ("responsible-engineer", write(wt / "scripts/deploy/staging.sh"), wt, B),
            ("responsible-engineer", write(wt / "Makefile"), wt, B),
            ("responsible-engineer", write(wt / ".husky/pre-commit"), wt, B),
            ("responsible-engineer", write(wt / ".semgrepignore"), wt, B),
            ("responsible-engineer", write(wt / "tests/acceptance/REQ-012/claims.spec.ts"), wt, B),
            ("responsible-engineer", bash("npm test -w apps/api"), wt, A),
            ("responsible-engineer", bash("npm install zod@3.23.8 -w apps/api"), wt, A),
            ("responsible-engineer", bash("npx prisma migrate dev --name review_queue"), wt, A),
            ("responsible-engineer", bash("npx node-pg-migrate create add-review-queue"), wt, A),
            ("responsible-engineer", bash("psql -d claims_test -c 'select count(*) from claims'"), wt, A),
            ("responsible-engineer", bash("npm run migrate:up"), wt, B),
            ("responsible-engineer", bash("npx knex migrate:latest"), wt, B),
            ("responsible-engineer", bash("npm run db:reset"), wt, B),
            ("responsible-engineer", bash("npx prisma migrate reset --force"), wt, B),
            ("responsible-engineer", bash("npx prisma db push --accept-data-loss"), wt, B),
            ("responsible-engineer", bash("psql -d claims_test -c 'DROP TABLE claims'"), wt, B),
            ("responsible-engineer", bash("node -e \"pool.query('truncate table claims')\""), wt, B),
            ("responsible-engineer", bash("pg_dump claims_test > dump.sql"), wt, B),
            ("responsible-engineer", bash("make deploy-staging"), wt, B),
            ("responsible-engineer", bash("git push origin HEAD:main"), wt, B),

            # --- Test Engineer: from the spec only ---
            ("test-engineer", write(repo / "tests/acceptance/REQ-012/claims.spec.ts"), repo, A),
            ("test-engineer", write(repo / "docs/crew/REQ-012/test-report.md"), repo, A),
            ("test-engineer", read(repo / "docs/crew/REQ-012/design.md"), repo, A),
            ("test-engineer", read(repo / "packages/contracts/src/index.ts"), repo, A),
            ("test-engineer", read(repo / "apps/api/src/routes/claims.ts"), repo, B),
            ("test-engineer", read(repo / "apps/web/src/App.tsx"), repo, B),
            ("test-engineer", read(repo / "apps/api/db/migrations/001_init.sql"), repo, B),
            ("test-engineer", read(repo / "apps/api/tests/claims.int.test.ts"), repo, B),
            ("test-engineer", read(repo / "docs/crew/REQ-012/build-claims-api.md"), repo, B),
            ("test-engineer", ("Grep", {"pattern": "useState", "glob": "apps/web/src/**/*.tsx"}), repo, B),
            ("test-engineer", bash("cat apps/api/src/routes/claims.ts"), repo, B),
            ("test-engineer", bash("npm ci && npx playwright test tests/acceptance/REQ-012"), repo, A),
            ("test-engineer", bash("npm install -D @playwright/test"), repo, B),
            ("test-engineer", write(repo / "playwright.config.ts"), repo, B),
            ("test-engineer", bash("npx prisma migrate reset --force"), repo, B),

            # --- Security Engineer: reports, never fixes ---
            ("security-engineer", bash("npm audit --omit=dev --json"), repo, A),
            ("security-engineer", bash("semgrep scan --config p/typescript --json -o /tmp/semgrep.json"), repo, A),
            ("security-engineer", bash("gitleaks detect --redact -v"), repo, A),
            ("security-engineer", bash("grep -rn 'DROP TABLE' apps/api/db/migrations"), repo, A),
            ("security-engineer", read(repo / "apps/api/src/db/pool.ts"), repo, A),
            ("security-engineer", bash("npm audit fix"), repo, B),
            ("security-engineer", bash("npx eslint apps --fix"), repo, B),
            ("security-engineer", write(repo / ".gitleaksignore"), repo, B),
            ("security-engineer", write(repo / "apps/api/src/db/pool.ts"), repo, B),

            # --- Launch Director: staging yes, production never ---
            ("launch-director", bash("make deploy-staging"), repo, A),
            ("launch-director", bash("npx prisma migrate deploy"), repo, A),
            ("launch-director", bash("npm run migrate:up -w apps/api"), repo, A),
            ("launch-director", bash("DATABASE_URL=$PROD_DATABASE_URL npx prisma migrate deploy"), repo, B),
            ("launch-director", bash("npm run migrate:up -- --env production"), repo, B),
            ("launch-director", write(repo / "docs/crew/REQ-012/release-pack.md"), repo, A),
            ("launch-director", write(repo / "apps/api/src/routes/claims.ts"), repo, B),
        ]

        failures = 0
        for role, (tool, ti), cwd, expected in cases:
            got, why = run(role, tool, ti, cwd)
            ok = got == expected
            failures += not ok
            target = ti.get("command") or ti.get("glob") or os.path.relpath(ti.get("file_path", ""), str(cwd))
            print("%s  %-20s %-5s %-62s -> %s%s" % ("ok  " if ok else "FAIL", role, tool, target[:62], got,
                                                   "" if ok else "   (expected %s) %s" % (expected, why)))
        print("\n%d cases, %d failures." % (len(cases), failures))
        sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
