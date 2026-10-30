# crew-starter
mkdir <<new-app>> or git clone an empty github repo >>
Turns an empty folder into a full-stack TypeScript repo (React web app, Node.js API, PostgreSQL)
that the agent crew can build in. Two things are inside:

- `bootstrap.sh` runs every setup command for you, in order.
- `kit/` holds the six crew agents, the guard (with the config for this layout), the go/no-go poll and the charter.

## 1. Prerequisites (once per machine, macOS)

```bash
brew install node@24 gh gitleaks semgrep osv-scanner   # or: nvm install 24
brew install --cask docker                              # or OrbStack / Rancher Desktop
gh auth login
```
You also need `git`, `python3` 3.8+ and Claude Code.

## 2. Create the project

```bash

bash ~/Downloads/crew-starter/bootstrap.sh
npx playwright install chromium        # browser for the Test Engineer's web tests
```

## 3. Write the product docs (you own these; the crew can only read them)

| File | What goes in it |
|---|---|
| `docs/product/PRD.md` | What you're building and why: users, goals, requirements FR-1, FR-2…, non-functional needs |
| `docs/product/backlog.md` | Ordered requests BL-001, BL-002…, each an outcome a user will see, linked to a PRD requirement |

You don't write tasks. When a backlog item goes to the crew, the Chief of Staff turns it into
`REQ-nnn` and tracks its tasks in `docs/crew/REQ-nnn/board.md`.

```bash
git add docs/product && git commit -m "docs: PRD and first backlog items"
```

## 4. GitHub: create the repo and protect main

```bash
gh repo create claims-portal --private --source . --push
gh api -X PUT 'repos/{owner}/{repo}/branches/main/protection' --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["ci"] },
  "enforce_admins": true,
  "required_pull_request_reviews": { "required_approving_review_count": 0 },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```
After this, every change reaches `main` through a pull request with green CI, including your own
PRD edits, which gives you a history of requirement changes. Set the review count to 1 if the crew
pushes with its own GitHub account; you can't approve your own pull request. Branch protection on
private repos needs a paid GitHub plan.

## 5. Start the crew

```bash
docker compose up -d --wait db && npm run check   # everything green before the crew starts
claude                                            # trust the folder, then run /permissions and /hooks to check, then exit
claude --agent chief-of-staff
```
Then type: `Take BL-002 from docs/product/backlog.md.`

## After a fresh clone (another machine or a teammate)

```bash
make setup        # git hooks, npm ci, .env from the example, PostgreSQL
```
