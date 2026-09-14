#!/usr/bin/env python3
"""
agentguard — scan repositories for content that targets AI agents.

Coding agents read CONTRIBUTING.md, issue bodies, and repo docs as trusted
context. Some of that content is written to manipulate them: to dump their
system prompt, to ignore warnings, or to farm engagement. agentguard finds it.

Requires: Python 3.8+. `gh` CLI only for the `scan <owner/repo>` mode.

    agentguard scan owner/repo          # scan a GitHub repo (files + issues)
    agentguard path .                   # scan a local checkout
    agentguard text ./CONTRIBUTING.md   # scan one file or stdin ("-")
    agentguard scan owner/repo --json   # machine-readable

Exit codes:  0 clean   1 suspect   2 hostile
"""

import argparse
import base64
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor

__version__ = "0.1.1"

# Files an agent typically ingests as trusted instructions.
DEFAULT_FILES = [
    "CONTRIBUTING.md", "README.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md",
    ".cursorrules", ".github/copilot-instructions.md", "SECURITY.md",
    "CODE_OF_CONDUCT.md", ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE.md",
]

# Extensions scanned in --all mode.
TEXT_EXT = {".md", ".txt", ".rst", ".yml", ".yaml", ".json", ".toml", ".cfg", ".ini",
            ".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".rb", ".go", ".rs", ".java", ".c", ".h", ".cpp"}

SEV_ORDER = {"hostile": 0, "suspect": 1, "info": 2}
EXIT = {"clean": 0, "suspect": 1, "hostile": 2}
EXIT_ERROR = 3          # distinct from any verdict, so CI can tell "found" from "failed"

C = {"r": "\033[31m", "y": "\033[33m", "g": "\033[32m", "d": "\033[90m",
     "b": "\033[36m", "B": "\033[1m", "0": "\033[0m"}


def _c(s, key, enabled=True):
    return f"{C[key]}{s}{C['0']}" if enabled and sys.stdout.isatty() else s


# --------------------------------------------------------------------------- rules

def load_rules(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    rules = []
    for r in data["rules"]:
        rules.append({**r, "_re": re.compile(r["regex"], re.I | re.S)})
    return rules


IGNORE_FILE = ".agentguardignore"


def load_ignore(root, extra=None):
    """Patterns from <root>/.agentguardignore plus any --exclude values."""
    pats = list(extra or [])
    path = os.path.join(root, IGNORE_FILE) if os.path.isdir(root) else None
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    pats.append(line)
    return pats


def ignored(rel, patterns):
    rel = rel.replace("\\", "/")
    for p in patterns:
        p = p.replace("\\", "/").rstrip("/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*")                 or rel == p or rel.startswith(p + "/"):
            return p
    return None


def scan_text(text, rules, source="", max_hits_per_rule=3):
    """Return findings for one blob of text."""
    if not text:
        return []
    findings = []
    for rule in rules:
        n = 0
        for m in rule["_re"].finditer(text):
            n += 1
            if n > max_hits_per_rule:
                break
            start = max(0, m.start() - 110)
            ctx = re.sub(r"\s+", " ", text[start:m.end() + 130]).strip()
            findings.append({
                "rule": rule["id"],
                "severity": rule["severity"],
                "category": rule["category"],
                "why": rule["why"],
                "match": m.group(0)[:90],
                "context": ctx[:240],
                "source": source,
            })
    return findings


def verdict(findings):
    if not findings:
        return "clean"
    if any(f["severity"] == "hostile" for f in findings):
        return "hostile"
    if any(f["severity"] == "suspect" for f in findings):
        return "suspect"
    return "clean"          # info-only does not fail a run


# --------------------------------------------------------------------------- gh

def _gh(path):
    try:
        p = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout)
    except Exception:
        return None


def fetch_repo_tree(repo, max_bytes=80_000_000):
    """Download + extract the default-branch tarball so `scan --all` can reach
    source files (the docs-only default misses exactly the class where hostile
    content has actually been found). Returns a temp dir, or None on any failure
    — a missing tarball must degrade to 'instruction files only', never to a
    crash, and never silently: the caller reports the narrowed scope.
    """
    url = f"https://codeload.github.com/{repo}/tar.gz/HEAD"
    tmp = None
    try:
        with urllib.request.urlopen(url, timeout=180) as r:   # noauth: public read
            blob = r.read(max_bytes + 1)
        if not blob or len(blob) > max_bytes:
            return None
        tmp = tempfile.mkdtemp(prefix="agentguard-")
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            try:
                tf.extractall(tmp, filter="data")             # Py>=3.12: no path/perm escapes
            except TypeError:                                  # older stdlib has no filter kwarg
                tf.extractall(tmp)
        return tmp
    except Exception:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        return None


def scan_github(repo, rules, n_issues=50, workers=6, scan_all=False):
    """Scan a GitHub repo: instruction files + recent open issues.

    With scan_all=True the repo tarball is also fetched and its source tree is
    scanned, and the reported scope becomes `all-text`.
    """
    meta = _gh(f"repos/{repo}")
    if not meta:
        print(f"agentguard: cannot read {repo} (missing repo, or `gh` not authenticated)", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)

    findings, scanned = [], []

    def fetch(path):
        d = _gh(f"repos/{repo}/contents/{path}")
        if isinstance(d, dict) and d.get("content"):
            try:
                return path, base64.b64decode(d["content"]).decode("utf-8", "replace")
            except Exception:
                return path, None
        return path, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for path, text in ex.map(fetch, DEFAULT_FILES):
            if text:
                scanned.append(path)
                findings += scan_text(text, rules, source=path)

    issues = _gh(f"repos/{repo}/issues?state=open&per_page={min(n_issues, 100)}") or []
    issues = [i for i in issues if "pull_request" not in i] if isinstance(issues, list) else []
    hits = 0
    for it in issues:
        blob = f"{it.get('title') or ''}\n{it.get('body') or ''}"
        f = scan_text(blob, rules, source=f"issue #{it['number']}")
        if f:
            hits += 1
        findings += f

    mode, considered, unscanned = "instruction-only", None, None
    if scan_all:
        tree = fetch_repo_tree(repo)
        if tree:
            try:
                more, s2 = scan_local(tree, rules, scan_all=True)
                findings += more
                scanned += s2["files_scanned"]
                considered, unscanned = s2["files_considered"], s2["files_unscanned"]
                mode = "all-text"
            finally:
                shutil.rmtree(tree, ignore_errors=True)
        else:
            print(f"agentguard: source tarball unavailable for {repo}; "
                  f"scanned instruction files only (scope stays narrow)", file=sys.stderr)

    stats = {
        "repo": repo,
        "mode": mode,
        "stars": meta.get("stargazers_count"),
        "forks": meta.get("forks_count"),
        "watchers": meta.get("subscribers_count"),
        "open_issues": meta.get("open_issues_count"),
        "files_scanned": scanned,
        "files_considered": considered,
        "files_unscanned": unscanned,
        "issues_scanned": len(issues),
        "issues_with_findings": hits,
    }
    return findings, stats


def scan_local(root, rules, scan_all=False, excludes=None):
    """Scan a local checkout.

    Scope is part of the verdict's meaning: by default only instruction files
    (DEFAULT_FILES) are read, so a 9-file repo can report "1 file scanned" and
    still read as repo-wide clean. The stats therefore always carry
    mode / files_considered / files_unscanned, and report() prints them.
    """
    findings, scanned, skipped = [], [], []
    considered = 0
    patterns = load_ignore(root, excludes)
    if os.path.isfile(root):
        candidates = [root]
        considered = 1
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv", "dist", "build"}]
            for fn in filenames:
                considered += 1
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if rel in DEFAULT_FILES or fn in DEFAULT_FILES or \
                        any(fn == d or fn.endswith("-" + d) or fn.endswith("_" + d) for d in DEFAULT_FILES):
                    candidates.append(full)
                elif scan_all and os.path.splitext(fn)[1].lower() in TEXT_EXT:
                    candidates.append(full)

    for full in candidates:
        try:
            if os.path.getsize(full) > 2_000_000:
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except (OSError, ValueError):
            continue
        rel = os.path.relpath(full, root).replace("\\", "/") if os.path.isdir(root) else os.path.basename(full)
        hit = ignored(rel, patterns)
        if hit:
            skipped.append({"file": rel, "pattern": hit})
            continue
        scanned.append(rel)
        findings += scan_text(text, rules, source=rel)

    unscanned = max(considered - len(scanned) - len(skipped), 0)
    return findings, {"root": os.path.abspath(root),
                      "mode": "all-text" if scan_all else "instruction-only",
                      "files_considered": considered,
                      "files_scanned": scanned,
                      "files_unscanned": unscanned,
                      "files_skipped": skipped}


# --------------------------------------------------------------------------- output

def report(findings, stats, as_json=False, color=True):
    v = verdict(findings)
    if as_json:
        print(json.dumps({"verdict": v, "scan": stats, "findings": findings},
                         ensure_ascii=False, indent=2))
        return v

    print()
    print(_c("agentguard", "B", color) + f"  v{__version__}")
    print("=" * 70)
    if "repo" in stats:
        print(f"  repo    {stats['repo']}")
        print(f"  stars={stats['stars']}  forks={stats['forks']}  "
              f"watchers={stats['watchers']}  open_issues={stats['open_issues']}")
        print(f"  scanned {len(stats['files_scanned'])} files, "
              f"{stats['issues_scanned']} issues "
              f"({stats['issues_with_findings']} with findings)")
    else:
        print(f"  source  {stats.get('source', '?')}")
        print(f"  scanned {len(stats['files_scanned'])} file(s)")
    if stats.get("files_skipped"):
        print(f"  skipped {len(stats['files_skipped'])} via .agentguardignore")

    # Scope must never be inferable-only-from-a-number: say what was NOT read.
    mode = stats.get("mode")
    if mode:
        n_read = len(stats.get("files_scanned") or [])
        n_un = stats.get("files_unscanned")
        line = f"  scope   {mode}: read {n_read} file(s)"
        if n_un:
            line += f", {n_un} NOT read"
        print(_c(line, "y" if mode == "instruction-only" else "d", color))
        if mode == "instruction-only":
            print(_c("          source files are outside this scope — add --all "
                     "before trusting a CLEAN verdict", "y", color))

    vc = {"hostile": "r", "suspect": "y", "clean": "g"}[v]
    print(f"\n  verdict: {_c(v.upper(), vc, color)}")

    if findings:
        print(f"\n  {len(findings)} finding(s):\n")
        for f in sorted(findings, key=lambda x: SEV_ORDER[x["severity"]]):
            sc = {"hostile": "r", "suspect": "y", "info": "d"}[f["severity"]]
            print(f"    {_c('[' + f['severity'].upper() + ']', sc, color)} "
                  f"{f['rule']}  {_c(f['category'], 'd', color)}  @ {f['source']}")
            print(f"        {f['why']}")
            print(f"        {_c(f['context'][:170], 'd', color)}")
            print()
    else:
        print("\n  No agent-targeted content found.\n")

    clean_note = "-> Nothing found. Still apply least privilege to your agent."
    if mode == "instruction-only":
        clean_note = ("-> Nothing found IN THE SCANNED SCOPE. Source files were not read; "
                      "a CLEAN here does not mean the repo is safe.")
    print("  " + _c({
        "hostile": "-> Do not let an autonomous agent read this repo unsupervised.",
        "suspect": "-> Review manually before running an agent here.",
        "clean": clean_note,
    }[v], vc, color))
    print()
    return v


# --------------------------------------------------------------------------- cli

def main(argv=None):
    # Force UTF-8 on our own streams. `--json` deliberately emits non-ASCII
    # (ensure_ascii=False) and report() echoes paths verbatim: on a zh-CN Windows
    # console those bytes come out cp936, so a non-ASCII path alone makes the
    # output invalid UTF-8 for every downstream consumer (CI actions, pipes).
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(prog="agentguard", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"agentguard {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan a GitHub repo (owner/repo)")
    s.add_argument("repo")
    s.add_argument("--issues", type=int, default=50)
    s.add_argument("--all", action="store_true",
                   help="also fetch the repo tarball and scan source files, not just instruction files")
    s.add_argument("--json", action="store_true")
    s.add_argument("--rules")

    p = sub.add_parser("path", help="scan a local file or directory")
    p.add_argument("path")
    p.add_argument("--all", action="store_true", help="scan all text files, not just instruction files")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="skip paths matching GLOB (repeatable); .agentguardignore is read automatically")
    p.add_argument("--json", action="store_true")
    p.add_argument("--rules")

    t = sub.add_parser("text", help="scan a single file or stdin")
    t.add_argument("file", help="path, or - for stdin")
    t.add_argument("--json", action="store_true")
    t.add_argument("--rules")

    a = ap.parse_args(argv)
    color = not a.json and sys.stdout.isatty()
    rules = load_rules(a.rules)

    if a.cmd == "scan":
        findings, stats = scan_github(a.repo.strip().strip("/"), rules, a.issues, scan_all=a.all)
    elif a.cmd == "path":
        if not os.path.exists(a.path):
            print(f"agentguard: no such path: {a.path}", file=sys.stderr)
            raise SystemExit(EXIT_ERROR)
        findings, stats = scan_local(a.path, rules, a.all, a.exclude)
    else:
        if a.file == "-":
            text, src = sys.stdin.read(), "<stdin>"
        else:
            with open(a.file, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            src = a.file
        findings = scan_text(text, rules, source=src)
        stats = {"source": src, "files_scanned": [src]}

    v = report(findings, stats, as_json=a.json, color=color)
    return EXIT[v]


if __name__ == "__main__":
    sys.exit(main())
