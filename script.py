#!/usr/bin/env python3
"""
Bitbucket Cloud to GitHub migration POC.

Automatic:
- Workspace discovery
- Project discovery (including empty projects)
- GitHub Projects V2 creation using original project names
- Repository discovery and private repository creation
- Git commits, branches, tags and Git LFS
- Existing repository handling without overwriting Git history
- Project to repository linking
- Historical sample Issues from migration-test-data/issues.json
- Pull Requests and comments
- Migration reporting

Credentials:
BITBUCKET_TOKEN and GITHUB_TOKEN environment variables,
or AWS Secrets Manager secret vcs-migration/tokens.

Usage:
python3 migrate_bitbucket_to_github_full.py

Optional:
DRY_RUN=true python3 migrate_bitbucket_to_github_full.py
"""

import json
import os
import re
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
    "DRY_RUN", "false"
).lower() in ("true", "1", "yes")

OWNER_OVERRIDE = os.getenv("GITHUB_OWNER", "")
REPORT_FILE = Path("migration-report.json")


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
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        env={**os.environ, **(env or {})},
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


def migrate_pull_requests(bb, gh, workspace, repo, base):
    stats = {
        "found": 0,
        "created_open": 0,
        "archived": 0,
        "existing": 0,
        "comments": 0,
        "errors": []
    }

    pull_requests = {}

    for state in (
        "OPEN", "MERGED", "DECLINED", "SUPERSEDED"
    ):
        try:
            rows = bb_pages(
                bb,
                f"{BB}/repositories/{workspace}/{repo}/pullrequests",
                {"state": state, "pagelen": 50}
            )

            for item in rows:
                pull_requests[item["id"]] = item

        except Exception as exc:
            stats["errors"].append(
                f"PR discovery {state}: {exc}"
            )

    stats["found"] = len(pull_requests)

    for pr in pull_requests.values():
        marker = (
            f"POC-SOURCE-PR-ID: "
            f"{workspace}/{repo}/{pr['id']}"
        )

        try:
            if find_marker(gh, base, marker):
                stats["existing"] += 1
                continue

            state = pr.get("state", "").upper()

            head = (
                (pr.get("source") or {})
                .get("branch") or {}
            ).get("name")

            target = (
                (pr.get("destination") or {})
                .get("branch") or {}
            ).get("name")

            author = (
                pr.get("author") or {}
            ).get("display_name", "Unknown")

            body = (
                "Migrated Bitbucket Pull Request\n\n"
                f"{marker}\n"
                f"Original state: {state}\n"
                f"Original author: {author}\n"
                f"Created: {pr.get('created_on')}\n"
                f"Head: {head}\n"
                f"Base: {target}\n\n"
                f"{pr.get('description') or ''}"
            )

            created = None

            if state == "OPEN" and head and target:
                try:
                    created = api(
                        gh, "POST", base + "/pulls",
                        expected=(201,),
                        json={
                            "title": pr.get(
                                "title", "Migrated PR"
                            ),
                            "head": head,
                            "base": target,
                            "body": body
                        }
                    )
                    stats["created_open"] += 1
                except Exception as exc:
                    stats["errors"].append(
                        f"Native PR #{pr['id']}: {exc}"
                    )

            if created is None:
                created = api(
                    gh, "POST", base + "/issues",
                    expected=(201,),
                    json={
                        "title": (
                            f"[Archived Bitbucket PR #{pr['id']}] "
                            f"{pr.get('title', 'PR')}"
                        ),
                        "body": body,
                        "labels": ["archived-bitbucket-pr"]
                    }
                )

                api(
                    gh, "PATCH",
                    f"{base}/issues/{created['number']}",
                    json={"state": "closed"}
                )

                stats["archived"] += 1

            number = created["number"]

            comments = bb_pages(
                bb,
                f"{BB}/repositories/{workspace}/{repo}"
                f"/pullrequests/{pr['id']}/comments",
                {"pagelen": 50}
            )

            for comment in comments:
                if comment.get("deleted"):
                    continue

                comment_author = (
                    comment.get("user") or {}
                ).get("display_name", "Unknown")

                api(
                    gh, "POST",
                    f"{base}/issues/{number}/comments",
                    expected=(201,),
                    json={
                        "body": (
                            f"Bitbucket comment by {comment_author}"
                            f" ({comment.get('created_on')}):\n\n"
                            f"{content_text(comment)}"
                        )
                    }
                )

                stats["comments"] += 1

        except Exception as exc:
            stats["errors"].append(
                f"PR #{pr.get('id')}: {exc}"
            )

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

        save_report(report)
        print(json.dumps(report["plan"], indent=2))
        return

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
                result["git"] = {
                    "git": "ALREADY_EXISTS_NOT_REPUSHED"
                }

            else:
                raise RuntimeError(
                    "GitHub repository lookup failed: "
                    f"HTTP {response.status_code}"
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
                    bb, gh, workspace, source, base
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


if __name__ == "__main__":
    main()
