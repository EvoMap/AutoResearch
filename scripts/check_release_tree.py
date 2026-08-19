#!/usr/bin/env python3
"""Check that what gets published contains no blocked path and no oversized file.

The release unit is the whole tree `git archive` produces from the published ref.
This repository has no curated subset: everything tracked on the default branch is
what the world downloads.

The check therefore runs on either an exported tree or the checkout itself, and
gets the same answer from both. Three things keep the checkout from drowning it in
noise: git-ignored files are skipped because they never ship, .git is skipped
because git archive never emits it, and directories are not judged because every
file inside one is judged on its own full path.

Anything the repository ships on purpose is named file by file in ALLOW_FILES.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path

def ignored_paths(root: Path) -> set[Path]:
    """Everything git ignores here. Ignored files are never part of a release."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others", "--ignored", "--exclude-standard",
         "--directory", "-z"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return set()
    return {(root / name).resolve() for name in result.stdout.split("\0") if name}


BLOCKED_PATTERNS = [
    ".env",
    ".env.*",
    # Registry and machine credentials. Both routinely carry auth tokens, and
    ".npmrc",
    ".netrc",
    "credentials.json",
    "config.json",
    "config.local.json",
    "*.local.json",
    ".claude",
    ".claude/*",
    "*/.claude/*",
    "settings.local.json",
    "*/settings.local.json",
    "twitter_cookies.json",
    "*cookie*.json",
    "*cookies*.json",
    "*session*.json",
    "*token*.json",
    "*secret*.json",
    "*credentials*.json",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "logs/*",
    "*/logs/*",
    "outputs/*",
    "*/outputs/*",
    "runs/*",
    "artifacts/*",
    "scratch/*",
    "node_modules/*",
    "*/node_modules/*",
    ".conda-env/*",
    "*/.conda-env/*",
    "*.pt",
    "*.pth",
    "*.ckpt",
    "*.safetensors",
]

# Carved out of BLOCKED_PATTERNS, one reviewed file per line. These are exact
# paths, not globs: a directory-wide entry clears whatever anyone drops into that
# directory later, which defeats the point of the gate. Adding a file under an
# already-listed directory is meant to fail here until someone reads it.
#
# The blocked list is broad on purpose. .gitignore excludes only the root
# /.claude/ plus autonomy and *.local.md, so the vendored project's skills and
# agents ship deliberately and each is listed below.
ALLOW_FILES = {
    ".env.example",
    "config.example.json",
    "config/providers.example.json",
    "scripts/secret_scan_allowlist.json",
    # ar-runtime ships reviewed workflow definitions for the official Claude Code CLI.
    "ar-runtime/.claude/agents/ar-blind-reviewer.md",
    "ar-runtime/.claude/agents/ar-coder.md",
    "ar-runtime/.claude/agents/ar-critic.md",
    "ar-runtime/.claude/agents/ar-gemini-reviewer.md",
    "ar-runtime/.claude/agents/ar-planner.md",
    "ar-runtime/.claude/agents/ar-runner.md",
    "ar-runtime/.claude/agents/ar-subcoder.md",
    "ar-runtime/.claude/settings.json",
    "ar-runtime/.claude/settings.local.example.json",
    "ar-runtime/.claude/skills/ar-coordinator/SKILL.md",
    "ar-runtime/.claude/skills/ar-experiment-runner/SKILL.md",
    "ar-runtime/.claude/skills/ar-gpu-preflight/SKILL.md",
    "ar-runtime/.claude/skills/ar-workspace-safety/SKILL.md",
}

DEFAULT_MAX_BYTES = 25 * 1024 * 1024


def matches(path: str, patterns: list[str]) -> bool:
    """Judge the full path and the bare filename against every pattern.

    Patterns without a leading wildcard -- .env, .env.*, config.json,
    settings.local.json -- are anchored at the root by fnmatch, so the same file
    one directory down was invisible. Testing the basename too makes a rule about
    a filename mean that filename anywhere, which is what every entry here
    intends. Patterns that contain a slash cannot match a basename, so the
    directory-scoped entries (logs/*, node_modules/*) are unaffected.
    """
    name = path.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern)
        for pattern in patterns
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="release tree to check")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--allow-git-worktree", action="store_true",
        help="accepted and ignored; a checkout and an exported tree now agree",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()

    if not root.is_dir():
        # A missing root used to report a pass, which is the worst possible answer
        # for a pre-publish gate: nothing was checked and it said so was fine.
        print(
            f"Not a directory: {root}\n"
            "Checked: that exact path.\n"
            "Export the tree first, then point this at it.",
            file=sys.stderr,
        )
        return 2

    # Ignored files never ship, so scanning them only produces noise: node_modules
    # alone accounts for the overwhelming majority of hits on this repository.
    skip = ignored_paths(root)
    findings = []

    for path in root.rglob("*"):
        # Directories carry nothing themselves, and every file inside one is
        # judged on its own full path, so a blocked directory still gets caught
        # through its contents. Judging the directory too only duplicated the
        # finding, and forced the allow list to name directories as well as files.
        #
        # Symlinks are judged even when they dangle. is_file() follows the link,
        # so a broken .env symlink was skipped entirely, and a live one pointing
        # outside the tree at ~/.aws/credentials would ship its name into the
        # release. The name is what the rules are about.
        is_link = path.is_symlink()
        if not is_link and not path.is_file():
            continue
        if any(parent in skip for parent in (path, *path.parents)):
            continue
        # git metadata is not part of any release: git archive never emits it.
        if ".git" in path.relative_to(root).parts[:1] or any(
            parent.name == ".git" for parent in path.parents
        ):
            continue
        rel = path.relative_to(root).as_posix()
        # An allow entry exempts the path it names, not everything beneath it. The
        # earlier version skipped the whole subtree, so a key or an oversized blob
        # dropped inside an allowed directory was never examined.
        exempt = rel in ALLOW_FILES

        if not exempt and matches(rel, BLOCKED_PATTERNS):
            findings.append((rel, "blocked-path"))
            continue

        if is_link:
            # A link's own name told us nothing about what it points at. One named
            # public-reference.txt aiming at $HOME/.aws/credentials passed
            # every check and shipped that path into the release for anyone to read.
            target = os.readlink(path)
            if not exempt and matches(target, BLOCKED_PATTERNS):
                findings.append((rel, f"blocked-link-target:{target}"))
                continue
            resolved = (path.parent / target).resolve()
            if Path(target).is_absolute() or not resolved.is_relative_to(root):
                findings.append((rel, f"link-escapes-tree:{target}"))
            elif not resolved.exists():
                # A link inside the tree that resolves to nothing is either a file
                # someone forgot to add or a target string being carried under a
                # harmless-looking name. Either way the release is not what it
                # claims to be.
                findings.append((rel, f"link-target-missing:{target}"))
            continue

        try:
            # lstat, so a dangling link reports its own size instead of raising.
            size = path.lstat().st_size if is_link else path.stat().st_size
        except OSError:
            continue
        # Size is checked even for reviewed files: having been read for content
        # says nothing about being small enough to ship.
        if size > args.max_bytes:
            findings.append((rel, f"large-file>{args.max_bytes}"))

    if findings:
        print("Release tree check failed:", file=sys.stderr)
        for rel, reason in findings:
            print(f"{rel}: {reason}", file=sys.stderr)
        return 1

    print("Release tree check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
