# AutoGPT desktop builds

Experimental desktop builds of the [AutoGPT Platform](https://github.com/Significant-Gravitas/AutoGPT):
a Windows installer, a macOS disk image, and a Linux AppImage and `.deb`. No
Docker, no virtual machine, nothing to configure before the first start.

This repository is a parallel fork of `Significant-Gravitas/AutoGPT`. It is
not an official distribution.

## Branches

| Branch | What it holds |
| --- | --- |
| `desktop` | Upstream `dev` with the desktop distribution on top, in `autogpt_platform/desktop/`. This is where the code is. |
| `main` | This page and the automation that keeps `desktop` in step with upstream. Deliberately not a copy of upstream, so that none of upstream's scheduled workflows or dependency bots run here. |
| `variant/<slug>` | An experiment on top of `desktop` that may change the platform itself. It is built into an app of its own, *AutoGPT (slug)*, which installs next to the normal one and shares no data, address or updates with it. See [Variants](docs/MAINTAINING.md#variants). |

Start with [`autogpt_platform/desktop/README.md`](https://github.com/ntindle/autogpt/blob/desktop/autogpt_platform/desktop/README.md)
on the `desktop` branch.

## How this fork is maintained

Everything that maintains the fork is on this branch. The full manual is
[`docs/MAINTAINING.md`](docs/MAINTAINING.md).

What runs, and when:

- **Every day at 05:23 UTC**, `sync-upstream.yml` merges upstream `dev` into
  `desktop`. It pushes only if the merge is clean and the desktop tests pass on
  the merged tree. After a push it disables any workflow that came from
  upstream and starts the installer build (`desktop-build.yml`).
- **Every day at 04:23 UTC**, `desktop-nightly.yml` builds the installers from
  `desktop` and tests them on fresh Windows, macOS and Linux machines.
- **When a build of `desktop` finishes**, `build-report.yml` records a failed
  build in the `upstream-sync` issue, and closes it after a green one.
- **Every week**, Dependabot proposes updates for the action pins on `main` and
  for the npm packages of the Electron shell on `desktop`.
- **On every pull request to `main`**, `main-checks.yml` tests the scripts in
  `scripts/` and lints the workflows.

When an issue labelled `upstream-sync` and titled "Upstream sync is blocked" is
open, `desktop` needs attention. In the first three cases below nothing is
broken: `desktop` is still at its last good commit and has only stopped
following upstream. The issue says which case it is and links to the steps:

| The issue says | Meaning | What to do |
| --- | --- | --- |
| does not merge cleanly | Upstream and the fork changed the same lines. | [Merge by hand](docs/MAINTAINING.md#resolve-a-merge-conflict-by-hand). |
| the desktop tests fail | Upstream changed something the desktop app depends on. | [Adapt the desktop code](docs/MAINTAINING.md#fix-a-failing-gate). |
| the sync job failed | Usually an expired token. | [Read the run](docs/MAINTAINING.md#the-sync-job-itself-failed). |
| the installer build failed | The installers do not build from the current `desktop`. | [Fix it before releasing](docs/MAINTAINING.md#the-build-of-desktop-failed). |

The first sync that succeeds closes the issue in the first three cases, and
the first green build in the fourth.

One secret is needed: `FORK_SYNC_TOKEN`, a fine-grained personal access token
limited to this repository with **Contents: Read and write** and **Workflows:
Read and write**, and nothing else. It is used only to push the merge to
`desktop`; the Workflows permission is required because upstream's merges
change workflow files. Create it at
<https://github.com/settings/personal-access-tokens/new>.

Store it in the `sync` environment, which must admit only the `main` branch,
and never as a repository secret: workflows on `desktop` come from upstream
unreviewed, and a repository secret is readable from any branch.
[`docs/MAINTAINING.md`](docs/MAINTAINING.md#enabling-actions-the-first-time)
has the commands that create the environment; the last one is
`gh secret set FORK_SYNC_TOKEN --env sync --repo ntindle/autogpt`. Without the
secret the sync does nothing and says so. With the secret in the wrong place
it refuses to run and says why.

Never push a branch named `dev`, `master`, `release-*` or `ci-test*` to this
repository: upstream's workflows trigger on those names.
