# AutoGPT Desktop (experimental)

The AutoGPT Platform as an installable desktop application: a Windows
installer, a macOS disk image, a Linux AppImage. No Docker, no virtual
machine, no configuration file to edit before the first start.

It is the [single-container appliance](../single-container/README.md) with
each Linux process replaced by a native one, started and stopped by a small
supervisor instead of supervisord.

> Experimental. It runs the same code as the appliance, but it has had far
> less testing. Whether a build is signed depends on the release it came
> from; see [Updates](#updates) and [Signing](#signing).

## How it fits together

```
AutoGPT.exe / AutoGPT.app            Electron shell (src/)
  └─ runtime process                  Python supervisor (runtime/autogpt_desktop/)
       ├─ PostgreSQL + pgvector
       ├─ Valkey (one node, cluster mode)
       ├─ RabbitMQ on a bundled Erlang
       ├─ 3 service hosts running the 8 AutoGPT backend services
       ├─ Next.js frontend server
       └─ reverse proxy on 127.0.0.1  the only address the window loads
```

**Shell.** Starts one process, shows its progress, opens a window on the URL
it reports, and closes that process's stdin to stop it. It knows nothing about
databases or ports. The contract is at the top of `src/runtime.js`.

**Runtime.** A port of the appliance's `entrypoint.sh`, `bootstrap.sh`,
`supervisord.conf` and `nginx.conf`:

| Appliance | Desktop |
| --- | --- |
| `runtime_config.py` generates secrets | the same file, shared |
| supervisord, two stop groups | `supervisor.py`: start tiers, stopped in reverse, each all at once |
| a process per backend service | service hosts: the same eight services in three processes (see [Memory](#memory)) |
| three Valkey nodes | one node owning all 16384 slots (the backend's `RedisCluster` client cannot tell) |
| nginx | `proxy.py` (same routes, streamed) |
| frontend role over a Unix socket | the same role policy, with a generated password over loopback TCP |
| container exit kills everything | a Job Object on Windows; process groups and a recorded-PID sweep elsewhere |
| `/data/home` as the services' home | `home/` in the data directory, so AutoPilot never finds the user's own Claude Code or Codex sign-in |
| FalkorDB / Graphiti memory | not included (Linux-only module, SSPL) |
| chat-bot bridge services | not included |
| `autogpt-admin promote`, then closing registration by hand | automatic: the first account is the owner and an admin, and registration closes behind it (see [Accounts](#accounts)) |
| sign-in with Google, GitHub, Discord | email and password only, by decision (see [Accounts](#accounts)) |

**Service hosts.** Every backend service process is a service host
(`servicehost.py`): it takes upstream's own service objects from upstream's
own entry points, runs each in a thread exactly as it would run in a process
of its own, and stops them properly. By default the eight services share
three hosts, which is what [Memory](#memory) is about. What a host depends on
upstream is a short list at the top of `servicehost.py`, each item checked
against the real backend by `runtime/tests/backend_contract.py`. When an
upstream change breaks one anyway, the host says what changed in its log and
the supervisor starts a host per service instead, each running its service
the way upstream's entry point would have: the app costs more memory rather
than failing to start. That covers the list at the top of `servicehost.py`
and uvicorn's part in the shared event loop; a change that stops a service
from running at all is that service's host exiting, and the app says so.

**Ready.** The window opens when what it talks to answers: the REST and
websocket servers, database-manager behind them, and the frontend. The other
five services are waited for as well, each on its own port, but where they
answer is upstream's to move (the executors answer on their metrics ports),
so they hold the app up for at most 90 seconds more. After that it opens
anyway and `logs/runtime.log` names the ones that never answered. A host
that exits while starting ends the wait at once.

A host that loses a service exits and is restarted as a whole (its log,
`logs/<host>.log`, names the service that ended); a host is restarted at
most three times in five minutes before the app gives up and says so.

**Stopping.** Quitting takes about five seconds. The service hosts are asked
to stop together: with SIGTERM, or on Windows, where a signal cannot reach a
process without a console, through a named event each host waits on. A host
calls every one of its services' `cleanup()` at once and leaves when they
are done, which takes about a second; it is killed, with whatever it
started, if it has not left after three (a run in flight makes the
executor's cleanup wait, and a quit does not wait for a run). The
database-manager is stopped after the other services, whose cleanup writes
through it. PostgreSQL, Valkey and RabbitMQ are then shut down properly. The
shell kills a runtime that has not stopped after a minute,
unless the runtime says it needs longer, which it does for exactly one thing:
a database migration, which is never interrupted. A first start that is cut
short anyway (power loss) is detected and redone from scratch on the next
one, since nobody has used that database yet.

**Interrupted agent runs.** An executor locks each run it works on, for five
minutes at a time, and a new executor that finds the lock of a dead one
drops the run. Here there is only ever one executor, so those locks are
cleared before every start of the process that hosts it, on boot and when it
is restarted after a crash; the run's message is still queued, and the new
executor picks the run up where it stopped (the step that was interrupted
runs again). At boot, before any service starts, a run that has been
"running" for more than 24 hours is marked failed instead of being resumed
out of the blue days later.

Everything listens on `127.0.0.1` only, including sockets that live for
microseconds: `build/erlang_patches.py` explains the two places where Erlang
and RabbitMQ would otherwise touch every interface, which is enough for
Windows Firewall to prompt on first launch. Ports are chosen free on first
start (from 15000–32000, below every OS's ephemeral range) and remembered, so
the app's origin stays stable. The app's own address is `127.0.0.1:18473`
(unassigned at IANA) when that port is free on the first start, and a random
free port otherwise. An install keeps the port it first got; it moves only
if another program is holding that port when the app starts.

**Bundle.** Nothing is frozen. The bundle is a relocatable CPython with the
backend's locked dependencies installed, plus upstream builds of everything
else, pinned in `build/artifacts.py`. Freezing was the 2024 attempt
(cx_Freeze) and is why Prisma could not find its engine; here the engine path
is set explicitly, as the appliance does.

| Component | Windows x64 | macOS arm64 | Linux x64 |
| --- | --- | --- | --- |
| Python 3.13 | python-build-standalone | same | same |
| PostgreSQL + pgvector | 18.6, prebuilt | 18.6, prebuilt | 16.12, built from source |
| Valkey 8.1 | built under MSYS2 | built from source | upstream binary |
| Erlang 27 + RabbitMQ 4.1 | upstream zips | erlef build + generic-unix | hex.pm build + generic-unix |
| Node 24, Prisma 5.17 engines | upstream | upstream | upstream |

The Linux bundle needs glibc 2.35 and OpenSSL 3 (Ubuntu 22.04, Debian 12,
Fedora 36, or newer); on a system without OpenSSL 3 the app stops at its
first start and says so. The build fetches the Prisma engines made for
OpenSSL 3 whatever the build machine has installed, and refuses to seal a
bundle whose engines are linked against anything else. The AppImage also
needs FUSE 2 (`libfuse2` on Ubuntu).

**The bundle is never written to.** Installed, it is read-only (a `.deb`, a
mounted AppImage) or sealed by a code signature (macOS), so everything the
app writes goes to the data directory:

- The Next server would keep three caches beside its own code (optimised
  images, `fetch` responses, regenerated pages). The build switches them off
  in the server's embedded configuration (`build/next_config.py`) and raises
  the time a browser may keep an optimised image from one minute to four
  hours, since the server no longer has a copy to answer from. A remote
  image replaced under the same URL can be that stale.
- The Prisma CLI, which applies migrations, would download a query engine
  for the platform it detects into its own package. It is told to use the
  two engines the bundle carries and touches nothing else
  (`runtime/autogpt_desktop/migrations.py`).
- The interpreter runs with `-B`, on bytecode the build compiled.
- The backend's optional file logging (`ENABLE_FILE_LOGGING` in
  `settings.env`) goes to `logs/backend` in the data directory.

The smoke test fails on any file or directory a run adds, changes or removes
in the bundle, and on Linux and macOS can run with the whole bundle
unwritable (see [Building](#building)).

**Paths with spaces.** RabbitMQ's launch scripts cannot take a space in any
path, and both the bundle and the data directory usually have one. On macOS
and Linux they are given a symlink under the user's cache directory. On
Windows they are given the folder's 8.3 short name; on a volume that keeps
no short names (any drive but the system one, by default), a directory
junction instead (`runtime/autogpt_desktop/winlinks.py`). Junctions live in
`%LOCALAPPDATA%\<install>\links` when that path is itself free of spaces,
and otherwise in `%ProgramData%\<install>-<user SID>`, a folder created
open to its user, the system and administrators only. One that is already
there is used only when it is a real folder, owned by one of those three,
that nobody else may change; all three facts are read from one open handle,
which also keeps the folder from being renamed while the junction is made.
RabbitMQ and Erlang are run, and read their configuration and cookie,
through the junctions, so another user of the machine must not be able to
point one elsewhere. Junctions whose folder is gone are removed at the next
start.

**PostgreSQL on Windows** is started through `pg_ctl start` for every user.
`postgres.exe` refuses to run for an account with administrative rights (an
elevated shell, a CI runner), and `pg_ctl` starts it with those rights taken
away. `pg_ctl` does not stay, so the runtime watches, records and stops the
server it finds through `postmaster.pid`; the server is still inside the
runtime's Job Object. macOS and Linux start `postgres` directly.

**Bundled tools.** `tools/bin` in the bundle is first on the backend
services' `PATH`. It holds `ffmpeg`, which the video blocks, the video
download block (yt-dlp) and recording transcription run by name: the binary
that the backend's own `imageio-ffmpeg` dependency ships, moved to where a
plain `ffmpeg` finds it (`build/bundled_tools.py`). Each OS's wheel carries a
different build by a different builder (gyan.dev, johnvansickle.com,
osxexperts.net), so the build asks the binary what it is, checks that it is
that builder's, and ships the licence text and a note of where its source
is in `tools/licenses/ffmpeg`. All three are GPL builds and go out under
GPL version 3; the macOS one is "version 2 or later", so both texts are
shipped with it. A build that may not be redistributed, or whose builder is
not the recorded one, fails the `tools` step, and `seal` refuses a bundle
whose ffmpeg lacks its licence or note.
There is no `ffprobe`: nothing in the backend needs it, and yt-dlp merges
separate video and audio streams into an mp4 without it.

## Where data lives

| | |
| --- | --- |
| Windows | `%LOCALAPPDATA%\AutoGPT` |
| macOS | `~/Library/Application Support/AutoGPT` |
| Linux | `$XDG_DATA_HOME/AutoGPT` (default `~/.local/share/AutoGPT`) |

`config/settings.env` holds provider keys (tray menu → *Settings file*);
`logs/` has one file per process (`workers.log`, `api.log`,
`database-manager.log`, `postgres.log`, …). Uninstalling the app leaves this
directory in place. It is readable by its owner only.

The data belongs to the PostgreSQL major version that created it (18 on
Windows and macOS, 16 on Linux). A build with a different major refuses to
start and says why; changing the bundled major needs a migration path first.

## Memory

Each interpreter that imports the backend holds 460–860 MB before it does
any work (the websocket server the least, database-manager the most), most of it the generated database client and the block library.
Eight services in eight processes held 5.7 GB of the app's 6.0 GB. They now
share three:

| Process | Services | Why these together |
| --- | --- | --- |
| `database-manager` | database-manager | the others' way to the database; alone |
| `workers` | executor, copilot-executor, scheduler, batch-executor, notification | none of them connects to the database itself |
| `api` | websocket, rest | both do, so they serve on one event loop: the database client belongs to the loop that connected it |

The grouping follows one rule of the backend's: whether a query goes
straight to the database or through database-manager is decided per process,
so a service that connects cannot share with one that does not. A watchdog
in the `workers` host ends it if that ever stops being true.

Measured on Windows x64 (31 GB, 24 cores), idle, 45 seconds after ready,
memory unique to the app's processes (USS):

| Profile | Python processes | Processes in all | Whole app |
| --- | --- | --- | --- |
| before: a process per service | 8 | 40+ | 6.0 GB |
| `balanced`, `compact` | 3 | 36–37 | 3.0 GB |
| `isolated` | 8 | 48 | 5.9 GB |

The profile is chosen from the machine and reported in `logs/runtime.log`:

| Profile | Chosen when | Layout | Agent runs at once | AutoPilot turns at once |
| --- | --- | --- | --- | --- |
| `compact` | 8 GB of memory or less | three service hosts | 4 | 2 |
| `balanced` | more than 8 GB | three service hosts | one per core, 4 to 10 | one per two cores, 2 to 5 |
| `isolated` | only when asked for | a host per service | 10 (upstream's default) | 5 |

`AUTOGPT_DESKTOP_PROFILE=isolated` (or `compact`, `balanced`) in
`config/settings.env` overrides the choice. `isolated` is the layout to fall
back on, and to compare against, when a merged process misbehaves. The pool
sizes are threads, so they bound the peak rather than the idle figure;
`NUM_GRAPH_WORKERS` and `NUM_COPILOT_WORKERS` in `settings.env` win over the
profile's. PostgreSQL is limited to 50 connections and one autovacuum worker
on every start; the scheduler's two connection pools are pinned at upstream's
size of 3 (`SCHEDULER_DB_POOL_SIZE`, also yours to raise in `settings.env`)
so that the budget behind the 50 does not move when upstream's default does.

A host of five services holds five services' files and sockets under one
per-process limit, and macOS gives a process started from a terminal 256. On
macOS and Linux the runtime raises its own soft limit to 8192 (or the hard
limit, if lower) before it starts anything, and every process inherits it.

The skills catalog is published by one more backend interpreter (about
700 MB while it runs). It starts a minute after the app is ready rather than
with it, and not at all when this version of the app has already published
into this data directory.

What sharing a process costs: a service that crashes takes its host's other
services down with it for the half minute a restart takes, and the services
in a host share one Python interpreter lock, so a block that computes for a
long time can delay the scheduler's tick or an AutoPilot stream (upstream
runs the batch executor apart from the scheduler for that reason).

## Accounts

**The first account created is the owner.** It is an admin from its first
sign-in (the `/admin` pages and the admin API work straight away), and
creating it closes registration: nobody else can sign up afterwards. That is
the trust model, and it is all of it: whoever reaches `127.0.0.1` on the
machine first, another user of the computer or another program included,
becomes the owner. Create your account when you first start the app.

To let more people create accounts, set `AUTH_ALLOW_NEW_ACCOUNTS=true` in
`config/settings.env` and restart. They are ordinary users, not admins. Remove
the line and restart to close registration again.
`AUTH_ALLOW_NEW_ACCOUNTS=false` is the same as no line at all: while there is
no account yet the first sign-up is always allowed, since otherwise nobody
could ever sign in. A sign-up refused in the same session the owner was
created in shows a generic "Failed to create user"; from the next start it
shows the proper "not allowed" message.

An install that already had accounts before this was added gets the same
treatment on its next start: the oldest account becomes the owner and
registration closes. A session that was already signed in sees the admin
pages within five minutes, or at once after signing out and in.

**Sign-in is by email and password only.** That is a decision, not a gap.
Sign-in with Google, GitHub or Discord would need each user to register their
own OAuth application for an account that never leaves their machine, and the
platform's self-hosted build has no button for it. No email is sent either
(there is no mail server), so the address is only a user name and "forgot
password" cannot deliver a link.

**A forgotten owner password** is reset from the computer itself: tray menu →
*Reset owner password…*, or *Account* in the menu bar (on Windows and Linux
the menu bar is hidden; press Alt in the AutoGPT window to show it. GNOME has
no tray icon without an extension, so there it is the way in). It asks for
the new password twice, restarts AutoGPT, and signs the owner out everywhere
within five minutes: the old sessions are deleted at once, but the app caches
a session in the browser's cookie for up to five minutes, and a browser that
was signed in keeps working until that runs out. The same by hand, for when
there is no window at all: put the new password, at least 12 characters, on
the first line of a file named `reset-password` in the data directory's
`config` folder, then start or restart AutoGPT.

```bash
# Linux (macOS: ~/Library/Application\ Support/AutoGPT/config/reset-password)
(umask 077; printf '%s\n' 'the new password' > ~/.local/share/AutoGPT/config/reset-password)
```

```powershell
Set-Content -Encoding UTF8 "$env:LOCALAPPDATA\AutoGPT\config\reset-password" 'the new password'
```

The file is read once and deleted as that start begins, whether or not the
password in it was usable; if it was not, `logs/runtime.log` says why. If
that start then fails or is closed before the database is up, the password
is not changed (the error on screen and the log say so) and the reset has to
be done again. It sets the password of the owner (the oldest admin account)
and nobody else's.

**Connecting integrations (OAuth).** Providers that sign you in through a
popup (GitHub, Google, Notion, …) need an OAuth application of your own, with
its client ID and secret in `config/settings.env`, and this redirect URL
registered with the provider:

```
http://127.0.0.1:18473/auth/integrations/oauth_callback
```

Use the exact one for your install: tray menu → *Copy OAuth redirect URL*,
or the same entry under *Account* in the menu bar (Alt shows it on Windows
and Linux). The port is not 18473 on an install made by an earlier version,
or when 18473 was taken as the app first started. Without the app's window,
the port is the `public` value in `config/ports.json` in the data directory,
and the URL is `http://127.0.0.1:<public>/auth/integrations/oauth_callback`.
MCP servers that use OAuth are sent to `/auth/integrations/mcp_callback` on
the same address; they register it themselves.

## Updates

Releases are GitHub Releases of
[`ntindle/autogpt`](https://github.com/ntindle/autogpt/releases), tagged
`desktop-v<version>`. An installed app looks at the latest one a minute after
it has finished starting, and every six hours after that. It never looks
while it is starting: a database migration runs then, and an update must not
land in one. There is one channel; pre-releases are not offered.

If AutoGPT cannot start at all, the app looks once as soon as the start has
failed, and the window that shows the error offers the newer version next to
*Show logs*: the release that broke the start is usually fixed by the next
one, and nobody should have to go and find it.

The app follows the latest release, also after it has downloaded one. If a
release is withdrawn (an older one is made the latest again) the offer to
install it goes away at the next look; if a newer one is published, that one
is downloaded instead.

| | What happens when a newer version exists |
| --- | --- |
| Windows | Downloaded in the background. The tray menu and the *Updates* menu (Alt shows the menu bar) then offer **Restart to update to X**. |
| Linux, AppImage | The same. The AppImage replaces its own file and keeps its name. |
| macOS, signed build | The same. The app must be in `/Applications` (or anywhere it was moved to by hand), not run from the disk image. |
| macOS, ad-hoc build | **Version X is available…** in the menus opens the release page; install it by hand. macOS only lets an app replace itself with one signed by the same Developer ID. |
| Linux, `.deb` | The same link: the package belongs to `apt`, which needs root. |

Nothing is ever installed without **Restart to update**: not when a download
finishes, not when the app quits. Choosing it looks once more whether that
version is still the latest release and whether the download is still on
disk, asks, stops AutoGPT the way *Quit* does (which waits for anything that
must not be interrupted), installs, and starts the new version. On Windows
the installer's progress window stays up while it works, which takes minutes.
What the updater did is in `logs/updater.log`.

When the install does not happen:

- **The download is gone** (a cleaner or antivirus removed it): nothing is
  stopped; the app downloads it again and offers it again.
- **The installer cannot be started** and the system says so: the version
  you have starts again, and the menu links to the download until the app is
  next started.
- **Windows, and the installer needs to be run elevated** (the system
  refused to start it directly; this does not happen with the per-user
  installer unless something on the machine blocks it): Windows asks for
  permission, and the app has quit by then. If you say no, nothing was
  changed and nothing starts by itself: start AutoGPT again, and it offers
  the update again. electron-updater gives no way to learn the answer.

**What is checked before an update is installed.** The app fetches
`latest.yml` (`latest-mac.yml`, `latest-linux.yml`) from the latest release
over HTTPS from `github.com`, and installs the file named there only if its
SHA-512 is the one in that file. A version lower than the installed one is
never taken. On top of that:

- **macOS, signed:** the new app must carry a valid signature of the same
  Developer ID team, or macOS refuses to swap it in.
- **Windows, signed:** the installer must be signed by the same publisher as
  the installed app.
- **Windows, unsigned (every build until there is a certificate):** nothing
  more. The update is exactly as trustworthy as the GitHub repository and
  the connection to it: whoever can publish a release there can ship code to
  every installed copy, and no publisher's signature stands in the way. The
  same is true of the first download.
- **Linux AppImage:** nothing more either; AppImages are not signed.

Every release also has a `SHA256SUMS` file and a build-provenance
attestation, for checking a download by hand:
`gh attestation verify <file> --repo ntindle/autogpt`.

A build that did not come from a release has the version `0.0.0-dev.<n>`
(`0.0.1-dev.<n>` for the second build the upgrade test installs) and never
looks for updates. `AUTOGPT_DESKTOP_UPDATES=off` in the environment switches
the updater off in any build; the installed-app tests set it.

## Signing

One configuration, `electron-builder.config.js`, builds signed and unsigned
installers. Which one comes out depends only on the environment the build
runs in; the release workflow (on the fork's `main` branch, with its manual
in `docs/MAINTAINING.md` there) sets it from the secrets it has.

| | Without a certificate | With one |
| --- | --- | --- |
| Windows | Unsigned. SmartScreen warns: *More info* → *Run anyway*. | Signed (Azure Trusted Signing, or a certificate file). No certificate exists yet. |
| macOS | Ad-hoc signature: a download counts as intact rather than "damaged", but is not trusted. macOS refuses the first launch until it is allowed under *System Settings → Privacy & Security → Open Anyway*. | Developer ID signature, hardened runtime, notarized by Apple, ticket stapled. Opens like any other downloaded app. |
| Linux | Not signed. | Not signed. |

A Developer ID build signs every program and library in the app, because
Apple notarizes nothing less. `build/mac_sign.js` does it and explains the
rules; the short version:

- The runtime's few thousand Mach-O files are signed one by one, with the
  hardened runtime. Entitlements are per program and are not inherited by
  what a program starts, so only the programs that need one get one: the
  Electron shell and its helpers (JIT, microphone), Python (JIT, executable
  memory, libraries nobody signed), Node and Erlang's `beam.smp` (JIT),
  PostgreSQL's programs (`DYLD_FALLBACK_LIBRARY_PATH`). Everything else gets
  none. The files are in `resources/entitlements.*.plist`.
- Everything is signed by this build, also files their vendor had signed:
  under the hardened runtime a program only loads libraries of its own team.
  The one exception is the Claude Code CLI, Anthropic's signed program, which
  is shipped byte for byte as Anthropic built it, on macOS and on Windows. If
  the bundled copy is ever not signed in a way Apple accepts, the build stops
  instead of signing it.
- A build that was asked for a Developer ID signature and did not get one
  (no valid *Developer ID Application* certificate), or was not notarized
  (no App Store Connect key), fails before any disk image is made.
  electron-builder would carry on without saying so.
- `bash build/sign_check_macos.sh <SHA-1>` rehearses all of this on a Mac
  that has the certificate, on a copy of a built app, without notarizing.

## Variants

A variant is an experiment: a branch that changes the platform itself, built
into an app that installs **next to** the normal one and next to other
variants. It is a branch plus a name:

- the branch is `variant/<slug>`, made from `desktop`;
- the name is the slug: 1 to 24 lower-case letters, digits or hyphens, not
  starting or ending with a hyphen (`voice`, `local-models`), given to the
  build as `AUTOGPT_DESKTOP_VARIANT`. It cannot be `updater` or end in
  `-updater`: the updater's download cache of another install is called that.

Nothing else names a variant. There is no list of them, and no file is edited
to add one: every name below is derived from the slug in one place,
[`src/identity.js`](src/identity.js), which the build
(`electron-builder.config.js`), the shell and the installed-app tests all
read.

| | Normal app | Variant `voice` |
| --- | --- | --- |
| Name in menus, windows, the tray, the installer | AutoGPT | AutoGPT (voice) |
| App id (bundle id, uninstall entry, Squirrel) | `co.agpt.autogpt.desktop` | `co.agpt.autogpt.desktop.voice` |
| Windows install directory | `%LOCALAPPDATA%\Programs\autogpt` | `...\Programs\autogpt-voice` |
| Windows executable | `AutoGPT.exe` | `autogpt-voice.exe` |
| macOS bundle | `AutoGPT.app` | `autogpt-voice.app` |
| `.deb` package, command, AppArmor profile | `autogpt` | `autogpt-voice` |
| `.deb` install directory | `/opt/AutoGPT` | `/opt/AutoGPT (voice)` |
| Installer files | `AutoGPT-Setup-1.2.3-x64.exe`, ... | `AutoGPT-voice-Setup-1.2.3-x64.exe`, ... |
| Data directory | `AutoGPT` (see [Where data lives](#where-data-lives)) | `AutoGPT-voice`, in the same place |
| Electron profile (cookies, the single-instance lock) | `AutoGPT` in the system's app-data folder | `AutoGPT-voice` |
| Updater's download cache | `autogpt-updater` | `autogpt-voice-updater` |
| Address it prefers | `http://localhost:18473` | `http://localhost:25954` (from the slug, 20000-29999) |
| Cookie names in a browser | as the platform names them | `AutoGPT-voice.<name>` |
| With `AUTOGPT_DESKTOP_DATA_DIR=D:\data` | `D:\data` | `D:\data-voice` |
| Release tags | `desktop-v<version>` | `desktop-voice-v<version>` |

**Nothing is shared.** A variant has its own database, its own secrets, its
own ports, its own sign-in, its own browser profile and its own logs. It can
run at the same time as the normal app. A variant may migrate its database to
a schema the normal app has never heard of and the normal app's data is not
touched by it; for the same reason there is no way to move data from one to
the other, and uninstalling one leaves the others alone.

What follows from the separate address: a variant's OAuth redirect URL is its
own (tray menu, *Copy OAuth redirect URL*), so an integration connected in
the normal app has to be connected again in the variant. The address is only
preferred, as for the normal app: an install keeps the port it first got, and
takes another when that one is taken. Two slugs can hash to the same port;
the one that starts second on a machine then gets another. No install puts
one of its internal services on a port between 20000 and 29999, so a
variant's address is not taken by another install's database.

Four things would be shared if nothing were done about them, and are not:

- **The data directory named in the environment.** `AUTOGPT_DESKTOP_DATA_DIR`
  is one value for every program a user starts. It names the normal app's
  directory; a variant takes the one next to it, `<directory>-<slug>`
  (`src/paths.js`). The installed-app tests follow the same rule.
- **A data directory reached some other way** (a link, a directory given to
  one install that is already another's). The first install to use a data
  directory leaves its name in `autogpt-install.json` there, and an install
  that finds another's name starts nothing and says so. A directory that
  holds data and no name is the normal app's.
- **Cookies in a browser.** *Open in browser* shows every install at
  `127.0.0.1`, and a browser keeps cookies per host, whatever the port. The
  runtime's proxy gives a variant's cookies names of their own and passes on
  to the platform only the cookies of its own install
  (`runtime/autogpt_desktop/cookies.py`), so signing in to one install does
  not sign the other out, and an experiment is never sent the normal app's
  session. The normal app's cookie names are unchanged. In a variant, a
  cookie that a page sets itself with `document.cookie` does not reach the
  server, and a script sees the server's cookies under the longer name; the
  platform's sign-in uses neither.
- **The programs a Windows installer closes.** electron-builder's installer
  closes every program whose path starts with the install directory's, and
  `...\Programs\autogpt` is the start of `...\Programs\autogpt-voice`: an
  update of the normal app would stop a running variant, database and all.
  `resources/installer.nsh` replaces that check with one that ends the
  directory's name with a backslash; `test/packaging.test.js` holds it
  against electron-builder's template.

**Updates never cross.** The normal app follows the repository's latest
release. A variant's releases are GitHub *pre-releases* tagged
`desktop-<slug>-v<version>`, and GitHub's latest release is never a
pre-release, so the normal app is never shown one. A variant does not look at
"latest" at all: it is built without that address. It lists the repository's
releases, takes the highest plain version (`X.Y.Z`) among the tags that are
exactly its own, and reads that one release. On top of both, every app
refuses an update unless each file in it is its own installer by name
(`AutoGPT-voice-...` for `voice`), whatever release it came from.
`test/variants.test.js` runs electron-updater's two providers against a
recorded network to hold this in place.

A variant asks `api.github.com` for the release list, without signing in:
GitHub allows 60 such requests an hour from one address, which the four
looks a day stay far below. A look that is refused is tried again at the next
one.

### Making one

```bash
git switch -c variant/voice desktop        # the experiment lives here
# ... change anything, anywhere in the repository ...
git push -u ntindle variant/voice
```

To build it locally, set the variable for the packaging step (the runtime
bundle is the same for every variant of one commit):

```bash
AUTOGPT_DESKTOP_VARIANT=voice npx electron-builder --config electron-builder.config.js --publish never
AUTOGPT_DESKTOP_VARIANT=voice npm start    # from the source tree, without packaging
```

On GitHub the slug is taken from the branch name: pushes to `variant/**` and
pull requests into such a branch are built as that variant by the same
workflows as `desktop`. Building, releasing, keeping a variant in step with
`desktop` and retiring one are in `docs/MAINTAINING.md` on `main`,
"Variants".

**A build of a variant branch without the slug is the normal app with the
experiment's code in it**, and would open the normal app's data. The
workflows refuse to make one: a build that names no variant must be of a
commit that is on `desktop`, or of a pull request. Do not install one you
built by hand on a machine whose AutoGPT data matters.

An installed app is the variant it was built as, whatever
`AUTOGPT_DESKTOP_VARIANT` says in the environment it is started from; the
variable only chooses when running from the source tree.

Not verified yet: installing two variants side by side, and an update of an
installed variant. Both need a build from the workflows.

## Building

Needs `uv`, and `pnpm` (on Windows; elsewhere the build fetches it).

```bash
cd autogpt_platform/desktop
uv run --python 3.13 --no-project build/build_runtime.py   # assembles build/runtime
npm install
npx electron-builder --config electron-builder.config.js --publish never   # writes dist/
```

That is a development build: version `0.0.0-dev.0`, unsigned on Windows,
ad-hoc signed on macOS. `AUTOGPT_DESKTOP_VERSION=1.2.3` in the environment
names a version; the other settings are at the top of
`electron-builder.config.js`.

`build_runtime.py` is a list of independent steps; `--only frontend,assets`
re-runs some of them. The frontend must be built on the OS it will run on.
The last step, `seal`, removes what a run from the bundle may have left in
it and refuses a bundle that would write into itself or fetch anything when
installed; run it again before packaging a bundle that has been run from.
`build/smoke_test.py` boots an assembled bundle, probes it and checks that
stopping it leaves no process behind and nothing in the bundle changed
(a file that came and went during the run shows in its directory's
modification time). It
starts the bundle three times on one data directory to prove the owner
account end to end (first sign-up is an admin; registration is closed after
a restart; a password reset takes effect); `--quick` stops after the first:

```bash
build/runtime/python/bin/python3 build/smoke_test.py build/runtime   # python\python.exe on Windows
```

The first start also puts the stack to work. Before it, the bundled backend
is checked against the service host's contract. While it is up: every
service answers on its own port; an agent of one calculator block runs to
the right result; a memory table is printed (per process and in total) and
held to the profile's budget; the process hosting the executor is killed
with a run in flight, and must come back, finish that run and take another;
the skills catalog publish must wait its minute. After each stop, every
service host's log must show that it was asked, that its services' cleanup
ran, and that it left by itself. `--profile isolated` runs all of it with a
process per service.

The runtime is started with no way out to the network through a proxy, so
a migration that tried to download an engine would fail. The data directory
is a temporary one, removed when the run ends, also when the run is
interrupted or told to end; `--data-dir` names one to use and keep, `--keep`
keeps the temporary one. The links a run made for paths with spaces are
removed either way. On macOS and Linux,
`--read-only` takes write permission off the whole bundle for the run, as a
system-wide install has it, and also fails on any service log that mentions
a refused write (not as root, whom permissions do not stop).

On Windows the bundled Valkey is built separately, inside MSYS2, into
`build/.cache/valkey-windows`:

```bash
bash build/valkey-windows.sh 8.1.10 build/.cache/valkey-windows
```

Without it the build stops. `--redis-stand-in` bundles a Redis build instead,
for local work only: it is not BSD-licensed, and Valkey cannot read the files
it writes.

Every download is pinned by SHA-256 in `build/artifacts.py` (the licence
texts shipped with ffmpeg, in `build/bundled_tools.py`).

## Tests

```bash
node --test "test/*.test.js"                    # shell <-> runtime contract, updates, packaging, signing rules
cd runtime && python -m pytest                  # config, ports, proxy, owner account, service hosts
```

The service host is tested against a stand-in backend
(`runtime/tests/fake_bundle`), so those tests need no backend and run in
seconds. Two more layers keep the stand-in honest: `test_upstream_names.py`
reads the names the desktop depends on out of the backend's source, and
fails with what to update when upstream renames one; and
`backend_contract.py` imports the real backend, in an interpreter of its
own, and checks every dependency of the host against it (run by
`test_backend_contract.py` when `build/runtime` exists, and by the smoke test
always).

A few of the shell tests read `electron-updater` and `@electron/osx-sign` to
notice when a new version of either drops something this relies on; they are
skipped until `npm install` has been run.

The proxy tests cover what nginx did for the appliance: route mapping,
unbuffered event streams, websockets, redirect rewriting.

## Running without the shell

The runtime is usable on its own, for example on a headless Linux box:

```bash
AUTOGPT_DESKTOP_DATA_DIR=~/autogpt-data build/runtime/python/bin/python3 -m autogpt_desktop serve
```

It prints one JSON object per line (`progress`, `ready` with the URL,
`error`) and stops when stdin closes or on Ctrl+C.

## Known limitations

- About 3 GB of RAM in use when idle (see [Memory](#memory)): three Python
  processes at roughly 850 MB each, because each one imports the whole
  backend. It was measured on Windows; macOS and Linux have yet to be.
- AutoPilot's sandboxed shell tool relies on bubblewrap and is unavailable
  outside Linux.
- Intel Macs are not supported (a locked dependency ships no x86_64 macOS
  wheel).
- No Windows build is signed by a known publisher yet, and a macOS build is
  only when its release had the certificate (see [Signing](#signing)).
  Signing, notarization, the release workflow and in-app updates are written
  and tested as far as they can be without a certificate and a release;
  none of them has been through a real release yet.
- On Linux only the runtime inside the packages has been run; the window
  itself has not been opened on a Linux desktop yet.
- An update, like installing a newer build over an old one by hand, keeps
  the data directory and applies database migrations on the next start.
  There is no way back to an older version once it has: older code does not
  know the newer database.
- An update is the whole installer again (700 to 900 MB); only the parts of
  it that changed are downloaded where the old installer is still cached.
- The Windows installer takes about ten minutes with Defender's real-time
  scanning on: the bundle is 57,000 files, a third of them bytecode.
- Quitting does not wait for an agent run in flight, and does not ask. The
  run is interrupted and picked up again at the next start if it began less
  than 24 hours ago (the interrupted step runs again); an older one is marked
  failed. There is no "runs are in progress" prompt yet, and no way to stop
  a run for good as the app closes.
- ImageMagick and a browser for AutoPilot's browsing tool are not bundled;
  the tools that need them fail without them.
- The Memory settings page talks to FalkorDB, which is not there.
- Running it as Administrator on Windows has not been tried on a real
  machine. PostgreSQL is started in the way that works for such an account
  (see above), from an ordinary account so far.
- A Windows user name longer than 20 characters can push bundled files past
  the 260-character path limit.
