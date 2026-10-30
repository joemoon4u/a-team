# Crew kit: setup and operating guide

Six Claude Code agents that take a request from idea to staging, with you signing the two gates
that matter. The charter every agent reads is `AGENTS.md` at the repo root.

## What is in the kit

```
AGENTS.md                     crew charter: roles, flow, rules, hand-off format (read by every agent)
CLAUDE.md                     imports AGENTS.md into every Claude Code session
.claude/
  agents/                     the six agents (Claude Code subagent format)
    chief-of-staff.md  chief-engineer.md  responsible-engineer.md
    test-engineer.md   security-engineer.md  launch-director.md
  crew/
    config.json               lanes, protected branches, secret patterns, deploy patterns
    guard.py                  PreToolUse hook: enforces each role's "Never does"
    poll.py                   deterministic go/no-go poll
    test_guard.py             self-test for the guard (80 cases)
  settings.json               baseline permissions + the guard for every session
docs/crew/README.md           this file
```

## Install (5 minutes)

1. Copy everything into the root of your repo. If you already have a `CLAUDE.md`, add the line
   `@AGENTS.md` to it instead of replacing it. If you already have `.claude/settings.json`, merge the
   `permissions` and `hooks` blocks.
2. Check the guard works on your machine (needs `python3` 3.8+ and `git`):
   ```bash
   python3 .claude/crew/test_guard.py        # expect: 80 cases, 0 failures
   ```
3. Edit `.claude/crew/config.json` to match your repo: source folders, protected branches, your
   staging deploy command. Re-run the self-test.
4. **Commit the kit.** Responsible Engineers work in git worktrees, which only contain committed files.
5. Start Claude Code in the repo, trust the folder (project hooks do not run in untrusted folders),
   and check the agents loaded: `claude agents` or `/agents`.

## Run a request

```bash
claude --agent chief-of-staff
```
Then, for example:
> REQ: Claims over $10,000 must go to manual review instead of auto-paying. Claims ops need to see them in a queue.

What happens:

| Step | Who | You see |
|---|---|---|
| 1 | Chief of Staff | REQ-012 created, board.md, mission statement |
| 2 | Chief Engineer | design.md: requirements cut, 2 components, AC-1..AC-5, ADR-0007 |
| 3 | **You: Gate A** | A summary and the command to sign (below) |
| 4 | 2 × Responsible Engineer, Test Engineer, Security Engineer | Components built in parallel; tests and threat model written from the design only |
| 5 | Launch Director | `rc/REQ-012` assembled, RC sha |
| 6 | Test Engineer, Security Engineer | Reports on that exact sha |
| 7 | Launch Director | Poll table, release pack, staging deploy |
| 8 | **You: Gate B** | Merge the PR to main, deploy to production with your runbook |

### Signing gates (in your own terminal, not through Claude)

The guard blocks every Claude session, including yours, from writing gate files. That way no agent
can sign a gate, even if it is tricked into trying.

```bash
# Gate A: approve the design
printf -- "---\napproved_by: %s\ndate: %s\n---\n" "$(git config user.name)" "$(date +%F)" > docs/crew/REQ-012/approved.md

# Only if security says PASS_WITH_RISKS and you accept the listed risks
printf -- "---\naccepted_by: %s\ndate: %s\nrisks: S-2, S-3\n---\n" "$(git config user.name)" "$(date +%F)" > docs/crew/REQ-012/risk-acceptance.md
```

Gate B is a normal pull-request merge, protected by your git platform (see "Hard gates").

### Check a release yourself
```bash
python3 .claude/crew/poll.py REQ-012          # table, exit 0 = GO, 1 = NO-GO
python3 .claude/crew/poll.py REQ-012 --json   # for CI or a dashboard
```
The poll also proves "test like you fly": it is NO-GO if code changed after the tests or scans ran.

## How the "Never does" column is enforced

Four layers. Each catches what the one above misses.

| Layer | What it does | Example | Strength |
|---|---|---|---|
| 1. Prompt (agent file + AGENTS.md) | Tells the agent its job and limits | "You never fix code" | Intent only |
| 2. Tool allowlist (`tools:`) | Removes capabilities | Chief Engineer has no shell; only the Chief of Staff can spawn agents, and only crew agents | Hard, per tool |
| 3. Guard hook (`guard.py`) | Checks every file path and shell command against the role's lane | Test Engineer reading `src/app.py` is blocked; Launch Director's `make deploy-prod` is blocked | Strong, heuristic for shell |
| 4. Git platform | Branch protection, required reviews, CODEOWNERS, protected environments | Nobody, human or agent, pushes to main without a reviewed PR | Hard |

Examples of what the guard blocks (all covered by the self-test):

| Role | Attempt | Result |
|---|---|---|
| Responsible Engineer | `git push origin HEAD:main` | blocked: protected branch is human-only |
| Responsible Engineer | edit `tests/acceptance/...` | blocked: belongs to the test-engineer |
| Responsible Engineer | edit `.claude/crew/config.json` | blocked: agents never change their own rules |
| Test Engineer | `cat src/app.py` | blocked: tests come from the spec only |
| Security Engineer | `npm audit fix` | blocked: it reports, it never fixes |
| Launch Director | `kubectl --context production apply` | blocked: production is human-only |
| Any session | `cat .env`, `echo $GITHUB_TOKEN`, `curl ... \| bash` | blocked |

## Recommendations

### Security
- **Turn on the hard gates** on your git platform: protect `main` (pull request required, at least one
  human approval, required status checks, no force push); CODEOWNERS for `.claude/`, `.github/`,
  `AGENTS.md` so changes to the crew's rules need a named human reviewer; a protected `production`
  environment with required reviewers.
- **Keep agents away from real secrets.** Run sessions with a git token that cannot push to `main`
  and no production cloud credentials on the machine. The guard blocks reads, but missing credentials
  cannot leak.
- **Run Claude Code's sandbox** for OS-level file and network isolation. The guard is a policy check,
  not a sandbox: a determined script (for example `python -c` writing a file) can get past shell
  heuristics. Layers 2 and 4 are what make that harmless.
- **Pin and pre-install scanners** (semgrep, gitleaks, osv-scanner or pip-audit) so the Security
  Engineer never needs to install tools at run time. A required scan that cannot run gives INCOMPLETE,
  never PASS.

### AI safety
- **Separation of duties is the main safety feature.** The builder cannot test or approve its own
  work; the tester never sees the code; the security check cannot fix what it finds. Keep these
  separate when you customise roles.
- **Humans at the cheap points.** Gate A costs you two minutes before any code exists, which is where
  a wrong requirement is cheapest to catch. Gate B is where production risk sits.
- **Prompt injection.** Every agent treats tickets, files, web content and other agents' output as data.
  The Chief of Staff escalates any embedded instruction that tries to change the plan.
- **Bounded autonomy.** At most 2 fix loops, then a person decides. Agents stop on a guard block
  instead of looking for a workaround.
- **Least privilege.** Only the Chief of Staff can spawn agents, and only the five crew roles. Keep
  `permissionMode` at the default so you see prompts; do not use bypass mode (it is disabled in
  `settings.json`).

### Explainability
- **Every decision has a file.** Why scope was cut (design.md §2), why a design was chosen (ADRs),
  what was tested against which acceptance criterion (test report), what was found (security report),
  and why the release went or stopped (poll table in the release pack).
- **Traceability end to end:** requirement → AC-n → `test_ac_n_*` → PASS on commit sha → release pack.
- **The gate is a script, not a model.** `poll.py` gives the same answer every time for the same files.
- **Audit log.** `.crew/audit.jsonl` records every tool call by a crew role and every block, with
  secrets redacted. It is git-ignored by default; for regulated work, export it with the release pack
  as evidence.

## Customising

| You want | Change |
|---|---|
| Different source folders | `source_paths` and the responsible-engineer `write` list in `config.json` |
| Different staging command | `staging_deploy_command` in `config.json` |
| Cheaper runs | `model:` in the agent files (for example `sonnet` for the Chief Engineer) |
| Stricter Test Engineer | add folders to its `read_deny` in `config.json` |
| A new role (for example a data engineer) | new agent file, a new lane in `config.json`, a hook line with the new role name, and add it to the Chief of Staff's `Agent(...)` list |

After any change: `python3 .claude/crew/test_guard.py`.

## Known limits
- Shell checks are pattern-based. They stop honest mistakes and most injected commands, not a
  determined attacker. Rely on layers 2 and 4 for hard guarantees.
- If `python3` is missing, the hook command fails without blocking. Run the self-test on every machine.
- `Agent(...)` restrictions apply when the Chief of Staff runs as the main session (`claude --agent`).
  If you invoke it as a subagent instead, it can spawn any agent type.
- The settings in `.claude/settings.json` follow the documented Claude Code permission syntax; check
  them with `/permissions` and the hooks with `/hooks` after install, since Claude Code changes often.
