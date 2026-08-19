#!/usr/bin/env python3
"""Redacted secret-pattern scan for a sanitized release tree.

This is a lightweight pre-publish guard. It intentionally reports only the file,
line, and rule name, never the matched token text.

Reviewed matches live in secret_scan_allowlist.json, recorded per (path, rule). The
vendored project ships fixture credentials in its own tests, and without a way to
record that this scan is always red -- which means nobody reads it and it cannot
enter CI. Exempting whole directories was rejected: a real key pasted into a test
file is precisely what this is for. An entry matching nothing fails the scan, so a
stale exemption surfaces rather than quietly widening what is tolerated.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import json
import re
import subprocess
import sys
from pathlib import Path

ALLOWLIST_PATH = Path(__file__).resolve().parent / "secret_scan_allowlist.json"

# 本次因为 git 忽略而没扫的文件数。静默跳过和静默放行一样危险。
_skipped_as_ignored = 0


def load_allowlist(path: Path) -> list[dict]:
    """Load reviewed exemptions, refusing to run silently without them.

    Returning an empty list when the file is missing hides the case that actually
    happened here: .gitignore's **/*secret*.json rule swallowed this file by name,
    so the scan ran with no exemptions and failed on everything. Silence turns a
    missing input into a wall of findings that look like the tool's fault.
    """
    if not path.is_file():
        raise SystemExit(
            f"Allowlist not found: {path}\n"
            "Checked: that exact path.\n"
            "The scan needs it to tell reviewed fixtures from real credentials.\n"
            "If it exists locally but git does not track it, check whether an ignore\n"
            "rule matches its name:\n"
            f"    git check-ignore -v {path}"
        )
    return json.loads(path.read_text()).get("allow", [])

# every directory with that name would have hidden a key committed there.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".cache",
}

SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".pt",
    ".pth",
    ".ckpt",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
    ".db",
}

ALLOW_PLACEHOLDER_PATTERNS = (
    "your_",
    "replace-with-",
    "example",
    "placeholder",
    "sk-your",
    "AIza-your",
)

RULES = [
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{20,}")),
    ("openai-style-key", re.compile(r"\bsk-[0-9A-Za-z_-]{20,}")),
    ("tavily-key", re.compile(r"\btvly-[0-9A-Za-z_-]{20,}")),
    ("anthropic-token", re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,}")),
    ("bearer-token", re.compile(r"Bearer\s+[0-9A-Za-z._~+/=-]{20,}", re.IGNORECASE)),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("service-account-json", re.compile(r'"type"\s*:\s*"service_account"')),
    ("private-path", re.compile(r"/data/(example-user-2|example-user-1|example-user-3)/")),
]


def should_skip(path: Path, root: Path | None = None) -> bool:
    # 相对扫描根判断，不看绝对路径。SKIP_DIRS 里是 build / dist / venv / .cache 这类
    # 常见目录名，clone 落在 ~/build/ 或 /data/dist/ 下时，整棵树的每个文件都会命中，
    # 扫描一个字节都不读却报绿——比报红危险得多。
    relevant = path.parts
    if root is not None:
        try:
            relevant = path.relative_to(root).parts
        except ValueError:
            pass
    if any(part in SKIP_DIRS for part in relevant):
        return True
    # A symlink is a short string, never a binary blob, so the suffix rule that
    # exists to avoid reading images and archives does not apply to it. Applying
    # it anyway meant naming a link ref.zip put its target beyond the scan.
    if path.is_symlink():
        return False
    return path.suffix.lower() in SKIP_SUFFIXES


def ignored_by_git(root: Path, paths: list[Path]) -> set[Path]:
    """这些路径里，git 会忽略的那些。

    判据是「会不会被发布」，不是「在不在磁盘上」。README 的第一条命令就是把真凭据写进
    被忽略的 .env，扫它等于让新机器的第一条命令必然红，而没人能弄绿的门会被关掉（#49）。

    未跟踪但未被忽略的文件仍然要扫：一次 git add 就会被发布。不在 git 仓里时返回空集，
    release tree 是 git archive 出来的，那里没有 .git，一个文件都不能漏。
    """
    if not paths:
        return set()
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--stdin", "-z"],
            input="\0".join(str(p) for p in paths), capture_output=True, text=True,
            timeout=60)
    except (OSError, subprocess.SubprocessError):
        return set()
    # 0 = 有被忽略的，1 = 一个都没有，其余（128 = 不是 git 仓）当作没有忽略规则。
    if done.returncode not in (0, 1):
        return set()
    return {Path(line) for line in done.stdout.split("\0") if line}


def iter_files(root: Path):
    if root.is_file():
        yield root
        return
    found = [path for path in root.rglob("*")
             # is_file() follows a link, so one that dangles was dropped before it
             # could be scanned. What ships is the link itself, and its target is text.
             if (path.is_symlink() or path.is_file()) and not should_skip(path, root)]
    ignored = ignored_by_git(root, found)
    global _skipped_as_ignored
    _skipped_as_ignored = len(ignored)
    if found and len(ignored) == len(found):
        # 把 release tree 解到仓库内部（`./release/`）时，.gitignore 会把它整个吃掉，
        # 于是这道门一个字节都没读却报绿——比报红危险得多。
        raise SystemExit(
            f"Everything under {root} is git-ignored, so nothing was scanned.\n"
            f"Checked: {len(found)} file(s), all ignored.\n"
            "Point this at a tree that would actually be published, or export it "
            "outside the repository first (git archive HEAD | tar -x -C ...)."
        )
    for path in found:
        if path not in ignored:
            yield path


def fingerprint(value: str) -> str:
    """A stable, non-reversible id for one matched value.

    The allowlist pins these rather than (path, rule): with only the pair, a file
    exempted for one openai-style-key silently covered every later key of the same
    shape pasted into it -- which is the case the scan exists to catch. Truncated
    SHA-256 identifies a hit without reproducing the secret in a tracked file.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def scan_file(path: Path):
    findings = []

    if path.is_symlink():
        # Never follow. read_text() resolved the link and scanned whatever it
        # pointed at, so a link out of the tree made the result depend on the
        # scanning machine: content read here, nothing read on CI where the target
        # is absent. What ships is the link, so the link target is what to scan.
        text = os.readlink(path)
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            findings.append((path, 0, "read-error", str(exc)))
            return findings

    for line_no, line in enumerate(text.splitlines(), start=1):
        for rule_name, pattern in RULES:
            for match in pattern.finditer(line):
                # Every match is recorded, not just the first on the line: a file
                # exempted for one hit must not carry a second one for free.
                findings.append((path, line_no, rule_name, fingerprint(match.group(0))))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="release tree to scan")
    parser.add_argument("--allowlist", type=Path, default=ALLOWLIST_PATH,
                        help="reviewed matches, recorded per (path, rule)")
    args = parser.parse_args()

    root = Path(args.root).resolve()

    if not root.exists():
        # Scanning nothing and printing a pass is the worst answer a pre-publish
        # gate can give: it reads exactly like a clean tree.
        print(
            f"Nothing to scan at {root}\n"
            "Checked: that exact path.\n"
            "Point this at the tree you are about to publish.",
            file=sys.stderr,
        )
        return 2
    allowlist = load_allowlist(args.allowlist)
    used: set[int] = set()

    findings = []
    for path in iter_files(root):
        for finding in scan_file(path):
            file_path, line_no, rule_name, digest = finding
            rel = str(file_path.relative_to(root) if file_path.is_relative_to(root) else file_path)
            index = next(
                (i for i, entry in enumerate(allowlist)
                 if entry.get("path") == rel
                 and rule_name in entry.get("rules", [])
                 and digest in entry.get("fingerprints", [])),
                None,
            )
            if index is None:
                findings.append((rel, line_no, rule_name, digest))
            else:
                # Recorded per fingerprint, not per entry. Marking the whole entry
                # used meant one live fingerprint kept its neighbours alive, so a
                # value that had already left the file went on exempting whatever
                # took its place.
                used.add((index, digest))

    stale = [
        (entry["path"], digest)
        for i, entry in enumerate(allowlist)
        for digest in entry.get("fingerprints", [])
        if (i, digest) not in used
    ]

    if findings:
        print("Secret-pattern scan failed; matched content is redacted:", file=sys.stderr)
        for rel, line_no, rule_name, digest in findings:
            print(f"{rel}:{line_no}: {rule_name} [{digest}]", file=sys.stderr)
        print(
            "\nIf a match is not a credential, add its fingerprint to "
            f"{args.allowlist.name} under that path and rule, with a reason.",
            file=sys.stderr,
        )
        return 1

    if stale:
        # A rule that stopped matching means the file changed. Keeping the exemption
        # would silently cover whatever appears there next.
        print(f"{len(stale)} allowlist fingerprint(s) matched nothing and should be removed:", file=sys.stderr)
        for rel, digest in stale:
            print(f"  {rel} [{digest}]", file=sys.stderr)
        return 1

    exempt = len(allowlist)
    skipped = f"，{_skipped_as_ignored} 个文件因 git 忽略未扫（发布不了）" \
        if _skipped_as_ignored else ""
    print(f"Secret-pattern scan passed ({exempt} reviewed exemption(s)){skipped}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
