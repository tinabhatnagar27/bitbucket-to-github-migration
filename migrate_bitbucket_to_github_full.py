#!/usr/bin/env python3

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import requests


# CONFIGURATION


BITBUCKET_API = "https://api.bitbucket.org/2.0"
GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

BITBUCKET_GIT_USER = "x-bitbucket-api-token-auth"

REPORT_FILE = os.getenv(
    "MIGRATION_REPORT",
    "migration-report.json"
)

DRY_RUN = (
    os.getenv("DRY_RUN", "false").lower()
    in ("true", "1", "yes")
)

SKIP_METADATA = (
    os.getenv("SKIP_METADATA", "false").lower()
    in ("true", "1", "yes")
)

SKIP_WEBHOOKS = (
    os.getenv("SKIP_WEBHOOKS", "false").lower()
    in ("true", "1", "yes")
)

ONLY_WORKSPACE = os.getenv("ONLY_WORKSPACE")

GITHUB_OWNER_OVERRIDE = os.getenv("GITHUB_OWNER")

EXISTING_REPO_MODE = os.getenv(
    "EXISTING_REPO_MODE",
    "skip"
).lower()



# LOGGING


def log(message):
    print(
        f"[INFO] {message}",
        flush=True
    )


def warn(message):
    print(
        f"[WARN] {message}",
        flush=True
    )


def fail(message, code=1):
    print(
        f"[ERROR] {message}",
        file=sys.stderr,
        flush=True
    )
    sys.exit(code)



# REQUIREMENTS

def check_requirements():

    if not shutil.which("git"):
        fail("Git is not installed.")

    if not shutil.which("git-lfs"):
        warn(
            "git-lfs is not installed. "
            "LFS migration will be skipped."
        )



# COMMAND RUNNER

def run_command(
    command,
    cwd=None,
    env=None
):

    safe_command = []

    for part in command:

        text = str(part)

        if (
            "Authorization:" in text
            or "BITBUCKET_TOKEN" in text
            or "GITHUB_TOKEN" in text
        ):
            safe_command.append("***REDACTED***")
        else:
            safe_command.append(text)

    log(
        "Running: "
        + " ".join(safe_command)
    )

    process_env = os.environ.copy()

    if env:
        process_env.update(env)

    subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=True,
        env=process_env
    )



# HTTP SESSIONS


def bitbucket_session(token):

    session = requests.Session()

    session.headers.update({
        "Authorization":
            f"Bearer {token}",

        "Accept":
            "application/json",

        "User-Agent":
            "vcs-migration-tool"
    })

    return session


def github_session(token):

    session = requests.Session()

    session.headers.update({
        "Authorization":
            f"Bearer {token}",

        "Accept":
            "application/vnd.github+json",

        "X-GitHub-Api-Version":
            GITHUB_API_VERSION,

        "User-Agent":
            "vcs-migration-tool"
    })

    return session



# HTTP HELPER

def api_request(
    session,
    method,
    url,
    expected=(200,),
    **kwargs
):

    for attempt in range(1, 6):

        response = session.request(
            method,
            url,
            timeout=90,
            **kwargs
        )

        if response.status_code in expected:

            if (
                response.status_code == 204
                or not response.content
            ):
                return None

            return response.json()

        if response.status_code in (
            429,
            502,
            503,
            504
        ):

            wait = min(
                2 ** attempt,
                30
            )

            warn(
                f"HTTP {response.status_code}. "
                f"Retry in {wait}s"
            )

            time.sleep(wait)
            continue

        raise RuntimeError(
            f"{method} {url} -> "
            f"HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    raise RuntimeError(
        f"API request failed after retries: {url}"
    )



# BITBUCKET PAGINATION


def paginate_bitbucket(
    session,
    url,
    params=None
):

    results = []
    first_request = True

    while url:

        data = api_request(
            session,
            "GET",
            url,
            expected=(200,),
            params=(
                params
                if first_request
                else None
            )
        )

        first_request = False

        results.extend(
            data.get(
                "values",
                []
            )
        )

        url = data.get("next")

    return results



# GITHUB AUTO DISCOVERY


def get_github_user(github):

    return api_request(
        github,
        "GET",
        f"{GITHUB_API}/user",
        expected=(200,)
    )



# BITBUCKET AUTO DISCOVERY


def get_bitbucket_workspaces(bitbucket):

    rows = paginate_bitbucket(
        bitbucket,
        f"{BITBUCKET_API}/user/workspaces",
        {
            "pagelen": 100
        }
    )

    workspaces = []

    for row in rows:

        workspace = (
            row.get("workspace")
            or row
        )

        slug = workspace.get("slug")

        if not slug:
            continue

        workspaces.append({
            "slug":
                slug,

            "name":
                workspace.get("name")
                or slug,

            "administrator":
                bool(
                    row.get(
                        "administrator",
                        False
                    )
                )
        })

    return workspaces


def get_workspace_repositories(
    bitbucket,
    workspace
):

    return paginate_bitbucket(
        bitbucket,
        (
            f"{BITBUCKET_API}"
            f"/repositories/"
            f"{quote(workspace)}"
        ),
        {
            "pagelen": 100,
            "sort": "name"
        }
    )



# GITHUB REPOSITORY

def github_repo_exists(
    github,
    owner,
    repo
):

    response = github.get(
        (
            f"{GITHUB_API}/repos/"
            f"{quote(owner)}/"
            f"{quote(repo)}"
        ),
        timeout=60
    )

    if response.status_code == 200:
        return True

    if response.status_code == 404:
        return False

    raise RuntimeError(
        "Unable to check GitHub repository: "
        f"{response.status_code} "
        f"{response.text}"
    )


def create_github_repository(
    github,
    owner,
    repo,
    personal_account
):

    payload = {

        "name":
            repo["slug"],

        "description":
            repo.get("description")
            or "",

        "homepage":
            repo.get("website")
            or "",

        "private":
            bool(
                repo.get(
                    "is_private",
                    True
                )
            ),

        "auto_init":
            False,

        "has_issues":
            True,

        "has_projects":
            False,

        "has_wiki":
            True
    }

    if personal_account:

        endpoint = (
            f"{GITHUB_API}/user/repos"
        )

    else:

        endpoint = (
            f"{GITHUB_API}/orgs/"
            f"{quote(owner)}/repos"
        )

    api_request(
        github,
        "POST",
        endpoint,
        expected=(201,),
        json=payload
    )

    log(
        "Created GitHub repo: "
        f"{owner}/{repo['slug']}"
    )



# GIT ASKPASS AUTH


def create_askpass_script(directory):

    askpass = (
        Path(directory)
        / "git-askpass.sh"
    )

    askpass.write_text(
        """#!/bin/sh
case "$1" in
    *Username*)
        printf '%s\\n' "$GIT_AUTH_USERNAME"
        ;;
    *Password*)
        printf '%s\\n' "$GIT_AUTH_PASSWORD"
        ;;
    *)
        printf '\\n'
        ;;
esac
""",
        encoding="utf-8"
    )

    askpass.chmod(0o700)

    return askpass


def auth_environment(
    askpass,
    username,
    token
):

    return {

        "GIT_ASKPASS":
            str(askpass),

        "GIT_TERMINAL_PROMPT":
            "0",

        "GIT_AUTH_USERNAME":
            username,

        "GIT_AUTH_PASSWORD":
            token
    }



# GIT + HISTORY + BRANCHES + TAGS + LFS


def migrate_git_repository(
    workspace,
    repo_name,
    github_owner,
    bitbucket_token,
    github_token
):

    source_url = (
        f"https://bitbucket.org/"
        f"{workspace}/"
        f"{repo_name}.git"
    )

    destination_url = (
        f"https://github.com/"
        f"{github_owner}/"
        f"{repo_name}.git"
    )

    result = {

        "git":
            "PENDING",

        "lfs":
            "SKIPPED",

        "lfs_fetch":
            "SKIPPED",

        "lfs_push":
            "SKIPPED"
    }

    with tempfile.TemporaryDirectory(
        prefix=
            f"migration-{repo_name}-"
    ) as tmp:

        tmp_path = Path(tmp)

        mirror_path = (
            tmp_path
            / f"{repo_name}.git"
        )

        askpass = (
            create_askpass_script(
                tmp_path
            )
        )

        bitbucket_env = (
            auth_environment(
                askpass,
                BITBUCKET_GIT_USER,
                bitbucket_token
            )
        )

        github_env = (
            auth_environment(
                askpass,
                "x-access-token",
                github_token
            )
        )

        
        # MIRROR CLONE
        

        run_command(
            [
                "git",
                "clone",
                "--mirror",
                source_url,
                str(mirror_path)
            ],
            env=bitbucket_env
        )

        
        # FETCH LFS FROM BITBUCKET

        
        if shutil.which("git-lfs"):

            try:

                run_command(
                    [
                        "git",
                        "lfs",
                        "fetch",
                        "--all",
                        "origin"
                    ],
                    cwd=mirror_path,
                    env=bitbucket_env
                )

                result[
                    "lfs_fetch"
                ] = "SUCCESS"

            except subprocess.CalledProcessError:

                result[
                    "lfs_fetch"
                ] = "FAILED"

                result[
                    "lfs"
                ] = "FAILED"

                warn(
                    "LFS fetch failed from "
                    f"Bitbucket: {repo_name}"
                )

        
        # ADD GITHUB REMOTE
   

        run_command(
            [
                "git",
                "remote",
                "add",
                "github",
                destination_url
            ],
            cwd=mirror_path
        )

        
        # PUSH MIRROR
      

        run_command(
            [
                "git",
                "push",
                "--mirror",
                "github"
            ],
            cwd=mirror_path,
            env=github_env
        )

        result[
            "git"
        ] = "SUCCESS"

        
        # PUSH LFS TO GITHUB
       

        if shutil.which("git-lfs"):

            if (
                result["lfs_fetch"]
                == "SUCCESS"
            ):

                try:

                    run_command(
                        [
                            "git",
                            "lfs",
                            "push",
                            "--all",
                            "github"
                        ],
                        cwd=mirror_path,
                        env=github_env
                    )

                    result[
                        "lfs_push"
                    ] = "SUCCESS"

                    result[
                        "lfs"
                    ] = "SUCCESS"

                except subprocess.CalledProcessError:

                    result[
                        "lfs_push"
                    ] = "FAILED"

                    result[
                        "lfs"
                    ] = "FAILED"

                    warn(
                        "LFS push failed to "
                        f"GitHub: {repo_name}"
                    )

            else:

                result[
                    "lfs_push"
                ] = "SKIPPED"

                result[
                    "lfs"
                ] = "FAILED"

    return result



# ISSUES


def migrate_issues(
    bitbucket,
    github,
    workspace,
    repo,
    owner
):

    stats = {

        "found":
            0,

        "created":
            0,

        "comments":
            0,

        "failed":
            0
    }

    try:

        issues = paginate_bitbucket(
            bitbucket,
            (
                f"{BITBUCKET_API}"
                f"/repositories/"
                f"{workspace}/"
                f"{repo}/issues"
            ),
            {
                "pagelen": 100
            }
        )

    except Exception as exc:

        stats[
            "unavailable"
        ] = str(exc)

        warn(
            "Bitbucket Issues unavailable: "
            f"{exc}"
        )

        return stats

    stats[
        "found"
    ] = len(issues)

    for issue in issues:

        try:

            reporter = (
                issue.get("reporter")
                or {}
            ).get(
                "display_name",
                "Unknown"
            )

            original_content = (
                issue.get("content")
                or {}
            ).get(
                "raw",
                ""
            )

            body = f"""
Migrated from Bitbucket

Original Issue ID: {issue.get('id')}
Original Reporter: {reporter}
Original Created: {issue.get('created_on')}
Original Updated: {issue.get('updated_on')}
Original State: {issue.get('state')}
Type: {issue.get('kind')}
Priority: {issue.get('priority')}

{original_content}
"""

            github_issue = api_request(
                github,
                "POST",
                (
                    f"{GITHUB_API}"
                    f"/repos/"
                    f"{owner}/"
                    f"{repo}/issues"
                ),
                expected=(201,),
                json={
                    "title":
                        issue.get("title")
                        or "Migrated Bitbucket Issue",

                    "body":
                        body
                }
            )

            issue_number = (
                github_issue["number"]
            )

            try:

                comments = paginate_bitbucket(
                    bitbucket,
                    (
                        f"{BITBUCKET_API}"
                        f"/repositories/"
                        f"{workspace}/"
                        f"{repo}/issues/"
                        f"{issue['id']}"
                        f"/comments"
                    ),
                    {
                        "pagelen": 100
                    }
                )

                for comment in comments:

                    if comment.get("deleted"):
                        continue

                    author = (
                        comment.get("user")
                        or {}
                    ).get(
                        "display_name",
                        "Unknown"
                    )

                    comment_body = (
                        comment.get("content")
                        or {}
                    ).get(
                        "raw",
                        ""
                    )

                    text = f"""
Migrated Bitbucket Comment

Original Author: {author}
Original Time: {comment.get('created_on')}

{comment_body}
"""

                    api_request(
                        github,
                        "POST",
                        (
                            f"{GITHUB_API}"
                            f"/repos/"
                            f"{owner}/"
                            f"{repo}/issues/"
                            f"{issue_number}/comments"
                        ),
                        expected=(201,),
                        json={
                            "body":
                                text
                        }
                    )

                    stats[
                        "comments"
                    ] += 1

            except Exception as exc:

                warn(
                    "Issue comments migration "
                    f"warning: {exc}"
                )

            state = (
                issue.get(
                    "state",
                    ""
                ).lower()
            )

            if state in (
                "resolved",
                "closed",
                "invalid",
                "duplicate",
                "wontfix"
            ):

                api_request(
                    github,
                    "PATCH",
                    (
                        f"{GITHUB_API}"
                        f"/repos/"
                        f"{owner}/"
                        f"{repo}/issues/"
                        f"{issue_number}"
                    ),
                    expected=(200,),
                    json={
                        "state":
                            "closed"
                    }
                )

            stats[
                "created"
            ] += 1

        except Exception as exc:

            stats[
                "failed"
            ] += 1

            warn(
                "Issue migration failed: "
                f"{exc}"
            )

    return stats



# PULL REQUESTS


def archive_pull_request(
    github,
    owner,
    repo,
    pr
):

    author = (
        pr.get("author")
        or {}
    ).get(
        "display_name",
        "Unknown"
    )

    source = (
        (
            pr.get("source")
            or {}
        ).get("branch")
        or {}
    ).get(
        "name",
        "Unknown"
    )

    destination = (
        (
            pr.get("destination")
            or {}
        ).get("branch")
        or {}
    ).get(
        "name",
        "Unknown"
    )

    body = f"""
Archived Bitbucket Pull Request

Original PR ID: {pr.get('id')}
Author: {author}
State: {pr.get('state')}
Created: {pr.get('created_on')}
Updated: {pr.get('updated_on')}
Source Branch: {source}
Destination Branch: {destination}

{pr.get('description') or ''}
"""

    issue = api_request(
        github,
        "POST",
        (
            f"{GITHUB_API}"
            f"/repos/"
            f"{owner}/"
            f"{repo}/issues"
        ),
        expected=(201,),
        json={
            "title":
                (
                    "[Migrated PR "
                    f"#{pr.get('id')}] "
                    f"{pr.get('title')}"
                ),

            "body":
                body
        }
    )

    issue_number = (
        issue["number"]
    )

    api_request(
        github,
        "PATCH",
        (
            f"{GITHUB_API}"
            f"/repos/"
            f"{owner}/"
            f"{repo}/issues/"
            f"{issue_number}"
        ),
        expected=(200,),
        json={
            "state":
                "closed"
        }
    )

    return issue_number


def migrate_pull_requests(
    bitbucket,
    github,
    workspace,
    repo,
    owner
):

    stats = {

        "found":
            0,

        "open_created":
            0,

        "historical_archived":
            0,

        "comments":
            0,

        "failed":
            0
    }

    prs = []

    for state in (
        "OPEN",
        "MERGED",
        "DECLINED",
        "SUPERSEDED"
    ):

        try:

            data = paginate_bitbucket(
                bitbucket,
                (
                    f"{BITBUCKET_API}"
                    f"/repositories/"
                    f"{workspace}/"
                    f"{repo}/pullrequests"
                ),
                {
                    "pagelen": 50,
                    "state": state
                }
            )

            prs.extend(data)

        except Exception:
            pass

    unique_prs = {}

    for pr in prs:

        unique_prs[
            pr.get("id")
        ] = pr

    prs = list(
        unique_prs.values()
    )

    stats[
        "found"
    ] = len(prs)

    for pr in prs:

        conversation_number = None

        try:

            state = (
                pr.get(
                    "state",
                    ""
                ).upper()
            )

            source_branch = (
                (
                    pr.get("source")
                    or {}
                ).get("branch")
                or {}
            ).get("name")

            destination_branch = (
                (
                    pr.get("destination")
                    or {}
                ).get("branch")
                or {}
            ).get("name")

            if (
                state == "OPEN"
                and source_branch
                and destination_branch
            ):

                try:

                    author = (
                        pr.get("author")
                        or {}
                    ).get(
                        "display_name",
                        "Unknown"
                    )

                    created_pr = api_request(
                        github,
                        "POST",
                        (
                            f"{GITHUB_API}"
                            f"/repos/"
                            f"{owner}/"
                            f"{repo}/pulls"
                        ),
                        expected=(201,),
                        json={
                            "title":
                                pr.get("title")
                                or "Migrated Bitbucket PR",

                            "head":
                                source_branch,

                            "base":
                                destination_branch,

                            "body":
                                f"""
Migrated from Bitbucket

Original PR ID: {pr.get('id')}
Original Author: {author}
Original Created: {pr.get('created_on')}

{pr.get('description') or ''}
"""
                        }
                    )

                    conversation_number = (
                        created_pr["number"]
                    )

                    stats[
                        "open_created"
                    ] += 1

                except Exception as exc:

                    warn(
                        "Cannot recreate open PR. "
                        f"Archiving: {exc}"
                    )

                    conversation_number = (
                        archive_pull_request(
                            github,
                            owner,
                            repo,
                            pr
                        )
                    )

                    stats[
                        "historical_archived"
                    ] += 1

            else:

                conversation_number = (
                    archive_pull_request(
                        github,
                        owner,
                        repo,
                        pr
                    )
                )

                stats[
                    "historical_archived"
                ] += 1

            try:

                comments = paginate_bitbucket(
                    bitbucket,
                    (
                        f"{BITBUCKET_API}"
                        f"/repositories/"
                        f"{workspace}/"
                        f"{repo}/"
                        f"pullrequests/"
                        f"{pr['id']}"
                        f"/comments"
                    ),
                    {
                        "pagelen": 100
                    }
                )

                for comment in comments:

                    if comment.get("deleted"):
                        continue

                    author = (
                        comment.get("user")
                        or {}
                    ).get(
                        "display_name",
                        "Unknown"
                    )

                    comment_body = (
                        comment.get("content")
                        or {}
                    ).get(
                        "raw",
                        ""
                    )

                    api_request(
                        github,
                        "POST",
                        (
                            f"{GITHUB_API}"
                            f"/repos/"
                            f"{owner}/"
                            f"{repo}/issues/"
                            f"{conversation_number}"
                            f"/comments"
                        ),
                        expected=(201,),
                        json={
                            "body":
                                f"""
Migrated Bitbucket PR Comment

Original Author: {author}
Original Created: {comment.get('created_on')}

{comment_body}
"""
                        }
                    )

                    stats[
                        "comments"
                    ] += 1

            except Exception as exc:

                warn(
                    "PR comment migration "
                    f"warning: {exc}"
                )

        except Exception as exc:

            stats[
                "failed"
            ] += 1

            warn(
                "PR migration failed: "
                f"{exc}"
            )

    return stats



# WEBHOOKS


WEBHOOK_MAP = {

    "repo:push":
        "push",

    "repo:fork":
        "fork",

    "repo:updated":
        "repository",

    "issue:created":
        "issues",

    "issue:updated":
        "issues",

    "issue:comment_created":
        "issue_comment",

    "pullrequest:created":
        "pull_request",

    "pullrequest:updated":
        "pull_request",

    "pullrequest:fulfilled":
        "pull_request",

    "pullrequest:rejected":
        "pull_request",

    "pullrequest:comment_created":
        "pull_request_review_comment"
}


def migrate_webhooks(
    bitbucket,
    github,
    workspace,
    repo,
    owner
):

    stats = {

        "found":
            0,

        "created":
            0,

        "skipped":
            0,

        "failed":
            0
    }

    if SKIP_WEBHOOKS:

        stats[
            "disabled"
        ] = True

        return stats

    try:

        hooks = paginate_bitbucket(
            bitbucket,
            (
                f"{BITBUCKET_API}"
                f"/repositories/"
                f"{workspace}/"
                f"{repo}/hooks"
            ),
            {
                "pagelen": 100
            }
        )

    except Exception as exc:

        stats[
            "unavailable"
        ] = str(exc)

        return stats

    stats[
        "found"
    ] = len(hooks)

    for hook in hooks:

        try:

            callback = (
                hook.get("url")
            )

            events = sorted({

                WEBHOOK_MAP[event]

                for event
                in hook.get(
                    "events",
                    []
                )

                if event in WEBHOOK_MAP
            })

            if (
                not callback
                or not events
            ):

                stats[
                    "skipped"
                ] += 1

                continue

            api_request(
                github,
                "POST",
                (
                    f"{GITHUB_API}"
                    f"/repos/"
                    f"{owner}/"
                    f"{repo}/hooks"
                ),
                expected=(201,),
                json={
                    "name":
                        "web",

                    "active":
                        bool(
                            hook.get(
                                "active",
                                True
                            )
                        ),

                    "events":
                        events,

                    "config": {

                        "url":
                            callback,

                        "content_type":
                            "json",

                        "insecure_ssl":
                            "0"
                    }
                }
            )

            stats[
                "created"
            ] += 1

        except Exception as exc:

            stats[
                "failed"
            ] += 1

            warn(
                "Webhook migration failed: "
                f"{exc}"
            )

    return stats



# EXTRA INVENTORY


def collect_extra_inventory(
    bitbucket,
    workspace,
    repo
):

    inventory = {}

    endpoints = {

        "branch_restrictions":
            (
                f"{BITBUCKET_API}"
                f"/repositories/"
                f"{workspace}/"
                f"{repo}/"
                f"branch-restrictions"
            ),

        "pipeline_variables":
            (
                f"{BITBUCKET_API}"
                f"/repositories/"
                f"{workspace}/"
                f"{repo}/"
                f"pipelines_config/"
                f"variables"
            ),

        "deploy_keys":
            (
                f"{BITBUCKET_API}"
                f"/repositories/"
                f"{workspace}/"
                f"{repo}/deploy-keys"
            ),

        "downloads":
            (
                f"{BITBUCKET_API}"
                f"/repositories/"
                f"{workspace}/"
                f"{repo}/downloads"
            )
    }

    for name, endpoint in endpoints.items():

        try:

            inventory[
                name
            ] = paginate_bitbucket(
                bitbucket,
                endpoint,
                {
                    "pagelen": 100
                }
            )

        except Exception as exc:

            inventory[
                name
            ] = {
                "not_migrated":
                    str(exc)
            }

    return inventory



# MIGRATE ONE REPOSITORY


def migrate_repository(
    bitbucket,
    github,
    workspace,
    repo,
    github_owner,
    personal_account,
    bitbucket_token,
    github_token
):

    repo_name = (
        repo["slug"]
    )

    result = {

        "workspace":
            workspace,

        "repository":
            repo_name,

        "destination":
            (
                f"{github_owner}/"
                f"{repo_name}"
            ),

        "status":
            "FAILED",

        "git":
            None,

        "issues":
            None,

        "pull_requests":
            None,

        "webhooks":
            None,

        "inventory":
            None,

        "errors":
            []
    }

    try:

        exists = github_repo_exists(
            github,
            github_owner,
            repo_name
        )

        if (
            exists
            and EXISTING_REPO_MODE
            == "skip"
        ):

            result[
                "status"
            ] = "SKIPPED_EXISTING"

            warn(
                "Destination already exists. "
                f"Skipped: "
                f"{github_owner}/"
                f"{repo_name}"
            )

            return result

        if not exists:

            create_github_repository(
                github,
                github_owner,
                repo,
                personal_account
            )

        else:

            log(
                "Using existing GitHub repo: "
                f"{github_owner}/"
                f"{repo_name}"
            )

        result[
            "git"
        ] = migrate_git_repository(
            workspace,
            repo_name,
            github_owner,
            bitbucket_token,
            github_token
        )

        if not SKIP_METADATA:

            result[
                "issues"
            ] = migrate_issues(
                bitbucket,
                github,
                workspace,
                repo_name,
                github_owner
            )

            result[
                "pull_requests"
            ] = migrate_pull_requests(
                bitbucket,
                github,
                workspace,
                repo_name,
                github_owner
            )

            result[
                "webhooks"
            ] = migrate_webhooks(
                bitbucket,
                github,
                workspace,
                repo_name,
                github_owner
            )

            result[
                "inventory"
            ] = collect_extra_inventory(
                bitbucket,
                workspace,
                repo_name
            )

        git_status = (
            result.get("git")
            or {}
        ).get("git")

        lfs_status = (
            result.get("git")
            or {}
        ).get("lfs")

        if git_status != "SUCCESS":

            result[
                "status"
            ] = "FAILED"

        elif lfs_status == "FAILED":

            result[
                "status"
            ] = "PARTIAL_SUCCESS"

            result[
                "errors"
            ].append(
                "Git migration succeeded "
                "but Git LFS migration failed."
            )

        else:

            result[
                "status"
            ] = "SUCCESS"

    except Exception as exc:

        result[
            "errors"
        ].append(
            str(exc)
        )

        result[
            "status"
        ] = "FAILED"

        warn(
            f"Repository "
            f"{workspace}/"
            f"{repo_name} failed: "
            f"{exc}"
        )

    return result



# MAIN


def main():

    check_requirements()

    bitbucket_token = (
        os.getenv(
            "BITBUCKET_TOKEN"
        )
    )

    github_token = (
        os.getenv(
            "GITHUB_TOKEN"
        )
    )

    if not bitbucket_token:

        fail(
            "BITBUCKET_TOKEN "
            "environment variable "
            "is missing."
        )

    if not github_token:

        fail(
            "GITHUB_TOKEN "
            "environment variable "
            "is missing."
        )

    bitbucket = (
        bitbucket_session(
            bitbucket_token
        )
    )

    github = (
        github_session(
            github_token
        )
    )

    
    # GITHUB AUTO DETECTION
    

    github_identity = (
        get_github_user(
            github
        )
    )

    github_authenticated_user = (
        github_identity[
            "login"
        ]
    )

    if GITHUB_OWNER_OVERRIDE:

        github_owner = (
            GITHUB_OWNER_OVERRIDE
        )

        personal_account = False

    else:

        github_owner = (
            github_authenticated_user
        )

        personal_account = True

    log(
        "GitHub account "
        "automatically detected: "
        f"{github_authenticated_user}"
    )

    log(
        "Migration destination: "
        f"{github_owner}"
    )

    
    # BITBUCKET WORKSPACES
    

    workspaces = (
        get_bitbucket_workspaces(
            bitbucket
        )
    )

    if ONLY_WORKSPACE:

        workspaces = [

            workspace

            for workspace in workspaces

            if workspace[
                "slug"
            ] == ONLY_WORKSPACE
        ]

    if not workspaces:

        fail(
            "No Bitbucket workspace "
            "discovered. Ensure token "
            "has read:workspace:bitbucket "
            "permission."
        )

    log(
        "Bitbucket workspaces discovered: "
        f"{len(workspaces)}"
    )

   
    # REPOSITORIES
   

    migration_plan = []

    for workspace in workspaces:

        workspace_slug = (
            workspace["slug"]
        )

        repos = (
            get_workspace_repositories(
                bitbucket,
                workspace_slug
            )
        )

        log(
            f"{workspace_slug}: "
            f"{len(repos)} "
            "repositories discovered"
        )

        for repo in repos:

            migration_plan.append({
                "workspace":
                    workspace_slug,

                "repo":
                    repo
            })

    log(
        "TOTAL repositories: "
        f"{len(migration_plan)}"
    )

   
    # DRY RUN
    

    if DRY_RUN:

        dry_report = {

            "mode":
                "DRY_RUN",

            "github_account":
                github_owner,

            "workspaces":
                workspaces,

            "repositories":
                [

                    {
                        "workspace":
                            item[
                                "workspace"
                            ],

                        "repository":
                            item[
                                "repo"
                            ][
                                "slug"
                            ]
                    }

                    for item in migration_plan
                ]
        }

        Path(
            REPORT_FILE
        ).write_text(
            json.dumps(
                dry_report,
                indent=2
            ),
            encoding="utf-8"
        )

        print(
            json.dumps(
                dry_report,
                indent=2
            )
        )

        return

    
    # ACTUAL MIGRATION
   

    results = []

    seen_repo_names = {}

    for index, item in enumerate(
        migration_plan,
        start=1
    ):

        workspace = (
            item["workspace"]
        )

        repo = (
            item["repo"]
        )

        repo_name = (
            repo["slug"]
        )

        log(
            f"[{index}/"
            f"{len(migration_plan)}] "
            f"{workspace}/"
            f"{repo_name}"
        )

        repo_key = (
            repo_name.lower()
        )

        if repo_key in seen_repo_names:

            results.append({

                "workspace":
                    workspace,

                "repository":
                    repo_name,

                "status":
                    "SKIPPED_NAME_COLLISION",

                "error":
                    (
                        "Same repository "
                        "name found in "
                        "multiple workspaces."
                    )
            })

            continue

        seen_repo_names[
            repo_key
        ] = workspace

        result = migrate_repository(

            bitbucket=
                bitbucket,

            github=
                github,

            workspace=
                workspace,

            repo=
                repo,

            github_owner=
                github_owner,

            personal_account=
                personal_account,

            bitbucket_token=
                bitbucket_token,

            github_token=
                github_token
        )

        results.append(
            result
        )

        log(
            f"{repo_name}: "
            f"{result['status']}"
        )

   
    # REPORT
    

    report = {

        "source":
            "Bitbucket Cloud",

        "destination":
            f"GitHub:{github_owner}",

        "github_user":
            github_authenticated_user,

        "workspaces_discovered":
            len(workspaces),

        "repositories_discovered":
            len(migration_plan),

        "repositories_successful":
            sum(
                1
                for result in results
                if result.get(
                    "status"
                ) == "SUCCESS"
            ),

        "repositories_partial":
            sum(
                1
                for result in results
                if result.get(
                    "status"
                ) == "PARTIAL_SUCCESS"
            ),

        "repositories_failed":
            sum(
                1
                for result in results
                if result.get(
                    "status"
                ) == "FAILED"
            ),

        "repositories_skipped":
            sum(
                1
                for result in results
                if str(
                    result.get(
                        "status",
                        ""
                    )
                ).startswith(
                    "SKIPPED"
                )
            ),

        "limitations": [

            (
                "Commit history, branches "
                "and tags are migrated "
                "using git mirror."
            ),

            (
                "LFS migration requires "
                "git-lfs and accessible "
                "Bitbucket LFS objects."
            ),

            (
                "Original Bitbucket issue "
                "and PR authors cannot "
                "become native GitHub authors."
            ),

            (
                "Original timestamps are "
                "preserved in issue/PR text."
            ),

            (
                "Historical merged or "
                "declined PRs are archived "
                "as GitHub issues."
            ),

            (
                "Bitbucket Issues API may "
                "be unavailable because "
                "the legacy Bitbucket "
                "Issues functionality "
                "has been deprecated."
            ),

            (
                "Bitbucket Pipelines are "
                "not automatically converted "
                "into GitHub Actions."
            ),

            (
                "Secret values cannot "
                "be exported and migrated "
                "automatically."
            ),

            (
                "Branch restrictions, teams "
                "and permissions require "
                "platform-specific mapping."
            )
        ],

        "results":
            results
    }

    Path(
        REPORT_FILE
    ).write_text(
        json.dumps(
            report,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        json.dumps(
            report,
            indent=2
        )
    )

    log(
        "Migration report: "
        f"{REPORT_FILE}"
    )

    if report[
        "repositories_failed"
    ]:

        sys.exit(2)

    if report[
        "repositories_partial"
    ]:

        sys.exit(3)


if __name__ == "__main__":
    main()
