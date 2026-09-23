# conan-openusd

Testing OpenUSD recipe for Conan

## `generate_components.py`

Generates `recipes/openusd/all/components/<version>.json`, the Conan component
dependency graph (`requires`, platform/option conditions) that `conanfile.py`'s
`package_info()` loads. It's generated instead of hand-maintained because it
parses upstream's own `pxr_library()`/ `pxr_plugin()` calls and
`add_subdirectory()` reachability directly out of the OpenUSD source tree for
that version, rather than having ~80 libraries' worth of dependencies drift
silently in a hand-written Python dict.

It stays in this repo, not in conan-center-index: the CCI PR only ships the
generated `.json` (via `exports = "components/*.json"` in the recipe), never the
generator itself.

### Regenerating for a new version

Whenever a new version is added to `recipes/openusd/config.yml`, its component
file must be generated (and the `.json` committed to the CCI PR) too:

```
curl -L -o openusd-X.Y.tar.gz \
    https://github.com/PixarAnimationStudios/OpenUSD/archive/refs/tags/vX.Y.tar.gz
mkdir src && tar xzf openusd-X.Y.tar.gz -C src --strip-components=1

python3 generate_components.py src X.Y recipes/openusd/all/components/X.Y.json
```

`package_info()` raises `ConanInvalidConfiguration` with this exact command if
`components/<version>.json` is missing, so a version bump without running it
fails immediately and loudly instead of silently reusing stale data.

The script aborts (rather than guessing) whenever it hits CMake it can't
mechanically classify: an unrecognized `LIBRARIES` token, a boolean condition it
can't reduce to a simple AND of `is_apple` / `with_openimageio` /
`with_materialx`, and so on. Read the error, and either extend
`EXTERNAL_TOKEN_MAP` / `FIXED_VARS` in the script (for a new upstream option or
external dependency) or investigate further before assuming the output is
correct. After it succeeds, diff the new file against the previous version's to
sanity-check the change is what you'd expect from that version's release notes.

## `verify_requires.py`

Ground-truth check for the generated JSON, run against an *actual* built
package: for every `libusd_<component>.{so,dylib}`, asks the linker what it
really needs (`otool -L` / `readelf -d`) and checks that's reachable (directly
or transitively) from what the JSON declares. Catches a missing `requires` that
`generate_components.py` alone can't, since it's checking the real build output,
not just parsed source.

```
conan create recipes/openusd/all --version=X.Y -o openusd/*:with_openimageio=True -o openusd/*:with_materialx=True -o materialx/*:shared=True --build=missing
conan list "openusd/X.Y:*"                       # get the package's rrev:pkgid
conan cache path "openusd/X.Y#<rrev>:<pkgid>"    # get its package folder

python3 verify_requires.py <package_folder> recipes/openusd/all/components/X.Y.json
```

Run it once per option combination that changes which components get built (at
minimum: default options, and `with_openimageio=True with_materialx=True`
together). A "MISSING requires" line is a real bug in the JSON; ignore anything
that turns out to be a build-system library (e.g. an OS framework pulled in
transitively by a third-party dependency), not one of USD's own components.
