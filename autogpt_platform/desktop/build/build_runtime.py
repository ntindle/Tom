"""Assemble the desktop runtime for the platform this script runs on.

    python build_runtime.py [--out DIR] [--only step,step] [--skip step,step]

The result is the directory the Electron shell ships as resources/runtime
(layout: runtime/autogpt_desktop/layout.py). Each step is independent and
idempotent, so a failed build resumes with --only.

Needs on PATH: uv, and pnpm (via corepack or standalone) for the frontend.
Everything else, Node included, is downloaded pinned from artifacts.py.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import frontend_role_sql
import lock_export
from artifacts import ARTIFACTS, platform_key
from fetch import download, extract

DESKTOP = Path(__file__).resolve().parents[1]
PLATFORM = DESKTOP.parent
REPO = PLATFORM.parent
WINDOWS = sys.platform == "win32"
EXE = ".exe" if WINDOWS else ""

# Build-time settings of the frontend, identical to the appliance image
# (single-container/Dockerfile): every browser-facing URL is same-origin and
# the runtime's proxy decides where it lands.
FRONTEND_BUILD_ENV = {
    "NODE_ENV": "production",
    "NEXT_TELEMETRY_DISABLED": "1",
    "NEXT_PUBLIC_AGPT_SERVER_URL": "/_agpt/api",
    "NEXT_PUBLIC_AGPT_WS_SERVER_URL": "/_agpt/ws",
    "NEXT_PUBLIC_FRONTEND_BASE_URL": "",
    "NEXT_PUBLIC_APP_ENV": "local",
    "NEXT_PUBLIC_BEHAVE_AS": "LOCAL",
    "NEXT_PUBLIC_LAUNCHDARKLY_ENABLED": "false",
    "NEXT_PUBLIC_SOURCEMAPS": "false",
    "NEXT_PUBLIC_TURNSTILE": "disabled",
    "NEXT_PUBLIC_VAPID_PUBLIC_KEY": "",
    "NEXT_SKIP_BUILD_CHECKS": "true",
    "BETTER_AUTH_SECRET": "build-only-placeholder-not-used-at-runtime",
    "DATABASE_URL": "postgresql://build:build@127.0.0.1:1/postgres",
    "CI": "true",
}


class Build:
    def __init__(self, out: Path, cache: Path) -> None:
        self.out = out
        self.cache = cache
        self.artifacts = ARTIFACTS[platform_key()]
        self.python = out / "python" / ("python.exe" if WINDOWS else "bin/python3")
        self.site_packages = out / "python" / (
            "Lib/site-packages" if WINDOWS else "lib/python3.13/site-packages"
        )
        self.node = out / "node" / f"node{EXE}"

    def fetch(self, name: str) -> Path:
        return download(self.artifacts[name], self.cache)

    # --- interpreters -----------------------------------------------------

    def step_python(self) -> None:
        extract(self.fetch("python"), self.out / "python", strip_top_level=True)
        shutil.rmtree(self.out / "site", ignore_errors=True)  # see step_relocate
        # uv treats a standalone build as externally managed; this tree is
        # ours to install into.
        for marker in (self.out / "python").rglob("EXTERNALLY-MANAGED"):
            marker.unlink()

    def step_node(self) -> None:
        source = self.fetch("node")
        target = self.out / "node"
        if WINDOWS:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, self.node)
            return
        extract(source, self.cache / "node-dist", strip_top_level=True)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.cache / "node-dist" / "bin" / "node", self.node)
        shutil.copy2(self.cache / "node-dist" / "LICENSE", target / "LICENSE")

    # --- AutoGPT backend --------------------------------------------------

    def step_deps(self) -> None:
        requirements = self.cache / "requirements.txt"
        lines = lock_export.export(PLATFORM / "backend" / "poetry.lock")
        requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")
        uv = ["uv", "pip", "install", "--python", str(self.python), "--break-system-packages"]
        run([*uv, "-r", str(requirements)])
        run([*uv, "--no-deps", str(PLATFORM / "autogpt_libs")])

    def step_backend(self) -> None:
        target = self.out / "backend"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        source = PLATFORM / "backend"
        shutil.copytree(
            source / "backend",
            target / "backend",
            ignore=shutil.ignore_patterns("__pycache__", "*_test.py", "snapshots"),
        )
        shutil.copytree(source / "migrations", target / "migrations")
        shutil.copy2(source / "schema.prisma", target / "schema.prisma")
        # backend/util/docs.py finds the docs by walking up to a `docs/platform`
        # directory; markdown only, as in the backend Dockerfile.
        docs = self.out / "docs"
        if docs.exists():
            shutil.rmtree(docs)
        for path in (REPO / "docs").rglob("*"):
            if path.suffix in (".md", ".mdx") and path.is_file():
                destination = docs / path.relative_to(REPO / "docs")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)

    def step_prisma(self) -> None:
        """Generate the Prisma client into the bundled interpreter and keep
        the CLI and engines it downloaded, so nothing is fetched at runtime."""
        prisma_cache = self.cache / f"prisma-{platform_key()}"
        scripts = self.python.parent / "Scripts" if WINDOWS else self.python.parent
        env = {
            **os.environ,
            "PRISMA_BINARY_CACHE_DIR": str(prisma_cache),
            # The Prisma CLI spawns the `prisma-client-py` generator by name
            # and needs a node; both come from the bundle being built.
            "PATH": os.pathsep.join(
                [str(scripts), str(self.python.parent), str(self.node.parent), os.environ["PATH"]]
            ),
        }
        run([str(self.python), "-m", "prisma", "generate"], cwd=self.out / "backend", env=env)

        target = self.out / "prisma"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        modules = _single(prisma_cache.rglob("node_modules/prisma/build/index.js")).parents[2]
        shutil.copytree(
            modules,
            target / "node_modules",
            ignore=shutil.ignore_patterns(".cache", "*.dll.node", "*.so.node", "*.dylib.node"),
        )
        engines = modules / "@prisma" / "engines"
        for kind in ("query-engine", "schema-engine"):
            engine = _single(
                path for path in engines.glob(f"{kind}-*") if path.suffix in ("", ".exe")
            )
            shutil.copy2(engine, target / f"{kind}{EXE}")

    # --- infrastructure ---------------------------------------------------

    def step_postgres(self) -> None:
        extract(self.fetch("postgres"), self.out / "postgres", strip_top_level=True)
        shutil.rmtree(self.out / "postgres" / "include", ignore_errors=True)

    def step_valkey(self) -> None:
        source = self.fetch("valkey")
        target = self.out / "valkey"
        staging = self.cache / "valkey-dist"
        extract(source, staging, strip_top_level=True)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        if WINDOWS:
            shutil.copy2(staging / "redis-server.exe", target / "valkey-server.exe")
            for library in staging.glob("*.dll"):
                shutil.copy2(library, target / library.name)
        elif sys.platform == "darwin":
            run(["make", f"-j{os.cpu_count() or 2}", "BUILD_TLS=no", "valkey-server"], cwd=staging)
            shutil.copy2(staging / "src" / "valkey-server", target / "valkey-server")
            shutil.copy2(staging / "COPYING", target / "COPYING")
        else:
            shutil.copy2(staging / "bin" / "valkey-server", target / "valkey-server")

    def step_erlang(self) -> None:
        target = self.out / "erlang"
        extract(self.fetch("erlang"), target, strip_top_level=not WINDOWS)
        if WINDOWS:
            _add_msvc_runtime(target)
            (target / "vc_redist.exe").unlink(missing_ok=True)
        elif sys.platform.startswith("linux"):
            _make_erlang_relocatable(target)
        for unused in ("doc", "usr/include"):
            shutil.rmtree(target / unused, ignore_errors=True)

    def step_rabbitmq(self) -> None:
        extract(self.fetch("rabbitmq"), self.out / "rabbitmq", strip_top_level=True)

    # --- frontend ---------------------------------------------------------

    def step_frontend(self) -> None:
        frontend = PLATFORM / "frontend"
        env = {**os.environ, **FRONTEND_BUILD_ENV}
        # A flat node_modules: pnpm's default symlink farm does not survive
        # being copied into an installer (Windows junctions store absolute
        # paths, and symlinks need elevation to create).
        pnpm = self._pnpm(env)
        run(
            [
                *pnpm,
                "install",
                "--frozen-lockfile",
                "--config.node-linker=hoisted",
                "--config.confirm-modules-purge=false",
            ],
            cwd=frontend,
            env=env,
        )
        run([*pnpm, "run", "generate:api"], cwd=frontend, env=env)
        shutil.rmtree(frontend / ".next", ignore_errors=True)
        run([*pnpm, "build"], cwd=frontend, env=env)

        target = self.out / "frontend"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(frontend / ".next" / "standalone", target, symlinks=False)
        shutil.copytree(frontend / ".next" / "static", target / ".next" / "static")
        shutil.copytree(frontend / "public", target / "public")
        shutil.rmtree(target / ".next" / "cache", ignore_errors=True)
        # webpack's build cache is several GB and only speeds up a rebuild.
        shutil.rmtree(frontend / ".next" / "cache", ignore_errors=True)

    def _pnpm(self, env: dict[str, str]) -> list[str]:
        if WINDOWS:
            # pnpm on Windows is a .cmd/.ps1 shim, which CreateProcess cannot run.
            return ["cmd", "/c", "pnpm"]
        if shutil.which("pnpm"):
            return ["pnpm"]
        # No pnpm installed: the pinned Node distribution that step_node
        # unpacked carries corepack, which fetches the pnpm version the
        # frontend's package.json names. The shims go in a directory on PATH
        # because the frontend's own scripts call `pnpm` again.
        node_bin = self.cache / "node-dist" / "bin"
        shims = self.cache / "corepack-bin"
        shims.mkdir(parents=True, exist_ok=True)
        env["PATH"] = os.pathsep.join([str(shims), str(node_bin), env["PATH"]])
        env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
        run(
            [str(node_bin / "corepack"), "enable", "--install-directory", str(shims), "pnpm"],
            env=env,
        )
        return [str(shims / "pnpm")]

    # --- glue -------------------------------------------------------------

    def step_assets(self) -> None:
        assets = self.out / "assets"
        (assets / "python").mkdir(parents=True, exist_ok=True)
        appliance = PLATFORM / "single-container"
        shutil.copy2(PLATFORM / "db" / "init" / "00-init.sql", assets / "00-init.sql")
        shutil.copy2(appliance / "runtime_config.py", assets / "runtime_config.py")
        shutil.copy2(
            appliance / "python" / "sitecustomize.py", assets / "python" / "sitecustomize.py"
        )
        (assets / "frontend-role.sql").write_text(
            frontend_role_sql.extract(appliance / "bootstrap.sh"), encoding="utf-8"
        )
        shutil.copy2(PLATFORM / "LICENSE.md", self.out / "LICENSE.md")

        package = self.out / "autogpt_desktop"
        if package.exists():
            shutil.rmtree(package)
        shutil.copytree(
            DESKTOP / "runtime" / "autogpt_desktop",
            package,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        manifest = {
            "win32": {"command": "python/python.exe", "args": ["-m", "autogpt_desktop", "serve"]},
            "default": {"command": "python/bin/python3", "args": ["-m", "autogpt_desktop", "serve"]},
        }
        (self.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


    # --- size and startup ------------------------------------------------

    def step_prune(self) -> None:
        """Drop what the runtime never loads. Erlang/OTP ships every
        application it has (a GUI toolkit, SNMP, CORBA-era protocols);
        RabbitMQ needs the handful in ERLANG_APPS."""
        for application in (self.out / "erlang" / "lib").iterdir():
            name = application.name.rsplit("-", 1)[0]
            if name not in ERLANG_APPS:
                shutil.rmtree(application)
                continue
            for unused in ("doc", "examples", "src", "c_src", "emacs"):
                shutil.rmtree(application / unused, ignore_errors=True)
        # The engines the runtime uses were copied to prisma/ by step_prisma;
        # the copies npm left inside node_modules are dead weight.
        for engine in (self.out / "prisma" / "node_modules").rglob("*-engine-*"):
            if engine.is_file() and engine.stat().st_size > 1_000_000:
                engine.unlink()
        for cache in (self.out / "python").rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(self.out / "python" / "include", ignore_errors=True)
        over_budget = [
            str(path.relative_to(self.out))
            for path in self.out.rglob("*")
            if path.is_file()
            and len(str(path.relative_to(self.out))) - RELOCATION_SAVING > MAX_RELATIVE_PATH
        ]
        if over_budget:
            raise RuntimeError(f"paths too long for a Windows install: {over_budget[:5]}")

    def step_relocate(self) -> None:
        """Move third-party packages from python/Lib/site-packages to site/.

        Windows limits a path to 260 characters and the bundle installs under
        the user's profile. Some SDKs generate module names over 150
        characters long; 20 characters saved on every path keeps the longest
        one inside the limit for any Windows user name. A .pth file makes the
        interpreter treat site/ exactly like site-packages (it runs the moved
        packages' own .pth files too, which pywin32 depends on)."""
        target = self.out / "site"
        if target.exists():
            shutil.rmtree(target)
        shutil.move(self.site_packages, target)
        self.site_packages.mkdir()
        (self.site_packages / "autogpt-desktop.pth").write_text(
            "import os, site, sys; "
            'site.addsitedir(os.path.join(sys.prefix, os.pardir, "site"))\n',
            encoding="utf-8",
        )

    def step_compile(self) -> None:
        """Ship bytecode. uv installs none, and the installed bundle is
        read-only, so without this every service would recompile every module
        on every start (measured: 51s to ready without, 34s with).
        `unchecked-hash` trusts the .pyc without stat-ing its source, which
        suits files an installer may give fresh timestamps."""
        library = self.python.parent / "Lib" if WINDOWS else self.out / "python" / "lib"
        run(
            [
                str(self.python),
                "-m",
                "compileall",
                "-q",
                "-j",
                "0",
                "--invalidation-mode",
                "unchecked-hash",
                str(library),
                str(self.out / "site"),
                str(self.out / "backend" / "backend"),
                str(self.out / "autogpt_desktop"),
            ],
            check=False,  # a few vendored test fixtures are intentionally invalid
        )
        # __pycache__/name.cpython-313.pyc is 24 characters longer than its
        # source. Where that would break the path budget, ship the source
        # alone; those few modules compile in memory when imported.
        for compiled in self.out.rglob("*.pyc"):
            if len(str(compiled.relative_to(self.out))) > MAX_RELATIVE_PATH:
                compiled.unlink()


# The install prefix on Windows is at most 80 characters
# (C:\Users\<20-character name>\AppData\Local\Programs\AutoGPT\resources\runtime\),
# which leaves this much of the 260-character limit for paths inside the bundle.
MAX_RELATIVE_PATH = 170
# What step_relocate takes off a site-packages path (prune runs before it).
RELOCATION_SAVING = len("python/Lib/site-packages") - len("site")

# OTP applications RabbitMQ 4.1 and its Elixir-based CLI load.
ERLANG_APPS = {
    "asn1",
    "compiler",
    "crypto",
    "eldap",
    "erts",
    "inets",
    "kernel",
    "mnesia",
    "os_mon",
    "public_key",
    "runtime_tools",
    "sasl",
    "ssl",
    "stdlib",
    "syntax_tools",
    "tools",
    "xmerl",
}

STEPS = (
    "python",
    "node",
    "deps",
    "backend",
    "prisma",
    "postgres",
    "valkey",
    "erlang",
    "rabbitmq",
    "frontend",
    "assets",
    "prune",
    "relocate",
    "compile",
)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> None:
    print(f"  $ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=check)


def _single(paths) -> Path:
    found = list(paths)
    if len(found) != 1:
        raise RuntimeError(f"expected exactly one match, found {found}")
    return found[0]


def _add_msvc_runtime(erlang: Path) -> None:
    """Erlang's Windows binaries link the MSVC runtime dynamically and ship
    an installer for it instead of the DLLs. Put the DLLs beside the
    binaries so a machine without the redistributable can still run them."""
    system32 = Path(os.environ["SYSTEMROOT"]) / "System32"
    for bin_dir in (erlang / "bin", *erlang.glob("erts-*/bin")):
        for name in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
            shutil.copy2(system32 / name, bin_dir / name)


def _make_erlang_relocatable(erlang: Path) -> None:
    """The hex.pm Linux build expects `./Install` to write its absolute path
    into the launcher scripts. Derive the path at run time instead."""
    relocating = 'ROOTDIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"\n'
    erts = _single(erlang.glob("erts-*"))
    for source, target in (
        (erts / "bin" / "erl.src", erlang / "bin" / "erl"),
        (erts / "bin" / "erl.src", erts / "bin" / "erl"),
        (erts / "bin" / "start.src", erlang / "bin" / "start"),
    ):
        script = source.read_text(encoding="utf-8")
        script = script.replace('ROOTDIR="%FINAL_ROOTDIR%"\n', relocating).replace(
            "%EMU%", "beam"
        )
        if target.parent == erts / "bin":
            script = script.replace(relocating, relocating.replace("/..", "/../.."))
        target.write_text(script, encoding="utf-8")
        target.chmod(0o755)
    for name in ("epmd", "run_erl", "to_erl", "erlc", "escript"):
        link = erlang / "bin" / name
        link.unlink(missing_ok=True)
        link.symlink_to(Path("..") / erts.name / "bin" / name)
    for boot in ("start.boot", "start_clean.boot", "start_sasl.boot", "no_dot_erlang.boot"):
        release = _single(erlang.glob(f"releases/*/{boot}"))
        shutil.copy2(release, erlang / "bin" / boot)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DESKTOP / "build" / "runtime")
    parser.add_argument("--cache", type=Path, default=DESKTOP / "build" / ".cache")
    parser.add_argument("--only", default="")
    parser.add_argument("--skip", default="")
    args = parser.parse_args()
    only = [name for name in args.only.split(",") if name]
    skip = {name for name in args.skip.split(",") if name}
    unknown = (set(only) | skip) - set(STEPS)
    if unknown:
        parser.error(f"unknown steps: {sorted(unknown)}")

    build = Build(args.out.resolve(), args.cache.resolve())
    build.out.mkdir(parents=True, exist_ok=True)
    for name in only or STEPS:
        if name in skip:
            continue
        print(f"[{name}]", flush=True)
        getattr(build, f"step_{name}")()
    print(f"runtime assembled for {platform_key()} at {build.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
