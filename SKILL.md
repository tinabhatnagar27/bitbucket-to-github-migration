
---
name: bitbucket-github-migration
description: Migrate Bitbucket Cloud Git repositories to GitHub using Git CLI, Git LFS, GitHub CLI and REST APIs without custom Python migration scripts.
---

# Bitbucket to GitHub Migration

## Objective

Perform secure, agent-driven repository migration
from Bitbucket Cloud to GitHub using AWS Bedrock
AgentCore Harness.

The agent must perform:

1. Repository discovery
2. Repository classification
3. Migration planning
4. Git migration
5. Git LFS migration
6. Migration validation
7. Reporting and rollback preparation

Do not execute any Python migration script.

Use Git CLI, Git LFS, GitHub CLI, shell tools
and REST APIs directly.

## Required Tools

- Git CLI
- Git LFS
- GitHub CLI (gh)
- curl
- Shell command execution capability
- Network access to Bitbucket and GitHub
- Secure credential access
- Writable temporary workspace

If any required capability is unavailable,
stop and report the issue.

## AWS AgentCore Harness Configuration

Harness Name:
bitbucket_github_migration_07

AWS Region:
us-east-1

AWS Secrets Manager Secret:
vcs-migration/tokens

The Harness must have:

- Access to the attached SKILL.md
- Secure Git command execution capability
- Network connectivity to source and destination
- An approved authentication mechanism
- Appropriate IAM permissions

Do not assume AgentCore Code Interpreter and
shell execution tools provide identical capabilities.

Do not execute custom Python migration scripts.

## Authentication

Required credentials:

- BITBUCKET_TOKEN
- GITHUB_TOKEN

Credentials are stored in AWS Secrets Manager.

Secret Name:
vcs-migration/tokens

Region:
us-east-1

### Authentication Rules

1. Verify the execution identity has permission
   to access the required secret.

2. Use an approved secure credential mechanism
   to access Bitbucket and GitHub.

3. Never print, log or expose secret values.

4. Never place credentials in Git remote URLs,
   shell command arguments or reports.

5. Do not expose credentials through debugging
   commands or verbose logging.

6. Do not assume IAM permission automatically
   injects credentials into the agent environment.

7. Do not attempt migration if authentication fails.

8. Use only the minimum permissions required.

## Step 1: Preflight Checks

Verify tool availability:

git --version
git lfs version
gh --version
curl --version

Verify:

- Bitbucket API connectivity
- GitHub API connectivity
- Bitbucket read access
- GitHub repository creation permission
- Git push permission
- AWS Secrets Manager access
- Temporary workspace availability

Check authentication securely without
displaying credentials.

If any critical check fails, stop.

## Step 2: Repository Discovery

Use Bitbucket Cloud REST APIs.

Tasks:

1. Discover accessible Bitbucket workspaces.
2. Select only the approved workspace.
3. List repositories in that workspace.
4. Handle API pagination.
5. Identify source clone URLs.
6. Identify default branches.
7. Collect branch names and tags.
8. Identify Git LFS usage.
9. Identify Git submodules.
10. Record repository metadata.
11. Identify archived or inactive repositories.
12. Classify migration complexity.

Do not automatically migrate all discovered
repositories.

Do not access unapproved repositories.

## Step 3: Migration Planning

For each approved repository:

1. Confirm Bitbucket workspace.
2. Confirm Bitbucket repository name.
3. Confirm destination GitHub owner.
4. Confirm destination repository name.
5. Check destination existence.
6. Identify Git history and refs.
7. Identify LFS and submodules.
8. Identify metadata requiring API migration.
9. Assess migration risks.
10. Prepare rollback instructions.

Present the migration plan before execution.

Require explicit approval before creating
or modifying GitHub repositories.

Do not overwrite existing repositories
without explicit authorization.

## Step 4: Create GitHub Repository

Use GitHub CLI or GitHub REST API.

Create an empty private repository unless
the user explicitly approves public visibility.

Example:

gh repo create OWNER/REPOSITORY --private

Do not initialize the repository with a
README, .gitignore or license.

Verify repository creation succeeded.

If the destination already exists,
stop and request approval.

## Step 5: Git Repository Migration

Use Git CLI through the approved shell tool.

Mirror-clone the Bitbucket repository:

git clone --mirror SOURCE_REPO_URL

Enter the created mirrored repository directory.

Verify the mirror clone succeeded.

Push Git references to GitHub:

git push --mirror DESTINATION_REPO_URL

Replace placeholders with approved URLs.

Use the configured secure credential helper.

Do not embed authentication tokens in URLs.

Do not push to an existing destination
without explicit authorization.

Do not delete the Bitbucket source repository.

Do not execute any Python migration script.

## Step 6: Git LFS Migration

If the source repository contains Git LFS
objects, migrate those objects separately.

From the mirrored repository:

git lfs fetch --all origin

Verify all required LFS objects were fetched.

Push LFS objects using an authenticated,
configured destination remote:

git lfs push --all DESTINATION_REMOTE

Do not use literal DESTINATION_REMOTE;
replace it with a configured Git remote name
or an approved destination URL.

Verify Git LFS objects are accessible
from the destination.

If LFS download or upload fails:

- Stop or mark migration incomplete
- Record errors
- Do not report successful validation
- Do not perform production cutover

## Step 7: Metadata Migration

Git mirror operations preserve Git references,
but do not automatically migrate platform metadata.

Identify:

- Pull requests
- Issues
- Review comments
- Repository permissions
- Webhooks
- Branch protection rules
- CI/CD configuration
- Repository settings

Use supported REST APIs only when the
metadata type is explicitly approved
for migration.

Otherwise document the metadata gap.

Do not claim that unsupported metadata
was successfully migrated.

## Step 8: Migration Validation

Verify source and destination repositories.

### Branch Validation

Compare source and destination branch names.

Run through the approved authenticated
Git execution mechanism:

git ls-remote --heads SOURCE_REPO_URL

git ls-remote --heads DESTINATION_REPO_URL

Compare branch names and commit hashes.

### Tag Validation

git ls-remote --tags SOURCE_REPO_URL

git ls-remote --tags DESTINATION_REPO_URL

Compare tag references and hashes.

Account for annotated tag peeled references.

### Commit History Validation

Verify:

- Commit IDs for matching Git refs
- Branch heads
- Tag objects
- Repository history
- Default branch settings

### Git LFS Validation

If applicable:

- Confirm successful LFS upload
- Verify required objects exist on GitHub
- Verify representative LFS files can be fetched
- Record missing objects

### Metadata Validation

If metadata migration was approved:

- Verify supported metadata on GitHub
- Record unsupported metadata
- Identify incomplete API migrations

Mark migration successful only when
all required validation checks pass.

## Step 9: Migration Report

Generate a Markdown or JSON report.

Include:

- Source workspace
- Source repository
- Destination GitHub repository
- Migration timestamp
- Migration status
- Branch validation results
- Tag validation results
- Git history validation
- Git LFS status
- Metadata migration status
- Errors and warnings
- Migration complexity
- Rollback instructions
- Final acceptance status

Possible statuses:

SUCCESS
PARTIAL
FAILED
BLOCKED

Never mark a repository SUCCESS if
required Git or LFS validation fails.

## Step 10: Rollback Strategy

1. Keep the Bitbucket source repository intact.
2. Do not delete source repositories.
3. Record destination state before cutover.
4. Do not update CI/CD integrations without approval.
5. Do not modify developer Git remotes
   without approval.
6. If validation fails, stop the migration workflow.
7. Preserve failure evidence.
8. Request approval before destructive rollback.
9. Never automatically delete GitHub repositories.

## Security and Safety Rules

- Never expose Bitbucket or GitHub tokens.
- Never store credentials in migration reports.
- Never log secrets.
- Never migrate unapproved repositories.
- Never overwrite repositories without approval.
- Never force-push to existing destinations
  without explicit authorization.
- Never delete the original Bitbucket repository.
- Stop on critical authentication failures.
- Validate every migrated repository.
- Generate an auditable migration report.
- Follow least-privilege IAM permissions.
- Do not execute migrate_bitbucket_to_github_full.py.
- Do not execute other custom migration scripts.
- Do not use Python to orchestrate migration.

## Expected Result

A GitHub repository containing:

- Complete approved Git history
- All required branches
- All required tags
- Git LFS objects where applicable
- Correct default branch configuration
- Approved migrated metadata, if supported

The migration must produce a validation report
and identify any unsupported metadata or
remaining manual actions.
