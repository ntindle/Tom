# Maintaining this fork

This is the operating manual for `ntindle/autogpt`. It assumes you have never
seen the repository. You need:

- the [`gh`](https://cli.github.com/) CLI, signed in as an owner of the repository;
- `git`;
- Python 3.11 or newer, to run the scripts in `scripts/`;
- [`uv`](https://docs.astral.sh/uv/) and Node 24, to run the desktop tests
  when you resolve a blocked sync by hand, and to test changes to the
  automation.

All commands use `ntindle/autogpt`; change the name if the repository moves.

## Branch model

| Branch | Contents | Who writes to it |
| --- | --- | --- |
| `main` (default) | Only fork-owned files: this manual, the README, `.github/` and `scripts/`. It is an orphan branch with no upstream history. | People, through pull requests; Dependabot for action pins. |
| `desktop` | Upstream `Significant-Gravitas/AutoGPT` `dev`, plus the desktop distribution in `autogpt_platform/desktop/` and one caller workflow, `.github/workflows/platform-desktop-build.yml`. | The daily sync, and people working on the desktop app. |

Why two branches:

- Scheduled workflows, manually dispatched workflows and Dependabot read only
  the default branch. Upstream has about 36 workflows, eight of them on a
  schedule, and a `dependabot.yml` with five ecosystems. With upstream's tree
  off the default branch, none of that runs here, and no upstream file had to
  be edited or deleted to achieve it. Editing upstream files would create merge
  conflicts for ever.
- All of the fork's workflow logic is on `main` and checks out `desktop`
  explicitly. `desktop` stays as close to upstream as possible, which keeps
  the daily merge clean.

`desktop` is merged, never rebased: released commits keep their SHAs, nothing
is force-pushed, and `git diff upstream/dev...desktop` is always exactly what
the fork adds.

### Branch names that must never be pushed here

`dev`, `master`, `release-*`, `ci-test*`.

Upstream's workflows trigger on pushes to branches with those names. A branch
carries its own copy of the workflow files, so pushing upstream's tree under
one of those names starts upstream's CI, image builds and deploy jobs in this
repository. A `dev` branch is also what upstream's Dependabot configuration
targets. Check that none exists:

```bash
gh api repos/ntindle/autogpt/branches --paginate -q '.[].name' | grep -E '^(dev|master|release-.*|ci-test.*)$' || echo "none"
```

When you clone upstream locally, fetch its `dev` as a remote-tracking branch
(`upstream/dev`) and never create a local `dev` that tracks the fork.

Branch names are not the only push trigger. One upstream workflow,
`copilot-setup-steps.yml`, runs on a push to any branch that changes that
file. See [What a sync push can start](#what-a-sync-push-can-start).

## What runs, and when

| Workflow (on `main`) | Trigger | What it does |
| --- | --- | --- |
| `sync-upstream.yml` | Daily at 05:23 UTC, or by hand | Merges upstream `dev` into `desktop` if the merge is clean and the tests pass; otherwise reports in the `upstream-sync` issue. |
| `desktop-build.yml` | Never by itself. It is started (1) by the sync after a push, (2) by hand, (3) by `desktop-nightly.yml`, (4) by the caller workflow on `desktop` for pull requests into `desktop` and for pushes to `desktop` that change the desktop app's files, and (5) by `desktop-release.yml`. | Builds and tests the installers from the commit it is given. Only (5) signs or names a version; every other build is `0.0.0-dev.<run number>`, which never looks for updates. |
| `desktop-release.yml` | By hand only | Builds a commit of `desktop` with a version, signs it with whatever certificates the `desktop-release` environment holds, runs the installed-app tests, and publishes a GitHub Release. See [Cutting a release](#cutting-a-release). |
| `desktop-nightly.yml` | Daily at 04:23 UTC | Calls `desktop-build.yml` for `desktop` with the installed-app tests switched on: the installers are installed, run, upgraded and removed on fresh Windows, macOS and Linux machines. This is the most expensive job in the repository. |
| `build-report.yml` | When a build of `desktop` finishes | Puts a failed build into the `upstream-sync` issue and closes it again after a green one. |
| `main-checks.yml` | Pull requests and pushes to `main` | Tests `scripts/` and lints the workflows. |
| Dependabot | Weekly | Action pins on `main`; npm packages of the Electron shell on `desktop`. |

On `desktop` there is one fork-owned workflow,
`platform-desktop-build.yml` ("AutoGPT Platform - Desktop build"). It only
calls `desktop-build.yml` on `main`. Every other workflow file on `desktop` is
upstream's and must be disabled here.

A sync push that changes one of the files the caller watches starts the build
through the caller. The sync notices that run and does not start a second one.

## Enabling Actions the first time

GitHub Actions starts out disabled for this repository. Enabling it starts
nothing by itself, because nothing of upstream's is on the default branch. Do
the steps in this order, so that upstream's workflows are disabled before the
first push or pull request into `desktop`, or the first release, could start
them.

1. Check the default branch, and clean up the branch list.

   ```bash
   gh api repos/ntindle/autogpt -q .default_branch        # must print: main
   gh api repos/ntindle/autogpt/branches --paginate -q '.[].name'
   gh pr list --repo ntindle/autogpt --state open --json number,headRefName,baseRefName
   ```

   The branch list must contain only `main`, `desktop` and branches you
   created yourself. Stop if the default branch is not `main`: change it first
   (`gh repo edit ntindle/autogpt --default-branch main`), or Dependabot keeps
   recreating what you delete. Then, before step 3:

   - Close every pull request that Dependabot opened against upstream's code,
     together with its branch: `gh pr close <number> --repo ntindle/autogpt --delete-branch`.
   - Delete every remaining `dependabot/*` branch, and any branch named `dev`,
     `master`, `release-*` or `ci-test*`:
     `gh api -X DELETE repos/ntindle/autogpt/git/refs/heads/<name>`.

2. Create the `sync` environment, restrict it to `main`, and store the token
   in it (see [The sync token](#the-sync-token) for how to create the token
   and why it must not be a repository secret).

   ```bash
   gh api -X PUT repos/ntindle/autogpt/environments/sync \
     -F 'deployment_branch_policy[protected_branches]=false' \
     -F 'deployment_branch_policy[custom_branch_policies]=true'
   gh api -X POST repos/ntindle/autogpt/environments/sync/deployment-branch-policies \
     -f name=main -f type=branch
   gh api repos/ntindle/autogpt/environments/sync/deployment-branch-policies \
     -q '.branch_policies[] | .type + ":" + .name'      # must print exactly: branch:main
   gh secret set FORK_SYNC_TOKEN --env sync --repo ntindle/autogpt   # paste the token at the prompt
   ```

3. Enable Actions for a short list of actions only, and make the default
   workflow token read-only.

   ```bash
   gh api -X PUT repos/ntindle/autogpt/actions/permissions -F enabled=true -f allowed_actions=selected
   gh api -X PUT repos/ntindle/autogpt/actions/permissions/selected-actions \
     -F github_owned_allowed=true -F verified_allowed=false \
     -f 'patterns_allowed[]=astral-sh/setup-uv@*' \
     -f 'patterns_allowed[]=pnpm/action-setup@*' \
     -f 'patterns_allowed[]=msys2/setup-msys2@*'
   gh api -X PUT repos/ntindle/autogpt/actions/permissions/workflow \
     -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
   ```

   The three patterns are the actions the workflows on `main` use besides
   GitHub's own (`actions/*`). An upstream workflow that slips through and
   uses any other action then fails when it starts. When a workflow on `main`
   needs a new third-party action, add its pattern here; `main-checks.yml`
   fails until this list names it.

4. Disable upstream's workflows. Run this from a checkout of `main`; the list
   of workflows to keep is read from that checkout's `.github/workflows`.

   ```bash
   git clone --branch main --single-branch https://github.com/ntindle/autogpt.git autogpt-main
   cd autogpt-main
   python scripts/disable_upstream_workflows.py --repo ntindle/autogpt --dry-run
   python scripts/disable_upstream_workflows.py --repo ntindle/autogpt
   ```

   The script uses `GH_TOKEN` or `GITHUB_TOKEN` if set, otherwise the token
   `gh` is signed in with. It prints what it disabled. It is safe to run again.

   GitHub may not list a workflow that exists only on `desktop` until that
   workflow has run once. If the dry run lists nothing to disable, that is the
   reason, not an error. The sync runs the same script every day and again
   after every push, so a workflow that appears later is disabled then. That
   is why the script is a backstop and the branch layout is the real
   protection.

5. Run the sync once by hand and watch it. A run takes a few seconds to
   appear in the list, hence the `sleep`.

   ```bash
   gh workflow run sync-upstream.yml --repo ntindle/autogpt
   sleep 10
   gh run list --repo ntindle/autogpt --workflow sync-upstream.yml --limit 1   # must show a run that is queued or in progress
   gh run watch --repo ntindle/autogpt "$(gh run list --repo ntindle/autogpt --workflow sync-upstream.yml --limit 1 --json databaseId -q '.[0].databaseId')"
   ```

6. Confirm that only fork-owned workflows are active.

   ```bash
   gh api repos/ntindle/autogpt/actions/workflows --paginate -q '.workflows[] | .state + "  " + .path'
   ```

   Expected active entries: the files in `main`'s `.github/workflows`
   (`sync-upstream.yml`, `desktop-build.yml`, `desktop-nightly.yml`,
   `desktop-release.yml`, `build-report.yml`, `main-checks.yml`),
   `.github/workflows/platform-desktop-build.yml`, and entries whose path
   starts with `dynamic/` (GitHub's own, such as Dependabot updates).

## How the sync works

`sync-upstream.yml` has six jobs. They are separate machines on purpose: the
one that runs upstream's code holds no credential, and the one that holds the
push token runs none of upstream's code.

| Job | What it does | What it may do |
| --- | --- | --- |
| Keep-alive and workflow check | Disables every workflow that is not the fork's and re-enables the fork's own (see [If scheduled runs stop](#if-scheduled-runs-stop)). Fails if `FORK_SYNC_TOKEN` is stored as a repository secret. | Actions: write |
| Token check | Looks for `FORK_SYNC_TOKEN` in the `sync` environment. If it is missing, the run ends green with a notice and nothing else happens. If it is there, the job fails unless the environment admits the branch `main` and nothing else. | Reads the environment |
| Merge | Fetches upstream `dev` and merges it into `desktop` with `git merge --no-ff`. Records the two commits and the resulting tree. Reports "already up to date" or the conflicted files. Pushes nothing. | Read-only |
| Tests on the merged tree | Repeats the merge and runs the gate: `node --test "test/*.test.js"` in `autogpt_platform/desktop`, and the pytest suite in `autogpt_platform/desktop/runtime`. These are the tests that turn red when upstream changes something the desktop app depends on. | Read-only. No secret, no cache. |
| Push and start the build | Runs only when the tests passed. On a fresh machine, repeats the merge from the commits the Merge job recorded and refuses to go on unless the tree is the one that was tested. Pushes it to `desktop` with `FORK_SYNC_TOKEN`. Disables upstream's workflows again, cancels any run they started for the pushed commit, and starts `desktop-build.yml` unless the push already started a build. | The `sync` environment; Actions: write |
| Update the sync issue | Always runs. Opens, updates or closes the `upstream-sync` issue. | Issues: write; Actions: write |

There is never more than one open issue from the sync. Its description always
shows the latest state; a comment is added only when the state changes. The
workflows recognise their own issue by a hidden marker in the description and
leave every other issue alone, including one a person opened with the same
label. Do not edit the description of the bot's issue; comment instead.

A blocked sync also makes the run red in the Actions list. Only the
missing-token case ends green.

If `desktop` is pushed but the build cannot be started, the issue records that
a build is owed, and the next run starts it before it closes the issue.

The gate is unit tests only. The three-platform build runs after the push, so
`desktop` is not guaranteed to be releasable at every commit. A failed build
is reported in the same issue by `build-report.yml`. Release only from a
commit whose build run is green.

### What a sync push can start

The push to `desktop` is made with a personal access token, and such a push
does start `on: push` workflows (a push made with the workflow's own token
would not). Two things can start:

- The fork's caller workflow, when the merge changed a file it watches. That
  is the installer build, and it is wanted.
- An upstream workflow whose push trigger has no branch filter. Today that is
  only `copilot-setup-steps.yml`, which runs when a push changes that file
  itself; upstream edits it now and then. A workflow that upstream adds later
  could do the same on its first sync. GitHub may not list such a workflow
  before its first run, so it cannot always be disabled in advance.

For the second case, right after the push the sync disables every workflow
that is not the fork's, watches for a minute for runs that upstream's
workflows started for the pushed commit, cancels them, and disables their
workflows. If you see a cancelled run of an upstream workflow on a sync
commit, this is what happened, and it will not start again.

Such a run cannot read `FORK_SYNC_TOKEN`: the token is in the `sync`
environment, which only `main` may use. Its own token is read-only (step 3 of
the setup), and it can only use GitHub's own actions and the three listed
there.

## When the `upstream-sync` issue is open

The issue says which of the four cases below applies. In the first three
`desktop` keeps working at its last good commit; the only cost of waiting is
that upstream's newer changes are not in it yet.

The commands below assume a clone of this repository with upstream added as a
remote:

```bash
git clone https://github.com/ntindle/autogpt.git && cd autogpt
git remote add upstream https://github.com/Significant-Gravitas/AutoGPT.git
```

If your clone was made from upstream instead (so `origin` is upstream and the
fork is a remote such as `ntindle`), swap the remote names in the commands.

Pushing a merge that changes files under `.github/workflows` needs a credential
that may write workflows. With `gh` as the git credential helper:
`gh auth refresh -s workflow`.

### Resolve a merge conflict by hand

```bash
git fetch origin desktop
git fetch --no-tags upstream dev
git switch desktop
git merge --ff-only origin/desktop
git merge --no-ff upstream/dev
git status                          # lists the conflicted files
```

For each conflicted file:

- Outside `autogpt_platform/desktop/` the fork should carry no changes, so take
  upstream's version: `git checkout --theirs -- <path> && git add <path>`.
  Then find out why the fork had touched that file and remove the reason, or
  the conflict will come back.
- Inside `autogpt_platform/desktop/`, upstream has started to change the same
  files. Merge by hand, then `git add <path>`.

Finish, test and push:

```bash
git merge --continue
( cd autogpt_platform/desktop && node --test "test/*.test.js" )
( cd autogpt_platform/desktop/runtime && uv run --python 3.13 --no-project \
    --with pytest --with pytest-asyncio --with aiohttp --with redis \
    --with psycopg2-binary --with pika --with psutil --with cryptography \
    python -m pytest -q )
git push origin desktop
```

Then start the build and let the sync close the issue:

```bash
gh workflow run desktop-build.yml --repo ntindle/autogpt --ref main -f ref=desktop
gh workflow run sync-upstream.yml --repo ntindle/autogpt
```

The sync finds `desktop` up to date and closes the issue. It does not run the
tests in that case and says so in its closing comment; the build you started
does.

### Fix a failing gate

The merge is clean, but a desktop test fails on the result. Usually upstream
changed something the desktop runtime copies or depends on: a file it is a
port of, a name it imports, a setting it reads. The issue quotes the end of
the failing test output.

```bash
git fetch origin desktop
git fetch --no-tags upstream dev
git switch desktop
git merge --ff-only origin/desktop
git merge --no-ff upstream/dev
```

Run the two test commands from the previous section and read the failure.
Then change the code in `autogpt_platform/desktop/` so that it matches what
upstream now does. If upstream's change really does not concern the desktop
app, change what the test expects, and say why in the commit message. Do not
delete or skip the test: it is what will catch the next change of that kind.
Commit the fix on top of the merge commit, push `desktop`, and run the build
and the sync as above.

### The sync job itself failed

The merge and the tests were not the problem; another step failed. The issue
says whether `desktop` was pushed before the failure. Open the run linked in
the issue. Common causes:

- `Push desktop` failed with 403 or "refusing to allow ... to create or update
  workflow": `FORK_SYNC_TOKEN` expired or lacks a permission. See
  [The sync token](#the-sync-token).
- `Push desktop` was rejected as not a fast-forward, or `Repeat the merge`
  says that `desktop` moved: somebody pushed to `desktop` while the run was in
  progress. Nothing was pushed. The next run starts from the new commit.
- `Repeat the merge` says the tree differs or that upstream no longer contains
  the tested commit: upstream rewrote `dev`, or the two machines merged
  differently. Nothing was pushed. Run the sync again; if it repeats, merge by
  hand as for a conflict.
- `Refuse a token stored as a repository secret` or `Check that only main may
  use the environment` failed: the token is readable by workflows on
  `desktop`. Fix it as described in [The sync token](#the-sync-token), and
  rotate the token if an upstream workflow could have run in the meantime.
- `Start the installer build` failed: `desktop-build.yml` is missing from
  `main` or is disabled (`gh workflow enable desktop-build.yml --repo ntindle/autogpt`).
  The issue says that no build has been started; the next run starts it.
- `Disable upstream's workflows ...` failed: read the listed paths and disable
  them by hand with `gh workflow disable <file> --repo ntindle/autogpt`.
- A job is shown as cancelled although nobody cancelled it: it ran into its
  time limit. Run the sync again.

The next successful run closes the issue.

### The build of desktop failed

The installers do not build from `desktop`, or the smoke test fails on at
least one platform. Typical cause: upstream bumped a Python dependency that
has no wheel for one platform. The issue links the failed build. Fix it on
`desktop` like any other bug, and do not release until a build of the fixed
commit is green. The first green build closes the issue.

Which builds are reported: a `desktop-build.yml` run that was dispatched (by
the sync or by hand, whatever `ref` it was given), and a build that a push to
`desktop` started through the caller. Builds for pull requests are not
reported. The nightly installed-app tests are not reported either; look at
them with:

```bash
gh run list --repo ntindle/autogpt --workflow desktop-nightly.yml --limit 5
```

## The sync token

`FORK_SYNC_TOKEN` is used for exactly one thing: pushing the merge to
`desktop`. The workflow's own `GITHUB_TOKEN` cannot do that, because GitHub
refuses pushes that create or change files under `.github/workflows` unless the
credential has the Workflows permission, and nearly every upstream merge
contains such a change. Everything else (issues, labels, disabling workflows,
starting the build) uses `GITHUB_TOKEN` with the permissions declared in the
workflow file.

Create it as a fine-grained personal access token
(<https://github.com/settings/personal-access-tokens/new>):

- Resource owner: `ntindle`
- Repository access: Only select repositories, `ntindle/autogpt`
- Repository permissions: **Contents: Read and write**, **Workflows: Read and
  write**. Metadata: Read-only is added automatically. Nothing else.
- Expiration: at most one year. Put the date in your calendar.

### Where the token is stored, and why

Store it as a secret of the `sync` **environment**, never as a repository
secret:

```bash
gh secret set FORK_SYNC_TOKEN --env sync --repo ntindle/autogpt
```

A repository secret can be read by any workflow file on any branch. `desktop`
receives upstream's workflow files without review, and the sync's push can
start one of them (see [What a sync push can start](#what-a-sync-push-can-start)).
An environment secret is only given to jobs that name the environment, and the
`sync` environment only admits jobs that run from `main`. Step 2 of
[Enabling Actions the first time](#enabling-actions-the-first-time) creates
the environment with that rule. To check both at any time:

```bash
gh api repos/ntindle/autogpt/environments/sync/deployment-branch-policies \
  -q '.branch_policies[] | .type + ":" + .name'          # must print exactly: branch:main
gh secret list --repo ntindle/autogpt                    # must not list FORK_SYNC_TOKEN
gh secret list --repo ntindle/autogpt --env sync         # must list FORK_SYNC_TOKEN
```

The sync checks the same two things on every run and refuses to continue when
either is wrong. If `FORK_SYNC_TOKEN` is listed as a repository secret, remove
it with `gh secret delete FORK_SYNC_TOKEN --repo ntindle/autogpt`.

In the sync, only two jobs name the environment: the token check, which only
learns whether the secret exists, and the job that pushes. The job that runs
the tests has no access to it. Each of the two shows up as a deployment to
`sync` on the repository page; that is expected.

### Rotating the token

1. Create a new token with the same settings.
2. `gh secret set FORK_SYNC_TOKEN --env sync --repo ntindle/autogpt` and paste it.
3. `gh workflow run sync-upstream.yml --repo ntindle/autogpt` and check the run is green.
4. Delete the old token at <https://github.com/settings/personal-access-tokens>.

If the token expires unnoticed, the sync fails at `Push desktop` and reports it
in the `upstream-sync` issue; nothing is lost.

If the token leaks, delete it on that page first, then rotate. With it, someone
could push to any branch of this repository, including workflow files, so also
check `git log` of `main` and `desktop` and the Actions run list for anything
you do not recognise.

A GitHub App installed on this one repository, with the same two permissions,
is the alternative that never expires. Using one means storing the App's ID
and private key in the `sync` environment, adding a step that mints an
installation token at the start of the `publish` job (the job named "Push and
start the build"), and passing that token to `Push desktop`. Mint it in that
job only; no other job may hold it.

## Dependabot

`.github/dependabot.yml` on `main` is the only Dependabot configuration that
is read. It has two entries: action pins in `main`'s workflows, and npm
packages in `autogpt_platform/desktop` on `desktop`. The comment in that file
explains why the first must never target `desktop`.

A Dependabot pull request into `desktop` is a pull request event on a branch
that carries upstream's workflows. Three of them (`repo-workflow-checker`,
`batch-reconcile`, `platform-dev-deploy-event-dispatcher`) have no branch or
path filter and would run on it. This is what the disable script prevents;
check step 6 of [Enabling Actions the first time](#enabling-actions-the-first-time)
if such a run ever appears.

## Cutting a release

A release is a GitHub Release of this repository named `desktop-v<version>`.
It is what people download and what installed apps update themselves from:
an app asks GitHub for the **latest** release, reads `latest.yml`,
`latest-mac.yml` or `latest-linux.yml` from it, and accepts the installer
named there if its SHA-512 matches. `desktop-release.yml` is the only thing
that creates one. Do not create, edit or re-upload a release by hand, and do
not publish any other kind of release in this repository: whatever GitHub
marks as latest is what every installed app moves to.

### The rules

- The tag is `desktop-v<version>` **on a commit of `main`**, never on
  `desktop`. GitHub runs `on: release` workflows from the tagged commit. A tag
  on `desktop` carries upstream's release workflows
  (`platform-single-container-docker`, `platform-autogpt-deploy-prod`,
  `classic-autogpt-docker-release`) and publishing the release would start
  them. A tag on `main` carries only the fork's workflows. The workflow puts
  the tag on the commit of `main` it was started from; the release notes
  record the `desktop` commit the installers were built from.
- The `desktop-v` prefix keeps the fork's tags apart from upstream's
  (`v0.4.7` and the like are upstream's and are in this repository).
- A version is released once. If a release is bad, the next one has a higher
  version (see [Taking a bad release back](#taking-a-bad-release-back)).
- The version is given when the workflow is started. Nothing in the
  repository is edited for a release: `package.json` on `desktop` always says
  `0.0.0-dev.0`.
- `1.2.3` is a full release. `1.3.0-rc.1` (anything with a `-` part) must be
  started as a pre-release: it gets a release page for manual testing and no
  installed app is offered it. There is one update channel.
- No leading zeros: `1.2.03` and `1.3.0-rc.01` are refused. The updater in
  an installed app reads versions strictly and would not accept them.

### Once: the signing environment

The certificates live in a GitHub environment named `desktop-release` that
only `main` may use, for the same reason as the sync token (see
[Where the token is stored, and why](#where-the-token-is-stored-and-why)):
`desktop` carries upstream's workflow files unreviewed, and a repository
secret can be read by any of them. Create the environment before the first
release, also when there is no certificate yet. The workflow refuses to start
if it is missing or admits another branch.

```bash
gh api -X PUT repos/ntindle/autogpt/environments/desktop-release \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X POST repos/ntindle/autogpt/environments/desktop-release/deployment-branch-policies \
  -f name=main -f type=branch
gh api repos/ntindle/autogpt/environments/desktop-release/deployment-branch-policies \
  -q '.branch_policies[] | .type + ":" + .name'      # must print exactly: branch:main
```

What gets signed depends only on which secrets are in that environment.
Adding them is the whole of "turning signing on"; no file changes.

| In the environment | macOS | Windows |
| --- | --- | --- |
| nothing | ad-hoc signature: Gatekeeper refuses the first launch until it is allowed in System Settings; the app cannot update itself and shows a link instead | unsigned: SmartScreen warns; updates are checked by SHA-512 only |
| the five Apple secrets | Developer ID signature, hardened runtime, notarized, stapled; updates itself | as above |
| Windows secrets as well | as above | signed; an update must also carry the same publisher's signature |

A set that is only partly there fails the run at the job `Signing gate`,
before anything is built, and the error names what is missing. That is every
partial combination: a certificate without its password or without the
notarization key, the notarization key without a certificate, one or two of
the three Azure secrets, the Azure secrets without the four variables or the
reverse, a password without its certificate. The gate also refuses a secret
named `CSC_LINK`, `CSC_KEY_PASSWORD` or `APPLE_API_KEY` in the environment:
those are electron-builder's own names, which nothing here reads, and a
certificate stored under one would be left unused without a word. What the
gate cannot see is a secret under any other wrong name: after adding secrets,
read the gate's summary in the next run. It says, for macOS and for Windows,
what will be signed. Linux packages are not signed.

Where the secrets go during a run. Three jobs of `desktop-build.yml` matter:

| Job | In the environment | What it runs |
| --- | --- | --- |
| `Signing gate` | yes | Its own few lines, which ask whether each secret exists. No checkout. |
| `Build` | **no** | Everything that is other people's code: the frontend's and backend's dependencies with their install scripts, the compilers, the stack in the smoke test. It packages the systems nobody signs. |
| `Sign` | yes | Only for a system that has a certificate. Takes the runtime `Build` assembled as an archive (data: it is copied and signed, never run), installs the packaging tools without their install scripts, restores no cache, and runs electron-builder and `build/mac_sign.js`. |

A step of a job can read whatever an earlier step of the same job left
behind, so "only the packaging step gets the secret" would protect nothing
if the build ran in the same job. That is why they are separate jobs, and
`scripts/tests/test_workflows.py` keeps them so. What the `Sign` job does
trust: the `desktop` commit being released (its `electron-builder.config.js`
and `build/mac_sign.js`), and the packages in its `package-lock.json`.

Every secret and variable, and nothing else, goes in with
`--env desktop-release`. Check with:

```bash
gh secret list --repo ntindle/autogpt                          # must list none of the names below
gh secret list --repo ntindle/autogpt --env desktop-release
gh variable list --repo ntindle/autogpt --env desktop-release
```

#### Apple: the Developer ID certificate

Two secrets: `MAC_CSC_LINK` (the certificate and its private key as a
base64-encoded `.p12`) and `MAC_CSC_KEY_PASSWORD` (the password of that
`.p12`). You do this yourself on the Mac that has the certificate; nobody
else needs to see the file.

1. Open **Keychain Access**, the **login** keychain, **My Certificates**.
   Find `Developer ID Application: <name> (<team id>)`. If several have that
   name, pick the one whose SHA-1 is the identity you sign with:

   ```bash
   security find-identity -v -p codesigning     # lists each identity with its SHA-1
   ```

   In Keychain Access, **Get Info** on a certificate shows the same SHA-1
   under Fingerprints. The entry must open to show a private key under it.
2. Select that one entry (the certificate, with its key), **File > Export
   Items**, format **Personal Information Exchange (.p12)**, and give it a
   long password that you use nowhere else. Exactly one identity may be in
   the file: the build signs with the first Developer ID it finds in it.
3. Store both, then delete the file:

   ```bash
   base64 -i DeveloperID.p12 | gh secret set MAC_CSC_LINK --env desktop-release --repo ntindle/autogpt
   gh secret set MAC_CSC_KEY_PASSWORD --env desktop-release --repo ntindle/autogpt   # paste the .p12 password at the prompt
   rm DeveloperID.p12
   ```

Before the first signed release, rehearse on that Mac. This signs a copy of
a built app exactly as the workflow will and lists every file Apple would
refuse; it does not notarize and uploads nothing. It needs a built app,
which takes about an hour the first time (the build is described in
`autogpt_platform/desktop/README.md`, "Building"; it needs `uv` and Node):

```bash
# in a checkout of `desktop`, in Terminal on the Mac (not over SSH)
cd autogpt_platform/desktop
uv run --python 3.13 --no-project build/build_runtime.py        # assembles build/runtime
npm ci
CSC_IDENTITY_AUTO_DISCOVERY=false \
  npx electron-builder --config electron-builder.config.js --publish never   # writes dist/mac-arm64/AutoGPT.app
bash build/sign_check_macos.sh <SHA-1 of the identity>          # must end with PASSED
```

To rehearse on an app that is somewhere else (one unpacked from a nightly
build's disk image, say), give its path as the second argument:
`bash build/sign_check_macos.sh <SHA-1> /path/to/AutoGPT.app`. `npm ci` is
needed either way.

#### Apple: the notarization key

Three secrets: `APPLE_API_KEY_P8`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER`.
Apple's notary service is called with an App Store Connect API key, not with
your Apple ID password.

1. Go to <https://appstoreconnect.apple.com/access/integrations/api>, **Team
   Keys**. The first time, the account holder has to press **Request
   Access**.
2. **Generate API Key**, any name, access **Developer**. Download the
   `AuthKey_<key id>.p8` file: Apple offers it once.
3. The **Key ID** is in the table; the **Issuer ID** is above it.
4. Check that the key works, store all three, then delete the file:

   ```bash
   xcrun notarytool history --key AuthKey_<key id>.p8 --key-id <key id> --issuer <issuer id>   # must answer, not refuse
   gh secret set APPLE_API_KEY_P8 --env desktop-release --repo ntindle/autogpt < AuthKey_<key id>.p8
   gh secret set APPLE_API_KEY_ID --env desktop-release --repo ntindle/autogpt --body '<key id>'
   gh secret set APPLE_API_ISSUER --env desktop-release --repo ntindle/autogpt --body '<issuer id>'
   rm AuthKey_<key id>.p8
   ```

To rotate either, repeat its steps. An installed app only updates itself to
a build signed by the same Apple developer team, so a renewed certificate
must be issued to the same team.

#### Windows: later, one of two ways

There is no Windows certificate yet. When there is one, add one of these
sets. If both are present, Azure is used.

- **Azure Trusted Signing** (Artifact Signing). Secrets `AZURE_TENANT_ID`,
  `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`: an app registration that has the
  *Trusted Signing Certificate Profile Signer* role on the certificate
  profile. Variables (not secrets; `gh variable set <name> --env
  desktop-release --repo ntindle/autogpt --body '<value>'`):
  `AZURE_SIGN_ENDPOINT` (such as `https://eus.codesigning.azure.net`),
  `AZURE_SIGN_ACCOUNT` (the signing account's name), `AZURE_SIGN_PROFILE`
  (the certificate profile's name) and `AZURE_SIGN_PUBLISHER` (the
  certificate's subject, exactly, such as `CN=Example, O=Example, C=US`;
  installed apps compare an update's signature with it).
- **A certificate file.** Secrets `WIN_CSC_LINK` (a base64-encoded `.pfx`)
  and `WIN_CSC_KEY_PASSWORD`. Only possible for a certificate whose key may
  leave its hardware; certificates issued since 2023 usually may not, which
  is why Azure comes first.

A signed app only takes an update whose signature names the publisher it
was built with. Apps installed from an unsigned release have no publisher to
compare with, so moving from unsigned to signed needs nothing. Moving later
to a certificate with a different subject does: apps signed with the old one
refuse the update and have to be reinstalled by hand. Keep the subject.

### Each time

1. Pick the commit of `desktop`: one for which the `upstream-sync` issue is
   closed and the **nightly** run is green. The default is the head of
   `desktop`. A green build is not enough: the release runs the
   installed-app tests on eight fresh machines and publishes only if every
   one passes, and only `desktop-nightly.yml` runs those beforehand. A red
   machine there means hours of building and no release.

   ```bash
   gh run list --repo ntindle/autogpt --workflow desktop-nightly.yml --limit 1      --json conclusion,createdAt,url      # conclusion must be "success"
   gh api repos/ntindle/autogpt/commits/desktop -q '.sha + " " + .commit.committer.date'
   ```

   The nightly builds whatever `desktop` was at 04:23 UTC. If the second
   command shows a commit newer than that run, the nightly did not test it:
   start a build with the tests by hand and wait for it to be green.

   ```bash
   gh workflow run desktop-build.yml --repo ntindle/autogpt --ref main -f ref=desktop -f e2e=true
   ```

2. Start the release from `main`:

   ```bash
   gh workflow run desktop-release.yml --repo ntindle/autogpt --ref main \
     -f version=1.2.3 -f ref=desktop
   # a pre-release:  -f version=1.3.0-rc.1 -f prerelease=true
   sleep 10
   gh run watch --repo ntindle/autogpt "$(gh run list --repo ntindle/autogpt --workflow desktop-release.yml --limit 1 --json databaseId -q '.[0].databaseId')"
   ```

3. It takes several hours. What it does:

   | Job | What it does |
   | --- | --- |
   | Check the request | Refuses a version that is malformed, already tagged, or not higher than the latest release; a commit that `desktop` does not contain; a signing environment that is missing or open to other branches. |
   | Build | `desktop-build.yml` for that commit, with that version: unit tests, the signing gate, the three builds, the `Sign` job for each system that has a certificate, then the installed-app tests on eight fresh machines. |
   | Publish | Only if all of that passed. Checks that every file is there and that the update files name the right version and checksums; writes `SHA256SUMS`; records a build-provenance attestation; creates a **draft**; uploads; compares what GitHub stored with what was built; publishes. The tag is created at that last moment. |

4. Look at the release page. The notes say how each system's files are
   signed. Then check one download as a user would:

   ```bash
   gh release download desktop-v1.2.3 --repo ntindle/autogpt --dir check && cd check
   sha256sum --check SHA256SUMS
   gh attestation verify AutoGPT-Setup-1.2.3-x64.exe --repo ntindle/autogpt
   ```

   On a Mac, for a signed release: open the disk image from a browser
   download. It must start without the *Open Anyway* step, and
   `spctl --assess --type execute -vv /Applications/AutoGPT.app` must say
   `source=Notarized Developer ID`.

### If the run fails

- **Check the request** failed: it says why. Nothing was built.
- **Build** failed: nothing was published. Fix `desktop`, start again with
  the same version. If it was an installed-app test (`Installed app (...)`),
  see step 1 of [Each time](#each-time): the nightly shows the same failure
  without a release attempt.
- **Publish** failed before its last step: there may be a draft, which
  nobody but you can see and no app reads. *Re-run failed jobs* on the run
  removes every draft of that version and tries again; the built files are
  kept for seven days. To give up instead, delete the drafts. A draft has no
  tag, so it is found in the list, and nothing else is left behind:

  ```bash
  gh api repos/ntindle/autogpt/releases --paginate \
    --jq '.[] | select(.draft and .tag_name == "desktop-v1.2.3") | .id' \
    | while read -r id; do gh api -X DELETE "repos/ntindle/autogpt/releases/${id}"; done
  ```

### Taking a bad release back

Installed apps never move to a lower version, so a release cannot be undone
for people who already have it. There are two separate things to do.

1. **Stop it spreading.** Make the previous good release the latest again
   and mark the bad one as a pre-release. Apps that have not installed it
   yet then stay where they are, and the download page shows the good one.
   An app that had already downloaded it stops offering it the next time it
   looks (within six hours while it runs, and in any case when *Restart to
   update* is chosen, which looks first). An app without a network at that
   moment still installs what it downloaded.

   ```bash
   gh release edit desktop-v1.2.3 --repo ntindle/autogpt --prerelease --latest=false
   gh release edit desktop-v1.2.2 --repo ntindle/autogpt --latest
   gh api repos/ntindle/autogpt/releases/latest -q .tag_name      # must print: desktop-v1.2.2
   ```

   Put a line at the top of the bad release's notes saying what is wrong
   (`gh release edit desktop-v1.2.3 --notes-file ...`). Do not delete its
   files: a download that is in progress would fail half way, and the notes
   are the record. Do not delete or move its tag, and never reuse `1.2.3`.

2. **Fix the people who have it.** Release `1.2.4` from a good commit. That
   is the only way an installed `1.2.3` changes. If the fix is to go back to
   the old code, `1.2.4` is built from the old `desktop` commit (the notes of
   `desktop-v1.2.2` name it): `-f version=1.2.4 -f ref=<that commit>`.

   Going back is safe for the program and not always for the data: a newer
   version may have migrated the database, and older code then meets a
   schema it does not know. Before releasing old code as a new version,
   check `git diff <old commit> <bad commit> -- autogpt_platform/backend/migrations`.
   If the bad version added migrations, do not go back; fix forward.

## If scheduled runs stop

GitHub disables scheduled workflows in repositories without activity for 60
days and sends an email first. Two workflows have a schedule:
`sync-upstream.yml` and `desktop-nightly.yml`. To prevent that, every sync run
re-enables all of the fork's workflows through the API. It leaves alone a
workflow that somebody disabled by hand, and says so in the run summary. If
the schedules stop anyway:

```bash
gh workflow enable sync-upstream.yml --repo ntindle/autogpt
gh workflow enable desktop-nightly.yml --repo ntindle/autogpt
gh workflow run sync-upstream.yml --repo ntindle/autogpt
```

## Changing the automation

Everything is on `main`. Before opening a pull request, run what
`main-checks.yml` runs:

```bash
uv run --python 3.13 --no-project --with pytest==8.4.2 python -m pytest -q
uvx --from actionlint-py==1.7.12.25 actionlint
```

A new workflow file on `main` is kept enabled automatically, because the
disable script builds its allow-list from the files in `.github/workflows`.
A new workflow file on `desktop` is disabled by the next sync unless its path
is added to `DESKTOP_CALLER`/the allow-list in
`scripts/disable_upstream_workflows.py`; keep `desktop` to the one caller
workflow.

Names that other files depend on. `scripts/tests/test_workflows.py` checks
them, except where noted:

- `build-report.yml` listens for workflows by name. It names `Desktop build`
  (`desktop-build.yml`) and `AutoGPT Platform - Desktop build` (the caller on
  `desktop`). If either is renamed, change `build-report.yml` too. The
  caller's name is on another branch, so no test on `main` can check it.
- The package list of the gate in `sync-upstream.yml` is the one of the unit
  job in `desktop-build.yml`.
- Third-party actions used on `main` are listed in step 3 of
  [Enabling Actions the first time](#enabling-actions-the-first-time).
- `desktop-release.yml` and `desktop-build.yml` share names with the
  `desktop` branch, which no test on `main` can see: the installers' file
  names and the environment variables `AUTOGPT_DESKTOP_VERSION`,
  `AUTOGPT_DESKTOP_OUTPUT`, `AUTOGPT_DESKTOP_MAC_SIGN` and
  `AUTOGPT_DESKTOP_WIN_SIGN` come from
  `autogpt_platform/desktop/electron-builder.config.js`, and
  `build/mac_sign.js check` is run on the signed macOS app. The release job
  fails, before anything is published, when a file it expects is missing.
- Every secret and variable the build reads is named in
  [Cutting a release](#cutting-a-release). Only the packaging steps of the
  `sign` job may hold one, and the `build` job is in no environment. The
  tests also run the gate's script with every partial set of secrets.
- The version pattern is written three times and must stay the same: in
  `scripts/release_plan.py`, in the `Version` step of `desktop-build.yml`
  (the tests run both against one list), and in
  `electron-builder.config.js` on `desktop`.
