#!/usr/bin/env bash
# Populate ./agora-sdk with the Agora Go Server SDK module + the matching
# native C SDK for this OS. Idempotent: re-running is a no-op if
# ./agora-sdk/agora_sdk already exists.
#
# go.mod has `replace github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2
# => ./agora-sdk`, so go build/run resolves the SDK from this directory.
# The directory is gitignored (per-machine).
#
# After running, set the runtime library search path:
#   Linux:  export LD_LIBRARY_PATH=$PWD/agora-sdk/agora_sdk:$LD_LIBRARY_PATH
#   macOS:  export DYLD_LIBRARY_PATH=$PWD/agora-sdk/agora_sdk_mac:$DYLD_LIBRARY_PATH

set -euo pipefail

cd "$(dirname "$0")"

if [ -d "agora-sdk/agora_sdk" ]; then
    echo "agora-sdk already populated; nothing to do"
    exit 0
fi

if ! command -v go >/dev/null 2>&1; then
    echo "go not found on PATH; install Go 1.21+ first" >&2
    exit 1
fi

# Pull the SDK module version and tidy module reference from go.mod
SDK_VERSION=$(awk '/^[[:space:]]*github\.com\/AgoraIO-Extensions\/Agora-Golang-Server-SDK\/v2/ {print $2; exit}' go.mod)
if [ -z "${SDK_VERSION:-}" ]; then
    echo "could not find Agora-Golang-Server-SDK version in go.mod" >&2
    exit 1
fi

# Step 1: download the Go module into GOMODCACHE without our replace getting
# in the way. We do that in a scratch copy of the project so we don't touch
# the real go.mod / go.sum on disk.
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

cp go.mod go.sum "$SCRATCH/"
# Strip any local replace pointing at ./agora-sdk so the download resolves.
grep -v 'AgoraIO-Extensions/Agora-Golang-Server-SDK/v2 =>' "$SCRATCH/go.mod" \
    > "$SCRATCH/go.mod.new"
mv "$SCRATCH/go.mod.new" "$SCRATCH/go.mod"

(
    cd "$SCRATCH"
    # Single-module download keeps the cache warm but doesn't try to build us.
    go mod download "github.com/AgoraIO-Extensions/Agora-Golang-Server-SDK/v2@${SDK_VERSION}"
)

# Step 2: copy the cached module into a writable ./agora-sdk so we can drop
# the native SDK in alongside it.
GOMODCACHE=$(go env GOMODCACHE)
SDK_SRC="${GOMODCACHE}/github.com/!agora!i!o-!extensions/!agora-!golang-!server-!s!d!k/v2@${SDK_VERSION}"
if [ ! -d "$SDK_SRC" ]; then
    echo "Module not found in cache: $SDK_SRC" >&2
    exit 1
fi
cp -a "$SDK_SRC" agora-sdk
chmod -R u+w agora-sdk

# Step 3: download the matching native C SDK into ./agora-sdk/agora_sdk
# (and ./agora-sdk/agora_sdk_mac on macOS). The script ships with the SDK
# release; it picks up the OS automatically.
( cd agora-sdk && bash scripts/install_agora_sdk.sh )

echo
echo "agora-sdk ready under $(pwd)/agora-sdk"
case "$(uname -s)" in
    Linux*)
        echo "  export LD_LIBRARY_PATH=$(pwd)/agora-sdk/agora_sdk:\$LD_LIBRARY_PATH" ;;
    Darwin*)
        echo "  export DYLD_LIBRARY_PATH=$(pwd)/agora-sdk/agora_sdk_mac:\$DYLD_LIBRARY_PATH" ;;
esac
