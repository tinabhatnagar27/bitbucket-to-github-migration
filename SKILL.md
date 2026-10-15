---
name: bitbucket-github-migration
description: Automatically discover and migrate all accessible Bitbucket Cloud Git repositories to GitHub using Git CLI, Git LFS, GitHub CLI and REST APIs without custom Python migration scripts.
---

# Automated Bitbucket to GitHub Migration

## Objective

Perform fully automated Bitbucket Cloud to GitHub
repository migration using AWS Bedrock AgentCore Harness.

The agent must automatically:

1. Prepare the migration environment.
2. Authenticate securely.
3. Discover all accessible Bitbucket workspaces.
4. Discover all accessible Git repositories.
5. Identify the authenticated GitHub account.
6. Generate destination repository mappings.
7. Create new private GitHub repositories.
8. Migrate Git history, branches and tags.
9. Migrate Git LFS objects.
10. Validate source and destination repositories.
11. Generate detailed migration reports.

Do not require manually entered repository names,
workspace names or destination repository names.

Do not execute custom Python migration scripts.

Use Shell, Git CLI, Git LFS, GitHub CLI and REST APIs.

## AWS AgentCore Configuration

Harness:
bitbucket_github_migration_07

AWS Region:
us-east-1

Secrets Manager:
vcs-migration/tokens

Credential identifiers:
BITBUCKET_TOKEN
GITHUB_TOKEN

Required execution capabilities:

- Built-in Shell tool
- Git CLI
- Git LFS
- GitHub CLI
- curl
- AWS CLI
- Network access to Bitbucket and GitHub
- Writable workspace
- Secure authentication mechanism

Use Shell for Git commands.

Code Interpreter must not be used as the
Git migration execution environment.

## Authorization and Scope

The user authorizes automatic discovery of all
Git repositories accessible through the configured
Bitbucket identity.

The user authorizes creation of new, empty,
private repositories under the authenticated
GitHub user account.

Do not require individual approval for each
new repository within this authorized scope.

Never overwrite or modify an existing
GitHub repository.

Never delete or modify Bitbucket source repositories.

Do not modify production systems, CI/CD,
webhooks, developer remotes or credentials.

Do not create repositories in GitHub organizations
unless separately authorized.

If a requested action exceeds this scope,
stop that action and report it.

## Step 1: Environment Preparation

Use the built-in Shell tool.

Check:

git --version
git lfs version
gh --version
curl --version
aws --version

Identify:

- Operating system
- CPU architecture
- Available disk space
- Writable directories
- Network connectivity

If Git LFS or GitHub CLI is missing:

1. Identify the required architecture.
2. Download an official compatible release.
3. Verify release checksums.
4. Install the required binary.
5. Verify the installed version.

Do not execute downloaded installation scripts.

Do not assume installed tools persist
across Harness sessions.

If installation fails, report BLOCKED.

## Step 2: Secure Authentication

Use AWS Secrets Manager:

Secret:
vcs-migration/tokens

Region:
us-east-1

Tasks:

1. Verify AWS execution identity.
2. Verify Secrets Manager access.
3. Access credentials securely through an
   approved credential mechanism.
4. Authenticate with Bitbucket Cloud.
5. Authenticate with GitHub.
6. Identify the authenticated GitHub owner.
7. Verify repository creation permissions.
8. Verify Git read and push permissions.

Authentication safety:

- Never print tokens.
- Never place tokens in command arguments.
- Never place tokens in Git remote URLs.
- Never expose Authorization headers.
- Never enable shell tracing with secrets.
- Never store credentials in reports.
- Never write tokens to persistent plaintext files.
- Do not pass credentials through visible tool output.

Use a secure credential broker or helper that
keeps credentials outside model-visible output.

IAM permission alone does not configure
Git or API authentication.

If secure authentication cannot be established,
stop and report BLOCKED.

## Step 3: Automatic Bitbucket Discovery

Discover all accessible Bitbucket Cloud workspaces.

Use supported Bitbucket REST APIs.

For every accessible workspace:

1. Enumerate all repositories.
2. Follow all pagination links.
3. Identify Git repositories.
4. Record repository names.
5. Record workspace names.
6. Record repository UUIDs.
7. Record repository visibility.
8. Identify the default branch.
9. Collect branch names.
10. Collect tag names.
11. Identify Git LFS usage.
12. Identify submodules.
13. Collect repository size where available.
14. Collect pull request counts where available.
15. Identify unsupported metadata.
16. Classify migration complexity.

Deduplicate repositories by stable source ID.

Track API errors and inaccessible repositories.

Do not silently skip repositories.

If discovery is incomplete, mark inventory
PARTIAL and disclose the limitation.

Display:

- Total workspaces discovered
- Total repositories discovered
- Total eligible Git repositories
- Total inaccessible repositories
- Repository inventory

Do not require manual workspace or repo names.

## Step 4: Automatic Destination Mapping

Identify the authenticated GitHub user.

For every eligible Bitbucket repository:

1. Preserve original repository name where possible.
2. Check if destination name already exists.
3. Detect naming collisions.
4. Generate deterministic unique names
   using the source workspace where needed.
5. Verify resulting names meet GitHub rules.
6. Preserve private visibility.
7. Record source-to-destination mapping.

If a destination already exists:

- Do not overwrite it.
- Do not force-push to it.
- Mark BLOCKED_EXISTING.
- Continue with other eligible repositories.

Record the mapping before migration.

Do not automatically target GitHub organizations.

## Step 5: Automated Repository Migration

Process eligible repositories sequentially
or in small controlled batches.

For every eligible repository:

### A. Prepare

1. Verify source accessibility.
2. Verify destination does not exist.
3. Verify available workspace capacity.
4. Record initial source references.
5. Identify LFS and submodule requirements.

### B. Create Destination

Create a new empty private GitHub repository.

Use GitHub CLI or REST API.

Do not initialize with:

- README
- .gitignore
- License

Verify the created destination.

If creation fails, record FAILED
and continue when safe.

### C. Mirror Clone

Run through Shell:

git clone --mirror SOURCE_REPO_URL

Use authenticated source access.

Verify successful clone.

Do not embed tokens in SOURCE_REPO_URL.

### D. Mirror Push

Configure an approved authenticated
destination remote.

For an empty approved destination:

git push --mirror DESTINATION_REMOTE

Verify the Git push succeeded.

Do not mirror-push to an existing repository.

Preserve:

- Git commit objects and history
- Branches
- Tags
- Required Git references

Verify user-facing branches and tags separately.

### E. Set Default Branch

Identify the Bitbucket default branch.

Set the matching branch as the GitHub
destination default branch.

Verify the setting through GitHub API.

### F. Cleanup

Clean temporary working files only after
required validation and reporting.

Never delete the source repository.

## Step 6: Git LFS Migration

Detect Git LFS usage.

If LFS is detected:

1. Identify required LFS pointers.
2. Verify Git LFS is installed.
3. Fetch all required LFS objects from Bitbucket.

Run:

git lfs fetch --all origin

Check command exit status.

4. Verify required LFS objects are available.
5. Push objects to GitHub:

git lfs push --all DESTINATION_REMOTE

6. Verify required objects can be fetched
   from the destination.

Record:

- Required LFS object count
- Available source objects
- Successfully validated destination objects
- Missing objects
- Failed uploads

If required LFS objects are unavailable,
mark the migration PARTIAL or FAILED.

Never declare LFS PASS from an upload
command alone without destination verification.

If Git LFS is absent from the repository,
record NOT APPLICABLE.

## Step 7: Migration Validation

Validate every migrated repository.

### Branch Validation

Collect:

git ls-remote --heads SOURCE_REPO_URL

git ls-remote --heads DESTINATION_REPO_URL

Compare:

- Branch count
- Branch names
- Branch HEAD commit hashes

### Tag Validation

Collect:

git ls-remote --tags SOURCE_REPO_URL

git ls-remote --tags DESTINATION_REPO_URL

Compare:

- Tag count
- Tag names
- Tag object hashes

Handle annotated-tag peeled references
correctly.

### Commit Validation

Verify:

- Default branch commit hash
- Matching branch HEAD commit hashes
- Git commit history
- Default branch commit count
- Required Git references

Use actual Git output.

Do not rely on counts alone.

### LFS Validation

Confirm that required LFS objects
are available in the GitHub destination.

### Additional Validation

Check:

- Repository exists
- Private visibility
- Default branch
- Git submodules if applicable
- Destination API accessibility

Report each item:

MATCH
MISMATCH
NOT APPLICABLE
NOT VERIFIED

Never fabricate validation results.

## Step 8: Metadata Assessment

Git mirror does not automatically migrate
all Bitbucket metadata.

Assess:

- Pull requests
- Issues
- Review comments
- Webhooks
- Branch restrictions
- CI/CD configuration
- Repository permissions
- Repository settings

Do not automatically migrate metadata
outside the authorized Git migration scope.

Record:

MIGRATED
NOT MIGRATED
NOT APPLICABLE
NOT VERIFIED

Include metadata gaps in the final report.

## Step 9: Error Handling

For each repository:

- Record discovery status.
- Record migration status.
- Record validation status.
- Record errors and warnings.
- Retry transient failures with limited attempts.
- Respect API rate limits.
- Do not blindly retry permission failures.
- Do not overwrite existing destinations.
- Do not delete failed destinations automatically.
- Do not delete source repositories.

Use statuses:

SUCCESS
PARTIAL
FAILED
BLOCKED
SKIPPED

Continue after isolated repository failures.

Stop the overall run on systemic security
or authentication failures.

## Step 10: Bulk Migration Checkpoints

Maintain a migration inventory containing:

- Source workspace
- Source repository UUID
- Destination mapping
- Discovery status
- Migration status
- Validation status
- Errors
- Timestamps

Use a configured durable checkpoint location
when available.

If durable storage is unavailable,
report that automatic resumption is unsupported.

Avoid duplicate destination creation
when retrying interrupted work.

Respect Harness execution time, storage
and model/tool invocation limits.

Do not claim unattended continuation
unless an external continuation mechanism
has actually been configured.

## Step 11: Final Migration Report

Generate:

migration-report.json
migration-report.md

Use only verified source and destination data.

### A. Migration Summary

Report:

TOTAL WORKSPACES DISCOVERED
TOTAL REPOSITORIES DISCOVERED
TOTAL ELIGIBLE REPOSITORIES
TOTAL SUCCESSFUL MIGRATIONS
TOTAL PARTIAL MIGRATIONS
TOTAL FAILED MIGRATIONS
TOTAL BLOCKED REPOSITORIES
TOTAL SKIPPED REPOSITORIES

### B. Source vs Destination Comparison

Create a table with:

- Source workspace
- Bitbucket repository name
- GitHub repository name
- Bitbucket branch count
- GitHub branch count
- Bitbucket tag count
- GitHub tag count
- Default branch
- Commit history validation
- Git LFS validation
- Metadata status
- Final status

### C. Branch Comparison

For every migrated repository:

Branch Name
Source Commit SHA
Destination Commit SHA
Validation Result

### D. Tag Comparison

For every migrated repository:

Tag Name
Source Tag SHA
Destination Tag SHA
Validation Result

### E. Git LFS Comparison

Show:

- Source required objects
- Destination verified objects
- Missing objects
- Failed transfers
- LFS validation status

### F. Migration Errors

Record all:

- Authentication errors
- API failures
- Git clone failures
- Push failures
- LFS errors
- Validation mismatches
- Naming conflicts
- Unsupported metadata

### G. Final Summary

Display:

Bitbucket Repositories:
GitHub Repositories Migrated:

Bitbucket Branches:
GitHub Branches:

Bitbucket Tags:
GitHub Tags:

Commit History:
MATCH / MISMATCH / NOT VERIFIED

Git LFS:
PASS / FAIL / NOT APPLICABLE / NOT VERIFIED

Migration Status:
SUCCESS / PARTIAL / FAILED / BLOCKED

Include created GitHub repository URLs.

Display the report in the Harness response.

Save reports only where a writable,
approved output location is available.

If report persistence is unavailable,
state the limitation.

## Step 12: Rollback and Recovery

- Preserve Bitbucket source repositories.
- Do not perform automatic destructive rollback.
- Do not delete GitHub repositories automatically.
- Keep source-to-destination mappings.
- Record incomplete migrations.
- Preserve failure evidence.
- Resume only when safe and supported.
- Never update production integrations
  without separate authorization.

## Security Rules

- No custom Python migration scripts.
- No Python-based migration orchestration.
- No manual repository name requirements.
- No manual workspace name requirements.
- No credential exposure.
- No overwriting existing GitHub repositories.
- No deletion of Bitbucket source repositories.
- No production cutover.
- No unapproved organization migration.
- No unsupported success claims.
- No silent skipping of repositories.

## Expected Result

Automatically discover all accessible
Bitbucket Cloud Git repositories.

Create approved new private GitHub destinations.

Migrate:

- Git history
- Branches
- Tags
- Required Git LFS objects

Validate every migration.

Generate a complete source-to-destination
comparison report showing:

- Total discovered repositories
- Total migrated repositories
- Branch and tag counts
- Commit SHA comparisons
- LFS results
- Metadata gaps
- Failed and skipped repositories
- Final migration status

Do not report full SUCCESS until all eligible
repositories have been processed and all
required validation checks have passed.
