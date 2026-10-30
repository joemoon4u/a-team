#!/usr/bin/env python3
"""
Go/no-go poll for one request. Deterministic: it reads the crew's files and git,
it does not ask a model for an opinion.

    python3 .claude/crew/poll.py REQ-042 [--rc rc/REQ-042] [--json]

GO only when every check passes:
  1. Gate A      docs/crew/<REQ>/approved.md exists (signed by a human)
  2. Design      design.md exists and defines acceptance criteria (AC-1, AC-2, ...)
  3. Builds      every build-*.md says  verdict: DONE
  4. Tests       test-report.md says  verdict: PASS  and shows every AC passing
  5. Security    security-report.md says  verdict: PASS
                 (PASS_WITH_RISKS also needs a human-signed risk-acceptance.md)
  6. Same build  test and security reports name the commit they checked, and it is
                 the release candidate (or differs from it only in docs/crew/** files)

Exit code: 0 = GO, 1 = NO-GO, 2 = usage error.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


def git(*args):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def meta(path):
    """Read 'key: value' pairs from YAML-style front matter, or from the whole file if there is none."""
    text = path.read_text(encoding="utf-8")
    block = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        block = text[3:end] if end != -1 else ""
    out = {}
    for line in block.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            v = v.split(" #", 1)[0].strip().strip("'\"")
            out.setdefault(k.strip().lower().replace(" ", "_"), v)
    return out


def main():
    ap = argparse.ArgumentParser(description="Crew go/no-go poll")
    ap.add_argument("req", help="request id, e.g. REQ-042")
    ap.add_argument("--rc", help="release candidate ref (default rc/<REQ>, falls back to HEAD)")
    ap.add_argument("--json", action="store_true", help="print machine-readable result")
    a = ap.parse_args()

    root = Path(git("rev-parse", "--show-toplevel") or ".")
    d = root / "docs" / "crew" / a.req
    if not d.is_dir():
        print("No folder %s. Is the request id right?" % d, file=sys.stderr)
        sys.exit(2)

    rc_ref = a.rc or ("rc/" + a.req)
    rc_sha = git("rev-parse", "--verify", "--quiet", rc_ref + "^{commit}") or git("rev-parse", "HEAD")
    rows = []

    def add(check, owner, ok, detail):
        rows.append({"check": check, "owner": owner, "result": "GO" if ok else "NO-GO", "detail": detail})

    # 1. Gate A
    gate = d / "approved.md"
    if gate.exists():
        m = meta(gate)
        add("Gate A signed", "human", bool(m.get("approved_by")),
            "approved_by: %s %s" % (m.get("approved_by", "(missing)"), m.get("date", "")))
    else:
        add("Gate A signed", "human", False, "approved.md not found; a human must sign the design")

    # 2. Design and acceptance criteria
    design = d / "design.md"
    acs = set()
    if design.exists():
        acs = set(re.findall(r"\bAC-\d+\b", design.read_text(encoding="utf-8")))
        add("Design + acceptance criteria", "chief-engineer", bool(acs), "%d acceptance criteria" % len(acs))
    else:
        add("Design + acceptance criteria", "chief-engineer", False, "design.md not found")

    # 3. Builds
    builds = sorted(d.glob("build-*.md"))
    if not builds:
        add("Builds", "responsible-engineer", False, "no build-*.md notes found")
    for b in builds:
        m = meta(b)
        add("Build: " + b.stem[len("build-"):], "responsible-engineer", m.get("verdict", "").upper() == "DONE",
            "%s on %s" % (m.get("verdict", "no verdict"), m.get("branch", "?")))

    # 4 + 5. Test and security reports
    checked = {}
    test = d / "test-report.md"
    if test.exists():
        m = meta(test)
        checked["test-report.md"] = m.get("commit", "")
        body = test.read_text(encoding="utf-8")
        passing = {ac for ac in acs if any(ac in ln and "PASS" in ln.upper() and "FAIL" not in ln.upper()
                                           for ln in body.splitlines() if re.search(r"\b%s\b" % ac, ln))}
        missing = sorted(acs - passing, key=lambda x: int(x.split("-")[1]))
        ok = m.get("verdict", "").upper() == "PASS" and not missing
        detail = "%s, %d/%d ACs pass" % (m.get("verdict", "no verdict"), len(passing), len(acs))
        if missing:
            detail += "; not passing: " + ", ".join(missing)
        add("Acceptance tests", "test-engineer", ok, detail)
    else:
        add("Acceptance tests", "test-engineer", False, "test-report.md not found")

    sec = d / "security-report.md"
    if sec.exists():
        m = meta(sec)
        checked["security-report.md"] = m.get("commit", "")
        v = m.get("verdict", "").upper()
        risk_file = d / "risk-acceptance.md"
        if v == "PASS":
            add("Security", "security-engineer", True, "PASS")
        elif v == "PASS_WITH_RISKS":
            signed = risk_file.exists() and bool(meta(risk_file).get("accepted_by"))
            add("Security", "security-engineer", signed,
                "PASS_WITH_RISKS, " + ("risks accepted by a human" if signed else "needs a human-signed risk-acceptance.md"))
        else:
            add("Security", "security-engineer", False, (v or "no verdict") + " (see security-report.md)")
    else:
        add("Security", "security-engineer", False, "security-report.md not found")

    # 6. Same build: what was checked is what will ship
    for name, commit in checked.items():
        if not commit:
            add("Same build: " + name, "launch-director", False, "report does not name the commit it checked")
            continue
        full = git("rev-parse", "--verify", "--quiet", commit + "^{commit}")
        if not full:
            add("Same build: " + name, "launch-director", False, "unknown commit %s" % commit)
        elif full == rc_sha:
            add("Same build: " + name, "launch-director", True, "checked %s = release candidate" % full[:9])
        else:
            changed = [f for f in git("diff", "--name-only", full, rc_sha).splitlines() if f]
            code = [f for f in changed if not f.startswith("docs/crew/")]
            add("Same build: " + name, "launch-director", not code,
                ("checked %s; later commits change only docs/crew" % full[:9]) if not code else
                "checked %s but the release candidate changed code since: %s" % (full[:9], ", ".join(code[:5])))

    overall = "GO" if rows and all(r["result"] == "GO" for r in rows) else "NO-GO"

    # Guard activity, for the release pack (explainability)
    blocks = Counter()
    common = git("rev-parse", "--git-common-dir")
    log = (Path(common).resolve().parent if common else root) / ".crew" / "audit.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                if e.get("decision") == "BLOCK":
                    blocks[e.get("role", "?")] += 1
            except ValueError:
                pass

    if a.json:
        print(json.dumps({"request": a.req, "release_candidate": rc_ref, "commit": rc_sha, "overall": overall,
                          "checks": rows, "guard_blocks_by_role": dict(blocks)}, indent=2))
    else:
        print("Go/no-go poll: %s   release candidate %s @ %s\n" % (a.req, rc_ref, rc_sha[:9]))
        print("%-32s %-22s %-6s %s" % ("CHECK", "OWNER", "RESULT", "DETAIL"))
        for r in rows:
            print("%-32s %-22s %-6s %s" % (r["check"][:32], r["owner"], r["result"], r["detail"]))
        if blocks:
            print("\nGuard blocks recorded: " + ", ".join("%s %d" % kv for kv in sorted(blocks.items())))
        print("\nOVERALL: %s" % overall)
    sys.exit(0 if overall == "GO" else 1)


if __name__ == "__main__":
    main()
