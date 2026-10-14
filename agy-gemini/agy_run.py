#!/usr/bin/env python3
"""agy-gemini: use Gemini (through the agy CLI) as a plain text model. Claude is the agent.

Gemini gets no tools: it cannot read files, run commands, or change anything.
This script sends file contents and a task to Gemini and prints the answer.
With --edit, it turns Gemini's edit blocks into a proposed diff. Nothing on
disk changes until `apply`.

Commands:
  setup                 Install the agy agent and the guard hook. Run once, and after updates.
  doctor [--offline]    Check the setup and test agy's behavior. Run after every agy update.
  ask [options] [--read-tools] [--max-reads N] [TASK]  Send a task to Gemini. TASK is read from stdin when it is not given.
  apply TASK_ID         Write the latest proposed changes of a task to disk.
  revert TASK_ID        Undo all applied changes of a task.
  uninstall [--purge]   Remove the agent and the hook. --purge also deletes the saved tasks.

Safety layers for every Gemini call:
  1. The agy agent "agy-gemini-plain" uses excludeDefaultComponents, so it has no built-in tools.
  2. agy runs in an empty folder, not in the project.
  3. A PreToolUse hook denies every tool call when AGY_GEMINI_GUARD is set.
  4. Secret files and secret-looking content are blocked before anything is sent.
  5. Edits are only proposals. Paths are checked. Nothing is written until `apply`.

Configuration (environment variables):
  AGY_GEMINI_MODEL_HIGH / _MEDIUM / _LOW   agy model names (see `agy models`)
  AGY_GEMINI_KEEP_TASKS                    number of saved tasks to keep (default 30)
"""
import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()
SELF = Path(__file__).resolve()
AGY_CONFIG = HOME / ".gemini" / "config"
AGENT_NAME = "agy-gemini-plain"
AGENT_FILE = AGY_CONFIG / "agents" / f"{AGENT_NAME}.md"
HOOKS_FILE = AGY_CONFIG / "hooks.json"
HOOK_KEY = "agy-gemini-guard"
RUNS = HOME / ".cache" / "agy-gemini" / "runs"
MODELS = {
    "high": os.environ.get("AGY_GEMINI_MODEL_HIGH", "gemini-3.8-flash-high"),
    "medium": os.environ.get("AGY_GEMINI_MODEL_MEDIUM", "gemini-3.8-flash-medium"),
    "low": os.environ.get("AGY_GEMINI_MODEL_LOW", "gemini-3.8-flash-low"),
}
KEEP_TASKS = int(os.environ.get("AGY_GEMINI_KEEP_TASKS", "30"))
MAX_PROMPT_BYTES = 150_000  # agy cuts longer messages (tested: 159 KB arrives complete, 287 KB is cut)
ANSWER_LINES = 150
INLINE_DIFF_LINES = 300
TESTED_AGY_VERSIONS = "1.3.4, 1.3.5"
READ_MAX_BYTES = 60_000
SEARCH_MAX_MATCHES = 50
LIST_MAX = 200
SEARCH_FILE_MAX_BYTES = 1_000_000
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    "target", ".next", ".idea", ".gradle",
}

AGENT_TEXT = f"""---
name: {AGENT_NAME}
description: Plain text model for agy-gemini. No tools. Answers only from the text in the prompt.
excludeDefaultComponents: true
tools: []
---
You are a text model. A supervisor program (Claude) calls you like an API.
You have no tools. You cannot read files, run commands, or browse the web.
Use only the text in the prompt. If information is missing, say exactly what is missing.
Follow the output format in the prompt exactly. Do not add greetings or extra commentary.
"""

# The hook does nothing unless AGY_GEMINI_GUARD is set, so the user's own agy sessions are not affected.
# Tested with agy 1.3.4: no output means "no opinion"; a deny decision blocks the tool call.
GUARD_REASON = "Tools are disabled. You are a plain text model. Answer only from the text in the prompt."
HOOK_COMMAND = (
    'if [ -n "$AGY_GEMINI_GUARD" ]; then '
    'L="${AGY_GEMINI_GUARD_LOG:-/dev/null}"; cat >> "$L"; echo "" >> "$L"; echo "---TOOLCALL---" >> "$L"; '
    "echo '" + json.dumps({"decision": "deny", "reason": GUARD_REASON}) + "'; fi"
)
HOOK_ENTRY = {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 10}]}]}

ANSWER_RULES = """## Output format
Answer the task directly. Be concise. Keep code, file paths, names, and error messages exact.
When you state a fact from the given text, quote the exact words, and name the file or section.
Do not use edit blocks."""

EDIT_RULES = """## Output format
Return code changes ONLY as edit blocks in this exact format:

FILE: path/relative/to/project
<<<<<<< SEARCH
exact lines copied from the current file
=======
new lines
>>>>>>> REPLACE

Rules:
- The SEARCH part must match whole lines of the current file exactly, including indentation.
- The SEARCH part must appear only once in the file. Add nearby lines if needed to make it unique.
- To create a new file, leave the SEARCH part empty.
- Use one block per change. Keep blocks small. Do not repeat unchanged code outside the blocks.
- Edit only the files shown in this conversation, or create new files. Do not delete files.
- Do not put edit blocks inside code fences.
- After all edit blocks, write a short section that starts with "NOTES:". List what you changed,
  the commands the supervisor should run to test the change, and any open questions."""

READ_TOOLS_RULES = """## Reading project files
Before you answer, you can ask the supervisor program to show you project files.
To ask, reply with ONLY request lines, one per line, and nothing else:
READ <path>                 show a whole file
READ <path> <start>-<end>   show lines start to end (1-based, inclusive)
SEARCH <regex>              search all project files with a Python regular expression
LIST <dir>                  list the files under a folder ("." is the project root)
Paths are relative to the project folder. You may send up to 5 request lines in one reply.
You get the results in the next message. You have at most {max_reads} request rounds.
When you have enough information, give your final answer in the output format above.
Never mix request lines and the final answer in one reply."""

SENSITIVE_DIRS = {".git", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure"}
SENSITIVE_FILES = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".netrc", ".npmrc", ".pypirc", ".git-credentials", "credentials", "credentials.*",
]
SAFE_FILES = {".env.example", ".env.sample", ".env.template", ".env.dist"}

# (pattern, label). When a pattern has a group, the group is the secret value; placeholders are ignored.
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}|\bgithub_pat_[A-Za-z0-9_]{22,}"), "GitHub token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "GitLab token"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), "Google API key"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"), "secret API key (sk-...)"),
    (re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}"), "Stripe live key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
    (re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@'\"]+:([^/\s@'\"]+)@"), "password in a URL"),
    (re.compile(r"(?i)\b[\w.-]*(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token"
                r"|access[_-]?token|client[_-]?secret|private[_-]?key)[\w.-]*[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']"),
     "hardcoded secret"),
    (re.compile(r"(?im)^\s*(?:export\s+)?[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)[A-Z0-9_]*"
                r"\s*=\s*([^\s\"'$<{#][^\s#]{7,})"), "secret in an environment assignment"),
]
PLACEHOLDER = re.compile(
    r"(?i)x{4,}|\*{3,}|change_?me|example|placeholder|your[_-]|dummy|redacted|<[^>]*>|\$\{|\{\{|%\(|\.\.\."
    r"|^(?:password|passwd|pass|secret|pwd)$"
)

REVIEW_FILES = re.compile(
    r"(^|/)(package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|requirements[^/]*\.txt|pyproject\.toml"
    r"|poetry\.lock|uv\.lock|Pipfile(\.lock)?|setup\.(py|cfg)|Cargo\.(toml|lock)|go\.(mod|sum)"
    r"|Gemfile(\.lock)?|composer\.(json|lock)|Dockerfile[^/]*|docker-compose[^/]*|Makefile"
    r"|\.gitlab-ci\.yml|\.github/workflows/[^/]+|[^/]+\.sh|\.gitignore|\.gitattributes)$"
)
RISKY_LINES = [
    (re.compile(r"rm\s+-[a-zA-Z]*[rf]"), "rm -rf"),
    (re.compile(r"\b(os\.system|subprocess\.|child_process|exec(Sync)?\(|shell\s*=\s*True)"), "runs shell commands"),
    (re.compile(r"\beval\("), "eval("),
    (re.compile(r"\b(curl|wget)\s"), "downloads (curl/wget)"),
    (re.compile(r"\|\s*(ba)?sh\b"), "pipes into a shell"),
    (re.compile(r"\bsudo\s|chmod\s+(-R\s+)?777"), "sudo / chmod 777"),
    (re.compile(r"\b(DROP\s+(TABLE|DATABASE)|TRUNCATE\s+TABLE|DELETE\s+FROM)\b", re.I), "destructive SQL"),
    (re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|InsecureSkipVerify", re.I), "disables TLS checks"),
] + [(rx, f"possible {label}") for rx, label in SECRET_PATTERNS]


# ---------- helpers ----------

def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def under(path, base):
    return path == base or base in path.parents


def is_sensitive(path):
    parts = path.parts
    if any(p in SENSITIVE_DIRS for p in parts) or (".config" in parts and "gcloud" in parts):
        return True
    return path.name not in SAFE_FILES and any(fnmatch.fnmatch(path.name, pat) for pat in SENSITIVE_FILES)


def find_secrets(text):
    """Return [(line_number, label, masked_value)] for secret-looking content."""
    found = []
    for rx, label in SECRET_PATTERNS:
        for m in rx.finditer(text):
            value = m.group(1) if rx.groups else m.group(0)
            if rx.groups and PLACEHOLDER.search(value):
                continue
            line = text.count("\n", 0, m.start()) + 1
            found.append((line, label, value[:4] + "..."))
    return sorted(set(found))


def read_text(path):
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        raise ValueError("it is a binary file")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("it is not UTF-8 text")


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def label_for(path, root):
    return str(path.relative_to(root)) if under(path, root) else str(path)


def load_meta(task_id):
    task_dir = RUNS / task_id
    meta_file = task_dir / "meta.json"
    if not meta_file.exists():
        die(f"Unknown task '{task_id}'. Tasks are in {RUNS}.")
    return task_dir, json.loads(meta_file.read_text())


def save_meta(task_dir, meta):
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=1))


def check_root(root):
    if root == HOME or root == Path("/") or under(HOME, root):
        die(f"Refusing to work in {root}. This folder is too broad. Run from inside one project folder.")


def cleanup_old_tasks():
    tasks = sorted(p for p in RUNS.iterdir() if p.is_dir() and (p / "meta.json").exists())
    for old in tasks[:-KEEP_TASKS] if KEEP_TASKS > 0 else []:
        shutil.rmtree(old, ignore_errors=True)


def load_hooks():
    if not HOOKS_FILE.exists():
        return {}
    try:
        hooks = json.loads(HOOKS_FILE.read_text() or "{}")
    except json.JSONDecodeError as exc:
        die(f"{HOOKS_FILE} is not valid JSON ({exc}). Fix it first. Nothing was changed.")
    if not isinstance(hooks, dict):
        die(f"{HOOKS_FILE} does not contain a JSON object. Fix it first. Nothing was changed.")
    return hooks


def setup_problems():
    problems = []
    if shutil.which("agy") is None:
        problems.append("agy is not installed or not on PATH")
    if not AGENT_FILE.exists() or AGENT_FILE.read_text() != AGENT_TEXT:
        problems.append(f"the agent file {AGENT_FILE} is missing or outdated")
    try:
        hooks = json.loads(HOOKS_FILE.read_text()) if HOOKS_FILE.exists() else {}
    except json.JSONDecodeError:
        hooks = {}
    if not isinstance(hooks, dict) or hooks.get(HOOK_KEY) != HOOK_ENTRY:
        problems.append(f"the guard hook '{HOOK_KEY}' in {HOOKS_FILE} is missing or outdated")
    return problems


# ---------- edit blocks ----------

def clean_path(raw):
    p = raw.strip().strip("`'\"*").strip()
    return p[2:] if p.startswith("./") else p


def parse_blocks(answer):
    """Return (blocks, notes, errors). A block is (path, search_lines, replace_lines)."""
    blocks, notes, errors = [], [], []
    lines = answer.splitlines()
    cur_file, i = None, 0
    while i < len(lines):
        s = lines[i].strip()
        m = re.match(r"^(?:#+\s*)?(?:\*\*)?FILE:\s*(.+?)(?:\*\*)?$", s)
        if m:
            cur_file = clean_path(m.group(1))
            i += 1
            continue
        if s == "<<<<<<< SEARCH":
            search, replace = [], []
            i += 1
            while i < len(lines) and lines[i].strip() != "=======":
                search.append(lines[i])
                i += 1
            i += 1
            while i < len(lines) and lines[i].strip() != ">>>>>>> REPLACE":
                replace.append(lines[i])
                i += 1
            if i >= len(lines):
                errors.append("An edit block is not closed with '>>>>>>> REPLACE'.")
                break
            if cur_file is None:
                errors.append("An edit block has no 'FILE:' line before it.")
            else:
                blocks.append((cur_file, search, replace))
            i += 1
            continue
        if not s.startswith("```"):
            notes.append(lines[i])
        i += 1
    return blocks, "\n".join(notes).strip(), errors


def replace_once(content, search_lines, replace_lines):
    nl = "\r\n" if "\r\n" in content else "\n"
    flines = content.split(nl)
    k = len(search_lines)
    for norm in (lambda x: x, lambda x: x.rstrip()):
        key = [norm(l) for l in search_lines]
        hits = [i for i in range(len(flines) - k + 1) if [norm(x) for x in flines[i:i + k]] == key]
        if len(hits) == 1:
            i = hits[0]
            return nl.join(flines[:i] + list(replace_lines) + flines[i + k:]), None
        if len(hits) > 1:
            return None, f"the SEARCH part appears {len(hits)} times; it must appear once"
    return None, "the SEARCH part was not found in the file"


def build_proposal(root, meta, blocks):
    root = Path(root).resolve()  # paths below are resolved; compare against a resolved root (macOS /var -> /private/var)
    files, errors = {}, []
    for n, (raw, search, replace) in enumerate(blocks, 1):
        if Path(raw).is_absolute():
            errors.append(f"Block {n}: '{raw}' is an absolute path. Only project-relative paths are allowed.")
            continue
        path = (root / raw).resolve()
        if not under(path, root):
            errors.append(f"Block {n}: '{raw}' is outside the project folder.")
            continue
        rel = str(path.relative_to(root))
        if is_sensitive(Path(rel)):
            errors.append(f"Block {n}: '{rel}' is a protected or secret file.")
            continue
        if rel not in files:
            if path.exists():
                if rel not in meta["sent"]:
                    errors.append(f"Block {n}: '{rel}' exists, but Gemini was never given this file.")
                    continue
                try:
                    old = read_text(path)
                except ValueError as exc:
                    errors.append(f"Block {n}: cannot edit '{rel}': {exc}.")
                    continue
            else:
                old = None
            files[rel] = {"old": old, "new": old}
        f = files[rel]
        if not any(l.strip() for l in search):
            if f["new"] is None:
                f["new"] = "\n".join(replace) + "\n"
            else:
                errors.append(f"Block {n}: '{rel}' already exists, but the SEARCH part is empty.")
            continue
        if f["new"] is None:
            errors.append(f"Block {n}: '{rel}' does not exist.")
            continue
        new, err = replace_once(f["new"], search, replace)
        if err:
            errors.append(f"Block {n} ({rel}): {err}.")
        else:
            f["new"] = new
    return {rel: f for rel, f in files.items() if f["new"] != f["old"]}, errors


def make_diff(files):
    out = []
    for rel, f in files.items():
        old = (f["old"] or "").splitlines(keepends=True)
        new = f["new"].splitlines(keepends=True)
        fromfile = "/dev/null" if f["old"] is None else f"a/{rel}"
        for line in difflib.unified_diff(old, new, fromfile=fromfile, tofile=f"b/{rel}"):
            out.append(line if line.endswith("\n") else line + "\n")
    return "".join(out)


def review_warnings(files, diff_text):
    warnings = []
    review = [rel for rel in files if REVIEW_FILES.search(rel)]
    if review:
        warnings.append(f"Config, dependency, CI, or script files change: {', '.join(review)}")
    new_files = [rel for rel, f in files.items() if f["old"] is None]
    if new_files:
        warnings.append(f"New files: {', '.join(new_files)}")
    current, changed = None, 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith(("+", "-")) and not line.startswith("---"):
            changed += 1
            if line.startswith("+"):
                for rx, label in RISKY_LINES:
                    if rx.search(line):
                        warnings.append(f"{current}: added line matches '{label}': {line[1:].strip()[:120]}")
    if changed > 400:
        warnings.append(f"Large change: {changed} lines added or removed.")
    return warnings


# ---------- read tools ----------

def parse_requests(text):
    if not text:
        return None
    requests = []
    has_non_ignored = False
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("```"):
            continue
        has_non_ignored = True
        if s.startswith(("- ", "* ")):
            s = s[2:].strip()
        while s.startswith("`") and s.endswith("`") and len(s) >= 2:
            s = s[1:-1].strip()
        if s.startswith(("- ", "* ")):
            s = s[2:].strip()

        m_range = re.match(r"^READ\s+(\S.+?)\s+(\d+-\d+)$", s)
        m_read = re.match(r"^READ\s+(\S.*)$", s)
        m_search = re.match(r"^SEARCH\s+(\S.*)$", s)
        m_list = re.match(r"^LIST\s+(\S.*)$", s)

        if m_range:
            requests.append(("READ", f"{m_range.group(1).strip()} {m_range.group(2)}"))
        elif m_read:
            requests.append(("READ", m_read.group(1).strip()))
        elif m_search:
            requests.append(("SEARCH", m_search.group(1).strip()))
        elif m_list:
            requests.append(("LIST", m_list.group(1).strip()))
        else:
            return None

    if not requests or not has_non_ignored:
        return None
    return requests[:5]


def project_files(root):
    root = Path(root)
    raw_paths = None
    if (root / ".git").exists():
        try:
            r = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                cwd=root, capture_output=True, text=True, errors="replace"
            )
            if r.returncode == 0:
                raw_paths = [p for p in r.stdout.split("\0") if p]
        except Exception:
            pass
    if raw_paths is None:
        raw_paths = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            dpath = Path(dirpath)
            for f in filenames:
                raw_paths.append((dpath / f).relative_to(root).as_posix())

    result = []
    for rel in raw_paths:
        p = Path(rel)
        if is_sensitive(p) or any(part in SKIP_DIRS for part in p.parts):
            continue
        result.append(rel)
    return sorted(set(result))


def run_request(root, kind, arg, allow_secrets):
    root = Path(root).resolve()
    header = f"=== {kind} {arg}\n"

    def make_entry(status, text, note=""):
        return {"kind": kind, "target": arg, "status": status, "bytes": len(text.encode("utf-8")), "note": note}

    if kind == "READ":
        m = re.match(r"^(\S.+?)\s+(\d+)-(\d+)$", arg)
        if m:
            path_str, start_line, end_line, has_range = m.group(1), int(m.group(2)), int(m.group(3)), True
        else:
            path_str, start_line, end_line, has_range = arg, 0, 0, False

        p = Path(path_str)
        if p.is_absolute() or path_str.startswith("/") or path_str.startswith("\\"):
            text = header + "refused: absolute path\n"
            return text, make_entry("refused", text, "absolute path")

        target_path = (root / p).resolve()
        if not under(target_path, root):
            text = header + "refused: outside project folder\n"
            return text, make_entry("refused", text, "outside project folder")

        rel = target_path.relative_to(root)
        if is_sensitive(rel):
            text = header + "refused: protected file\n"
            return text, make_entry("refused", text, "protected file")
        if any(part in SKIP_DIRS for part in rel.parts):
            text = header + "refused: ignored directory\n"
            return text, make_entry("refused", text, "ignored directory")

        if not target_path.is_file():
            msg = "file not found" if not target_path.exists() else "not a file"
            text = header + f"error: {msg}\n"
            return text, make_entry("error", text, msg)

        try:
            content = read_text(target_path)
        except ValueError as exc:
            msg = str(exc)
            text = header + f"error: {msg}\n"
            return text, make_entry("error", text, msg)

        if not allow_secrets:
            found = find_secrets(content)
            if found:
                lines_str = ", ".join(str(line) for line in sorted({f[0] for f in found}))
                msg = f"refused: possible secrets at lines {lines_str}; the file was not shown"
                text = header + msg + "\n"
                return text, make_entry("refused", text, "possible secrets")

        flines = content.splitlines()
        n_lines = len(flines)
        if has_range:
            start = max(1, start_line)
            end = min(n_lines, end_line)
            selected = [(i, flines[i - 1]) for i in range(start, end + 1)] if start <= end else []
        else:
            selected = [(i, flines[i - 1]) for i in range(1, n_lines + 1)]

        out_lines = []
        total_bytes = 0
        cut = False
        for i, line in selected:
            formatted = f"{i:4d}| {line}"
            line_bytes = len(formatted.encode("utf-8")) + 1
            if total_bytes + line_bytes > READ_MAX_BYTES:
                cut = True
                if not out_lines:
                    out_lines.append(formatted.encode("utf-8")[:READ_MAX_BYTES].decode("utf-8", errors="ignore"))
                break
            out_lines.append(formatted)
            total_bytes += line_bytes

        if cut:
            out_lines.append("[cut: file continues; ask for a line range]")

        body = "\n".join(out_lines) + ("\n" if out_lines else "")
        text = header + body
        return text, make_entry("ok", text)

    elif kind == "SEARCH":
        try:
            rx = re.compile(arg)
        except re.error as exc:
            msg = f"invalid regex: {exc}"
            text = header + f"error: {msg}\n"
            return text, make_entry("error", text, msg)

        matches = []
        total_matches = 0
        for rel in project_files(root):
            fpath = root / rel
            try:
                if fpath.stat().st_size > SEARCH_FILE_MAX_BYTES:
                    continue
                content = read_text(fpath)
            except (ValueError, OSError):
                continue

            for line_num, line in enumerate(content.splitlines(), 1):
                if rx.search(line):
                    total_matches += 1
                    if len(matches) < SEARCH_MAX_MATCHES:
                        if not allow_secrets and find_secrets(line):
                            matches.append(f"{rel}:{line_num}: [hidden: possible secret]")
                        else:
                            matches.append(f"{rel}:{line_num}: {line[:200]}")

        if total_matches > SEARCH_MAX_MATCHES:
            summary = f"Total matches: {total_matches} (cut to {SEARCH_MAX_MATCHES})"
        else:
            summary = f"Total matches: {total_matches} (not cut)"

        out_lines = matches + [summary]
        text = header + "\n".join(out_lines) + "\n"
        return text, make_entry("ok", text)

    elif kind == "LIST":
        path_str = arg.strip()
        if not path_str or path_str == ".":
            target_path = root
        else:
            p = Path(path_str)
            if p.is_absolute() or path_str.startswith("/") or path_str.startswith("\\"):
                text = header + "refused: absolute path\n"
                return text, make_entry("refused", text, "absolute path")
            target_path = (root / p).resolve()

        if not under(target_path, root):
            text = header + "refused: outside project folder\n"
            return text, make_entry("refused", text, "outside project folder")

        rel = target_path.relative_to(root)
        if is_sensitive(rel):
            text = header + "refused: protected file\n"
            return text, make_entry("refused", text, "protected file")
        if any(part in SKIP_DIRS for part in rel.parts):
            text = header + "refused: ignored directory\n"
            return text, make_entry("refused", text, "ignored directory")

        if not target_path.is_dir():
            msg = f"'{arg}' is not a directory"
            text = header + f"error: {msg}\n"
            return text, make_entry("error", text, msg)

        all_files = project_files(root)
        if target_path == root:
            matching = all_files
        else:
            matching = [f for f in all_files if under((root / f).resolve(), target_path)]

        total = len(matching)
        shown = matching[:LIST_MAX]
        if total > LIST_MAX:
            summary = f"Total: {total} files (cut to {LIST_MAX})"
        else:
            summary = f"Total: {total} files" if total != 1 else "Total: 1 file"

        out_lines = shown + [summary]
        text = header + "\n".join(out_lines) + "\n"
        return text, make_entry("ok", text)

    else:
        msg = f"unknown request kind '{kind}'"
        text = header + f"error: {msg}\n"
        return text, make_entry("error", text, msg)


# ---------- agy calls ----------

def guard_env(log_path):
    return dict(os.environ, AGY_GEMINI_GUARD="1", AGY_GEMINI_GUARD_LOG=str(log_path))


def count_tool_calls(log_path):
    return log_path.read_text().count("---TOOLCALL---") if log_path.exists() else 0


def call_gemini(task_dir, n, model, message, conversation_id, timeout):
    workdir = task_dir / "empty"
    workdir.mkdir(parents=True, exist_ok=True)
    guard_log = task_dir / f"round-{n}-guard.log"
    cmd = ["agy", "--agent", AGENT_NAME, "--model", model,
           "--input-format", "stream-json", "--output-format", "stream-json",
           "--print-timeout", f"{timeout}s"]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    stdin = json.dumps({"event": "user", "message": {"role": "user", "content": message}}) + "\n"
    try:
        r = subprocess.run(cmd, cwd=workdir, env=guard_env(guard_log), input=stdin, text=True,
                           errors="replace", capture_output=True, timeout=timeout + 60)
        stdout, stderr = r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "response": "", "error": f"agy did not finish within {timeout + 60} seconds."}, 0
    result = None
    for line in stdout.splitlines():
        if line.startswith('{"event":"result"'):
            try:
                result = json.loads(line)["result"]
            except (json.JSONDecodeError, KeyError):
                pass
    if result is None:
        result = {"status": "NO_RESULT", "response": "", "error": (stderr or stdout).strip()[-2000:]}
    return result, count_tool_calls(guard_log)


def read_loop(task_dir, n, model, message, conversation_id, a, root, meta):
    root = Path(root).resolve()
    start_time = time.time()
    read_log = []
    total_tool_calls = 0
    total_usage = {"input_tokens": 0, "output_tokens": 0}
    has_usage = False
    conv_id = conversation_id
    sent_cutoff = False

    def update_usage(res):
        nonlocal has_usage
        u = res.get("usage") or {}
        if isinstance(u.get("input_tokens"), int):
            total_usage["input_tokens"] += u["input_tokens"]
            has_usage = True
        if isinstance(u.get("output_tokens"), int):
            total_usage["output_tokens"] += u["output_tokens"]
            has_usage = True

    def finish(res):
        if has_usage:
            res.setdefault("usage", {})
            res["usage"]["input_tokens"] = total_usage["input_tokens"]
            res["usage"]["output_tokens"] = total_usage["output_tokens"]
        if conv_id:
            res["conversation_id"] = conv_id
        (task_dir / f"round-{n}-reads.log").write_text("".join(json.dumps(e) + "\n" for e in read_log))
        return res, total_tool_calls, read_log

    call_timeout = max(60, int(a.timeout - (time.time() - start_time)))
    result, calls = call_gemini(task_dir, n, model, message, conv_id, call_timeout)
    total_tool_calls += calls
    update_usage(result)
    if result.get("conversation_id"):
        conv_id = result["conversation_id"]
    if result.get("status") != "SUCCESS":
        return finish(result)

    request_round = 0
    while True:
        requests = parse_requests(result.get("response") or "")
        if requests is None:
            break

        request_round += 1
        remaining_time = a.timeout - (time.time() - start_time)
        if request_round > a.max_reads or remaining_time < 120:
            if sent_cutoff:
                result["status"] = "READ_LIMIT"
                break
            sent_cutoff = True
            next_message = "No more requests are allowed. Give your final answer now, in the output format above."
        else:
            request_texts = []
            for kind, arg in requests:
                text, entry = run_request(root, kind, arg, a.allow_secrets)
                read_log.append(entry)
                request_texts.append(text)
                if kind == "READ" and entry["status"] == "ok":
                    m = re.match(r"^(\S.+?)\s+(\d+)-(\d+)$", arg)
                    path_str = m.group(1) if m else arg
                    target_path = (root / path_str).resolve()
                    if under(target_path, root) and target_path.is_file():
                        try:
                            rel = str(target_path.relative_to(root))
                            meta["sent"][rel] = sha(read_text(target_path))
                        except (ValueError, OSError):
                            pass
            next_message = "\n\n".join(request_texts)
            msg_bytes = next_message.encode("utf-8")
            if len(msg_bytes) > MAX_PROMPT_BYTES:
                next_message = msg_bytes[:MAX_PROMPT_BYTES].decode("utf-8", errors="ignore") + "\n[cut: results too large; ask for less]"

        call_timeout = max(60, int(a.timeout - (time.time() - start_time)))
        result, calls = call_gemini(task_dir, n, model, next_message, conv_id, call_timeout)
        total_tool_calls += calls
        update_usage(result)
        if result.get("conversation_id"):
            conv_id = result["conversation_id"]
        if result.get("status") != "SUCCESS":
            break

    return finish(result)


# ---------- commands ----------

def cmd_setup(a):
    if shutil.which("agy") is None:
        die("agy is not installed or not on PATH. Install the Antigravity CLI first.")
    hooks = load_hooks()
    AGENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    AGENT_FILE.write_text(AGENT_TEXT)
    print(f"Agent file: {AGENT_FILE}")
    if hooks.get(HOOK_KEY) == HOOK_ENTRY:
        print(f"Guard hook: already up to date in {HOOKS_FILE}")
    else:
        if HOOKS_FILE.exists():
            backup = HOOKS_FILE.with_name(f"hooks.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(HOOKS_FILE, backup)
            print(f"Backup of your hooks file: {backup}")
        hooks[HOOK_KEY] = HOOK_ENTRY
        HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        HOOKS_FILE.write_text(json.dumps(hooks, indent=2) + "\n")
        print(f"Guard hook: added '{HOOK_KEY}' to {HOOKS_FILE}. Your other hooks were not changed.")
    print(f"Next: run {SELF} doctor")


def cmd_uninstall(a):
    hooks = load_hooks()
    if HOOK_KEY in hooks:
        del hooks[HOOK_KEY]
        if hooks:
            HOOKS_FILE.write_text(json.dumps(hooks, indent=2) + "\n")
        else:
            HOOKS_FILE.unlink()
        print(f"Removed the guard hook '{HOOK_KEY}' from {HOOKS_FILE}.")
    if AGENT_FILE.exists():
        AGENT_FILE.unlink()
        print(f"Removed {AGENT_FILE}.")
    if a.purge and RUNS.exists():
        shutil.rmtree(RUNS)
        print(f"Deleted the saved tasks in {RUNS}.")
    print(f"To remove the skill itself, delete this folder: {SELF.parent}")


def cmd_doctor(a):
    results = []

    def check(name, passed, detail=""):
        results.append(passed)
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))

    agy = shutil.which("agy")
    version = subprocess.run(["agy", "--version"], capture_output=True, text=True).stdout.strip() if agy else ""
    check("agy is installed", bool(agy), f"version {version} (tested: {TESTED_AGY_VERSIONS})" if agy else "not on PATH")
    problems = setup_problems()
    check("setup is complete", not problems, "; ".join(problems) + f". Run: {SELF} setup" if problems else "")
    if agy:
        listed = subprocess.run(["agy", "models"], capture_output=True, text=True, timeout=120).stdout
        for level, model in MODELS.items():
            check(f"model '{model}' ({level}) exists", model in listed,
                  "" if model in listed else f"set AGY_GEMINI_MODEL_{level.upper()}; see `agy models`")
    if a.offline or problems or not agy:
        print("Skipped the live tests." if not a.offline else "Skipped the live tests (--offline).")
    else:
        with tempfile.TemporaryDirectory(prefix="agy-gemini-doctor-") as tmp:
            tmp = Path(tmp)
            result, calls = call_gemini(tmp / "t1", 1, MODELS["low"],
                                        "Use a tool to list the files in the current folder. "
                                        "If you have no tools, reply exactly: NO-TOOLS", None, 120)
            answer = (result.get("response") or "").strip()
            check("the plain agent has no tools", "NO-TOOLS" in answer and calls == 0,
                  f"answer={answer[:80]!r}, tool calls={calls}")

            probe_dir, log = tmp / "t2", tmp / "t2.log"
            probe_dir.mkdir()
            subprocess.run(["agy", "--model", MODELS["low"], "--mode", "accept-edits", "--output-format", "json",
                            "--print-timeout", "120s", "-p",
                            "Use your file tool to create a file named probe.txt that contains the word x."],
                           cwd=probe_dir, env=guard_env(log), capture_output=True, text=True, timeout=180)
            blocked = not (probe_dir / "probe.txt").exists() and count_tool_calls(log) > 0
            check("the guard hook blocks tool calls", blocked,
                  f"tool calls blocked={count_tool_calls(log)}, probe.txt created={(probe_dir / 'probe.txt').exists()}")

            filler = "\n".join(f"line {i}: the quick brown fox" for i in range(3800))
            message = ("Below is a list of numbered lines, then a secret word. Reply with only the secret word.\n\n"
                       f"{filler}\n\nSecret word: PELICAN")
            result, _ = call_gemini(tmp / "t3", 1, MODELS["low"], message, None, 120)
            answer = (result.get("response") or "").strip()
            check(f"a {len(message.encode()) // 1024} KB message arrives complete", "PELICAN" in answer,
                  f"answer={answer[:80]!r}")
    ok = all(results)
    print("\nAll checks passed." if ok else "\nSome checks failed. Do not use agy-gemini until they pass.")
    sys.exit(0 if ok else 1)


def cmd_ask(a):
    prompt = (a.prompt if a.prompt is not None else sys.stdin.read()).strip()
    if not prompt:
        die("The task is empty.")
    problems = setup_problems()
    if problems:
        die("Setup is not complete: " + "; ".join(problems) + f".\nRun: {SELF} setup")

    if a.cont:
        task_dir, meta = load_meta(a.cont)
        root = Path(meta["root"])
        if not meta.get("conversation_id"):
            die("This task has no Gemini conversation ID. Start a new task instead.")
    else:
        root = Path.cwd().resolve()
        check_root(root)
        task_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        task_dir = RUNS / task_id  # created below, after the files and the prompt pass the checks
        meta = {"task_id": task_id, "root": str(root), "conversation_id": None, "sent": {}, "rounds": []}

    # Files to send: the --file arguments, plus (in a follow-up) files that changed since Gemini saw them.
    to_send = {}
    for f in a.file or []:
        path = Path(f).expanduser().resolve()
        if not path.is_file():
            die(f"File not found: {f}")
        if is_sensitive(path):
            die(f"Refusing to send '{f}' to Gemini. It looks like a secret or protected file.")
        try:
            to_send[label_for(path, root)] = read_text(path)
        except ValueError as exc:
            die(f"Cannot send '{f}': {exc}.")
    if a.cont:
        for label, digest in meta["sent"].items():
            path = Path(label) if Path(label).is_absolute() else root / label
            if label not in to_send and path.is_file():
                text = read_text(path)
                if sha(text) != digest:
                    to_send[label] = text

    if not a.allow_secrets:
        findings = [(label, *f) for label, text in list(to_send.items()) + [("task text", prompt)]
                    for f in find_secrets(text)]
        if findings:
            listing = "\n".join(f"  {label}:{line}: {kind} ({masked})" for label, line, kind, masked in findings)
            die("Possible secrets found. Nothing was sent to Gemini.\n" + listing +
                "\nRemove or redact them. Use --allow-secrets only if the user agrees that these are not secrets.")

    parts = []
    if not a.cont:
        parts.append(f"Project folder: {root.name}. File paths are relative to the project folder.")
    if to_send:
        parts.append("## Files (current versions)" if a.cont else "## Files")
        for label, text in to_send.items():
            fence = "`````" if "````" in text else "````"
            parts.append(f"### {label}\n{fence}\n{text.rstrip(chr(10))}\n{fence}")
    parts.append("## Task\n" + prompt)
    parts.append(EDIT_RULES if a.edit else ANSWER_RULES)
    if a.read_tools:
        parts.append(READ_TOOLS_RULES.format(max_reads=a.max_reads))
    message = "\n\n".join(parts)
    size = len(message.encode("utf-8"))
    if size > MAX_PROMPT_BYTES:
        sizes = ", ".join(f"{l} ({len(t.encode()) // 1024} KB)" for l, t in to_send.items())
        die(f"The prompt is {size // 1024} KB. The limit is {MAX_PROMPT_BYTES // 1024} KB, because agy cuts "
            f"longer messages. Send fewer or smaller files, or only the relevant parts. Files: {sizes}")

    if not a.cont:
        RUNS.mkdir(parents=True, exist_ok=True)
        cleanup_old_tasks()
    task_dir.mkdir(parents=True, exist_ok=True)
    n = len(meta["rounds"]) + 1
    model = MODELS.get(a.model, a.model)
    (task_dir / f"round-{n}-prompt.md").write_text(message)
    started = time.time()
    if a.read_tools:
        result, tool_calls, read_log = read_loop(task_dir, n, model, message, meta["conversation_id"], a, root, meta)
    else:
        result, tool_calls = call_gemini(task_dir, n, model, message, meta["conversation_id"], a.timeout)
        read_log = []
    seconds = int(time.time() - started)
    answer = result.get("response") or ""
    (task_dir / f"round-{n}-answer.md").write_text(answer)
    meta["conversation_id"] = result.get("conversation_id") or meta["conversation_id"]
    for label, text in to_send.items():
        meta["sent"][label] = sha(text)

    rnd = {"round": n, "model": model, "edit": a.edit, "status": result.get("status"),
           "seconds": seconds, "proposal_ok": False, "applied": False, "reverted": False}
    usage = result.get("usage") or {}
    print(f"TASK {meta['task_id']} | round {n} | {model} | status {rnd['status']} | {seconds}s | "
          f"Gemini tokens in {usage.get('input_tokens', '?')} out {usage.get('output_tokens', '?')}")
    print(f"Project: {root}")
    print(f"Files sent this round: {', '.join(to_send) or 'none'}")
    if a.read_tools:
        print(f"\n== Gemini read ({len(read_log)} requests)")
        for entry in read_log:
            note_str = f" - {entry['note']}" if entry.get("note") else ""
            print(f"{entry['status']} {entry['kind']} {entry['target']} ({entry['bytes']} bytes){note_str}")
    if result.get("error"):
        print(f"\n== agy error\n{result['error']}")
    if tool_calls:
        print(f"\nWARNING: Gemini tried to use tools {tool_calls} times. The guard blocked all of them. "
              f"Log: {task_dir / f'round-{n}-guard.log'}")

    if not a.edit:
        lines = answer.splitlines()
        print(f"\n== Gemini answer ({len(lines)} lines; full text: {task_dir / f'round-{n}-answer.md'})")
        print("\n".join(lines[:ANSWER_LINES]) if lines else "(empty)")
        if len(lines) > ANSWER_LINES:
            print(f"... ({len(lines) - ANSWER_LINES} more lines in the file above)")
    else:
        blocks, notes, errors = parse_blocks(answer)
        files, build_errors = build_proposal(root, meta, blocks)
        errors += build_errors
        if not blocks and not errors:
            errors.append("Gemini returned no edit blocks.")
        diff_text = make_diff(files)
        (task_dir / f"round-{n}.diff").write_text(diff_text)
        (task_dir / f"round-{n}-proposal.json").write_text(json.dumps(files))
        rnd["proposal_ok"] = bool(files) and not errors

        print(f"\n== Gemini notes\n{notes or '(none)'}")
        print(f"\n== Proposed changes: {len(blocks)} edit blocks, {len(files)} files")
        for rel, f in files.items():
            print(f"{'A' if f['old'] is None else 'M'}  {rel}")
        print(f"\n== Problems ({len(errors)})")
        print("\n".join(f"- {e}" for e in errors) if errors else "(none)")
        warnings = review_warnings(files, diff_text)
        print(f"\n== Warnings ({len(warnings)})")
        print("\n".join(f"- {w}" for w in warnings) if warnings else "(none)")
        dlines = diff_text.splitlines()
        if dlines and len(dlines) <= INLINE_DIFF_LINES:
            print(f"\n== Proposed diff ({len(dlines)} lines). Nothing is written yet.")
            print(diff_text.rstrip())
        elif dlines:
            print(f"\n== Proposed diff is {len(dlines)} lines. Read it in parts:")
            print(f"sed -n 1,{INLINE_DIFF_LINES}p {task_dir / f'round-{n}.diff'}")
        print("\n== Next")
        if rnd["proposal_ok"]:
            print(f"Apply after review: {SELF} apply {meta['task_id']}")
        else:
            print("This proposal cannot be applied. Ask Gemini to fix the problems with --continue.")
    print(f"Follow-up: {SELF} ask --continue {meta['task_id']} [--edit] [--file PATH] <<'GEMINI_TASK' ... GEMINI_TASK")

    meta["rounds"].append(rnd)
    save_meta(task_dir, meta)


def cmd_apply(a):
    task_dir, meta = load_meta(a.task)
    root = Path(meta["root"])
    if not meta["rounds"]:
        die("This task has no rounds.")
    rnd = meta["rounds"][-1]
    if not rnd.get("edit"):
        die("The latest round has no proposed changes.")
    if not rnd.get("proposal_ok"):
        die("The latest proposal has problems and cannot be applied.")
    if rnd.get("applied"):
        die("The latest proposal is already applied.")
    files = json.loads((task_dir / f"round-{rnd['round']}-proposal.json").read_text())
    for rel, f in files.items():
        path = root / rel
        current = read_text(path) if path.exists() else None
        if current != f["old"]:
            die(f"'{rel}' changed after Gemini made the proposal. Nothing was written. "
                f"Ask Gemini again with --continue. The script sends the new version automatically.")
    for rel, f in files.items():
        write_text(root / rel, f["new"])
        meta["sent"][rel] = "applied"  # never matches a hash, so a follow-up re-sends the current version
    rnd["applied"] = True
    save_meta(task_dir, meta)
    print(f"Applied round {rnd['round']} of task {meta['task_id']} in {root}:")
    for rel, f in files.items():
        print(f"{'A' if f['old'] is None else 'M'}  {rel}")
    print(f"Undo: {SELF} revert {meta['task_id']}")


def cmd_revert(a):
    task_dir, meta = load_meta(a.task)
    root = Path(meta["root"])
    rounds = [r for r in meta["rounds"] if r.get("applied") and not r.get("reverted")]
    if not rounds:
        die("This task has no applied changes to undo.")
    restored, removed, skipped = [], [], []
    for rnd in reversed(rounds):
        files = json.loads((task_dir / f"round-{rnd['round']}-proposal.json").read_text())
        for rel, f in files.items():
            path = root / rel
            current = read_text(path) if path.exists() else None
            if current != f["new"]:
                skipped.append(rel)
                continue
            if f["old"] is None:
                path.unlink()
                removed.append(rel)
            else:
                write_text(path, f["old"])
                restored.append(rel)
        rnd["reverted"] = True
    save_meta(task_dir, meta)
    restored, removed, skipped = (list(dict.fromkeys(x)) for x in (restored, removed, skipped))
    print(f"Reverted task {meta['task_id']} in {root}")
    print(f"Restored to the old version: {', '.join(restored) or 'none'}")
    print(f"Removed (files Gemini created): {', '.join(removed) or 'none'}")
    if skipped:
        print(f"SKIPPED, because they changed after the apply: {', '.join(skipped)}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    d = sub.add_parser("doctor")
    d.add_argument("--offline", action="store_true", help="skip the live tests that call Gemini")
    q = sub.add_parser("ask")
    q.add_argument("--model", default="high", help="high, medium, low, or a full agy model name")
    q.add_argument("--file", action="append", metavar="PATH", help="send this file to Gemini (repeatable)")
    q.add_argument("--edit", action="store_true", help="ask for edit blocks and build a proposed diff")
    q.add_argument("--continue", dest="cont", metavar="TASK_ID", help="follow-up in the same Gemini conversation")
    q.add_argument("--timeout", type=int, default=480, help="seconds for Gemini (default 480)")
    q.add_argument("--allow-secrets", action="store_true", help="skip the secret scan (only with the user's consent)")
    q.add_argument("--read-tools", action="store_true", help="enable the request loop")
    q.add_argument("--max-reads", type=int, default=8, metavar="N", help="maximum number of request rounds (default 8)")
    q.add_argument("prompt", nargs="?")
    ap = sub.add_parser("apply")
    ap.add_argument("task")
    rv = sub.add_parser("revert")
    rv.add_argument("task")
    u = sub.add_parser("uninstall")
    u.add_argument("--purge", action="store_true", help=f"also delete the saved tasks in {RUNS}")
    a = p.parse_args()
    {"setup": cmd_setup, "doctor": cmd_doctor, "ask": cmd_ask, "apply": cmd_apply,
     "revert": cmd_revert, "uninstall": cmd_uninstall}[a.cmd](a)


if __name__ == "__main__":
    main()
