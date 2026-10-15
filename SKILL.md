
---
name: bitbucket-github-migration
description: Migrate Bitbucket Cloud Git repositories to GitHub using Git CLI, Git LFS, GitHub CLI and REST APIs without executing a custom migration script.
---

# Bitbucket to GitHub Migration

## Objective

Perform agent-driven repository migration from
Bitbucket Cloud to GitHub.

Do not use any Python migration script.

The AI agent must perform discovery, migration,
validation and reporting directly through available
shell tools, Git commands and REST APIs.

## Required Tools

- Git CLI
- Git LFS
- GitHub CLI (gh)
- Shell execution capability
- HTTP/API access
- Secure credential access

## Authentication

Required credentials:

- BITBUCKET_TOKEN
- GITHUB_TOKEN

Never print, log or expose credentials.

Never place tokens inside Git remote URLs,
command-line arguments or migration reports.

Use a secure credential helper or approved
authentication mechanism.

## Step 1: Preflight Checks

Verify the availability of:

- git
- git-lfs
- gh
- curl

Verify Bitbucket read access.

Verify GitHub authentication and repository
creation permissions.

Stop if authentication or required tools fail.

## Step 2: Repository Discovery

Use Bitbucket Cloud REST APIs to:

1. Discover accessible workspaces.
2. List repositories for the approved workspace.
3. Record repository names and clone URLs.
4. Identify default branches.
5. Identify branches and tags.
6. Detect Git LFS and submodules.
7. Record repository metadata.

Respect pagination and API rate limits.

Do not migrate all discovered repositories
without explicit user approval.

## Step 3: Migration Planning

For each repository:

1. Confirm the source repository.
2. Confirm the GitHub owner.
3. Check if the destination exists.
4. Classify migration complexity.
5. Identify unsupported metadata.
6. Prepare a migration plan.

Present the plan and request approval before
creating or modifying destination repositories.

## Step 4: Create GitHub Repository

Use GitHub CLI or GitHub REST API.

Create an empty private repository unless
the user explicitly approves another visibility.

Do not initialize with a README or license.

Never overwrite an existing repository
without explicit approval.

## Step 5: Git Repository Migration

Use Git CLI through the shell execution tool.

Mirror-clone the source repository:

git clone --mirror SOURCE_REPO_URL

Move into the mirrored repository directory.

Push the Git references:

git push --mirror DESTINATION_REPO_URL

The agent must replace placeholders with
approved repository locations.

Use an approved credential mechanism.

Do not run custom migration scripts.

## Step 6: Git LFS Migration

If the repository uses Git LFS:

1. Verify Git LFS installation.
2. Fetch all required LFS objects from Bitbucket.
3. Verify the fetch succeeded.
4. Push all LFS objects to GitHub.
5. Record missing or failed LFS objects.

Do not declare success if required LFS
objects are missing.

## Step 7: Metadata Migration

Identify and report:

- Pull requests
- Issues
- Repository permissions
- Webhooks
- Branch protection rules
- CI/CD configuration
- Repository settings

Git mirror operations do not migrate these.

Use supported APIs only for metadata types
explicitly included in the approved scope.

Report unsupported items separately.

## Step 8: Migration Validation

Verify:

1. Source and destination branches.
2. Source and destination tags.
3. Commit history and Git references.
4. Default branch configuration.
5. Git LFS objects, if applicable.
6. Repository accessibility.
7. Supported metadata, if included.

Compare source and destination Git references
using appropriate Git commands.

Do not mark the migration successful until
the required validation checks pass.

## Step 9: Migration Report

Generate a migration report containing:

- Source workspace
- Source repository
- Destination repository
- Migration start and completion status
- Branch validation
- Tag validation
- Commit reference validation
- Git LFS validation
- Metadata coverage
- Errors and warnings
- Rollback procedure
- Final migration status

Use a structured Markdown or JSON report.

## Safety Rules

- Never delete the Bitbucket source repository.
- Never expose tokens or secrets.
- Never overwrite repositories without approval.
- Never force-push to an existing destination
  without explicit authorization.
- Never migrate unapproved repositories.
- Stop on authentication or critical errors.
- Keep rollback instructions.
- Report incomplete migrations honestly.
- Do not invoke migrate_bitbucket_to_github_full.py.
- Do not invoke any other custom Python migration
  script to perform migration.

## Expected Result

A validated GitHub repository containing the
approved Git history, branches, tags and LFS
objects where applicable.

Produce a migration report including any
metadata that was not migrated.
