#!/usr/bin/env python3
"""
Crew guard: a Claude Code PreToolUse hook that enforces each role's "Never does" list.

    python3 .claude/crew/guard.py <role>

    role = baseline | chief-of-staff | chief-engineer | responsible-engineer |
           test-engineer | security-engineer | launch-director

Reads the hook event (JSON) on stdin.
    exit 0 -> allow
    exit 2 -> block. The reason on stderr is sent back to the agent so it can re-plan.

Every decision for a crew role, and every block, is appended to .crew/audit.jsonl
(secrets redacted) so you can explain afterwards what each agent tried to do.

This is a policy layer, not a sandbox. It stops honest mistakes and most
prompt-injected actions. Branch protection, CODEOWNERS and CI are the hard gates
behind it. Python 3.8+, standard library only. Fails closed on internal errors.
"""
import datetime as dt
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
ROLES = CONFIG["roles"]
CREW = set(ROLES)
PROTECTED = set(CONFIG["protected_branches"])

FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
FILE_READ_TOOLS = {"Read"}
SEARCH_TOOLS = {"Grep", "Glob"}

NEXT_STEP = ("If you believe this block is wrong, stop and report it in your HANDOFF "
             "(status: BLOCKED). Do not look for another route around it.")


class Block(Exception):
    def __init__(self, reason, hint=""):
        super().__init__(reason)
        self.reason, self.hint = reason, hint


# ----------------------------------------------------------------------------- helpers

def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", str(cwd)] + list(args),
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def nearest_dir(p):
    p = Path(p)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p if p.is_dir() else p.parent


def repo_root_for(path_or_dir):
    d = nearest_dir(path_or_dir)
    return git(d, "rev-parse", "--show-toplevel") or str(d)


def resolve(raw, cwd):
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = Path(cwd) / p
    return Path(os.path.realpath(str(p)))


def rel_to(abs_path, root):
    try:
        return os.path.relpath(str(abs_path), os.path.realpath(root)).replace(os.sep, "/")
    except ValueError:
        return "../" + str(abs_path)


def outside(rel):
    return rel == ".." or rel.startswith("../")


def glob_match(rel, pattern):
    rel = rel.replace(os.sep, "/")
    if rel.startswith("./"):
        rel = rel[2:]
    for candidate in (rel, rel.rstrip("/") + "/_"):          # second form lets "src" match "src/**"
        if fnmatch.fnmatchcase(candidate, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatchcase(candidate, pattern[3:]):
            return True
    if "/" not in pattern and fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern):
        return True
    return False


def matches_any(rel, patterns):
    return any(glob_match(rel, p) for p in patterns)


HOME_SECRET = re.compile(
    r"(^|[\s'\"=:(])(~|\$HOME|\$\{HOME\}|/home/[^/\s]+|/Users/[^/\s]+|/root)/"
    r"\.(ssh|aws|azure|kube|gnupg|docker/config\.json|config/gcloud|netrc|git-credentials|npmrc|pypirc)")


def is_secret(rel, abs_path=None):
    if matches_any(rel, CONFIG["secret_patterns"]) and not matches_any(rel, CONFIG["secret_allowlist"]):
        return True
    if abs_path is not None and HOME_SECRET.search(" " + str(abs_path)):
        return True
    return False


def owner_of(rel, exclude=None):
    for role, spec in ROLES.items():
        if role != exclude and matches_any(rel, spec["write"]):
            return role
    return None


def split_tokens(s):
    try:
        return shlex.split(s, posix=True)
    except ValueError:
        return s.split()


def clean_token(t):
    t = re.sub(r"^[<>|&;()$`'\"]+", "", t)
    t = re.sub(r"[<>|&;()`'\"]+$", "", t)
    return t


def path_like_tokens(segment):
    """Yield every token in a shell segment that could be a file path."""
    for tok in split_tokens(segment):
        t = clean_token(tok)
        parts = [t]
        if "=" in t:
            parts.append(t.split("=", 1)[1])
        for p in parts:
            if p and not p.startswith("-") and ("/" in p or "." in p or p in ("~",)):
                yield p


def segments(cmd):
    """Split a shell command on ; && || | & and newlines (quote-aware), plus $(...) / `...` / sh -c '...' bodies."""
    out, buf, q, i, n = [], [], None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q:
            buf.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                buf.append(cmd[i + 1]); i += 1
            elif c == q:
                q = None
        elif c in "'\"":
            q = c; buf.append(c)
        elif c in ";\n|&":
            prev = cmd[i - 1] if i else ""
            nxt = cmd[i + 1] if i + 1 < n else ""
            if c == "&" and (prev in "<>" or nxt == ">"):
                buf.append(c)
            else:
                out.append("".join(buf)); buf = []
                if nxt == c:
                    i += 1
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    extra = []
    for a, b in re.findall(r"\$\(([^()]*)\)|`([^`]*)`", cmd):
        extra.append(a or b)
    for body in re.findall(r"\b(?:ba|z|da)?sh\s+-c\s+(['\"])(.*?)\1", cmd):
        extra.append(body[1])
    for body in re.findall(r"\beval\s+(['\"])(.*?)\1", cmd):
        extra.append(body[1])
    result = [s.strip() for s in out if s.strip()]
    for e in extra:
        result.extend(segments(e) if e.strip() else [])
    return result


REDACT = [
    (re.compile(r"(?i)((?:secret|token|password|passwd|api[_-]?key|private[_-]?key|access[_-]?key|authorization)[\w-]*\s*[=:]\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)\S+"), r"\1***"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"), "***"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}\b"), "***"),
]


def redact(s):
    s = str(s)[:400]
    for rx, rep in REDACT:
        s = rx.sub(rep, s)
    return s


def audit(role, event, decision, reason=""):
    try:
        cwd = event.get("cwd") or os.getcwd()
        common = git(cwd, "rev-parse", "--git-common-dir")
        if common:
            common_path = Path(common) if Path(common).is_absolute() else Path(cwd) / common
            root = common_path.resolve().parent
        else:
            root = Path(cwd)
        log_dir = root / ".crew"
        log_dir.mkdir(exist_ok=True)
        if not (log_dir / ".gitignore").exists():   # keep the local audit log out of git by default
            (log_dir / ".gitignore").write_text("*\n", encoding="utf-8")
        ti = event.get("tool_input") or {}
        target = ti.get("command") or ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ti.get("pattern") or ""
        entry = {
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "role": role,
            "tool": event.get("tool_name"),
            "target": redact(target),
            "decision": decision,
            "reason": reason,
            "branch": git(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
            "session": event.get("session_id"),
        }
        with open(log_dir / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # auditing must never break the session


# ----------------------------------------------------------------------------- file tools

def check_file_tool(role, tool, ti, cwd):
    raw = ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ""
    spec = ROLES.get(role, {})
    crew = role in CREW

    # Grep's "glob" filter can point into a read-denied folder without a path
    if crew and tool == "Grep" and spec.get("read_deny"):
        g = (ti.get("glob") or "").lstrip("./")
        for pat in spec["read_deny"]:
            prefix = pat.split("*", 1)[0]
            if prefix and g.startswith(prefix):
                raise Block("you test from the spec only; '%s' is implementation code" % g,
                            "Use the interface contracts and acceptance criteria in docs/crew/<REQ>/design.md.")
    if not raw:
        return

    abs_path = resolve(raw, cwd)
    root = repo_root_for(abs_path)
    rel = rel_to(abs_path, root)

    if is_secret(rel, abs_path):
        raise Block("'%s' is a secret or credential file" % raw,
                    "Never read or write secrets. Reference config by name and add a placeholder to .env.example.")

    if tool in FILE_WRITE_TOOLS:
        if not outside(rel) and matches_any(rel, CONFIG["human_gate_files"]):
            raise Block("'%s' is a human gate record; only a person signs gates" % rel,
                        "Ask the human to sign it in their own terminal (see docs/crew/README.md).")
        if crew:
            if outside(rel):
                tmp = os.path.realpath(tempfile.gettempdir())
                if not str(abs_path).startswith(tmp + os.sep):
                    raise Block("'%s' is outside the repository" % raw, "Write only inside your lane in this repo.")
                return
            if matches_any(rel, CONFIG["guarded_paths"]):
                raise Block("'%s' is a guarded file (agent definitions, guardrails, CI, charter)" % rel,
                            "Crew members never change their own rules or CI. Report the need to the Chief of Staff.")
            if not matches_any(rel, spec["write"]):
                owner = owner_of(rel, exclude=role)
                lane = ("it belongs to the %s" % owner) if owner else "it is outside every crew lane"
                raise Block("'%s' is not in your lane (%s)" % (rel, lane),
                            "Your lane is: %s" % ", ".join(spec["write"]))

    if crew and tool in FILE_READ_TOOLS | SEARCH_TOOLS and not outside(rel):
        if matches_any(rel, spec.get("read_deny", [])):
            raise Block("you test from the spec only; '%s' is implementation code or the builder's notes" % rel,
                        "Use the interface contracts and acceptance criteria in docs/crew/<REQ>/design.md.")


# ----------------------------------------------------------------------------- bash: shared rules

SECRET_VAR = re.compile(r"\$\{?[A-Z0-9_]*(SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY|API_KEY|APIKEY|CREDENTIAL)[A-Z0-9_]*\}?")
ENV_DUMP = re.compile(r"(^|[;&|(]\s*)(env|printenv|export\s+-p|declare\s+-x|set)\s*($|[;&|)>])|\bprintenv\b|/proc/\S*/environ")
CRED_CMDS = re.compile(r"\bgh\s+auth\s+token\b|\bsecurity\s+find-(generic|internet)-password\b|"
                       r"\baws\s+configure\s+(get|export-credentials)\b|\bgcloud\s+auth\s+print-\w*token\b|"
                       r"\baz\s+account\s+get-access-token\b|\bkubectl\s+get\s+secrets?\b.*-o")
REMOTE_EXEC = re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?((ba|z|da|k)?sh|python3?|node|perl|ruby)\b|"
                         r"(^|[\s;&|])((ba|z)?sh|source|\.)\s+<\(\s*(curl|wget)")
HOOK_BYPASS = re.compile(r"--no-verify\b|core\.hookspath|\bHUSKY=0\b|(^|\s)SKIP=\S", re.I)
HUMAN_ONLY = [
    (re.compile(r"\bgh\s+pr\s+merge\b|\bglab\s+mr\s+merge\b|\bgh\s+api\b.*/merges?\b"), "merging pull requests"),
    (re.compile(r"\bgh\s+pr\s+review\b.*(--approve|\s-a\b)|\bglab\s+mr\s+approve\b|\bgh\s+api\b.*APPROVE"), "approving pull requests"),
    (re.compile(r"\bgh\s+release\s+create\b|\bnpm\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bgem\s+push\b"), "publishing releases"),
]
WRITE_OPS = re.compile(
    r"(?<![0-9&<>=-])(>>|>)(?![>&])(?!\s*/dev/null)|\btee\b|\bsed\s+(-[a-zA-Z]*i|--in-place)|\bperl\s+-[a-zA-Z]*i|"
    r"(^|[\s;&|(])(rm|mv|cp|truncate|touch|mkdir|chmod|chown|ln|install|patch|dd|unlink|rsync)\s|"
    r"\bgit\s+(apply|restore|checkout\s+--|rm|mv)\b")
READ_OPS = re.compile(r"(^|[\s;&|(])(cat|less|more|head|tail|bat|nl|strings|xxd|od|grep|egrep|rg|ag|ack|awk|sed|cut|"
                      r"diff|base64|tar|zip|cp|rsync)\s|\bgit\s+(show|diff|blame)\b|\bpython3?\s+-c\b")
READONLY_SEG = re.compile(r"^\s*(cat|less|more|head|tail|grep|egrep|rg|ag|ls|find|echo|printf|wc|git\s+(log|diff|show|status|grep|blame))\b")
LOCKFILE_MUTATION = re.compile(r"\b(npm|pnpm|yarn)\s+(i|install|add|update|upgrade|remove|uninstall)\b|"
                               r"\bpoetry\s+(add|update|lock|remove)\b|\buv\s+(add|lock|remove)\b|\bgo\s+get\b")
INSTALLS = re.compile(r"\b(npm|pnpm|yarn)\s+(i|install|add|ci|update|upgrade)\b|\bpip3?\s+install\b|"
                      r"\b(brew|apt|apt-get|yum|dnf|apk|pipx)\s+install\b|\buv\s+(pip|add|sync)\b|\bpoetry\s+(add|install|update)\b")
INTERPRETERS = re.compile(r"(^|[\s;&|(])(python3?|node|ruby|perl|php|deno|bun|bash|sh|zsh)\b")
GH_MUTATION = re.compile(r"\bgh\s+(pr|issue|release|repo|workflow|secret|variable|label|run)\s+"
                         r"(create|edit|close|comment|delete|merge|review|run|set|reopen|ready|lock|rerun|cancel)\b|"
                         r"\bgh\s+api\b.*-X\s*(POST|PUT|PATCH|DELETE)")
AUTOFIX = re.compile(r"--fix\b|--fix-|--autofix\b|\baudit\s+fix\b|--write\b|\bgofmt\b.*\s-w\b|\bblack\b(?!.*--check)")


def strip_ref(name):
    return re.sub(r"^(refs/heads/|origin/|upstream/)", "", name)


def parse_git(segment):
    toks = split_tokens(segment)
    for i, t in enumerate(toks):
        if t == "git" or t.endswith("/git"):
            rest, j = toks[i + 1:], 0
            while j < len(rest) and rest[j].startswith("-"):
                j += 2 if rest[j] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
            return (rest[j], rest[j + 1:]) if j < len(rest) else None
    return None


def git_shared_rules(sub, args, branch):
    flags = [a for a in args if a.startswith("-")]
    pos = [a for a in args if not a.startswith("-")]
    if sub == "push":
        bad = {"--force", "--force-with-lease", "--force-if-includes", "--mirror", "--all", "--delete", "-d", "--tags", "--prune"}
        if any(f.split("=")[0] in bad or re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*", f) for f in flags) or \
                any(r.startswith(("+", ":")) for r in pos[1:]):
            raise Block("force, mirror, delete, --all and --tags pushes are human-only", "Push a normal feature or rc branch.")
        targets = [strip_ref(r.split(":", 1)[-1]) for r in pos[1:]]
        if (not targets and branch in PROTECTED) or any(t in PROTECTED or (t == "HEAD" and branch in PROTECTED) for t in targets):
            raise Block("pushing to a protected branch (%s) is human-only" % ", ".join(sorted(PROTECTED)),
                        "Work on feature/<REQ>-<component> or rc/<REQ>. A human merges to main.")
    if sub in ("commit", "merge", "rebase", "cherry-pick", "revert", "am") and branch in PROTECTED:
        raise Block("'git %s' on protected branch '%s' is human-only" % (sub, branch),
                    "Create or switch to your feature/<REQ>-<component> branch first.")
    if sub == "commit" and "-n" in flags:
        raise Block("skipping commit hooks (-n) is not allowed", "Fix what the hook reports instead.")
    if sub == "branch" and any(f in ("-d", "-D", "--delete", "-m", "-M", "--move", "-f", "--force", "-c", "-C") for f in flags) \
            and any(strip_ref(p) in PROTECTED for p in pos):
        raise Block("deleting, moving or force-resetting a protected branch is human-only")
    if sub in ("checkout", "switch") and any(f in ("-B", "-C", "--force-create") for f in flags) \
            and any(strip_ref(p) in PROTECTED for p in pos):
        raise Block("force-resetting a protected branch is human-only")
    if sub == "update-ref" and any(("refs/heads/" + b) in a for a in pos for b in PROTECTED):
        raise Block("rewriting a protected branch ref is human-only")


def next_branch(sub, args, branch):
    if sub not in ("checkout", "switch") or "--" in args:
        return branch
    pos = [a for a in args if not a.startswith("-")]
    if "--detach" in args or "-d" in args:
        return "(detached)"
    return strip_ref(pos[0]) if pos else branch


READONLY_GIT = {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe", "fetch",
                "remote", "shortlog", "grep", "rev-list", "cat-file", "for-each-ref", "merge-base", "name-rev", "ls-tree"}


def git_role_rules(role, sub, args):
    mode = ROLES[role]["bash"]
    if mode in ("read-only", "test", "scan"):
        if sub == "branch":
            ok = all(a in ("-a", "-r", "-v", "-vv", "--all", "--list", "-l", "--show-current", "--merged",
                           "--no-merged", "--contains", "--remotes") for a in args if a.startswith("-"))
            if ok and (not [a for a in args if not a.startswith("-")] or "--list" in args or "-l" in args
                       or any(a in ("--merged", "--no-merged", "--contains") for a in args)):
                return
            raise Block("the %s does not create, move or delete branches" % role)
        if sub == "config" and any(a in ("--get", "--get-all", "--list", "-l", "--get-regexp") for a in args):
            return
        if sub not in READONLY_GIT:
            raise Block("'git %s' changes the repository; the %s only reads it" % (sub, role),
                        "Only the Responsible Engineer (in its own worktree) and the Launch Director change git state. "
                        "Your output is your report file.")
    if mode == "release" and sub in ("clean", "stash", "restore") or \
            (mode == "release" and sub == "reset" and "--hard" in args):
        raise Block("'git %s' in the shared checkout could wipe other roles' reports" % sub,
                    "If the checkout is dirty, stop and report to the Chief of Staff.")
    if role in CREW and sub == "worktree" and ROLES[role]["bash"] != "build":
        raise Block("only Responsible Engineers work in worktrees")


def check_write_targets(role, seg, cwd):
    """Bash writes into a guarded path, a human gate file, or another role's lane."""
    if not WRITE_OPS.search(seg):
        return
    crew = role in CREW
    root = repo_root_for(cwd)
    for raw in path_like_tokens(seg):
        abs_path = resolve(raw, cwd)
        rel = rel_to(abs_path, root)
        if outside(rel):
            continue
        if matches_any(rel, CONFIG["human_gate_files"]):
            raise Block("'%s' is a human gate record; only a person signs gates" % rel,
                        "Ask the human to sign it in their own terminal.")
        if not crew:
            continue
        if matches_any(rel, CONFIG["guarded_paths"]):
            raise Block("'%s' is a guarded file (agent definitions, guardrails, CI, charter)" % rel)
        if matches_any(rel, ROLES[role]["write"]):
            continue
        owner = owner_of(rel, exclude=role)
        if owner:
            raise Block("'%s' belongs to the %s, not to you" % (rel, owner),
                        "Your lane is: %s" % ", ".join(ROLES[role]["write"]))


def check_rm(seg, cwd):
    toks = split_tokens(seg)
    if not toks or "rm" not in [Path(t).name for t in toks[:3]]:
        return
    flags = "".join(t for t in toks if t.startswith("-") and not t.startswith("--"))
    if not re.search(r"[rR]", flags) and "--recursive" not in toks:
        return
    home = os.path.realpath(os.path.expanduser("~"))
    root = repo_root_for(cwd)
    for t in toks[1:]:
        if t.startswith("-"):
            continue
        if t in ("/", "~", "$HOME", "*", ".", "..", "./", "../", "/*", "~/*"):
            raise Block("recursive delete of '%s' is never allowed" % t)
        p = os.path.realpath(os.path.expanduser(t if os.path.isabs(t) or t.startswith("~") else os.path.join(cwd, t)))
        if p in ("/", home, os.path.realpath(root)) or rel_to(p, root) in (".git",) or rel_to(p, root).startswith(".git/"):
            raise Block("recursive delete of '%s' is never allowed" % t)


def check_bash(role, cmd, cwd):
    crew = role in CREW
    mode = ROLES[role]["bash"] if crew else "baseline"
    if mode == "none":
        raise Block("the %s has no shell; it designs, it does not build or run things" % role)

    # --- rules for everyone (including your own session) ---
    root = repo_root_for(cwd)
    for tok in split_tokens(cmd):
        for t in [clean_token(tok)] + ([clean_token(tok).split("=", 1)[1]] if "=" in tok else []):
            if not t or t.startswith("-"):
                continue
            abs_path = resolve(t, cwd)
            if is_secret(rel_to(abs_path, root), abs_path):
                raise Block("the command touches a secret or credential file ('%s')" % t,
                            "Never read, print or copy secrets. Use .env.example placeholders.")
    if HOME_SECRET.search(" " + cmd) or SECRET_VAR.search(cmd) or CRED_CMDS.search(cmd):
        raise Block("the command reads credentials or secret environment variables",
                    "Never read, print or copy secrets.")
    if ENV_DUMP.search(cmd):
        raise Block("dumping the environment can expose secrets", "Ask for the one non-secret value you need.")
    if REMOTE_EXEC.search(cmd):
        raise Block("piping a download straight into an interpreter runs untrusted code",
                    "Download, inspect and pin the tool, or ask a human to install it.")
    if HOOK_BYPASS.search(cmd):
        raise Block("bypassing git hooks is not allowed", "Fix what the hook reports instead.")
    for rx, what in HUMAN_ONLY:
        if rx.search(cmd):
            raise Block("%s is human-only" % what, "Prepare the evidence; a human does this step.")
    if crew and re.search(r"\bgh\s+pr\s+review\b|\bglab\s+mr\s+(note|review)\b", cmd):
        raise Block("crew members do not review or approve pull requests", "Write your findings in your report file.")

    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    staging = CONFIG.get("staging_deploy_command", "")
    for seg in segments(cmd):
        check_rm(seg, cwd)
        parsed = parse_git(seg)
        if parsed:
            sub, args = parsed
            git_shared_rules(sub, args, branch)
            if crew:
                git_role_rules(role, sub, args)
            branch = next_branch(sub, args, branch)

        if not READONLY_SEG.search(seg):
            is_deploy = any(re.search(p, seg) for p in CONFIG["deploy_patterns"]) or (staging and staging in seg)
            if is_deploy and any(re.search(p, seg, re.I) for p in CONFIG["production_patterns"]):
                raise Block("production deploys are human-only (Gate B)",
                            "The Launch Director deploys to staging; a human deploys to production.")
            if is_deploy and crew and role != "launch-director":
                raise Block("only the Launch Director deploys, and only to staging")

        check_write_targets(role, seg, cwd)
        if not crew:
            continue

        if mode == "read-only":
            if WRITE_OPS.search(seg):
                raise Block("the Chief of Staff does not write files from the shell",
                            "Update docs/crew/<REQ>/board.md with the Edit tool.")
            if INSTALLS.search(seg) or GH_MUTATION.search(seg):
                raise Block("the Chief of Staff tracks status; it does not install, create or change things")
            if INTERPRETERS.search(seg) and ".claude/crew/poll.py" not in seg:
                raise Block("the Chief of Staff only runs read-only commands",
                            "To check a release, ask the Launch Director to run the poll.")
        if mode in ("test", "scan") and LOCKFILE_MUTATION.search(seg):
            raise Block("changing dependencies or lockfiles belongs to the Responsible Engineer",
                        "Use the lockfile as-is (npm ci, pip install -r, uv sync --locked).")
        if mode == "scan" and AUTOFIX.search(seg):
            raise Block("the Security Engineer reports and can block; it never fixes code",
                        "Record the finding with file:line and a recommended fix in security-report.md.")
        deny = ROLES[role].get("read_deny", [])
        if deny and READ_OPS.search(seg):
            root = repo_root_for(cwd)
            for raw in path_like_tokens(seg):
                rel = rel_to(resolve(raw, cwd), root)
                if not outside(rel) and matches_any(rel, deny):
                    raise Block("you test from the spec only; '%s' is implementation code" % rel,
                                "Use the interface contracts and acceptance criteria in design.md.")


# ----------------------------------------------------------------------------- main

def main():
    role = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    event = {}
    try:
        if role not in CREW and role != "baseline":
            raise Block("unknown crew role '%s'" % role, "Fix the hook command in the agent file.")
        event = json.load(sys.stdin)
        tool = event.get("tool_name", "")
        ti = event.get("tool_input") or {}
        cwd = event.get("cwd") or os.getcwd()
        if tool in FILE_WRITE_TOOLS | FILE_READ_TOOLS | SEARCH_TOOLS:
            check_file_tool(role, tool, ti, cwd)
        elif tool == "Bash":
            check_bash(role, ti.get("command", ""), cwd)
    except Block as b:
        audit(role, event, "BLOCK", b.reason)
        msg = "CREW GUARD blocked this for %s: %s." % (role, b.reason)
        if b.hint:
            msg += "\nDo instead: " + b.hint
        print(msg + "\n" + NEXT_STEP, file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # fail closed
        audit(role, event, "BLOCK", "guard error: %r" % e)
        print("CREW GUARD error (failing closed): %r. Report this to the human." % e, file=sys.stderr)
        sys.exit(2)
    if role in CREW:
        audit(role, event, "ALLOW")
    sys.exit(0)


if __name__ == "__main__":
    main()
