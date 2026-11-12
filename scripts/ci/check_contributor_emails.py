#!/usr/bin/env python3
"""Fail when a commit since the merge base with the PR base has an unmapped author email.

Run by the ``Check for unmapped contributor emails`` step of
``.github/workflows/repo-guards.yml``. An email is mapped if it has a file in
``contributors/emails/`` (one file per email, conflict-free) or an entry in the frozen legacy
AUTHOR_MAP in ``scripts/releases/authors_legacy.py``. Maintainer, bot and GitHub id+login
noreply emails are skipped. Writes the ``contributor-check`` review status
(scripts/ci/review_status.py).

Exit codes: 0 = every email mapped (or no new commits), 1 = at least one unmapped email.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.review_status import action_required, publish

ARTIFACT = "contributor-check"
GIT_TIMEOUT = 120
LEGACY_MAP = Path("scripts/releases/authors_legacy.py")
SKIPPED = ("*teknium*", "*noreply@github.com*", "*dependabot*", "*github-actions*",
           "*anthropic.com*", "*cursor.com*")
# GitHub id+login noreply emails auto-resolve.
GITHUB_NOREPLY = re.compile(r"\+.*@users\.noreply\.github\.com")

HOW_TO_FIX = """\
Run from the PR branch:
```
python3 scripts/audit_pr_attribution.py --fix
git add contributors && git commit -m "chore: map contributor emails" && git push
```
Or map one email manually (do NOT edit AUTHOR_MAP in scripts/releases/authors_legacy.py):
```
python3 scripts/add_contributor.py <email> <github-username>
```
To find the GitHub username for an email:
```
gh api 'search/users?q=EMAIL+in:email' --jq '.items[0].login'
```
"""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("git", *args), stdout=subprocess.PIPE, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT,
                          check=False)


def new_author_emails() -> list[str] | int:
    """Return authors on the PR side of the checked-out base, or a Git error code."""
    base = os.environ.get("_HERMES_PR_BASE_SHA") or "origin/main"
    head = os.environ.get("_HERMES_PR_HEAD_SHA")
    if head:
        resolved = _git("rev-parse", "--verify", f"{head}^{{commit}}")
        if resolved.returncode:
            return resolved.returncode
        head = resolved.stdout.strip()
        parents = _git("rev-list", "--parents", "-n", "1", "HEAD")
        if parents.returncode:
            return parents.returncode
        commits = parents.stdout.split()
        if not commits:
            return 1
        if commits[0] != head:
            if len(commits) != 3 or commits[2] != head:
                print("::error::PR checkout does not match the event head.", file=sys.stderr)
                return 1
            ancestry = _git("merge-base", "--is-ancestor", base, commits[1])
            if ancestry.returncode:
                return ancestry.returncode
            base = commits[1]
    else:
        head = "HEAD"
    merge_base = _git("merge-base", base, head)
    if merge_base.returncode:
        return merge_base.returncode
    log = _git("log", f"{base}..{head}", "--format=%ae", "--no-merges")
    if log.returncode:
        return log.returncode
    emails = sorted(set(log.stdout.splitlines()))
    return emails if "".join(emails) else []


def is_mapped(email: str, legacy_map: str) -> bool:
    if any(fnmatch.fnmatchcase(email, pattern) for pattern in SKIPPED):
        return True
    if GITHUB_NOREPLY.search(email):
        return True
    if Path("contributors/emails", email).is_file():
        return True
    return f'"{email}"' in legacy_map


def author_name(email: str) -> str:
    return _git("log", f"--author={email}", "--format=%an", "-1").stdout.strip()


def report(missing: list[tuple[str, str]]) -> None:
    print()
    print("⚠️  New contributor email(s) without a mapping:")
    print()
    for email, author in missing:
        print(f"  {email} ({author})")
    print()
    print("Add a mapping file (do NOT edit AUTHOR_MAP in scripts/releases/authors_legacy.py):")
    print("  python3 scripts/audit_pr_attribution.py --fix   # auto-resolve + create files")
    print("or manually:")
    for email, _ in missing:
        print(f"    python3 scripts/add_contributor.py {email} <github-username>")
    print()
    print("To find the GitHub username for an email:")
    print("  gh api 'search/users?q=EMAIL+in:email' --jq '.items[0].login'")


def main() -> int:
    emails = new_author_emails()
    if isinstance(emails, int):
        return emails
    if not emails:
        print("No new commits to check.")
        publish([], ARTIFACT)
        return 0
    try:
        legacy_map = LEGACY_MAP.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        legacy_map = ""
    missing = [(email, author_name(email)) for email in emails if not is_mapped(email, legacy_map)]
    if not missing:
        print("✅ All contributor emails are mapped.")
        publish([], ARTIFACT)
        return 0
    report(missing)
    detail = "\n".join(f"{email} ({author})" for email, author in missing)
    publish(action_required("contributor attribution", "Unmapped contributor email(s)",
                            "New contributor email(s) are not in AUTHOR_MAP.", HOW_TO_FIX, detail),
            ARTIFACT, ascii_only=False)
    return 1


if __name__ == "__main__":
    sys.exit(main())
