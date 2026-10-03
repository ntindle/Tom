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
| supervisord, two stop tiers | `supervisor.py`, reverse start order |
| three Valkey nodes | one node owning all 16384 slots (the backend's `RedisCluster` client cannot tell) |
| nginx | `proxy.py` (same routes, streamed) |
| frontend role over a Unix socket | the same role policy, with a generated password over loopback TCP |
| container exit kills everything | a Job Object on Windows; a recorded-PID sweep elsewhere |
| FalkorDB / Graphiti memory | not included (Linux-only module, SSPL) |
| chat-bot bridge services | not included |

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
Fedora 36, or newer).

## Where data lives

| | |
| --- | --- |
| Windows | `%LOCALAPPDATA%\AutoGPT` |
| macOS | `~/Library/Application Support/AutoGPT` |
| Linux | `$XDG_DATA_HOME/AutoGPT` (default `~/.local/share/AutoGPT`) |

`config/settings.env` holds provider keys (tray menu → *Settings file*);
`logs/` has one file per service. Uninstalling the app leaves this directory
in place.

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

On Windows the bundled Valkey is built separately, inside MSYS2:

```bash
bash build/valkey-windows.sh 8.1.10 <output-directory>
```

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
- Not code-signed: Windows SmartScreen and macOS Gatekeeper will warn.
- No in-place updater yet; installing a newer build over an old one keeps
  the data directory and applies database migrations on first start.
- The Windows installer takes about ten minutes with Defender's real-time
  scanning on: the bundle is 57,000 files, a third of them bytecode.
- A few third-party builds are not pinned by checksum yet; the build prints
  `UNPINNED` with the digest it saw for each.
