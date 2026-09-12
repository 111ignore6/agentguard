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
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

__version__ = "0.1.0"

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


def scan_github(repo, rules, n_issues=50, workers=6):
    """Scan a GitHub repo: instruction files + recent open issues."""
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

    stats = {
        "repo": repo,
        "stars": meta.get("stargazers_count"),
        "forks": meta.get("forks_count"),
        "watchers": meta.get("subscribers_count"),
        "open_issues": meta.get("open_issues_count"),
        "files_scanned": scanned,
        "issues_scanned": len(issues),
        "issues_with_findings": hits,
    }
    return findings, stats


def scan_local(root, rules, scan_all=False, excludes=None):
    """Scan a local checkout."""
    findings, scanned, skipped = [], [], []
    patterns = load_ignore(root, excludes)
    if os.path.isfile(root):
        candidates = [root]
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv", "dist", "build"}]
            for fn in filenames:
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

    return findings, {"root": os.path.abspath(root), "files_scanned": scanned,
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

    print("  " + _c({
        "hostile": "-> Do not let an autonomous agent read this repo unsupervised.",
        "suspect": "-> Review manually before running an agent here.",
        "clean": "-> Nothing found. Still apply least privilege to your agent.",
    }[v], vc, color))
    print()
    return v


# --------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="agentguard", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"agentguard {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan a GitHub repo (owner/repo)")
    s.add_argument("repo")
    s.add_argument("--issues", type=int, default=50)
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
        findings, stats = scan_github(a.repo.strip().strip("/"), rules, a.issues)
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
