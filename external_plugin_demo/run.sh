#!/usr/bin/env bash
# Builds this directory as an ordinary Conan+CMake consumer of `openusd`
# (conanfile.txt + CMakeLists.txt + find_package), then runs the plugin
# discovery checks. See README.md for what this proves and why.
#
# Requires the openusd version this conanfile.txt asks for to already be in
# the local Conan cache (or a remote), e.g.:
#   conan create recipes/openusd/all --version=26.08 --build=missing
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DEMO_DIR"

case "$(uname -s)" in
  Darwin|Linux) ;;
  *)
    echo "This demo only supports macOS and Linux." >&2
    exit 1
    ;;
esac

rm -rf build

echo "== conan install =="
conan install . --output-folder=build --build=missing "$@"

echo
echo "== cmake configure + build =="
cmake --preset conan-release
cmake --build --preset conan-release

# Locate the generators folder and built artifacts by name rather than by a
# hardcoded path: it depends on the generator/build_type Conan picked
# (cmake_layout's own convention), and we don't want to guess that here.
# Searched with an absolute root: PXR_PLUGINPATH_NAME anchors *relative*
# entries to wherever libusd_plug itself lives (see Plug_InitConfig), not to
# the caller's cwd, so PLUGIN_DIR below must be absolute.
GENERATORS_DIR="$(find "$DEMO_DIR/build" -type d -name generators | head -1)"
CONANRUN="$GENERATORS_DIR/conanrun.sh"
EXE="$(find "$DEMO_DIR/build" -type f -name test_external_plugin | head -1)"
PLUGIN_DIR="$(dirname "$(find "$DEMO_DIR/build" -type f -name 'libMyExternalPlugin.*' | head -1)")"

if [ -z "$GENERATORS_DIR" ] || [ -z "$EXE" ] || [ -z "$PLUGIN_DIR" ]; then
  echo "FAIL: could not locate the build output (generators dir, executable or plugin lib) under build/" >&2
  exit 1
fi

# Activates the same runtime environment (LD_LIBRARY_PATH/DYLD_LIBRARY_PATH
# etc.) that a `self.run(..., env="conanrun")` call would in a conanfile.py,
# so test_external_plugin can resolve libusd_tf/libusd_plug.
# shellcheck disable=SC1090
source "$CONANRUN"

echo
echo "== Negative control: without PXR_PLUGINPATH_NAME, the plugin must NOT be found =="
if env -u PXR_PLUGINPATH_NAME "$EXE"; then
  echo "FAIL: found the plugin without PXR_PLUGINPATH_NAME set (the test proves nothing)" >&2
  exit 1
fi
echo "  ok, not found (expected)"

echo
echo "== Positive: with PXR_PLUGINPATH_NAME pointing at the plugin's own directory =="
if ! PXR_PLUGINPATH_NAME="$PLUGIN_DIR" "$EXE"; then
  echo "FAIL: PlugRegistry did not discover/load the external plugin" >&2
  exit 1
fi
echo "  ok, discovered and loaded"

# shellcheck disable=SC1090
source "$GENERATORS_DIR/deactivate_conanrun.sh" 2>/dev/null || true

echo
echo "PASS: USD's Plug discovery works for a plugin this recipe never built"
