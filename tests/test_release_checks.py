"""Tests for the two pre-publish checks.

Both were unusable before: pointed at a working checkout they reported hundreds of
findings that were not real, so they were always red and could not enter CI. These
cases pin the behaviour that makes them meaningful — that they still fail on the
things they exist to catch.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SECRET_SCAN = SCRIPTS / "secret_scan.py"
RELEASE_TREE = SCRIPTS / "check_release_tree.py"

ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin"}

# Not a credential: 48 repeated characters behind a real prefix, so it matches the
# pattern without ever having been a key.
FIXTURE_KEY = "sk-" + "a" * 48


def fingerprint(value: str) -> str:
    """Mirror of secret_scan.fingerprint, so a drift in either shows up as a failure."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def run(script: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(script), *args], capture_output=True, text=True, env=ENV
    )
    return result.returncode, result.stdout + result.stderr


@pytest.fixture
def export(tmp_path: Path) -> Path:
    """A directory shaped like an exported release, with no git metadata."""
    root = tmp_path / "release"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n")
    return root


def test_secret_scan_flags_a_real_looking_key(export: Path, tmp_path: Path) -> None:
    (export / "src" / "leak.py").write_text('KEY = "sk-' + "a" * 48 + '"\n')
    empty = tmp_path / "allow.json"
    empty.write_text(json.dumps({"allow": []}))

    code, out = run(SECRET_SCAN, str(export), "--allowlist", str(empty))
    assert code == 1
    assert "leak.py" in out


def test_secret_scan_honours_a_reviewed_exemption(export: Path, tmp_path: Path) -> None:
    (export / "src" / "fixture.py").write_text(f'KEY = "{FIXTURE_KEY}"\n')
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(json.dumps({"allow": [{
        "path": "src/fixture.py",
        "rules": ["openai-style-key"],
        "fingerprints": [fingerprint(FIXTURE_KEY)],
        "reason": "test fixture",
    }]}))

    code, out = run(SECRET_SCAN, str(export), "--allowlist", str(allowlist))
    assert code == 0, out
    assert "1 reviewed exemption" in out


def test_secret_scan_exemption_covers_only_the_reviewed_value(
    export: Path, tmp_path: Path
) -> None:
    """A second key in the same file under the same rule is a different secret.

    Exempting by path and rule cleared whatever else appeared there later, and the
    scan recorded one match per line, so a second key on the reviewed line was never
    even seen.
    """
    other = "sk-" + "b" * 48
    (export / "src" / "fixture.py").write_text(
        f'REVIEWED = "{FIXTURE_KEY}"\nLATER = "{other}"\n'
    )
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(json.dumps({"allow": [{
        "path": "src/fixture.py",
        "rules": ["openai-style-key"],
        "fingerprints": [fingerprint(FIXTURE_KEY)],
        "reason": "test fixture",
    }]}))

    code, out = run(SECRET_SCAN, str(export), "--allowlist", str(allowlist))
    assert code == 1
    assert fingerprint(other) in out
    assert fingerprint(FIXTURE_KEY) not in out


def test_secret_scan_reads_a_directory_named_env(export: Path, tmp_path: Path) -> None:
    """ar-runtime/src/commands/env is source, and skipping it hid a key once."""
    (export / "src" / "env").mkdir()
    (export / "src" / "env" / "index.ts").write_text(f'const k = "{FIXTURE_KEY}"\n')
    empty = tmp_path / "allow.json"
    empty.write_text(json.dumps({"allow": []}))

    code, out = run(SECRET_SCAN, str(export), "--allowlist", str(empty))
    assert code == 1
    assert "env/index.ts" in out


def test_secret_scan_does_not_excuse_a_key_beside_the_word_example(
    export: Path, tmp_path: Path
) -> None:
    """The placeholder test read the whole line, so any nearby word cleared it."""
    (export / "src" / "leak.py").write_text(f'KEY = "{FIXTURE_KEY}"  # see example\n')
    empty = tmp_path / "allow.json"
    empty.write_text(json.dumps({"allow": []}))

    code, _ = run(SECRET_SCAN, str(export), "--allowlist", str(empty))
    assert code == 1


@pytest.mark.parametrize("value", [
    "sk-example" + "a" * 42,
    "sk-placeholder" + "a" * 38,
    "sk-your" + "a" * 45,
])
def test_secret_scan_does_not_excuse_a_key_carrying_a_marker(
    export: Path, tmp_path: Path, value: str
) -> None:
    """A marker can sit inside a real credential as easily as inside a stand-in.

    Deciding "placeholder" from a substring of a high-entropy value is a fail-open,
    and dropping the heuristic produces no new findings on this repository, so
    nothing was relying on it. Templates that trip a rule go in the allowlist by
    fingerprint like everything else.
    """
    (export / "src" / "conf.py").write_text(f'KEY = "{value}"\n')
    empty = tmp_path / "allow.json"
    empty.write_text(json.dumps({"allow": []}))

    assert run(SECRET_SCAN, str(export), "--allowlist", str(empty))[0] == 1


def test_secret_scan_refuses_a_missing_root(tmp_path: Path) -> None:
    """Scanning nothing and printing a pass reads exactly like a clean tree."""
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"allow": []}))

    code, out = run(SECRET_SCAN, str(tmp_path / "absent"), "--allowlist", str(allow))
    assert code == 2
    assert "Nothing to scan" in out


def test_stale_fingerprint_is_reported_even_beside_a_live_one(
    export: Path, tmp_path: Path
) -> None:
    """Usage was recorded per entry, so one live fingerprint kept its neighbours alive.

    The dead one then went on exempting whatever took that value's place.
    """
    gone = "sk-" + "c" * 48
    (export / "src" / "fixture.py").write_text(f'A = "{FIXTURE_KEY}"\n')
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(json.dumps({"allow": [{
        "path": "src/fixture.py",
        "rules": ["openai-style-key"],
        "fingerprints": [fingerprint(FIXTURE_KEY), fingerprint(gone)],
        "reason": "one of these has left the file",
    }]}))

    code, out = run(SECRET_SCAN, str(export), "--allowlist", str(allowlist))
    assert code == 1
    assert fingerprint(gone) in out
    assert "matched nothing" in out


def test_every_listed_fingerprint_still_present_passes(export: Path, tmp_path: Path) -> None:
    second = "sk-" + "c" * 48
    (export / "src" / "fixture.py").write_text(f'A = "{FIXTURE_KEY}"\nB = "{second}"\n')
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(json.dumps({"allow": [{
        "path": "src/fixture.py",
        "rules": ["openai-style-key"],
        "fingerprints": [fingerprint(FIXTURE_KEY), fingerprint(second)],
        "reason": "both reviewed",
    }]}))

    assert run(SECRET_SCAN, str(export), "--allowlist", str(allowlist))[0] == 0


def test_secret_scan_exemption_is_per_rule(export: Path, tmp_path: Path) -> None:
    """A file exempted for one pattern must still fail on a different one."""
    (export / "src" / "fixture.py").write_text(f'KEY = "{FIXTURE_KEY}"\n')
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(json.dumps({"allow": [{
        "path": "src/fixture.py",
        "rules": ["anthropic-token"],
        "fingerprints": [fingerprint(FIXTURE_KEY)],
        "reason": "different rule",
    }]}))

    code, _ = run(SECRET_SCAN, str(export), "--allowlist", str(allowlist))
    assert code == 1


def test_secret_scan_reports_a_stale_exemption(export: Path, tmp_path: Path) -> None:
    """An entry that stopped matching would otherwise cover whatever lands there next."""
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(json.dumps({"allow": [
        {"path": "src/gone.py", "rules": ["openai-style-key"],
         "fingerprints": [fingerprint(FIXTURE_KEY)], "reason": "no longer present"}
    ]}))

    code, out = run(SECRET_SCAN, str(export), "--allowlist", str(allowlist))
    assert code == 1
    assert "matched nothing" in out


def test_release_tree_passes_on_an_export(export: Path) -> None:
    code, out = run(RELEASE_TREE, str(export))
    assert code == 0
    assert "passed" in out


def test_release_tree_still_flags_a_blocked_path(export: Path) -> None:
    (export / ".env").write_text("SECRET=1\n")
    code, out = run(RELEASE_TREE, str(export))
    assert code == 1
    assert ".env" in out


def test_release_tree_refuses_a_missing_root(tmp_path: Path) -> None:
    """A root that is not there used to print that the check passed."""
    code, out = run(RELEASE_TREE, str(tmp_path / "absent"))
    assert code == 2
    assert "Not a directory" in out


# Every one of these is a rule that fnmatch anchors at the root, so the same file
# one directory down used to pass. A hardcoded basename set covered four of them
# and left the rest.
NESTED_BLOCKED = [
    ".env", ".env.production", ".env.local", "config.json", "config.local.json",
    "settings.local.json", "twitter_cookies.json", ".npmrc", ".netrc",
    "credentials.json", "private.key", "server.pem",
]


@pytest.mark.parametrize("name", NESTED_BLOCKED)
def test_release_tree_flags_a_blocked_name_at_depth(export: Path, name: str) -> None:
    (export / "service").mkdir(exist_ok=True)
    (export / "service" / name).write_text("SECRET=1\n")
    code, out = run(RELEASE_TREE, str(export))
    assert code == 1, f"{name} passed at depth"
    assert f"service/{name}" in out


def test_release_tree_judges_a_dangling_symlink(export: Path) -> None:
    """is_file() follows the link, so a broken one was skipped without a word."""
    (export / "service").mkdir(exist_ok=True)
    (export / "service" / ".env").symlink_to("/nonexistent/target")
    code, out = run(RELEASE_TREE, str(export))
    assert code == 1
    assert "service/.env" in out


def test_release_tree_judges_a_symlink_pointing_outside(export: Path, tmp_path: Path) -> None:
    """A link named .env ships that name into the release whatever it resolves to."""
    outside = tmp_path / "elsewhere"
    outside.write_text("AWS_SECRET_ACCESS_KEY=x\n")
    (export / "service").mkdir(exist_ok=True)
    (export / "service" / ".env").symlink_to(outside)
    assert run(RELEASE_TREE, str(export))[0] == 1


def test_release_tree_allow_does_not_cover_the_directory(export: Path) -> None:
    """An allow entry clears the file it names, never its neighbours.

    Allowing a directory cleared everything dropped into it afterwards, which is the
    opposite of what a gate is for: keys, unreviewed files and oversized blobs alike.
    """
    skills = export / "ar-runtime" / ".claude" / "skills" / "ar-coordinator"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# reviewed\n")
    assert run(RELEASE_TREE, str(export))[0] == 0, "the reviewed file itself passes"

    (skills / "private.key").write_text("k\n")
    assert run(RELEASE_TREE, str(export))[0] == 1
    (skills / "private.key").unlink()

    (skills / "NOTES.md").write_text("unreviewed\n")
    assert run(RELEASE_TREE, str(export))[0] == 1


def test_release_tree_checks_size_of_a_reviewed_file(export: Path) -> None:
    """Having been read for content says nothing about being small enough to ship."""
    skills = export / "ar-runtime" / ".claude" / "skills" / "ar-coordinator"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("x" * 4096)

    code, out = run(RELEASE_TREE, str(export), "--max-bytes", "1024")
    assert code == 1
    assert "large-file" in out


def test_release_tree_agrees_with_itself_on_a_checkout(tmp_path: Path) -> None:
    """A checkout and an exported tree must give the same answer.

    They did not while the check judged directories and read .git, which is why it
    used to refuse a checkout outright.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, env=ENV)

    code, _ = run(RELEASE_TREE, str(repo), "--allow-git-worktree")
    assert code == 0


def test_release_tree_skips_ignored_files(tmp_path: Path) -> None:
    """node_modules alone produced most of the noise on this repository."""
    repo = tmp_path / "repo"
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n")
    (repo / ".gitignore").write_text("node_modules/\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, env=ENV)

    code, out = run(RELEASE_TREE, str(repo), "--allow-git-worktree")
    assert code == 0, out


def test_secret_scan_refuses_a_missing_allowlist(export: Path, tmp_path: Path) -> None:
    """Silence here caused a real failure.

    .gitignore's **/*secret*.json rule matched this allowlist by name, so it was
    never committed. Returning an empty list made CI run with no exemptions and fail
    on every fixture, which reads as the tool being broken rather than an input being
    absent.
    """
    code, out = run(SECRET_SCAN, str(export), "--allowlist", str(tmp_path / "gone.json"))
    assert code != 0
    assert "Allowlist not found" in out
    assert "git check-ignore" in out, "the message must point at the likely cause"


def test_repository_allowlist_is_tracked() -> None:
    """The file the scan depends on must survive a clone.

    Same failure mode as #1: a file present locally, matched by an ignore rule, and
    therefore absent for everyone else.
    """
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "scripts/secret_scan_allowlist.json"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, env=ENV,
    )
    assert result.returncode == 0, "secret_scan_allowlist.json is not tracked by git"


def test_release_tree_allows_the_scan_allowlist() -> None:
    """Three rules matched this file by name before it could do its job.

    .gitignore's **/*secret*.json kept it out of git, and this check's own
    *secret*.json pattern then flagged it as a blocked path. Neither was about its
    contents, which are file paths and rule names.
    """
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(RELEASE_TREE), str(repo), "--allow-git-worktree"],
        capture_output=True, text=True, env=ENV,
    )
    assert "secret_scan_allowlist.json" not in result.stdout + result.stderr


def _is_ignored(rel: str) -> bool:
    repo = Path(__file__).resolve().parents[1]
    return subprocess.run(
        ["git", "check-ignore", "-q", rel],
        cwd=str(repo), capture_output=True, env=ENV,
    ).returncode == 0


@pytest.mark.parametrize("rel", [
    "data/projects/my_idea_slug/.venv/lib/x.so",
    "data/projects/my_idea_slug/state.md",
    "data/projects/my_idea_slug/decisions.log",
    "data/projects/my_idea_slug/workflow_queue.lock",
    "data/projects/my-project/code/__pycache__/x.pyc",
])
def test_run_leftovers_are_ignored(rel: str) -> None:
    """What a run leaves behind: environments, logs, locks, and its own state file."""
    assert _is_ignored(rel), f"{rel} should not be tracked"


@pytest.mark.parametrize("rel", [
    "data/projects/my_idea_slug/code/run_experiment.py",
    "data/projects/my_idea_slug/results/pilot.json",
    "data/projects/my_idea_slug/plan.md",
    "data/projects/my_idea_slug/critic.md",
])
def test_what_a_run_produced_stays_visible(rel: str) -> None:
    """An earlier rule ignored data/projects/* wholesale.

    That also hid the experiment code, the results and the reports -- the things a
    research repository exists to keep. One run directory was 69MB, of which 52K
    was code and the rest a virtualenv.
    """
    assert not _is_ignored(rel), f"{rel} would disappear from git status"


def test_public_release_starts_without_tracked_data() -> None:
    """Runtime output paths stay usable, but no maintainer data ships."""
    repo = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "data"], cwd=str(repo),
        capture_output=True, text=True, env=ENV, check=True,
    ).stdout.split()
    assert tracked == []


def test_release_tree_flags_a_link_pointing_outside(export: Path, tmp_path: Path) -> None:
    """The link's own name said nothing about what it pointed at.

    One named public-reference.txt aiming at $HOME/.aws/credentials passed
    every check and shipped that path into the release for anyone to read.
    """
    outside = tmp_path / "credentials"
    outside.write_text("aws_secret_access_key = x\n")
    (export / "public-reference.txt").symlink_to(outside)

    code, out = run(RELEASE_TREE, str(export))
    assert code == 1
    assert "link-escapes-tree" in out


def test_release_tree_allows_a_relative_link_inside_the_tree(export: Path) -> None:
    (export / "src" / "alias.py").symlink_to("app.py")
    assert run(RELEASE_TREE, str(export))[0] == 0


def test_secret_scan_does_not_follow_a_link_out_of_the_tree(
    export: Path, tmp_path: Path
) -> None:
    """read_text() resolved the link, so results depended on the scanning machine.

    Content was read here and nothing was read on CI, where the target does not
    exist. What ships is the link, so the link is what gets scanned.
    """
    outside = tmp_path / "leak.py"
    outside.write_text(f'KEY = "{FIXTURE_KEY}"\n')
    (export / "src" / "ref.py").symlink_to(outside)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"allow": []}))

    assert run(SECRET_SCAN, str(export), "--allowlist", str(allow))[0] == 0


def test_secret_scan_reads_the_link_target_itself(export: Path, tmp_path: Path) -> None:
    """A dangling link was dropped by is_file() before it could be looked at."""
    (export / "src" / "weird.txt").symlink_to(FIXTURE_KEY)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"allow": []}))

    assert run(SECRET_SCAN, str(export), "--allowlist", str(allow))[0] == 1


def test_a_skipped_suffix_does_not_hide_a_symlink(export: Path, tmp_path: Path) -> None:
    """The suffix rule exists to avoid reading images and archives.

    A symlink is a short string, never a blob, so applying that rule to one put
    its target beyond the scan: src/ref.zip -> sk-<48 chars> passed both gates.
    """
    (export / "src" / "ref.zip").symlink_to(FIXTURE_KEY)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"allow": []}))

    assert run(SECRET_SCAN, str(export), "--allowlist", str(allow))[0] == 1


def test_release_tree_flags_a_dangling_link_inside_the_tree(export: Path) -> None:
    """A link resolving to nothing is a missing file or a string in disguise."""
    (export / "src" / "ref.zip").symlink_to(FIXTURE_KEY)
    code, out = run(RELEASE_TREE, str(export))
    assert code == 1
    assert "link-target-missing" in out


def test_phase_0_calls_the_shared_mcp_preflight() -> None:
    """The skill and CI must invoke the same entry point, from the same directory.

    Twice in a row the servers reported their own state correctly and the skill
    called them wrongly: once inferring credentials from variable lengths, once
    running `cd ar-runtime` from inside ar-runtime/ and appending
    `; echo "rc=$?"`, which replaces the exit status the caller sees with zero.
    CI proving the self-tests behave proves nothing about the wiring.
    """
    repo = Path(__file__).resolve().parents[1]
    skill = (repo / "ar-runtime" / ".claude" / "skills" / "ar-coordinator"
             / "SKILL.md").read_text(encoding="utf-8")

    assert "Bash ./scripts/ar-preflight-mcp.sh" in skill
    assert "cd ar-runtime && bun scripts/ar-" not in skill, (
        "Phase 0's cwd is already ar-runtime/"
    )
    # A trailing echo would hand the caller its own exit code instead of the check's.
    assert 'ar-preflight-mcp.sh; echo' not in skill

    script = repo / "ar-runtime" / "scripts" / "ar-preflight-mcp.sh"
    assert script.exists() and os.access(script, os.X_OK), "not executable"

    workflow = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "./scripts/ar-preflight-mcp.sh" in workflow, (
        "CI must exercise the entry point the skill uses, not the servers directly"
    )
