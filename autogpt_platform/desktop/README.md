# AutoGPT Desktop (experimental)

The AutoGPT Platform as an installable desktop application: a Windows
installer, a macOS disk image, a Linux AppImage. No Docker, no virtual
machine, no configuration file to edit before the first start.

It is the [single-container appliance](../single-container/README.md) with
each Linux process replaced by a native one, started and stopped by a small
supervisor instead of supervisord.

> Experimental. It runs the same code as the appliance, but it has had far
> less testing, and Windows and macOS builds are not code-signed yet.

## How it fits together

```
AutoGPT.exe / AutoGPT.app            Electron shell (src/)
  └─ runtime process                  Python supervisor (runtime/autogpt_desktop/)
       ├─ PostgreSQL + pgvector
       ├─ Valkey (one node, cluster mode)
       ├─ RabbitMQ on a bundled Erlang
       ├─ 8 AutoGPT backend services
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
| three Valkey nodes | one node owning all 16384 slots (the backend's `RedisCluster` client cannot tell) |
| nginx | `proxy.py` (same routes, streamed) |
| frontend role over a Unix socket | the same role policy, with a generated password over loopback TCP |
| container exit kills everything | a Job Object on Windows; process groups and a recorded-PID sweep elsewhere |
| `/data/home` as the services' home | `home/` in the data directory, so AutoPilot never finds the user's own Claude Code or Codex sign-in |
| FalkorDB / Graphiti memory | not included (Linux-only module, SSPL) |
| chat-bot bridge services | not included |
| sign-in with Google, GitHub, Discord | not included: email and password only |

**Stopping.** Quitting takes about five seconds. The backend services are
asked to stop together and killed after three seconds, the appliance's own
finding being that they finish their cleanup in well under one and then wait
on telemetry teardown. PostgreSQL, Valkey and RabbitMQ are then shut down
properly. The shell kills a runtime that has not stopped after a minute,
unless the runtime says it needs longer, which it does for exactly one thing:
a database migration, which is never interrupted. A first start that is cut
short anyway (power loss) is detected and redone from scratch on the next
one, since nobody has used that database yet.

Everything listens on `127.0.0.1` only, including sockets that live for
microseconds: `build/erlang_patches.py` explains the two places where Erlang
and RabbitMQ would otherwise touch every interface, which is enough for
Windows Firewall to prompt on first launch. Ports are chosen free on first
start (from 15000–32000, below every OS's ephemeral range) and remembered, so
the app's origin stays stable.

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
Fedora 36, or newer). The AppImage also needs FUSE 2 (`libfuse2` on Ubuntu).

## Where data lives

| | |
| --- | --- |
| Windows | `%LOCALAPPDATA%\AutoGPT` |
| macOS | `~/Library/Application Support/AutoGPT` |
| Linux | `$XDG_DATA_HOME/AutoGPT` (default `~/.local/share/AutoGPT`) |

`config/settings.env` holds provider keys (tray menu → *Settings file*);
`logs/` has one file per service. Uninstalling the app leaves this directory
in place. It is readable by its owner only.

Anyone who can reach `127.0.0.1` on the machine can create an account until
`AUTH_ALLOW_NEW_ACCOUNTS=false` is set in `settings.env`; do that once yours
exists if the computer is shared.

The data belongs to the PostgreSQL major version that created it (18 on
Windows and macOS, 16 on Linux). A build with a different major refuses to
start and says why; changing the bundled major needs a migration path first.

## Building

Needs `uv`, and `pnpm` (on Windows; elsewhere the build fetches it).

```bash
cd autogpt_platform/desktop
uv run --python 3.13 --no-project build/build_runtime.py   # assembles build/runtime
npm install
npx electron-builder --publish never                       # writes dist/
```

`build_runtime.py` is a list of independent steps; `--only frontend,assets`
re-runs some of them. The frontend must be built on the OS it will run on.
`build/smoke_test.py` boots an assembled bundle, probes it and checks that
stopping it leaves no process behind:

```bash
build/runtime/python/bin/python3 build/smoke_test.py build/runtime   # python\python.exe on Windows
```

On Windows the bundled Valkey is built separately, inside MSYS2, into
`build/.cache/valkey-windows`:

```bash
bash build/valkey-windows.sh 8.1.10 build/.cache/valkey-windows
```

Without it the build stops. `--redis-stand-in` bundles a Redis build instead,
for local work only: it is not BSD-licensed, and Valkey cannot read the files
it writes.

Every download is pinned by SHA-256 in `build/artifacts.py`.

## Tests

```bash
node --test "test/*.test.js"                    # shell <-> runtime contract
cd runtime && python -m pytest                  # config, ports, proxy
```

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

- About 6 GB of RAM in use: ten Python processes at roughly 600 MB each,
  because each one imports the whole backend.
- AutoPilot's sandboxed shell tool relies on bubblewrap and is unavailable
  outside Linux.
- Intel Macs are not supported (a locked dependency ships no x86_64 macOS
  wheel).
- Not signed by a known publisher. Windows SmartScreen warns (*More info* →
  *Run anyway*). The macOS build has an ad-hoc signature only: enough for a
  downloaded copy to count as intact rather than "damaged", not enough to
  be trusted, so macOS refuses the first launch until it is allowed under
  *System Settings → Privacy & Security → Open Anyway*.
- On Linux only the runtime inside the packages has been run; the window
  itself has not been opened on a Linux desktop yet.
- No in-place updater yet; installing a newer build over an old one keeps
  the data directory and applies database migrations on first start.
- The Windows installer takes about ten minutes with Defender's real-time
  scanning on: the bundle is 57,000 files, a third of them bytecode.
- On Windows a service is stopped by ending its process, so an agent run in
  flight when the app quits is left marked as running. Elsewhere services
  get three seconds, which a run in flight will not finish in either.
- There is no admin account: nothing here does what the appliance's
  `autogpt-admin promote` does.
- `ffmpeg`, ImageMagick and a browser for AutoPilot's browsing tool are not
  bundled; the blocks and tools that need them fail without them.
- Two things are written into the install directory, which the appliance
  redirects with symlinks and this does not yet: saved admin settings
  (`backend/config.json`) and the Next image cache. Neither survives an
  upgrade, and a read-only install (the `.deb`) cannot write them at all.
- The Memory settings page talks to FalkorDB, which is not there.
- Do not run it as Administrator on Windows: PostgreSQL refuses to start.
- A Windows user name longer than 20 characters can push bundled files past
  the 260-character path limit.
