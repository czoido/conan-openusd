#!/usr/bin/env python3
"""
Cross-checks components/<version>.json against the REAL linked dependencies
of an actual built openusd package: for every libusd_<comp>.{so,dylib}, asks
the linker what it actually needs (otool -L / readelf -d) and compares that
against the "requires" declared for that component in the JSON.

A real internal dependency missing from "requires" is a bug (the component
would fail to link/resolve symbols for a consumer that only pulls in what
Conan declares). A declared "requires" that isn't a real link-time need is
usually harmless (over-declaration) and only printed as a note.

Usage: verify_requires.py <package_folder> <components.json>
"""
import json
import platform
import re
import subprocess
import sys
from pathlib import Path

IS_MACOS = platform.system() == "Darwin"
LIB_RE = re.compile(r"libusd_(\w+)\.(so|dylib)(\.\d+)*$")
PLUGIN_RE = re.compile(r"^(\w+)\.(so|dylib)$")


def linked_libs(path):
    if IS_MACOS:
        out = subprocess.run(["otool", "-L", str(path)], capture_output=True, text=True, check=True).stdout
        names = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            libpath = line.split(" (")[0]
            names.append(Path(libpath).name)
        return names
    else:
        out = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True, check=True).stdout
        names = []
        for line in out.splitlines():
            m = re.search(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", line)
            if m:
                names.append(m.group(1))
        return names


def to_component(basename, plugin_names):
    m = LIB_RE.match(basename)
    if m:
        return m.group(1)
    m = PLUGIN_RE.match(basename)
    if m and m.group(1) in plugin_names:
        return m.group(1)
    return None


def component_file(pkg, comp_name, comp_info):
    ext = "dylib" if IS_MACOS else "so"
    if comp_info.get("is_plugin"):
        return pkg / "plugin" / "usd" / f"{comp_name}.{ext}"
    return pkg / "lib" / f"libusd_{comp_name}.{ext}"


def declared_internal_requires(comp_info, components):
    reqs = set(comp_info.get("requires", []))
    for extra in comp_info.get("conditional", []):
        reqs |= set(extra.get("requires", []))
    return {r for r in reqs if "::" not in r and r in components}


def transitive_closure(comp_name, direct, cache):
    if comp_name in cache:
        return cache[comp_name]
    cache[comp_name] = set()  # guard against cycles
    result = set()
    for dep in direct.get(comp_name, ()):
        result.add(dep)
        result |= transitive_closure(dep, direct, cache)
    cache[comp_name] = result
    return result


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    pkg = Path(sys.argv[1])
    components = json.loads(Path(sys.argv[2]).read_text())

    # A real .so ends up NEEDING not just its own direct LIBRARIES, but also
    # whatever those dependencies themselves publicly/transitively pull in
    # (normal C++ shared-library propagation) -- Conan's `requires` models
    # the same transitive propagation via CMakeDeps, so the correct
    # comparison is against the *transitive* closure of declared requires,
    # not just the direct list.
    direct = {name: declared_internal_requires(info, components) for name, info in components.items()}
    cache = {}
    transitive = {name: transitive_closure(name, direct, cache) for name in components}
    plugin_names = {name for name, info in components.items() if info.get("is_plugin")}

    missing_total = 0
    checked = 0
    for comp_name, comp_info in sorted(components.items()):
        f = component_file(pkg, comp_name, comp_info)
        if not f.is_file():
            print(f"SKIP {comp_name}: {f.name} not found in this build "
                  f"(likely gated by an option not enabled for this package)")
            continue
        checked += 1
        actual = set()
        for lib in linked_libs(f):
            comp = to_component(lib, plugin_names)
            if comp and comp != comp_name:
                actual.add(comp)

        missing = actual - transitive[comp_name]
        if missing:
            missing_total += len(missing)
            print(f"MISSING requires: {comp_name} actually links (directly or transitively) "
                  f"against {sorted(missing)}, which is unreachable from its declared "
                  f"requires {sorted(direct[comp_name])} even transitively")

    print(f"\nChecked {checked}/{len(components)} components present in this build.")
    print(f"Total missing-requires findings: {missing_total}")
    sys.exit(1 if missing_total else 0)


if __name__ == "__main__":
    main()
