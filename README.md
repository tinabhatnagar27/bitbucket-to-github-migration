# Bitbucket to GitHub Migration Script

This repository contains a Python-based migration utility for moving repositories from Bitbucket Cloud to GitHub.

The script is designed to reduce manual work during repository migration. It can discover Bitbucket workspaces and repositories, detect the authenticated GitHub account, create or reuse destination repositories, migrate Git history, branches, tags, and Git LFS objects, and generate a migration report.

## Features

- Automatically detects the authenticated GitHub account
- Discovers Bitbucket workspaces
- Discovers repositories inside the selected workspace
- Creates a GitHub repository if it does not already exist
- Can reuse an existing GitHub repository
- Migrates complete Git history using mirror clone and mirror push
- Migrates branches and tags
- Migrates Git LFS objects
- Attempts to migrate Pull Requests and comments where possible
- Attempts to migrate webhooks where possible
- Collects additional repository metadata
- Supports dry-run mode
- Generates a JSON migration report

## Prerequisites

Make sure the following are installed:

- Python 3
- Git
- Git LFS
- Python `requests` package

Install the Python dependency:

```bash
pip3 install requests
```

Initialize Git LFS:

```bash
git lfs install
```

## Authentication

The script reads authentication tokens from environment variables.

Set the Bitbucket token:

```bash
export BITBUCKET_TOKEN="your-bitbucket-token"
```

Set the GitHub token:

```bash
export GITHUB_TOKEN="your-github-token"
```

Do not store tokens directly inside the Python script or commit them to the repository.

## Optional Environment Variables

### Migrate only one Bitbucket workspace

```bash
export ONLY_WORKSPACE="your-workspace-name"
```

### Use an existing GitHub repository

```bash
export EXISTING_REPO_MODE="use"
```

The default behavior is to skip a repository if the destination repository already exists.

### Run in dry-run mode

```bash
export DRY_RUN="true"
```

Dry-run mode discovers the source workspaces and repositories and generates a report without performing the actual migration.

### Skip metadata migration

```bash
export SKIP_METADATA="true"
```

### Skip webhook migration

```bash
export SKIP_WEBHOOKS="true"
```

### Override the GitHub destination owner

```bash
export GITHUB_OWNER="your-github-user-or-organization"
```

### Change the report file name

```bash
export MIGRATION_REPORT="migration-report.json"
```

## Run the Script

```bash
python3 migrate_bitbucket_to_github_full.py
```

## Migration Flow

```text
Bitbucket Token + GitHub Token
            |
            v
Authenticate with Bitbucket and GitHub
            |
            v
Detect GitHub Account
            |
            v
Discover Bitbucket Workspaces
            |
            v
Discover Repositories
            |
            v
Check / Create GitHub Repository
            |
            v
Mirror Clone Bitbucket Repository
            |
            v
Fetch Git LFS Objects
            |
            v
Mirror Push Repository to GitHub
            |
            v
Push Git LFS Objects to GitHub
            |
            v
Migrate / Collect Supported Metadata
            |
            v
Generate migration-report.json
```

## What Gets Migrated

The core Git migration includes:

- Commit history
- Branches
- Tags
- Git references
- Git LFS objects

The script also attempts to process additional repository data such as:

- Pull Requests
- Pull Request comments
- Issues and comments, where the Bitbucket API is available
- Webhooks
- Branch restrictions
- Pipeline variables
- Deploy keys
- Downloads metadata

## Git LFS

Git LFS objects are handled separately from normal Git objects.

The script first fetches all LFS objects from Bitbucket:

```bash
git lfs fetch --all origin
```

It then pushes them to GitHub:

```bash
git lfs push --all github
```

This ensures that large files tracked using Git LFS are also migrated.

## Migration Report

After execution, the script generates a JSON report.

Default file:

```text
migration-report.json
```

The report includes:

- Source platform
- Destination account
- Workspaces discovered
- Repositories discovered
- Successful migrations
- Partial migrations
- Failed migrations
- Skipped repositories
- Git migration status
- Git LFS fetch and push status
- Pull Request migration details
- Webhook migration details
- Additional repository inventory
- Errors and platform limitations

## Migration Status

A repository can have one of the following final statuses:

### SUCCESS

Git migration completed successfully and Git LFS migration also completed successfully.

### PARTIAL_SUCCESS

Git migration succeeded, but Git LFS migration failed.

### FAILED

The main repository migration failed.

### SKIPPED_EXISTING

The destination repository already exists and `EXISTING_REPO_MODE` is set to its default `skip` behavior.

## Known Limitations

Some Bitbucket and GitHub features cannot be migrated exactly because both platforms use different APIs and data models.

Current limitations include:

- Original Bitbucket issue and Pull Request authors cannot be recreated as native GitHub authors
- Original issue and Pull Request timestamps may be preserved in migrated text instead of native GitHub timestamps
- Historical merged or declined Pull Requests may be archived as GitHub issues instead of being recreated exactly
- Bitbucket Pipelines are not automatically converted to GitHub Actions
- Secret variable values cannot be exported and migrated automatically
- Branch restrictions, teams, and repository permissions may require platform-specific mapping
- The legacy Bitbucket Issues API may be unavailable
- Some Bitbucket APIs may depend on the workspace plan

## Security

Never commit credentials or tokens to GitHub.

Recommended `.gitignore`:

```gitignore
__pycache__/
*.pyc
migration-report.json
.env
```

Tokens should always be provided through environment variables or a secure secret-management system.

## POC Scope

The current proof of concept validates the following migration flow:

- Bitbucket workspace discovery
- Repository discovery
- GitHub account auto-detection
- GitHub repository creation or reuse
- Complete Git history migration
- Branch migration
- Tag migration
- Git LFS migration
- Migration report generation

The script can later be integrated with an orchestration or CI/CD platform such as Harness for controlled and repeatable repository migrations.
