#!/usr/bin/env bash
# Build valkey-server for Windows inside an MSYS2 shell.
#
#   valkey-windows.sh <valkey-version> <output-directory>
#
# Valkey publishes no Windows binaries and assumes POSIX throughout (fork for
# snapshots, rename over open files for the cluster config). MSYS2's runtime
# supplies those semantics; a native MSVC build was tried and cannot rename
# its own nodes.conf, which cluster mode does on every start. The result is
# valkey-server.exe plus the MSYS2 runtime DLL it needs beside it.
#
# The same approach the redis-windows project uses for Redis; Valkey is built
# here instead because it stays BSD-licensed.

set -euo pipefail

version="${1:?usage: valkey-windows.sh <valkey-version> <output-directory>}"
output="${2:?usage: valkey-windows.sh <valkey-version> <output-directory>}"

pacman -S --noconfirm --needed gcc make pkgconf tar >/dev/null

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
cd "${work}"
curl -fsSL "https://github.com/valkey-io/valkey/archive/refs/tags/${version}.tar.gz" | tar -xz
cd "valkey-${version}"

# dlfcn.h hides dladdr() behind __GNU_VISIBLE on this platform; the server's
# crash reporter uses it unconditionally.
sed -i 's/__GNU_VISIBLE/1/' /usr/include/dlfcn.h

make -j"$(nproc)" BUILD_TLS=no CFLAGS="-Wno-char-subscripts" valkey-server

mkdir -p "${output}"
cp src/valkey-server.exe "${output}/valkey-server.exe"
cp COPYING "${output}/COPYING"
# Everything the binary loads from the MSYS2 runtime, and nothing else.
ldd src/valkey-server.exe | awk '$3 ~ /^\/usr\/bin\// {print $3}' | sort -u |
  while read -r library; do cp "${library}" "${output}/"; done

"${output}/valkey-server.exe" --version
