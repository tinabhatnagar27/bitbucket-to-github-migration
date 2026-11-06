#!/usr/bin/env python3

import json
import os
import re
import sqlite3
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import requests

try:
    import boto3
except ImportError:
    boto3 = None


BB = "https://api.bitbucket.org/2.0"
GH = "https://api.github.com"

DRY_RUN = os.getenv(
    "DRY_RUN", "true"
).lower() in ("true", "1", "yes")

OWNER_OVERRIDE = os.getenv("GITHUB_OWNER", "")
REPORT_FILE = Path("migration-report.json")
# Optional targeted retry: no Git mirror, project, issue or comment changes.
REVIEWERS_ONLY = os.getenv("REVIEWERS_ONLY", "false").lower() in ("true", "1", "yes")


def log(*message):
    print("[INFO]", *message, flush=True)


def api(session, method, url, expected=(200,), **kwargs):
    for attempt in range(5):
        try:
            response = session.request(
                method, url, timeout=60, **kwargs
            )
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
            continue

        if response.status_code in expected:
            return response.json() if response.content else {}

        if response.status_code in (429, 500, 502, 503, 504):
            if attempt < 4:
                time.sleep(min(60, 2 ** (attempt + 1)))
                continue

        raise RuntimeError(
            f"{method} {url}: HTTP {response.status_code} "
            f"{response.text[:250]}"
        )

    raise RuntimeError("HTTP retries exhausted")


def bb_pages(session, url, params=None):
    rows = []
    while url:
        data = api(session, "GET", url, params=params)
        rows.extend(data.get("values", []))
        url = data.get("next")
        params = None
    return rows


def gh_pages(session, url):
    results = []
    page = 1

    while True:
        items = api(
            session, "GET", url,
            params={"per_page": 100, "page": page}
        )
        results.extend(items)
        if len(items) < 100:
            break
        page += 1

    return results


def credentials():
    bb_token = os.getenv("BITBUCKET_TOKEN")
    gh_token = os.getenv("GITHUB_TOKEN")

    if bb_token and gh_token:
        log("Using environment credentials")
        return bb_token, gh_token

    if boto3 is None:
        raise RuntimeError("boto3 not installed")

    client = boto3.client(
        "secretsmanager",
        region_name=os.getenv("AWS_REGION", "us-east-1")
    )

    secret = client.get_secret_value(
        SecretId=os.getenv(
            "MIGRATION_SECRET_NAME",
            "vcs-migration/tokens"
        )
    )

    data = json.loads(secret["SecretString"])

    if not data.get("BITBUCKET_TOKEN") or not data.get("GITHUB_TOKEN"):
        raise RuntimeError("Tokens missing in Secrets Manager")

    log("Using AWS Secrets Manager credentials")
    return data["BITBUCKET_TOKEN"], data["GITHUB_TOKEN"]



def run_git(args, cwd=None, env=None, capture=False):
    git_env = {
        **os.environ,
        **(env or {})
    }

    git_env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    git_env["GIT_CONFIG_NOSYSTEM"] = "1"
    git_env["GIT_CONFIG_COUNT"] = "1"
    git_env["GIT_CONFIG_KEY_0"] = "credential.helper"
    git_env["GIT_CONFIG_VALUE_0"] = ""

    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        env=git_env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None
    )

    return result.stdout.strip() if capture else ""



def git_auth(directory, user, token):
    helper = Path(directory) / "askpass.sh"

    if not helper.exists():
        helper.write_text(
            '#!/bin/sh\n'
            'case "$1" in\n'
            '*Username*) printf "%s\\n" "$GIT_USER";;\n'
            '*Password*) printf "%s\\n" "$GIT_PASS";;\n'
            'esac\n'
        )
        helper.chmod(0o700)

    return {
        "GIT_ASKPASS": str(helper),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_USER": user,
        "GIT_PASS": token
    }


def git_refs(url, env):
    text = run_git(
        ["ls-remote", url, "refs/heads/*", "refs/tags/*"],
        env=env,
        capture=True
    )

    return {
        ref: sha
        for sha, ref in (
            line.split("\t", 1)
            for line in text.splitlines()
        )
        if not ref.endswith("^{}")
    }


def migrate_git(workspace, source, owner, target,
                bb_token, gh_token):
    source_url = (
        f"https://bitbucket.org/{workspace}/{source}.git"
    )
    destination_url = (
        f"https://github.com/{owner}/{target}.git"
    )

    result = {
        "git": "FAILED",
        "lfs": "NOT_CHECKED",
        "refs_match": False
    }

    with tempfile.TemporaryDirectory(
        prefix="migration-poc-"
    ) as temp:
        bb_env = git_auth(
            temp, "x-bitbucket-api-token-auth", bb_token
        )
        gh_env = git_auth(
            temp, "x-access-token", gh_token
        )

        local = str(Path(temp) / "mirror.git")

        log("Cloning", source_url)
        run_git(
            ["clone", "--mirror", source_url, local],
            env=bb_env
        )

        before = git_refs(source_url, bb_env)

        lfs_files = ""

        if shutil.which("git-lfs"):
            try:
                run_git(
                    ["lfs", "fetch", "--all", "origin"],
                    cwd=local, env=bb_env
                )
                lfs_files = run_git(
                    ["lfs", "ls-files", "--all", "--long"],
                    cwd=local, capture=True
                )
                result["lfs"] = "FETCHED" if lfs_files else "NONE"
            except subprocess.CalledProcessError as exc:
                result["lfs"] = f"FETCH_FAILED: {exc}"
        else:
            result["lfs"] = "GIT_LFS_MISSING"

        run_git(
            ["remote", "add", "github", destination_url],
            cwd=local
        )

        log("Pushing", destination_url)
        run_git(
            ["push", "--mirror", "github"],
            cwd=local, env=gh_env
        )

        result["git"] = "SUCCESS"

        if lfs_files and result["lfs"] == "FETCHED":
            try:
                run_git(
                    ["lfs", "push", "--all", "github"],
                    cwd=local, env=gh_env
                )
                result["lfs"] = "SUCCESS"
            except subprocess.CalledProcessError as exc:
                result["lfs"] = f"PUSH_FAILED: {exc}"

        after = git_refs(destination_url, gh_env)

        result["refs_match"] = before == after
        result["source_refs"] = len(before)
        result["destination_refs"] = len(after)

    return result


def safe_topic(text):
    return re.sub(
        r"[^a-z0-9-]", "-",
        str(text).lower()
    ).strip("-")[:50]


def sync_topics(gh, owner, repo, workspace, project):
    url = f"{GH}/repos/{owner}/{repo}/topics"

    previous = api(gh, "GET", url).get("names", [])

    new = set(previous)
    new.add(safe_topic("bb-workspace-" + workspace))

    if project:
        new.add(safe_topic("bb-project-" + project))

    new.discard("")

    if set(previous) != new:
        api(
            gh, "PUT", url,
            json={"names": sorted(new)}
        )

    return sorted(new)


def content_text(record):
    content = record.get("content") or ""
    if isinstance(content, dict):
        return content.get("raw", "")
    return str(content)


def find_marker(gh, base, marker):
    for state in ("open", "closed"):
        url = f"{base}/issues?state={state}"

        for item in gh_pages(gh, url):
            if marker in (item.get("body") or ""):
                return item

    return None


def discover_issue_fixture(bb, workspace, repo):
    url = (
        f"{BB}/repositories/{workspace}/{repo}/refs/branches"
    )

    branches = bb_pages(
        bb, url, {"pagelen": 100}
    )

    for branch in branches:
        branch_name = branch.get("name", "")

        revision = (
            (branch.get("target") or {}).get("hash")
            or branch_name
        )

        file_url = (
            f"{BB}/repositories/{workspace}/{repo}/src/"
            f"{quote(revision, safe='')}/"
            "migration-test-data/issues.json"
        )

        response = bb.get(file_url, timeout=45)

        if response.status_code == 404:
            continue

        if response.status_code != 200:
            raise RuntimeError(
                f"Issue fixture HTTP {response.status_code}"
            )

        data = response.json()

        if (
            data.get("repository") == repo
            and data.get("source")
            == "bitbucket-historical-test-fixture"
        ):
            return data.get("issues", []), branch_name

    return [], None


def migrate_issues(bb, gh, workspace, repo, base):
    issues, branch = discover_issue_fixture(
        bb, workspace, repo
    )

    stats = {
        "fixture_branch": branch,
        "source": len(issues),
        "created": 0,
        "existing": 0,
        "comments": 0,
        "errors": []
    }

    for issue in issues:
        marker = (
            f"POC-SOURCE-ISSUE-ID: "
            f"{workspace}/{repo}/{issue['id']}"
        )

        try:
            if find_marker(gh, base, marker):
                stats["existing"] += 1
                continue

            body = (
                "Historical Bitbucket Issue TEST FIXTURE\n"
                "(Not a live Bitbucket Cloud Issue)\n\n"
                f"{marker}\n"
                f"Original status: {issue.get('status')}\n"
                f"Type: {issue.get('kind')}\n"
                f"Priority: {issue.get('priority')}\n\n"
                f"{content_text(issue)}"
            )

            created = api(
                gh, "POST", base + "/issues",
                expected=(201,),
                json={
                    "title": issue.get(
                        "title", "Migrated Issue"
                    ),
                    "body": body,
                    "labels": [
                        safe_topic(
                            issue.get("kind", "task")
                        ),
                        safe_topic(
                            "priority-" +
                            issue.get("priority", "medium")
                        )
                    ]
                }
            )

            stats["created"] += 1
            number = created["number"]

            for comment in issue.get("comments", []):
                api(
                    gh, "POST",
                    f"{base}/issues/{number}/comments",
                    expected=(201,),
                    json={
                        "body": (
                            "Migrated test comment:\n\n"
                            + content_text(comment)
                        )
                    }
                )
                stats["comments"] += 1

            if str(issue.get("status", "")).lower() in (
                "resolved", "closed", "invalid",
                "duplicate", "wontfix"
            ):
                api(
                    gh, "PATCH",
                    f"{base}/issues/{number}",
                    json={"state": "closed"}
                )

        except Exception as exc:
            stats["errors"].append(
                f"Issue {issue.get('id')}: {exc}"
            )

    return stats



# Additional PR migration support (integrated from enhanced PR script)
DB = Path("reviewer_mapping.sqlite3")

def pages_bb(session, url, params=None):
    return bb_pages(session, url, params)

def pages_gh(session, url, params=None):
    results = []
    page = 1
    while True:
        query = dict(params or {})
        query.update({"per_page": 100, "page": page})
        items = api(session, "GET", url, params=query)
        results.extend(items)
        if len(items) < 100:
            return results
        page += 1

def name(person):
    person = person or {}

    return (
        person.get("display_name")
        or person.get("nickname")
        or "Unknown"
    )


def comment_text(comment):
    value = comment.get("content") or {}

    if isinstance(value, dict):
        return value.get("raw", "")

    return str(value)


def source_marker(workspace, slug, number):
    return (
        f"POC-SOURCE-PR-ID: "
        f"{workspace}/{slug}/{number}"
    )


def open_mapping_db():
    conn = sqlite3.connect(DB)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviewer_mapping (
            bitbucket_identity TEXT PRIMARY KEY,
            github_login TEXT NOT NULL,
            confirmed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    return conn


def reviewer_identity(user):
    return str(
        user.get("account_id")
        or user.get("uuid")
        or ""
    )


def resolve_reviewer(gh, conn, person, stats, cache):
    identity = reviewer_identity(person)

    if not identity:
        stats["unmapped_reviewers"].append({
            "name": name(person),
            "reason": "no stable ID"
        })
        return None

    row = conn.execute(
        """
        SELECT github_login
        FROM reviewer_mapping
        WHERE bitbucket_identity=?
        """,
        (identity,)
    ).fetchone()

    login = row[0] if row else None

    if not login:
        print(
            f"\n[REVIEWER] Bitbucket: {name(person)}"
        )
        print(
            f"[REVIEWER] Account ID: "
            f"{person.get('account_id')}"
        )
        print(
            f"[REVIEWER] UUID: "
            f"{person.get('uuid')}"
        )
        print(
            "[REVIEWER] Enter VERIFIED GitHub login "
            "(Enter = skip)."
        )

        try:
            candidate = input(
                "GitHub username: "
            ).strip().lstrip("@")
        except EOFError:
            candidate = ""

        if candidate:
            login = candidate
            newly_entered = True
        else:
            stats["unmapped_reviewers"].append({
                "name": name(person),
                "account_id": person.get("account_id"),
                "uuid": person.get("uuid")
            })
            return None

    else:
        newly_entered = False

    if login.lower() not in cache:
        response = gh.get(
            f"{GH}/users/{quote(login, safe='')}",
            timeout=45
        )

        if response.status_code == 404:
            cache[login.lower()] = None

        elif response.status_code == 200:
            data = response.json()

            cache[login.lower()] = (
                data.get("login")
                if data.get("type") == "User"
                else None
            )

        else:
            raise RuntimeError(
                "GitHub user verification HTTP "
                f"{response.status_code}"
            )

    verified = cache[login.lower()]

    if not verified:
        stats["errors"].append(
            "GitHub account not found or "
            f"not a user: {login}"
        )
        return None

    if newly_entered:
        print(
            f"[REVIEWER] GitHub account: "
            f"https://github.com/{verified}"
        )

        print(
            "[REVIEWER] Confirm this is the SAME "
            "person as the Bitbucket reviewer."
        )

        try:
            confirmation = input(
                "Save mapping? [y/N]: "
            ).strip().lower()
        except EOFError:
            confirmation = ""

        if confirmation not in ("y", "yes"):
            stats["unmapped_reviewers"].append({
                "name": name(person),
                "reason": "not confirmed"
            })
            return None

        conn.execute(
            """
            INSERT OR REPLACE INTO reviewer_mapping
            (bitbucket_identity, github_login)
            VALUES (?, ?)
            """,
            (identity, verified)
        )

        conn.commit()

        log("Reviewer mapping saved in", DB)

    return verified


def prs_for(bb, workspace, slug):
    found = {}
    errors = []

    root = (
        f"{BB}/repositories/"
        f"{quote(workspace, safe='')}/"
        f"{quote(slug, safe='')}/pullrequests"
    )

    for state in (
        "OPEN",
        "MERGED",
        "DECLINED",
        "SUPERSEDED"
    ):
        try:
            rows = pages_bb(
                bb,
                root,
                {
                    "state": state,
                    "pagelen": 50
                }
            )

            for pr in rows:
                found[pr["id"]] = pr

        except Exception as exc:
            errors.append(f"{state}: {exc}")

    return list(found.values()), errors


def bb_pr_url(workspace, slug, pr_id, suffix=""):
    return (
        f"{BB}/repositories/"
        f"{quote(workspace, safe='')}/"
        f"{quote(slug, safe='')}/"
        f"pullrequests/{pr_id}{suffix}"
    )


def existing_record(gh, base, marker):
    issues = pages_gh(
        gh,
        base + "/issues",
        {"state": "all"}
    )

    for issue in issues:
        body = issue.get("body") or ""

        if marker in body.splitlines():
            return issue

    return None


def history(pr, activity):
    lines = [
        "### Original requested reviewers"
    ]

    for reviewer in pr.get("reviewers") or []:
        lines.append(
            f"- {name(reviewer)} "
            f"(Bitbucket UUID: "
            f"{reviewer.get('uuid', 'N/A')})"
        )

    lines.append("\n### Original participants")

    for item in pr.get("participants") or []:
        lines.append(
            f"- {name(item.get('user'))}: "
            f"approved={item.get('approved', False)}, "
            f"state={item.get('state', 'unknown')}"
        )

    lines.append("\n### Historical review activity")

    for item in activity:
        for kind in (
            "approval",
            "unapproval",
            "update"
        ):
            event = item.get(kind)

            if isinstance(event, dict):
                person = (
                    event.get("user")
                    or event.get("author")
                    or {}
                )

                event_date = (
                    event.get("date")
                    or item.get("created_on")
                )

                lines.append(
                    f"- {kind}: "
                    f"{name(person)} at {event_date}"
                )

    return "\n".join(lines)


def body_for(workspace, slug, pr, activity):
    head = (
        (pr.get("source") or {})
        .get("branch") or {}
    ).get("name")

    target = (
        (pr.get("destination") or {})
        .get("branch") or {}
    ).get("name")

    original_url = (
        f"https://bitbucket.org/"
        f"{workspace}/{slug}/pull-requests/"
        f"{pr['id']}"
    )

    return (
        "## Migrated Bitbucket Pull Request\n\n"
        f"{source_marker(workspace, slug, pr['id'])}\n\n"
        f"Original source: {original_url}\n\n"
        f"Original state: {pr.get('state')}\n\n"
        f"Original author: "
        f"{name(pr.get('author'))}\n\n"
        f"Original created date: "
        f"{pr.get('created_on')}\n\n"
        f"Original updated date: "
        f"{pr.get('updated_on')}\n\n"
        f"Head branch: {head}\n\n"
        f"Base branch: {target}\n\n"
        "## Original Description\n\n"
        f"{pr.get('description') or ''}\n\n"
        "## Historical Review Information\n\n"
        f"{history(pr, activity)}\n\n"
        "---\n"
        "Original author, reviewer, approval, "
        "and timestamps are metadata, "
        "not native GitHub review history."
    )


def update_history(
    gh, base, issue, pr, activity, stats
):
    old = issue.get("body") or ""

    heading = (
        "## Historical Review Information\n\n"
    )

    if heading not in old:
        stats["errors"].append(
            "Review history section missing "
            "in existing PR body"
        )
        return

    before, after = old.split(heading, 1)

    separator = "\n\n---\n"

    if separator not in after:
        stats["errors"].append(
            "Review history separator missing "
            "in existing PR body"
        )
        return

    _, suffix = after.split(separator, 1)

    updated = (
        before
        + heading
        + history(pr, activity)
        + separator
        + suffix
    )

    if updated != old:
        if DRY_RUN:
            stats["body_update_planned"] = True
        else:
            api(
                gh,
                "PATCH",
                f"{base}/issues/{issue['number']}",
                json={"body": updated}
            )

            stats["body_updated"] = True


def branch_exists(gh, base, branch):
    response = gh.get(
        base + "/branches/"
        + quote(branch, safe=""),
        timeout=45
    )

    if response.status_code == 404:
        return False

    if response.status_code != 200:
        raise RuntimeError(
            "Branch check HTTP "
            f"{response.status_code}"
        )

    return True


def create_pr(gh, base, pr, body):
    head = (
        (pr.get("source") or {})
        .get("branch") or {}
    ).get("name")

    target = (
        (pr.get("destination") or {})
        .get("branch") or {}
    ).get("name")

    if not head or not target:
        raise RuntimeError(
            "Missing source or destination branch"
        )

    if (
        not branch_exists(gh, base, head)
        or not branch_exists(gh, base, target)
    ):
        raise RuntimeError(
            "One or both branches missing on GitHub"
        )

    return api(
        gh,
        "POST",
        base + "/pulls",
        expected=(201,),
        json={
            "title": (
                pr.get("title") or "Migrated PR"
            ),
            "head": head,
            "base": target,
            "body": body,
            "draft": bool(
                pr.get("draft", False)
            )
        }
    )


def archive(gh, base, pr, body):
    result = api(
        gh,
        "POST",
        base + "/issues",
        expected=(201,),
        json={
            "title": (
                f"[Archived Bitbucket PR #{pr['id']}] "
                f"{pr.get('title') or 'Pull Request'}"
            ),
            "body": body
        }
    )

    api(
        gh,
        "PATCH",
        f"{base}/issues/{result['number']}",
        json={"state": "closed"}
    )

    return result


def import_comments(
    gh, base, number, comments, stats
):
    existing = pages_gh(
        gh,
        f"{base}/issues/{number}/comments"
    )

    seen = {
        line.strip()
        for item in existing
        for line in (
            item.get("body") or ""
        ).splitlines()
        if line.startswith("BB-COMMENT-ID:")
    }

    for comment in comments:
        if comment.get("deleted"):
            continue

        comment_id = comment.get("id")

        if comment_id is None:
            stats["errors"].append(
                "Comment without ID skipped"
            )
            continue

        marker = f"BB-COMMENT-ID: {comment_id}"

        if marker in seen:
            stats["comments_existing"] += 1
            continue

        message = (
            f"{marker}\n\n"
            f"Original Bitbucket author: "
            f"{name(comment.get('user'))}\n\n"
            f"Original date: "
            f"{comment.get('created_on')}\n\n"
            f"{comment_text(comment)}"
        )

        inline = comment.get("inline") or {}

        if inline:
            line_number = (
                inline.get("to")
                if inline.get("to") is not None
                else inline.get("from")
            )

            message += (
                "\n\nOriginal inline position:\n"
                f"File: {inline.get('path')}\n"
                f"Line: {line_number}"
            )

        if DRY_RUN:
            stats["comments_planned"] += 1
        else:
            api(
                gh,
                "POST",
                f"{base}/issues/{number}/comments",
                expected=(201,),
                json={"body": message}
            )

            stats["comments_created"] += 1

        seen.add(marker)


def ensure_reviewer_access(gh, base, login, stats):
    """Check access and invite (never impersonate the reviewer to accept)."""
    endpoint = f"{base}/collaborators/{quote(login, safe='')}"
    response = gh.get(endpoint, timeout=45)
    if response.status_code == 204:
        return True
    if response.status_code != 404:
        stats["errors"].append(
            f"Cannot check collaborator {login}: HTTP {response.status_code} "
            f"{response.text[:180]}"
        )
        return False

    # Avoid repeated invites while an earlier one is still pending.
    pending = gh.get(f"{base}/invitations", params={"per_page": 100}, timeout=45)
    if pending.status_code == 200:
        for invite in pending.json():
            if (invite.get("invitee") or {}).get("login", "").lower() == login.lower():
                stats.setdefault("reviewer_invites_pending", []).append(login)
                return False
    elif pending.status_code != 404:
        stats["errors"].append(
            f"Cannot inspect pending collaborator invitations: HTTP {pending.status_code}"
        )
        return False

    if DRY_RUN:
        stats.setdefault("reviewer_invites_planned", []).append(login)
        return False

    try:
        result = api(
            gh, "PUT", endpoint, expected=(201, 204),
            json={"permission": "push"}
        )
        # 201 means invitation issued; 204 can mean direct collaborator add.
        check = gh.get(endpoint, timeout=45)
        if check.status_code == 204:
            return True
        stats.setdefault("reviewer_invites_sent", []).append(login)
        return False
    except Exception as exc:
        stats["errors"].append(f"Cannot invite reviewer {login}: {exc}")
        return False


def request_reviewers(
    gh, base, number, pr, conn, cache, stats
):
    gh_pr = api(
        gh,
        "GET",
        f"{base}/pulls/{number}"
    )

    if gh_pr.get("state") != "open":
        stats["errors"].append(
            "Destination PR not open; "
            "reviewer request skipped"
        )
        return

    author = (
        (gh_pr.get("user") or {}).get("login")
        or ""
    ).lower()

    already = api(
        gh,
        "GET",
        f"{base}/pulls/{number}/requested_reviewers"
    )

    requested = {
        item["login"].lower()
        for item in already.get("users", [])
    }

    reviewed = pages_gh(
        gh,
        f"{base}/pulls/{number}/reviews"
    )

    for review in reviewed:
        login = (
            review.get("user") or {}
        ).get("login")

        if login:
            requested.add(login.lower())

    for person in pr.get("reviewers") or []:
        login = resolve_reviewer(
            gh, conn, person, stats, cache
        )

        if not login:
            continue

        key = login.lower()

        if key == author:
            stats["errors"].append(
                f"Cannot request PR author: {login}"
            )

        elif key in requested:
            stats["reviewers_existing"] += 1

        elif not ensure_reviewer_access(gh, base, login, stats):
            # Invitation must be accepted before a review can be requested.
            stats.setdefault("reviewers_waiting_for_access", []).append(login)

        elif DRY_RUN:
            stats["reviewers_planned"] += 1
            requested.add(key)

        else:
            try:
                api(
                    gh,
                    "POST",
                    f"{base}/pulls/{number}/"
                    "requested_reviewers",
                    expected=(201,),
                    json={
                        "reviewers": [login]
                    }
                )

                stats["reviewers_requested"] += 1
                requested.add(key)

            except Exception as exc:
                stats["errors"].append(
                    f"Reviewer {login}: {exc}"
                )


def migrate_pr(
    bb,
    gh,
    workspace,
    slug,
    base,
    brief,
    conn,
    cache
):
    pr_id = brief["id"]

    pr = api(
        bb,
        "GET",
        bb_pr_url(workspace, slug, pr_id)
    )

    state = (pr.get("state") or "").upper()

    stats = {
        "source_pr_id": pr_id,
        "source_state": state,
        "destination": None,
        "status": "PENDING",
        "comments_created": 0,
        "comments_existing": 0,
        "comments_planned": 0,
        "reviewers_requested": 0,
        "reviewers_existing": 0,
        "reviewers_planned": 0,
        "unmapped_reviewers": [],
        "errors": []
    }

    existing = existing_record(
        gh,
        base,
        source_marker(workspace, slug, pr_id)
    )

    comments = pages_bb(
        bb,
        bb_pr_url(
            workspace, slug, pr_id, "/comments"
        ),
        {"pagelen": 50}
    )

    try:
        activity = pages_bb(
            bb,
            bb_pr_url(
                workspace, slug, pr_id, "/activity"
            ),
            {"pagelen": 50}
        )

    except Exception as exc:
        activity = []

        stats["errors"].append(
            f"Activity read: {exc}"
        )

    body = body_for(
        workspace, slug, pr, activity
    )

    if existing:
        result = existing
        stats["status"] = "ALREADY_EXISTS"

        update_history(
            gh,
            base,
            existing,
            pr,
            activity,
            stats
        )

    elif DRY_RUN:
        stats["status"] = "PLANNED"

        stats["comments_planned"] = sum(
            not c.get("deleted")
            for c in comments
        )

        for reviewer in pr.get("reviewers") or []:
            resolve_reviewer(
                gh, conn, reviewer, stats, cache
            )

        return stats

    elif state == "OPEN":
        try:
            result = create_pr(
                gh, base, pr, body
            )

            stats["status"] = "OPEN_PR_CREATED"

        except Exception as exc:
            stats["errors"].append(
                f"Native PR not created: {exc}"
            )

            result = archive(
                gh, base, pr, body
            )

            stats["status"] = "ARCHIVED_FALLBACK"

    else:
        result = archive(
            gh, base, pr, body
        )

        stats["status"] = "ARCHIVED"

    stats["destination"] = result.get(
        "html_url"
    )

    number = result["number"]

    import_comments(
        gh, base, number, comments, stats
    )

    is_pr = (
        bool(result.get("pull_request"))
        or "/pulls/" in result.get("url", "")
    )

    if state == "OPEN" and is_pr:
        request_reviewers(
            gh,
            base,
            number,
            pr,
            conn,
            cache,
            stats
        )

    return stats

def migrate_pull_requests(bb, gh, workspace, repo, base, conn, reviewer_cache):
    # Enriched PR migration: existing PRs still receive missing comments/reviewer metadata.
    short_prs, discovery_errors = prs_for(bb, workspace, repo)
    stats = {
        "found": len(short_prs), "created_open": 0, "archived": 0,
        "existing": 0, "comments": 0, "comments_existing": 0,
        "reviewers_requested": 0, "reviewers_planned": 0,
        "unmapped_reviewers": [], "details": [], "errors": list(discovery_errors)
    }
    for brief in short_prs:
        try:
            result = migrate_pr(bb, gh, workspace, repo, base, brief, conn, reviewer_cache)
            stats["details"].append(result)
            status = result.get("status")
            if status == "OPEN_PR_CREATED":
                stats["created_open"] += 1
            elif status in ("ARCHIVED", "ARCHIVED_FALLBACK"):
                stats["archived"] += 1
            elif status == "ALREADY_EXISTS":
                stats["existing"] += 1
            stats["comments"] += result.get("comments_created", 0)
            stats["comments_existing"] += result.get("comments_existing", 0)
            stats["reviewers_requested"] += result.get("reviewers_requested", 0)
            stats["reviewers_planned"] += result.get("reviewers_planned", 0)
            stats["unmapped_reviewers"].extend(result.get("unmapped_reviewers", []))
            stats["errors"].extend(
                f'PR #{brief["id"]}: {error}' for error in result.get("errors", [])
            )
            log("PR", brief["id"], status, "comments_created", result.get("comments_created", 0),
                "reviewers_requested", result.get("reviewers_requested", 0))
        except Exception as exc:
            message = f'PR #{brief["id"]}: {exc}'
            stats["details"].append({"source_pr_id": brief["id"], "status": "FAILED", "error": str(exc)})
            stats["errors"].append(message)
            log("PR failed", message)
    return stats


def graphql(gh, query, variables):
    response = api(
        gh, "POST", GH + "/graphql",
        json={
            "query": query,
            "variables": variables
        }
    )

    if response.get("errors"):
        raise RuntimeError(
            str(response["errors"])[:400]
        )

    return response["data"]


def sync_github_projects(gh, owner, projects, results):
    """
    Create GitHub Projects V2 with Bitbucket project titles.
    Include empty Bitbucket projects.
    Link migrated repositories where supported.
    """

    owner_info = api(
        gh, "GET",
        f"{GH}/users/{quote(owner)}"
    )

    owner_id = owner_info["node_id"]

    owner_field = (
        "organization"
        if owner_info.get("type") == "Organization"
        else "user"
    )

    query = f"""
    query($login: String!, $cursor: String) {{
      {owner_field}(login: $login) {{
        projectsV2(first: 100, after: $cursor) {{
          nodes {{
            id
            number
            title
            url
            shortDescription
          }}
          pageInfo {{
            hasNextPage
            endCursor
          }}
        }}
      }}
    }}
    """

    existing_projects = []
    cursor = None

    while True:
        data = graphql(
            gh, query,
            {
                "login": owner,
                "cursor": cursor
            }
        )

        connection = (
            data[owner_field]["projectsV2"]
        )

        existing_projects.extend(
            connection["nodes"]
        )

        if not connection["pageInfo"]["hasNextPage"]:
            break

        cursor = (
            connection["pageInfo"]["endCursor"]
        )

    summary = []
    name_counts = Counter(
        p["name"] for p in projects
    )

    for project in projects:
        workspace = project["workspace"]
        key = project.get("key")
        original_name = project["name"]

        # Same-name projects from different workspaces
        # must remain distinct.
        title = (
            original_name
            if name_counts[original_name] == 1
            else f"{original_name} ({workspace})"
        )

        marker = (
            f"Migrated from Bitbucket workspace {workspace}; "
            f"project key {key}"
        )

        item = {
            "workspace": workspace,
            "project_key": key,
            "bitbucket_project": original_name,
            "github_title": title,
            "status": "PENDING",
            "linked_repositories": [],
            "errors": []
        }

        try:
            github_project = next(
                (
                    p for p in existing_projects
                    if p["title"] == title
                    and marker in (
                        p.get("shortDescription") or ""
                    )
                ),
                None
            )

            if github_project is None:
                title_exists = any(
                    p["title"] == title
                    for p in existing_projects
                )

                if title_exists:
                    item["status"] = "TITLE_CONFLICT"
                    item["errors"].append(
                        "An unrelated GitHub project "
                        "already uses this title."
                    )
                    summary.append(item)
                    continue

                mutation = """
                mutation($owner: ID!, $title: String!) {
                  createProjectV2(
                    input: {
                      ownerId: $owner
                      title: $title
                    }
                  ) {
                    projectV2 {
                      id
                      number
                      title
                      url
                      shortDescription
                    }
                  }
                }
                """

                created = graphql(
                    gh, mutation,
                    {
                        "owner": owner_id,
                        "title": title
                    }
                )

                github_project = (
                    created["createProjectV2"]["projectV2"]
                )

                existing_projects.append(
                    github_project
                )

                item["status"] = "CREATED"

                update = """
                mutation($id: ID!, $description: String!) {
                  updateProjectV2(
                    input: {
                      projectId: $id
                      shortDescription: $description
                    }
                  ) {
                    projectV2 {
                      id
                    }
                  }
                }
                """

                graphql(
                    gh, update,
                    {
                        "id": github_project["id"],
                        "description": marker
                    }
                )

                github_project["shortDescription"] = marker

            else:
                item["status"] = "ALREADY_EXISTS"

            item["url"] = github_project["url"]

            for repo in results:
                if (
                    repo.get("workspace") != workspace
                    or repo.get("project") != key
                ):
                    continue

                destination = repo["destination"]

                try:
                    repository = api(
                        gh, "GET",
                        f"{GH}/repos/{destination}"
                    )

                    link_mutation = """
                    mutation($project: ID!, $repository: ID!) {
                      linkProjectV2ToRepository(
                        input: {
                          projectId: $project
                          repositoryId: $repository
                        }
                      ) {
                        repository {
                          id
                        }
                      }
                    }
                    """

                    graphql(
                        gh, link_mutation,
                        {
                            "project": github_project["id"],
                            "repository": repository["node_id"]
                        }
                    )

                    item["linked_repositories"].append(
                        destination
                    )

                except Exception as exc:
                    item["errors"].append(
                        f"{destination}: {str(exc)[:200]}"
                    )

        except Exception as exc:
            item["status"] = "FAILED"
            item["errors"].append(
                str(exc)[:350]
            )

        summary.append(item)

        log(
            "Project:",
            original_name,
            "->",
            item.get("url", item["status"])
        )

    return summary


def create_github_repo(gh, owner, personal, target, source):
    endpoint = (
        GH + "/user/repos"
        if personal
        else f"{GH}/orgs/{owner}/repos"
    )

    return api(
        gh, "POST", endpoint,
        expected=(201,),
        json={
            "name": target,
            "description": str(
                source.get("description") or ""
            )[:350],
            "private": True,
            "auto_init": False,
            "has_issues": True
        }
    )


def sync_description(gh, owner, target, source):
    """Sync the Bitbucket description even when the GitHub repo already exists."""
    url = f"{GH}/repos/{owner}/{target}"
    description = str(source.get("description") or "")[:350]
    existing = api(gh, "GET", url)
    if (existing.get("description") or "") == description:
        return "ALREADY_MATCHED"
    api(gh, "PATCH", url, json={"description": description})
    return "UPDATED"


def save_report(report):
    REPORT_FILE.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8"
    )


def main():
    bb_token, gh_token = credentials()

    bb = requests.Session()
    gh = requests.Session()

    bb.headers.update({
        "Authorization": f"Bearer {bb_token}",
        "Accept": "application/json"
    })

    gh.headers.update({
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    })

    github_user = api(
        gh, "GET", GH + "/user"
    )["login"]

    owner = OWNER_OVERRIDE or github_user
    personal = owner.lower() == github_user.lower()

    log("GitHub destination:", owner)

    workspaces_data = bb_pages(
        bb, BB + "/user/workspaces",
        {"pagelen": 100}
    )

    workspaces = sorted({
        (row.get("workspace") or row).get("slug")
        for row in workspaces_data
        if (row.get("workspace") or row).get("slug")
    })

    projects = []
    repositories = []
    discovery_errors = []

    # Automatically discover all accessible workspaces.
    for workspace in workspaces:
        try:
            project_data = bb_pages(
                bb,
                f"{BB}/workspaces/"
                f"{quote(workspace, safe='')}/projects",
                {"pagelen": 100}
            )

            for project in project_data:
                projects.append({
                    "workspace": workspace,
                    "key": project.get("key"),
                    "name": project.get("name"),
                    "repositories": []
                })

        except Exception as exc:
            discovery_errors.append(
                f"Projects {workspace}: {exc}"
            )

        try:
            repo_data = bb_pages(
                bb,
                f"{BB}/repositories/"
                f"{quote(workspace, safe='')}",
                {"pagelen": 100}
            )

            for repo in repo_data:
                if repo.get("scm", "git") == "git":
                    repositories.append({
                        "workspace": workspace,
                        "repo": repo
                    })

        except Exception as exc:
            discovery_errors.append(
                f"Repositories {workspace}: {exc}"
            )

    # Deduplicate using Bitbucket repository UUID.
    unique = {}

    for item in repositories:
        repo = item["repo"]
        key = repo.get("uuid") or (
            item["workspace"], repo["slug"]
        )
        unique[key] = item

    repositories = sorted(
        unique.values(),
        key=lambda x: (
            x["workspace"],
            x["repo"]["slug"]
        )
    )

    counts = Counter(
        x["repo"]["slug"].lower()
        for x in repositories
    )

    used_names = set()

    for item in repositories:
        workspace = item["workspace"]
        repo = item["repo"]
        original = repo["slug"]

        candidate = (
            f"{workspace}-{original}"
            if counts[original.lower()] > 1
            else original
        )

        candidate = re.sub(
            r"[^a-zA-Z0-9_.-]", "-",
            candidate
        )[:90]

        name = candidate
        counter = 2

        while name.lower() in used_names:
            name = f"{candidate}-{counter}"
            counter += 1

        used_names.add(name.lower())
        item["target"] = name

        project_key = (
            repo.get("project") or {}
        ).get("key")

        for project in projects:
            if (
                project["workspace"] == workspace
                and project["key"] == project_key
            ):
                project["repositories"].append(
                    original
                )

    log("Workspaces discovered:", len(workspaces))
    log("Projects discovered:", len(projects))
    log("Repositories discovered:", len(repositories))

    report = {
        "mode": "DRY_RUN" if DRY_RUN else "MIGRATION",
        "github_owner": owner,
        "workspaces": workspaces,
        "projects_discovered": len(projects),
        "repositories_discovered": len(repositories),
        "projects": projects,
        "discovery_errors": discovery_errors,
        "results": [],
        "github_projects": [],
        "limitations": [
            "GitHub Projects V2 are planning boards, not nested repository containers.",
            "Historical Bitbucket issues in JSON are simulated source data.",
            "Merged/declined PRs are archived as GitHub Issues.",
            "Native PR author, review approvals and original timestamps are not preserved.",
            "Pipelines, secrets, permissions, branch protection, deploy keys and downloads need separate migration.",
            "Existing GitHub repositories are not mirror-pushed again."
        ]
    }

    if REVIEWERS_ONLY:
        report["mode"] = "REVIEWERS_ONLY_DRY_RUN" if DRY_RUN else "REVIEWERS_ONLY"
        report["reviewer_results"] = []
        conn_review = open_mapping_db()
        reviewer_cache = {}
        try:
            for item in repositories:
                ws = item["workspace"]
                slug = item["repo"]["slug"]
                target = item["target"]
                base = f"{GH}/repos/{owner}/{target}"
                item_report = {"repository": f"{owner}/{target}", "prs": [], "errors": []}
                report["reviewer_results"].append(item_report)
                try:
                    if gh.get(base, timeout=45).status_code != 200:
                        item_report["errors"].append("Destination repository inaccessible")
                        continue
                    prs, discovery_errors = prs_for(bb, ws, slug)
                    item_report["errors"].extend(discovery_errors)
                    for brief in prs:
                        if (brief.get("state") or "").upper() != "OPEN":
                            continue
                        pr = api(bb, "GET", bb_pr_url(ws, slug, brief["id"]))
                        existing = existing_record(gh, base, source_marker(ws, slug, brief["id"]))
                        if not existing or not existing.get("pull_request"):
                            item_report["errors"].append(
                                f"PR {brief['id']} has no native destination open PR")
                            continue
                        stats = {"source_pr_id": brief["id"], "github_pr": existing["number"],
                                 "reviewers_requested": 0, "reviewers_existing": 0,
                                 "reviewers_planned": 0, "unmapped_reviewers": [], "errors": []}
                        item_report["prs"].append(stats)
                        request_reviewers(gh, base, existing["number"], pr,
                                          conn_review, reviewer_cache, stats)
                except Exception as exc:
                    item_report["errors"].append(str(exc))
                save_report(report)
        finally:
            conn_review.close()
        save_report(report)
        log("Reviewer-only results:", REPORT_FILE.resolve())
        print(json.dumps(report["reviewer_results"], indent=2))
        return

    if DRY_RUN:
        report["plan"] = [
            {
                "workspace": item["workspace"],
                "project": (
                    item["repo"].get("project") or {}
                ).get("key"),
                "source": item["repo"]["slug"],
                "destination": item["target"]
            }
            for item in repositories
        ]

        # Read-only PR assessment on existing destination repositories.
        # Does not create repositories, PRs, projects, issues or comments.
        report["pr_dry_run"] = []
        conn_preview = open_mapping_db()
        cache_preview = {}
        try:
            for item in repositories:
                ws, slug, target = item["workspace"], item["repo"]["slug"], item["target"]
                base = f"{GH}/repos/{owner}/{target}"
                preview = {"source": f"{ws}/{slug}", "destination": f"{owner}/{target}"}
                report["pr_dry_run"].append(preview)
                try:
                    lookup = gh.get(base, timeout=45)
                    if lookup.status_code == 200:
                        preview["pull_requests"] = migrate_pull_requests(
                            bb, gh, ws, slug, base, conn_preview, cache_preview
                        )
                    elif lookup.status_code == 404:
                        preview["status"] = "DESTINATION_NOT_CREATED"
                    else:
                        preview["error"] = f"Destination lookup HTTP {lookup.status_code}"
                except Exception as exc:
                    preview["error"] = str(exc)
                save_report(report)
        finally:
            conn_preview.close()
        save_report(report)
        log("Dry-run report:", REPORT_FILE.resolve())
        print(json.dumps({"workspaces": len(workspaces), "projects": len(projects),
                          "repositories": len(repositories), "pr_checks": len(report["pr_dry_run"])}, indent=2))
        return

    # Shared reviewer identity mappings for all repositories.
    conn = open_mapping_db()
    reviewer_cache = {}

    # Repositories
    for index, item in enumerate(repositories, start=1):
        workspace = item["workspace"]
        repo = item["repo"]
        source = repo["slug"]
        target = item["target"]

        project_key = (
            repo.get("project") or {}
        ).get("key")

        base = f"{GH}/repos/{owner}/{target}"

        result = {
            "workspace": workspace,
            "project": project_key,
            "source": source,
            "destination": f"{owner}/{target}",
            "status": "FAILED",
            "errors": []
        }

        log(
            f"[{index}/{len(repositories)}]",
            workspace,
            source,
            "->",
            f"{owner}/{target}"
        )

        try:
            response = gh.get(base, timeout=60)

            if response.status_code == 404:
                create_github_repo(
                    gh, owner, personal, target, repo
                )

                result["git"] = migrate_git(
                    workspace, source, owner, target,
                    bb_token, gh_token
                )
        
            elif response.status_code == 200:
                # Retry a failed migration only when the destination has no Git refs.
                # Never mirror-push over an existing populated repository.
                with tempfile.TemporaryDirectory(prefix="github-ref-check-") as temp:
                    gh_env = git_auth(temp, "x-access-token", gh_token)
                    remote_refs = git_refs(
                        f"https://github.com/{owner}/{target}.git", gh_env
                    )

                if not remote_refs:
                    log("Recovering empty repository:", target)
                    result["git"] = migrate_git(
                        workspace, source, owner, target,
                        bb_token, gh_token
                    )
                else:
                    result["git"] = {
                        "git": "ALREADY_EXISTS_NOT_REPUSHED"
                    }

            else:
                raise RuntimeError(
                    "GitHub repository lookup failed: "
                    f"HTTP {response.status_code}"
                )

            result["description"] = sync_description(
                gh, owner, target, repo
            )

            result["topics"] = sync_topics(
                gh, owner, target,
                workspace, project_key
            )

            try:
                result["issues"] = migrate_issues(
                    bb, gh, workspace, source, base
                )
            except Exception as exc:
                result["errors"].append(
                    f"Issues: {exc}"
                )

            try:
                result["pull_requests"] = migrate_pull_requests(
                    bb, gh, workspace, source, base, conn, reviewer_cache
                )
            except Exception as exc:
                result["errors"].append(
                    f"Pull Requests: {exc}"
                )

            for section in ("issues", "pull_requests"):
                details = result.get(section) or {}
                result["errors"].extend(
                    details.get("errors", [])
                )

            git_result = result["git"]

            if (
                git_result.get("git") == "SUCCESS"
                and not git_result.get("refs_match")
            ):
                result["errors"].append(
                    "Git refs mismatch"
                )

            if git_result.get(
                "lfs", "NONE"
            ) not in ("NONE", "SUCCESS"):
                result["errors"].append(
                    "LFS incomplete or not checked"
                )

            result["status"] = (
                "PARTIAL"
                if result["errors"]
                else "COMPLETED_ATTEMPT"
            )

        except Exception as exc:
            result["errors"].append(str(exc))
            result["status"] = "FAILED"

        report["results"].append(result)
        save_report(report)

        log("Repository status:", result["status"])

    # GitHub Projects V2
    # Runs even when project has zero repositories.
    report["github_projects"] = sync_github_projects(
        gh, owner, projects, report["results"]
    )

    save_report(report)

    report["summary"] = dict(
        Counter(
            item["status"]
            for item in report["results"]
        )
    )

    save_report(report)

    conn.close()

    log("Report:", str(REPORT_FILE.resolve()))

    print(
        json.dumps(
            {
                "workspaces": len(workspaces),
                "projects": len(projects),
                "repositories": len(repositories),
                "repository_status": report["summary"],
                "github_projects": [
                    {
                        "name": p["github_title"],
                        "status": p["status"],
                        "url": p.get("url"),
                        "errors": p.get("errors", [])
                    }
                    for p in report["github_projects"]
                ]
            },
            indent=2
        )
    )




# ===== ADDITIVE PIPELINE MIGRATION MODULE (existing functions unchanged) =====
# PIPELINES_ONLY=true: audit Bitbucket pipeline configs and GitHub workflow presence.
# PIPELINE_CONVERT=true: additionally invoke GitHub Actions Importer locally.
# No repository writes, workflow push, PR creation or secret copying in this mode.
PIPELINES_ONLY = os.getenv("PIPELINES_ONLY", "false").lower() in ("true", "1", "yes")
PIPELINE_CONVERT = os.getenv("PIPELINE_CONVERT", "false").lower() in ("true", "1", "yes")
PIPELINE_WORKSPACE = os.getenv("PIPELINE_WORKSPACE", "").strip()
PIPELINE_REPO = os.getenv("PIPELINE_REPO", "").strip()
PIPELINE_REPORT = Path("pipeline-migration-report.json")
PIPELINE_OUTPUT = Path("pipeline-dry-run")


def pipeline_repo_source(bb, workspace, slug, revision):
    """Read the source config (never print or save its contents)."""
    path = (f"{BB}/repositories/{quote(workspace, safe='')}/"
            f"{quote(slug, safe='')}/src/{quote(revision, safe='')}/"
            "bitbucket-pipelines.yml")
    response = bb.get(path, timeout=45)
    if response.status_code == 404:
        return "NOT_FOUND", None
    if response.status_code != 200:
        raise RuntimeError(f"Bitbucket pipeline file HTTP {response.status_code}")
    text = response.text
    if not text.strip():
        return "EMPTY", None
    # Metadata only. Never include potentially sensitive source config in JSON.
    return "FOUND", len(text.encode("utf-8"))


def pipeline_destination(gh, owner, target):
    """List native workflows in an existing GitHub repository (read only)."""
    base = f"{GH}/repos/{quote(owner, safe='')}/{quote(target, safe='')}"
    response = gh.get(base, timeout=45)
    if response.status_code == 404:
        return "REPO_MISSING", []
    if response.status_code != 200:
        raise RuntimeError(f"GitHub repository lookup HTTP {response.status_code}")
    workflows = gh.get(base + "/actions/workflows", params={"per_page": 100}, timeout=45)
    if workflows.status_code == 404:
        return "WORKFLOWS_UNAVAILABLE", []
    if workflows.status_code != 200:
        raise RuntimeError(f"GitHub workflow listing HTTP {workflows.status_code}")
    return "OK", [
        {"name": entry.get("name"), "path": entry.get("path"), "state": entry.get("state")}
        for entry in workflows.json().get("workflows", [])
    ]


def pipeline_importer_dry_run(workspace, slug, output_dir):
    """Use official importer for LOCAL conversion only; never invoke 'migrate'."""
    if not shutil.which("gh"):
        return {"status": "TOOL_MISSING", "message": "Install GitHub CLI (gh)"}
    if not shutil.which("docker"):
        return {"status": "TOOL_MISSING", "message": "Install Docker and start its daemon"}
    extension = subprocess.run(
        ["gh", "extension", "list"], capture_output=True, text=True, timeout=30
    )
    if extension.returncode != 0 or "actions-importer" not in extension.stdout:
        return {"status": "TOOL_MISSING", "message": "Run: gh extension install github/gh-actions-importer"}
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["gh", "actions-importer", "dry-run", "bitbucket",
           "--workspace", workspace, "--repository", slug,
           "--output-dir", str(output_dir)]
    try:
        # Not capturing detailed stderr: importer may display auth-sensitive diagnostics.
        completed = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=900)
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "output_dir": str(output_dir)}
    if completed.returncode != 0:
        return {"status": "FAILED", "exit_code": completed.returncode,
                "output_dir": str(output_dir),
                "message": "Check local importer setup/authentication and generated logs; do not share tokens"}
    files = sorted(str(p) for p in output_dir.rglob("*.yml"))
    files += sorted(str(p) for p in output_dir.rglob("*.yaml"))
    return {"status": "CONVERTED_LOCALLY" if files else "CHECK_OUTPUT",
            "output_dir": str(output_dir), "workflow_files": files}


def pipeline_main():
    """Independent pipeline-only mode; does not call the original main()."""
    if PIPELINE_CONVERT and not DRY_RUN:
        raise RuntimeError(
            "Pipeline conversion is deliberately local-only. Run with DRY_RUN=true; "
            "review workflow and secrets before publishing via a separate PR."
        )
    bb_token, gh_token = credentials()
    bb = requests.Session()
    gh = requests.Session()
    bb.headers.update({"Authorization": f"Bearer {bb_token}", "Accept": "application/json"})
    gh.headers.update({"Authorization": f"Bearer {gh_token}",
                       "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28"})
    owner = OWNER_OVERRIDE or api(gh, "GET", GH + "/user")["login"]
    discovered = bb_pages(bb, BB + "/user/workspaces", {"pagelen": 100})
    workspaces = sorted({
        (entry.get("workspace") or entry).get("slug") for entry in discovered
        if (entry.get("workspace") or entry).get("slug")
    })
    if PIPELINE_WORKSPACE:
        workspaces = [ws for ws in workspaces if ws == PIPELINE_WORKSPACE]
        if not workspaces:
            raise RuntimeError("Requested workspace not found in accessible workspaces")
    discovered_repos = []
    for ws in workspaces:
        for item in bb_pages(bb, f"{BB}/repositories/{quote(ws, safe='')}",
                             {"pagelen": 100}):
            if item.get("scm", "git") != "git":
                continue
            slug = item["slug"]
            if PIPELINE_REPO and slug != PIPELINE_REPO:
                continue
            discovered_repos.append({"workspace": ws, "slug": slug, "repo": item})
    if PIPELINE_REPO and not discovered_repos:
        raise RuntimeError("Requested repository not discovered")
    counts = Counter(row["slug"].lower() for row in discovered_repos)
    report = {"mode": "PIPELINE_LOCAL_DRY_RUN" if PIPELINE_CONVERT else "PIPELINE_AUDIT",
              "github_owner": owner, "results": [],
              "notes": [
                  "No existing migration functionality was executed in this mode.",
                  "Local conversion is not proof of a working GitHub Actions pipeline.",
                  "Secured variables, deploy keys, permissions, runners and deploy targets require separate checks.",
                  "No workflow has been pushed or PR opened by this mode."
              ]}
    for row in discovered_repos:
        ws, slug, src = row["workspace"], row["slug"], row["repo"]
        target = f"{ws}-{slug}" if counts[slug.lower()] > 1 else slug
        target = re.sub(r"[^a-zA-Z0-9_.-]", "-", target)[:90]
        entry = {"source": f"{ws}/{slug}", "destination": f"{owner}/{target}",
                 "status": "PENDING", "errors": []}
        report["results"].append(entry)
        try:
            branch = (src.get("mainbranch") or {}).get("name") or "main"
            # Use the main branch only. Other branches may have different pipeline configs.
            status, size = pipeline_repo_source(bb, ws, slug, branch)
            entry["bitbucket_pipeline"] = {"status": status, "branch": branch}
            if size is not None:
                entry["bitbucket_pipeline"]["bytes"] = size
            gh_status, workflows = pipeline_destination(gh, owner, target)
            entry["github_workflows"] = {"status": gh_status, "items": workflows}
            if status == "FOUND":
                entry["status"] = "FOUND_NEEDS_CONVERSION"
                if PIPELINE_CONVERT:
                    entry["conversion"] = pipeline_importer_dry_run(
                        ws, slug, PIPELINE_OUTPUT / ws / slug)
                    entry["status"] = entry["conversion"]["status"]
            else:
                entry["status"] = status
        except Exception as exc:
            entry["status"] = "ERROR"
            entry["errors"].append(str(exc))
        PIPELINE_REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        log("Pipeline:", entry["source"], "->", entry["status"])
    log("Pipeline report:", PIPELINE_REPORT.resolve())
    print(json.dumps(report["results"], indent=2))


# ===== END ADDITIVE PIPELINE MODULE =====

# ===== SAFE PIPELINE DEPLOYMENT/VALIDATION EXTENSION =====
# Preserve the original migration entrypoint and functions. In pipeline mode,
# only read-only API calls are made unless explicitly opted in to a migration PR.
PIPELINE_PUBLISH = os.getenv("PIPELINE_PUBLISH", "false").lower() in ("true", "1", "yes")
PIPELINE_CONFIRM = os.getenv("PIPELINE_CONFIRM", "")
PIPELINE_AUDIT_VARIABLES = os.getenv("PIPELINE_AUDIT_VARIABLES", "true").lower() in ("true", "1", "yes")
PIPELINE_VALIDATE = os.getenv("PIPELINE_VALIDATE", "true").lower() in ("true", "1", "yes")
PIPELINE_VERIFY_RUNS = os.getenv("PIPELINE_VERIFY_RUNS", "true").lower() in ("true", "1", "yes")


def pipeline_variable_audit(bb, workspace, slug):
    """Return names and security flags only; NEVER transfer or expose secret values."""
    scopes = {
        "repository": f"{BB}/repositories/{quote(workspace, safe='')}/{quote(slug, safe='')}/pipelines_config/variables",
        "workspace": f"{BB}/workspaces/{quote(workspace, safe='')}/pipelines-config/variables",
    }
    result = {}
    for scope, url in scopes.items():
        try:
            rows = bb_pages(bb, url, {"pagelen": 100})
            result[scope] = {"status": "OK", "variables": [
                {"name": row.get("key"), "secured": bool(row.get("secured"))}
                for row in rows if row.get("key")
            ]}
        except Exception as exc:
            # Token permissions vary. Do not block conversion for missing audit permissions.
            result[scope] = {"status": "UNAVAILABLE", "reason": type(exc).__name__}
    return result


def pipeline_check_yaml(files):
    """Best-effort static validation, not a substitute for a successful Actions run."""
    result = {"status": "NOT_RUN", "files": [], "warnings": []}
    if not files:
        result["status"] = "NO_WORKFLOW_FILES"
        return result
    try:
        import yaml
    except ImportError:
        result["status"] = "PYYAML_NOT_INSTALLED"
        result["warnings"].append("Install PyYAML to enable structural YAML validation")
        return result
    # PyYAML YAML 1.1 treats unquoted 'on' as boolean True. Correct that for Actions.
    class ActionsLoader(yaml.SafeLoader):
        pass
    ActionsLoader.yaml_implicit_resolvers = {
        key: [(tag, regex) for tag, regex in patterns
              if tag != 'tag:yaml.org,2002:bool']
        for key, patterns in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    bad = False
    for item in files:
        path = Path(item)
        row = {"path": str(path), "status": "INVALID"}
        try:
            data = yaml.load(path.read_text(encoding="utf-8"), Loader=ActionsLoader)
            if not isinstance(data, dict):
                raise ValueError("workflow must be a mapping")
            if not data.get("jobs") or not isinstance(data["jobs"], dict):
                raise ValueError("workflow has no jobs mapping")
            if "on" not in data:
                raise ValueError("workflow has no on trigger")
            row["status"] = "STRUCTURE_OK"
            row["triggers"] = str(data.get("on"))[:120]
        except Exception as exc:
            bad = True
            row["error"] = str(exc)[:200]
        result["files"].append(row)
    result["status"] = "INVALID" if bad else "STRUCTURE_OK"
    result["warnings"].append("Inspect imported steps, permissions, runner compatibility, secrets and deploy targets manually")
    return result


def pipeline_publish_importer_pr(workspace, slug, owner, target, output_dir):
    """Official importer 'migrate' creates a GitHub PR, not a direct push/merge."""
    if DRY_RUN or not PIPELINE_PUBLISH or PIPELINE_CONFIRM != "CREATE_PIPELINE_PR":
        return {"status": "BLOCKED", "message": "Requires DRY_RUN=false PIPELINE_PUBLISH=true PIPELINE_CONFIRM=CREATE_PIPELINE_PR"}
    if not shutil.which("gh") or not shutil.which("docker"):
        return {"status": "TOOL_MISSING", "message": "GitHub CLI and Docker required"}
    extension = subprocess.run(["gh", "extension", "list"], capture_output=True, text=True, timeout=30)
    if extension.returncode != 0 or "actions-importer" not in extension.stdout:
        return {"status": "TOOL_MISSING", "message": "GitHub Actions Importer extension not found"}
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["gh", "actions-importer", "migrate", "bitbucket",
           "--workspace", workspace, "--repository", slug,
           "--target-url", f"https://github.com/{owner}/{target}",
           "--output-dir", str(output_dir)]
    try:
        completed = subprocess.run(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, timeout=1200)
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "message": "Importer migration command timed out; inspect GitHub for a newly opened PR before retrying"}
    if completed.returncode:
        return {"status": "FAILED", "exit_code": completed.returncode,
                "message": "Importer failed; inspect local importer config and PRs before retrying"}
    # Only extract PR URL, never record full importer stdout (may contain sensitive data).
    match = re.search(r"https://github\.com/[^\s/]+/[^\s/]+/pull/\d+", completed.stdout or "")
    return {"status": "PR_OPENED_CHECK_GITHUB", "pull_request_url": match.group(0) if match else None,
            "message": "Review and merge the PR manually; do not mark pipeline complete before a successful Actions run"}


_original_pipeline_main = pipeline_main


def pipeline_actions_runs(gh, owner, repo):
    """Read-only Actions run evidence. A successful run isn't full parity proof."""
    url = f"{GH}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/actions/runs"
    response = gh.get(url, params={"per_page": 10}, timeout=45)
    if response.status_code != 200:
        return {"status": "UNAVAILABLE", "http_status": response.status_code}
    data = response.json()
    return {"status": "OK", "total_count": data.get("total_count", 0),
            "recent_runs": [{"id": r.get("id"), "event": r.get("event"),
                             "status": r.get("status"), "conclusion": r.get("conclusion"),
                             "url": r.get("html_url")}
                            for r in data.get("workflow_runs", [])]}


def pipeline_main():
    """Full pipeline mode: discovery, variable audit, conversion, validation, opt-in PR."""
    global PIPELINE_CONVERT
    if PIPELINE_PUBLISH:
        if DRY_RUN or PIPELINE_CONFIRM != "CREATE_PIPELINE_PR" or not PIPELINE_REPO or not PIPELINE_WORKSPACE:
            raise RuntimeError("Publish requires PIPELINE_WORKSPACE and PIPELINE_REPO plus DRY_RUN=false "
                               "PIPELINE_PUBLISH=true PIPELINE_CONFIRM=CREATE_PIPELINE_PR")
    elif not DRY_RUN and PIPELINE_CONVERT:
        raise RuntimeError("Conversion is read-only unless explicit PIPELINE_PUBLISH opt-in is set")

    # Run the original working discovery. In publish mode suppress local dry-run conversion,
    # since the importer migrate command performs its own conversion and opens a PR.
    requested_convert = PIPELINE_CONVERT
    if PIPELINE_PUBLISH:
        PIPELINE_CONVERT = False
    try:
        _original_pipeline_main()
    finally:
        PIPELINE_CONVERT = requested_convert

    if not PIPELINE_REPORT.exists():
        raise RuntimeError("Pipeline discovery report missing")
    report = json.loads(PIPELINE_REPORT.read_text(encoding="utf-8"))
    report["mode"] = "PIPELINE_PUBLISH_PR" if PIPELINE_PUBLISH else report["mode"]
    for entry in report.get("results", []):
        workspace, slug = entry["source"].split("/", 1)
        owner, target = entry["destination"].split("/", 1)
        if entry.get("bitbucket_pipeline", {}).get("status") != "FOUND":
            continue
        if PIPELINE_VERIFY_RUNS:
            gh_verify = requests.Session()
            gh_verify.headers.update({"Authorization": f"Bearer {credentials()[1]}",
                                      "Accept": "application/vnd.github+json",
                                      "X-GitHub-Api-Version": "2022-11-28"})
            try:
                entry["github_actions_runs"] = pipeline_actions_runs(gh_verify, owner, target)
            finally:
                gh_verify.close()
        if PIPELINE_AUDIT_VARIABLES:
            token, _ = credentials()
            bb = requests.Session()
            bb.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
            entry["variables_audit"] = pipeline_variable_audit(bb, workspace, slug)
            bb.close()
        if requested_convert and not PIPELINE_PUBLISH:
            if PIPELINE_VALIDATE:
                conversion = entry.get("conversion", {})
                if conversion.get("status") == "CONVERTED_LOCALLY":
                    entry["validation"] = pipeline_check_yaml(conversion.get("workflow_files", []))
                else:
                    entry["validation"] = {"status": "NOT_CONVERTED"}
        if PIPELINE_PUBLISH:
            if entry.get("github_workflows", {}).get("items"):
                entry["publication"] = {"status": "BLOCKED_EXISTING_WORKFLOWS",
                                        "message": "Review existing workflows before creating a migration PR"}
            else:
                entry["publication"] = pipeline_publish_importer_pr(
                    workspace, slug, owner, target, PIPELINE_OUTPUT / workspace / slug / "publish")
            entry["status"] = entry["publication"]["status"]
        PIPELINE_REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log("Final pipeline report:", PIPELINE_REPORT.resolve())
    print(json.dumps({"mode": report["mode"], "results": [
        {"source": r["source"], "status": r["status"],
         "validation": r.get("validation", {}).get("status"),
         "publication": r.get("publication", {}).get("status")}
        for r in report["results"]]}, indent=2))


# ===== END SAFE PIPELINE EXTENSION =====


# ===== ADDITIVE WEBHOOK MIGRATION MODULE =====
# This module is intentionally independent of repository, PR and pipeline flows.
# WEBHOOKS_ONLY=true selects it. Defaults are read-only. Never print secrets or URLs.
WEBHOOKS_ONLY = os.getenv("WEBHOOKS_ONLY", "false").lower() in ("true", "1", "yes")
WEBHOOK_WORKSPACE = os.getenv("WEBHOOK_WORKSPACE", "vcs-migration-poc-tina").strip()
WEBHOOK_REPO = os.getenv("WEBHOOK_REPO", "backend-api-test").strip()
WEBHOOK_TARGET_REPO = os.getenv("WEBHOOK_TARGET_REPO", WEBHOOK_REPO).strip()
WEBHOOK_TARGET_URL = os.getenv("WEBHOOK_TARGET_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
WEBHOOK_CONFIRM = os.getenv("WEBHOOK_CONFIRM", "").strip()
# Explicit authorization when the approved GitHub receiver differs from Bitbucket.
WEBHOOK_APPROVE_DIFFERENT_URL = os.getenv("WEBHOOK_APPROVE_DIFFERENT_URL", "false").lower() in ("true", "1", "yes")
WEBHOOK_REPORT = Path("webhook-migration-report.json")
WEBHOOK_EVENT_MAP = {"repo:push": "push"}


def webhook_safe_endpoint_label(url):
    """Do not expose webhook.site IDs, signed URLs, query credentials or secrets."""
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or "unknown"
    return host.lower()


def webhook_collect(session, url):
    """Pagination with no raw webhook config persisted."""
    items = []
    while url:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        obj = response.json()
        if isinstance(obj, list):
            items.extend(obj)
            break
        if "values" in obj:
            items.extend(obj["values"])
            url = obj.get("next")
        else:
            items.extend(obj.get("hooks", []))
            break
    return items


def webhook_audit_and_migrate():
    """Read-only by default. Create one GitHub hook only with explicit opt-in."""
    from urllib.parse import quote, urlsplit

    if not WEBHOOK_WORKSPACE or not WEBHOOK_REPO or not WEBHOOK_TARGET_REPO:
        raise RuntimeError("Webhook workspace/source/target repository must be set")

    # Make all decisions before making a write request. Never infer target URL.
    publishing = not DRY_RUN
    if publishing:
        if WEBHOOK_CONFIRM != "CREATE_GITHUB_WEBHOOK":
            raise RuntimeError("Creation blocked: WEBHOOK_CONFIRM=CREATE_GITHUB_WEBHOOK required")
        target = urlsplit(WEBHOOK_TARGET_URL)
        if target.scheme != "https" or not target.hostname or target.username or target.password:
            raise RuntimeError("Provide a valid HTTPS WEBHOOK_TARGET_URL without embedded credentials")
        if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
            raise RuntimeError("WEBHOOK_SECRET must be set securely (minimum 16 characters)")

    bb_token, gh_token = credentials()
    bb = requests.Session()
    gh = requests.Session()
    bb.headers.update({"Authorization": f"Bearer {bb_token}", "Accept": "application/json"})
    gh.headers.update({"Authorization": f"Bearer {gh_token}",
                       "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28"})
    owner = OWNER_OVERRIDE or api(gh, "GET", GH + "/user")["login"]
    bb_base = (f"{BB}/repositories/{quote(WEBHOOK_WORKSPACE, safe='')}/"
               f"{quote(WEBHOOK_REPO, safe='')}/hooks")
    gh_base = (f"{GH}/repos/{quote(owner, safe='')}/"
               f"{quote(WEBHOOK_TARGET_REPO, safe='')}/hooks")

    report = {"mode": "WEBHOOK_AUDIT" if DRY_RUN else "WEBHOOK_CREATE",
              "source": f"{WEBHOOK_WORKSPACE}/{WEBHOOK_REPO}",
              "destination": f"{owner}/{WEBHOOK_TARGET_REPO}",
              "results": [], "notes": [
                  "Only repository webhooks are included, not workspace/connect hooks.",
                  "Only repo:push -> push is mapped; unsupported event sets are blocked.",
                  "Webhook URLs and secret values are intentionally omitted from reports.",
                  "Bitbucket payloads and signatures differ from GitHub; receiver compatibility requires manual validation.",
                  "GitHub requires its own secret value supplied by the operator; the Bitbucket secret cannot be read back.",
                  "A different GitHub receiver requires WEBHOOK_APPROVE_DIFFERENT_URL=true and manual receiver ownership review.",
                  "Destination webhook delivery and receiver-side HMAC validation require separate testing."
              ]}
    try:
        source_hooks = webhook_collect(bb, bb_base)
        dest_hooks = webhook_collect(gh, gh_base + "?per_page=100")
        # GitHub REST hooks use link-header pagination for >100; POC target is small.
        for hook in source_hooks:
            events = sorted(set(hook.get("events") or []))
            mapped = sorted(set(WEBHOOK_EVENT_MAP[e] for e in events if e in WEBHOOK_EVENT_MAP))
            unsupported = sorted(set(events) - set(WEBHOOK_EVENT_MAP))
            source_url = hook.get("url") or ""
            item = {"title": hook.get("description") or "(untitled)",
                    "source_active": bool(hook.get("active")),
                    "source_secret_configured": hook.get("secret_set"),
                    "source_events": events, "mapped_github_events": mapped,
                    "unmapped_events": unsupported,
                    "source_receiver_host": webhook_safe_endpoint_label(source_url)}
            matches = [h for h in dest_hooks if
                       (h.get("config") or {}).get("url") == (WEBHOOK_TARGET_URL or source_url)]
            if matches:
                item["status"] = "DESTINATION_EXISTS_REVIEW_EVENTS"
                item["destination_active"] = bool(matches[0].get("active"))
                item["destination_events"] = matches[0].get("events", [])
                item["destination_hook_id"] = matches[0].get("id")
            elif not hook.get("active"):
                item["status"] = "SOURCE_INACTIVE_SKIP"
            elif unsupported or not mapped:
                item["status"] = "UNMAPPED_EVENTS_MANUAL_REVIEW"
            elif DRY_RUN:
                item["status"] = "READY_FOR_MANUAL_REVIEW"
            elif source_url != WEBHOOK_TARGET_URL and not WEBHOOK_APPROVE_DIFFERENT_URL:
                # Different target URLs require an extra explicit operator opt-in.
                # The source webhook is never changed.
                item["status"] = "TARGET_URL_DIFFERS_MANUAL_REVIEW"
            else:
                payload = {"name": "web", "active": True, "events": mapped,
                           "config": {"url": WEBHOOK_TARGET_URL,
                                      "content_type": "json",
                                      "insecure_ssl": "0",
                                      "secret": WEBHOOK_SECRET}}
                response = gh.post(gh_base, json=payload, timeout=60)
                if response.status_code != 201:
                    # Never print response content; it could contain URL or secrets.
                    item["status"] = "CREATE_FAILED"
                    item["http_status"] = response.status_code
                else:
                    item["status"] = "CREATED_VERIFY_DELIVERY"
                    item["destination_hook_id"] = response.json().get("id")
                    dest_hooks.append({"config": {"url": WEBHOOK_TARGET_URL},
                                       "events": mapped, "active": True})
            report["results"].append(item)
        WEBHOOK_REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        print("[WEBHOOK] Report:", WEBHOOK_REPORT.resolve())
    finally:
        bb.close()
        gh.close()


if __name__ == "__main__" and WEBHOOKS_ONLY:
    webhook_audit_and_migrate()
    raise SystemExit(0)

# ===== END ADDITIVE WEBHOOK MIGRATION MODULE =====

if __name__ == "__main__":
    if PIPELINES_ONLY:
        pipeline_main()
    else:
        main()

# ===== GITHUB ACTIONS VERIFIED STATUS PATCH =====
# Additive, read-only post-processing for pipeline audit reports.
# Existing migration functions and repository operations remain unchanged.

def verify_migrated_pipeline_status(report):
    for entry in report.get("results", []):
        workflow_data = entry.get("github_workflows") or {}
        workflows = workflow_data.get("items") or []

        if entry.get("bitbucket_pipeline", {}).get("status") != "FOUND":
            continue

        source = entry.get("source", "")
        destination = entry.get("destination", "")

        if not workflows or "/" not in destination:
            continue

        owner, repo = destination.split("/", 1)

        try:
            _, github_token = credentials()
            session = requests.Session()
            session.headers.update({
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"
            })

            response = session.get(
                f"{GH}/repos/{owner}/{repo}/actions/runs",
                params={
                    "branch": "main",
                    "event": "push",
                    "per_page": 100
                },
                timeout=45
            )
            response.raise_for_status()

            runs = response.json().get("workflow_runs", [])

            active_paths = {
                w.get("path")
                for w in workflows
                if w.get("state") == "active"
            }

            successful = next(
                (
                    run for run in runs
                    if run.get("path") in active_paths
                    and run.get("status") == "completed"
                    and run.get("conclusion") == "success"
                ),
                None
            )

            entry["verification"] = {
                "active_workflows": sorted(active_paths),
                "latest_successful_main_run": (
                    successful.get("id") if successful else None
                ),
                "conclusion": (
                    successful.get("conclusion") if successful else None
                ),
                "full_parity": "NOT_YET_VERIFIED"
            }

            if successful:
                entry["status"] = "PIPELINE_MIGRATED_VERIFIED"
            else:
                entry["status"] = "WORKFLOW_EXISTS_EXECUTION_UNVERIFIED"

        except Exception as exc:
            entry["verification"] = {
                "status": "VERIFICATION_ERROR",
                "message": str(exc)[:200]
            }

    return report


def refresh_pipeline_verification_report():
    report_path = Path("pipeline-migration-report.json")

    if not report_path.exists():
        return

    report = json.loads(report_path.read_text(encoding="utf-8"))

    # The existing script may also record a final summary.
    # Only update the established pipeline results.
    verify_migrated_pipeline_status(report)

    report_path.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8"
    )

    for entry in report.get("results", []):
        print(
            "[PIPELINE VERIFICATION]",
            entry.get("source"),
            "->",
            entry.get("status")
        )


if __name__ == "__main__" and PIPELINES_ONLY:
    refresh_pipeline_verification_report()

# ===== END GITHUB ACTIONS VERIFIED STATUS PATCH =====
