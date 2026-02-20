#!/usr/bin/env python3
"""
GitHub Integration — Security Handshake branch + PR management.

Uses the GitHub REST API directly (no gh CLI required).
Token: ~/.config/cursor-github-mcp-token

Enforces the Human-in-the-Loop (HitL) safety constraint:
  - Security Agent can CREATE and UPDATE PRs on security-fixes branch
  - Security Agent CANNOT merge into main
  - PRs are created with "needs-human-review" label and explicit HitL notice
  - merge_pull_request() is intentionally NOT implemented here

Workflow:
  1. ensure_security_branch()  → create security-fixes from main if absent
  2. commit_fix_proposal()     → push fix proposal file to security-fixes
  3. ensure_pull_request()     → open PR from security-fixes → main (or update)
  4. Human reviews in Cursor   → approves/rejects the PR
"""

import base64
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

GITHUB_API = "https://api.github.com"
REPO_OWNER = "ariesfiredragonfu"
REPO_NAME  = "howell-forge-agent"
SECURITY_BRANCH = "security-fixes"
MAIN_BRANCH = "main"
TOKEN_PATH = Path.home() / ".config" / "cursor-github-mcp-token"


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _token() -> str:
    if not TOKEN_PATH.exists():
        raise GitHubError(0, "GitHub token not found", TOKEN_PATH)
    return TOKEN_PATH.read_text().strip()


def _headers(extra: Optional[dict] = None) -> dict:
    h = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Howell-Forge-SecurityAgent/1.0",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _api(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    timeout: int = 20,
) -> dict:
    url = f"{GITHUB_API}{path}"
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise GitHubError(e.code, f"GitHub API HTTP {e.code} on {path}: {body}", path) from e
    except urllib.error.URLError as e:
        raise GitHubError(0, f"GitHub connection error: {e.reason}", path) from e


# ─── Branch management ────────────────────────────────────────────────────────

def get_branch_sha(branch: str = MAIN_BRANCH) -> str:
    """Return the HEAD commit SHA of a branch."""
    data = _api("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/refs/heads/{branch}")
    if isinstance(data, list):
        for ref in data:
            if ref.get("ref") == f"refs/heads/{branch}":
                return ref["object"]["sha"]
        raise GitHubError(404, f"Branch {branch!r} not found", branch)
    return data["object"]["sha"]


def branch_exists(branch: str) -> bool:
    """Return True if the branch exists on origin."""
    try:
        get_branch_sha(branch)
        return True
    except GitHubError as e:
        if e.status_code == 404:
            return False
        raise


def ensure_security_branch() -> str:
    """
    Create the security-fixes branch from main's HEAD if it doesn't exist.
    Returns the branch SHA.
    """
    if branch_exists(SECURITY_BRANCH):
        sha = get_branch_sha(SECURITY_BRANCH)
        print(f"[GitHub] Branch '{SECURITY_BRANCH}' exists (SHA: {sha[:8]})")
        return sha

    main_sha = get_branch_sha(MAIN_BRANCH)
    _api("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/refs", {
        "ref": f"refs/heads/{SECURITY_BRANCH}",
        "sha": main_sha,
    })
    print(f"[GitHub] Created branch '{SECURITY_BRANCH}' from main ({main_sha[:8]})")
    return main_sha


# ─── File commit ──────────────────────────────────────────────────────────────

def commit_fix_proposal(
    filename: str,
    content: str,
    commit_message: str,
    branch: str = SECURITY_BRANCH,
) -> dict:
    """
    Create or update a file on the security-fixes branch.
    Returns the commit data from GitHub.
    """
    path = f"security-fixes/{filename}"
    encoded = base64.b64encode(content.encode()).decode()

    # Check for existing file (needed for update SHA)
    existing_sha: Optional[str] = None
    try:
        existing = _api("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}?ref={branch}")
        existing_sha = existing.get("sha")
    except GitHubError as e:
        if e.status_code != 404:
            raise

    payload: dict = {
        "message": commit_message,
        "content": encoded,
        "branch": branch,
    }
    if existing_sha:
        payload["sha"] = existing_sha

    result = _api(
        "PUT",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}",
        payload,
    )
    commit_sha = result.get("commit", {}).get("sha", "?")
    print(f"[GitHub] Committed '{path}' → {SECURITY_BRANCH} (commit: {commit_sha[:8]})")
    return result


# ─── Pull Request ─────────────────────────────────────────────────────────────

_HITL_NOTICE = """
---

## ⚠️ Human-in-the-Loop (HitL) Requirement

**This PR was created automatically by the Security Handshake Agent.**

> No code may be merged into `main` without explicit human review and approval.
> The Security Agent proposes; the human disposes.

**To review:**
1. Open this PR in Cursor (GitHub panel) or at the link above
2. Read the Fix Proposal carefully
3. If you approve: merge via GitHub UI / Cursor
4. If you reject: close the PR and add a comment with the reason

The Security Agent will NOT auto-merge regardless of CI status.
"""


def ensure_pull_request(
    title: str,
    body: str,
    head: str = SECURITY_BRANCH,
    base: str = MAIN_BRANCH,
) -> dict:
    """
    Open a PR from security-fixes → main, or update the existing open PR.
    The HitL notice is always appended to the PR body.
    Returns the PR data dict.
    """
    full_body = body.rstrip() + _HITL_NOTICE

    # Check for an existing open PR on this branch
    open_prs = _api(
        "GET",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls?state=open&head={REPO_OWNER}:{head}&base={base}",
    )
    if isinstance(open_prs, list) and open_prs:
        pr = open_prs[0]
        pr_number = pr["number"]
        updated = _api(
            "PATCH",
            f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{pr_number}",
            {"title": title, "body": full_body},
        )
        print(f"[GitHub] Updated existing PR #{pr_number}: {updated.get('html_url', '?')}")
        _ensure_labels(pr_number)
        return updated

    # Create new PR
    new_pr = _api(
        "POST",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls",
        {
            "title": title,
            "body": full_body,
            "head": head,
            "base": base,
            "draft": False,
        },
    )
    pr_number = new_pr.get("number")
    print(f"[GitHub] Created PR #{pr_number}: {new_pr.get('html_url', '?')}")
    if pr_number:
        _ensure_labels(pr_number)
    return new_pr


def _ensure_labels(pr_number: int) -> None:
    """Add security + HitL labels to the PR. Creates labels if absent."""
    labels = ["security-handshake", "needs-human-review", "do-not-auto-merge"]
    for label in labels:
        _ensure_label_exists(label)
    try:
        _api(
            "POST",
            f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{pr_number}/labels",
            {"labels": labels},
        )
    except GitHubError:
        pass  # Labels may already be set


def _ensure_label_exists(name: str) -> None:
    """Create a repo label if it doesn't exist."""
    colors = {
        "security-handshake":  "d93f0b",
        "needs-human-review":  "e4e669",
        "do-not-auto-merge":   "cc0000",
    }
    try:
        _api("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels/{name}")
    except GitHubError as e:
        if e.status_code == 404:
            try:
                _api("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels", {
                    "name": name,
                    "color": colors.get(name, "bfd4f2"),
                    "description": f"Auto-applied by Security Handshake Agent",
                })
            except GitHubError:
                pass


# ─── Add comment ──────────────────────────────────────────────────────────────

def add_pr_comment(pr_number: int, body: str) -> dict:
    """Post a comment on an existing PR."""
    return _api(
        "POST",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{pr_number}/comments",
        {"body": body},
    )


# ─── Occurrence counter (dedup update) ────────────────────────────────────────

_OCCURRENCE_MARKER = "<!-- occurrence-counter -->"


def update_pr_occurrence(
    pr_number: int,
    occurrence_count: int,
    first_seen: str,
) -> None:
    """
    Update the PR body's occurrence counter banner in-place.

    Called by fortress_watcher when a duplicate event (same sha256 dedup key
    within the 5-minute window) hits an already-open PR.  Instead of opening
    a new PR we increment a visible counter so the reviewer knows how many
    times the event fired.

    The banner looks like:
        <!-- occurrence-counter -->
        > 🔄 **Event has occurred 3×** (first seen: 2026-02-20T17:00:00Z)
    """
    try:
        pr = _api("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{pr_number}")
    except GitHubError:
        return

    current_body = pr.get("body") or ""
    new_banner = (
        f"{_OCCURRENCE_MARKER}\n"
        f"> 🔄 **Event has occurred {occurrence_count}×** "
        f"(first seen: {first_seen})\n"
    )

    if _OCCURRENCE_MARKER in current_body:
        # Replace existing banner (everything up to the next blank line / section)
        lines = current_body.split("\n")
        new_lines: list[str] = []
        skip = False
        for line in lines:
            if _OCCURRENCE_MARKER in line:
                new_lines.append(new_banner.rstrip())
                skip = True
                continue
            if skip:
                # Skip the old "🔄" line then resume normal content
                skip = False
                continue
            new_lines.append(line)
        new_body = "\n".join(new_lines)
    else:
        new_body = new_banner + "\n" + current_body

    try:
        _api(
            "PATCH",
            f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{pr_number}",
            {"body": new_body},
        )
        print(f"[GitHub] Updated PR #{pr_number} occurrence counter → {occurrence_count}×")
    except GitHubError:
        pass


# ─── Error ────────────────────────────────────────────────────────────────────

class GitHubError(Exception):
    def __init__(self, status_code: int, message: str, path=None):
        super().__init__(message)
        self.status_code = status_code
        self.path = path

    def __repr__(self) -> str:
        return f"GitHubError(status={self.status_code}, path={self.path})"
