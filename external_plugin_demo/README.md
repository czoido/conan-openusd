# External plugin discovery demo

`test_package` only proves that USD's `Plug` registry can discover and load
plugins **shipped inside this recipe's own package** (e.g. `hioOpenEXR`),
without ever linking their `.so`/`.dylib` directly. It says nothing about
whether a real consumer can drop their **own** plugin (a custom file format
reader, a custom Hydra render delegate, a custom shader parser...) on top of
this package and have it get discovered the same way.

This directory answers that question the way a real third-party plugin
author actually would: as an ordinary Conan+CMake **consumer** of the
`openusd` package (`conanfile.txt` + `find_package(openusd)`), building a
plugin that the recipe, its `components/<version>.json`, and its
`test_package` never see or build:

- [`conanfile.txt`](conanfile.txt) + [`CMakeLists.txt`](CMakeLists.txt):
  nothing but `[requires] openusd/26.08` and two CMake targets, exactly what
  you'd write for any project depending on this package.
- [`myplugin.cpp`](myplugin.cpp) registers a trivial `TfType`
  (`MyExternalPlugin`) and nothing else. Linked only against `openusd::tf`.
- [`myplugin/plugInfo.json.in`](myplugin/plugInfo.json.in) is its manifest,
  rendered by `CMakeLists.txt` via `configure_file()` the same way upstream's
  own `_plugInfo_subst()` renders the recipe's built-in plugins' manifests.
- [`main.cpp`](main.cpp) asks `PlugRegistry` for a plugin named
  `myExternalPlugin`, loads it, and checks that `MyExternalPlugin` actually
  got registered. Linked against `openusd::tf` and `openusd::plug`, **never**
  against `MyExternalPlugin`.

The mechanism being tested is `PXR_PLUGINPATH_NAME`: an environment variable
listing extra directories where `Plug` should look for `plugInfo.json`, on
top of the package's own `lib/usd` and `plugin/usd`. It's the way any
consumer would register their own plugins alongside this package.

## Running it

Needs the `openusd` version `conanfile.txt` requires already in the Conan
cache (or a remote):

```bash
conan create recipes/openusd/all --version=26.08 --build=missing
./external_plugin_demo/run.sh
```

`run.sh` runs `conan install .` (forwarding any extra arguments to it, e.g.
`-o`/`-s` if you built `openusd` with non-default options/settings, so it
resolves the exact same binary instead of triggering a rebuild) and
`cmake --build --preset conan-release`, then runs the harness twice:

1. **Without** `PXR_PLUGINPATH_NAME` set: the plugin must **not** be found.
   This is a control, without it a passing "positive" run wouldn't mean
   anything (the plugin could be getting picked up by accident from some
   other fallback path).
2. **With** `PXR_PLUGINPATH_NAME` pointing at the plugin's own directory: it
   must be found, loaded, and its `TfType` registration must have run.

Only macOS and Linux are supported (plain `.so`/`.dylib` + `PXR_PLUGINPATH_NAME`
with a POSIX path list). Windows needs `cl.exe`-specific shared library
flags and DLL search-path handling this script doesn't attempt, matching the
same macOS/Linux-only scope as `verify_requires.py`.

`conanfile.txt` hardcodes `openusd/26.08` since that's the only version
`recipes/openusd/config.yml` lists today; bump it there if/when a second
version is added (a `conanfile.txt` can't take the version as a parameter).
