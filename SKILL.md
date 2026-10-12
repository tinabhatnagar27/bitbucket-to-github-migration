---
name: bitbucket-github-migration
description: Migrate Bitbucket Cloud repositories to GitHub using the provided Python migration script.
---

# Bitbucket to GitHub Migration

Use the migration script available in this repository:

`migrate_bitbucket_to_github_full.py`

## Required Environment Variables

The following environment variables must be available:

- `BITBUCKET_TOKEN`
- `GITHUB_TOKEN`

Optional:

- `EXISTING_REPO_MODE=use`

Do not print or expose token values in logs or responses.

## Migration Workflow

1. Verify Python 3 is available.
2. Verify Git is available.
3. Verify Git LFS is available.
4. Verify the Python `requests` package is installed.
5. Validate the migration script syntax.
6. Run the migration script.
7. Allow the script to automatically discover accessible Bitbucket workspaces and repositories.
8. Allow the script to automatically detect the authenticated GitHub account.
9. Do not require a manually provided workspace name unless `ONLY_WORKSPACE` is intentionally configured.
10. After migration, read `migration-report.json`.
11. Summarize the migration result.

## Validation Commands

Run:

```bash
python3 --version
git --version
git lfs version
python3 -c "import requests; print(requests.__version__)"
