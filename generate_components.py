#!/usr/bin/env python3
"""
Extracts the OpenUSD component dependency graph directly from upstream's own
CMakeLists.txt files (pxr_library()/pxr_plugin() calls, add_subdirectory()
reachability, and in-file conditional LIBRARIES construction), producing the
same shape of data the recipe used to hand-maintain in `components_info`.

Usage: generate_components.py <path-to-openusd-source-root> <version> <output.json>

Fails loudly (raises) on anything it cannot mechanically resolve, by design:
this script is meant to be re-run and reviewed on every version bump, not to
silently guess.
"""
import json
import re
import sys
from pathlib import Path

# --- Keyword vocabulary of pxr_library()/pxr_plugin(), from
# cmake/macros/Public.cmake's cmake_parse_arguments() call. Kept literal
# (not derived) because it almost never changes across USD releases; if it
# does, tokens will spill into the wrong section and show up as bogus
# "external" library names, which the EXTERNAL_TOKENS check below will catch.
SECTION_KEYWORDS = {
    "DISABLE_PRECOMPILED_HEADERS", "INCLUDE_SCHEMA_FILES",
    "TYPE", "PRECOMPILED_HEADER_NAME",
    "PUBLIC_CLASSES", "PUBLIC_HEADERS", "PRIVATE_CLASSES", "PRIVATE_HEADERS",
    "CPPFILES", "LIBRARIES", "INCLUDE_DIRS", "DOXYGEN_FILES", "RESOURCE_FILES",
    "PYTHON_PUBLIC_CLASSES", "PYTHON_PRIVATE_CLASSES", "PYTHON_PUBLIC_HEADERS",
    "PYTHON_PRIVATE_HEADERS", "PYTHON_CPPFILES", "PYMODULE_CPPFILES",
    "PYMODULE_DIRS", "PYMODULE_FILES", "PYSIDE_UI_FILES",
}
ONE_VALUE_ARGS = {"TYPE", "PRECOMPILED_HEADER_NAME"}

# Fixed CMake option values under this recipe's toolchain configuration
# (see generate() in conanfile.py) plus their upstream defaults
# (cmake/defaults/Options.cmake) for everything the recipe does not
# override. PXR_BUILD_GPU_SUPPORT is derived (GL default ON, so it's
# always True regardless of platform).
FIXED_VARS = {
    # set explicitly by the recipe's CMakeToolchain
    "PXR_BUILD_USDVIEW": False,
    "PXR_BUILD_TESTS": False,
    "PXR_BUILD_EXAMPLES": False,
    "PXR_BUILD_TUTORIALS": False,
    "PXR_ENABLE_PYTHON_SUPPORT": False,
    "PXR_BUILD_USD_TOOLS": False,
    # upstream defaults the recipe does not touch
    "PXR_USE_DEBUG_PYTHON": False,
    "PXR_BUILD_EXEC": True,
    "PXR_BUILD_USD_VALIDATION": True,
    "PXR_ENABLE_GL_SUPPORT": True,
    "PXR_BUILD_GPU_SUPPORT": True,  # ON because PXR_ENABLE_GL_SUPPORT is ON
    "PXR_BUILD_ALEMBIC_PLUGIN": False,
    "PXR_BUILD_DRACO_PLUGIN": False,
    "PXR_BUILD_EMBREE_PLUGIN": False,
    "PXR_BUILD_PRMAN_PLUGIN": False,
    "PXR_ENABLE_OSL_SUPPORT": False,
    "PXR_ENABLE_PTEX_SUPPORT": False,
    "PXR_ENABLE_OPENVDB_SUPPORT": False,
    "PXR_ENABLE_VULKAN_SUPPORT": False,
    "PXR_BUILD_MONOLITHIC": False,
    "PXR_APPLE_EMBEDDED": False,
    "PXR_BUILD_IMAGEIO_PLUGIN": True,
    "PXR_BUILD_OPENCOLORIO_PLUGIN": False,
    "APPLE": "is_apple",  # handled specially below (it's also a plain CMake var)
}
# Variables whose truth-value is a per-build/per-platform Conan condition,
# not a fixed value we can bake in at generation time.
SYMBOLIC_VARS = {
    "PXR_ENABLE_METAL_SUPPORT": "is_apple",
    "PXR_BUILD_OPENIMAGEIO_PLUGIN": "with_openimageio",
    "PXR_ENABLE_MATERIALX_SUPPORT": "with_materialx",
    # both driven by the same recipe option
    "PXR_BUILD_IMAGING": "with_imaging",
    "PXR_BUILD_USD_IMAGING": "with_imaging",
    "APPLE": "is_apple",
}

# Small, stable bridge between upstream's external CMake targets/variables
# and the Conan targets/frameworks this recipe's `requirements()` exposes.
# Anything not listed here AND not resolvable as an internal component name
# AND not a "-framework X" token makes the script abort loudly.
EXTERNAL_TOKEN_MAP = {
    "TBB::tbb": {"requires": ["onetbb::libtbb"]},
    "${OPENSUBDIV_OSDCPU_LIBRARY}": {"requires": ["opensubdiv::osdcpu"]},
    "${OPENSUBDIV_LIBRARIES}": {"requires": ["opensubdiv::osdcpu", "opensubdiv::osdgpu"]},
    # On Apple this variable actually resolves (via find_library() calls in
    # upstream's Packages.cmake, not visible in any committed CMakeLists.txt
    # text) to Cocoa/UIKit + Foundation frameworks rather than a linkable
    # Conan target; this mapping is curated, not derivable from source.
    # On Linux it also holds ${X11_LIBRARIES}: package_info() adds
    # xorg::x11 to garch for that case, since this map is platform-agnostic.
    "${GARCH_PLATFORM_LIBRARIES}": {"requires": ["opengl::opengl"], "frameworks": ["Foundation", "$kit_framework"]},
    "${OIIO_LIBRARIES}": {"requires": ["openimageio::openimageio"]},
    "${__OIIO_IMATH_LIBS}": {},  # transitively covered by openimageio::openimageio
    "MaterialXCore": {"requires": ["materialx::MaterialXCore"]},
    "MaterialXFormat": {"requires": ["materialx::MaterialXFormat"]},
    "MaterialXGenShader": {"requires": ["materialx::MaterialXGenShader"]},
    "MaterialXGenGlsl": {"requires": ["materialx::MaterialXGenGlsl"]},
    "MaterialXGenMsl": {"requires": ["materialx::MaterialXGenMsl"]},
    "MaterialXRender": {"requires": ["materialx::MaterialXRender"]},
    "MaterialXRenderGlsl": {"requires": ["materialx::MaterialXRenderGlsl"]},
    # Pure system-level linker flags: already handled generically by
    # package_info()'s per-OS system_libs fallback, safe to drop here.
    "${WINLIBS}": {},
    "${CMAKE_DL_LIBS}": {},
    "${ARCH_PLATFORM_LIBS}": {},
    "${work_impl_target}": {},  # empty unless a custom PXR_WORK_IMPL is configured (we don't)
    # find_library(FOUNDATION Foundation ...) et al in hioImageIO/CMakeLists.txt:
    # these variables hold a framework path, equivalent to "-framework X".
    "${FOUNDATION}": {"frameworks": ["Foundation"]},
    "${IMAGEIO}": {"frameworks": ["ImageIO"]},
    "${COREGRAPHICS}": {"frameworks": ["CoreGraphics"]},
}


class Unresolvable(Exception):
    pass


# ---------------------------------------------------------------------------
# Tiny boolean-condition evaluator for CMake if() guards.
# Evaluates to True, False, or a frozenset of symbol names meaning
# "true only if ALL of these Conan-side conditions hold" (pure AND-of-symbols
# is the only symbolic shape that actually occurs in the 26.08 tree; anything
# else raises so a human looks at the new case).
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(r"\(|\)|\$\{[^}]+\}|[A-Za-z_][A-Za-z0-9_]*|\"[^\"]*\"")


def _clean_var(tok):
    if tok.startswith("${") and tok.endswith("}"):
        return tok[2:-1]
    if tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1]
    return tok


# A symbolic condition is a frozenset of (name, negated) literal pairs,
# meaning the AND of `NOT name` (negated=True) or `name` (negated=False)
# across all entries. `name` is either a real Conan-facing symbol
# (is_apple/with_imaging/with_openimageio/with_materialx) or an "__unknown__::<text>"
# opaque marker for anything this evaluator can't otherwise resolve.
# condition_key() raises if an opaque marker or a negated literal survives
# into real output.

def _lookup_var(name):
    name = name.strip()
    if name in FIXED_VARS:
        val = FIXED_VARS[name]
        if val == "is_apple":
            return frozenset({("is_apple", False)})
        return bool(val)
    if name in SYMBOLIC_VARS:
        return frozenset({(SYMBOLIC_VARS[name], False)})
    if name in ("1", "ON", "TRUE"):
        return True
    if name in ("0", "OFF", "FALSE", ""):
        return False
    # Unknown variable (e.g. a local loop/helper var unrelated to feature
    # gating, like `if (Imath_FOUND)` or `if (value)`). Tag it opaque
    # instead of failing here: most of these live in branches that never
    # end up contributing to a component actually present in the output
    # (their LIBRARIES tokens get intercepted by EXTERNAL_TOKEN_MAP first).
    return frozenset({(f"__unknown__::{name}", False)})


def _and(a, b):
    if a is False or b is False:
        return False
    if a is True:
        return b
    if b is True:
        return a
    return a | b


def _or(a, b):
    if a is True or b is True:
        return True
    if a is False:
        return b
    if b is False:
        return a
    if a == b:
        return a
    # OR of two distinct symbolic conditions can't be represented as a
    # simple AND-of-literals; tag opaque so condition_key() raises loudly
    # if this ever reaches real output instead of silently mis-simplifying.
    text = f"OR({sorted(a)},{sorted(b)})"
    return frozenset({(f"__unknown__::{text}", False)})


def _not(a):
    if a is True:
        return False
    if a is False:
        return True
    if len(a) == 1:
        ((name, negated),) = a
        return frozenset({(name, not negated)})
    # NOT of a conjunction of multiple literals is a disjunction, which
    # isn't representable as a simple AND-of-literals; tag opaque.
    text = f"NOT({sorted(a)})"
    return frozenset({(f"__unknown__::{text}", False)})


def eval_condition(expr_text):
    """Evaluate a CMake if()-style boolean expression to True/False/frozenset."""
    tokens = TOKEN_RE.findall(expr_text)
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def parse_or():
        nonlocal pos
        val = parse_and()
        while peek() == "OR":
            pos += 1
            val = _or(val, parse_and())
        return val

    def parse_and():
        nonlocal pos
        val = parse_not()
        while peek() == "AND":
            pos += 1
            val = _and(val, parse_not())
        return val

    def parse_not():
        nonlocal pos
        if peek() == "NOT":
            pos += 1
            return _not(parse_not())
        return parse_atom()

    def parse_atom():
        nonlocal pos
        tok = peek()
        if tok is None:
            raise Unresolvable(f"empty condition atom in {expr_text!r}")
        if tok == "(":
            pos += 1
            val = parse_or()
            if peek() != ")":
                raise Unresolvable(f"unbalanced condition {expr_text!r}")
            pos += 1
            return val
        if tok == "TARGET":
            pos += 1
            target_name = _clean_var(peek()) if peek() is not None else None
            pos += 1
            # Every target this recipe could plausibly probe for existence
            # is one this build never creates under our fixed toolchain
            # config (e.g. python_modules, since PXR_ENABLE_PYTHON_SUPPORT
            # is always False here).
            known_absent_targets = {"python_modules"}
            if target_name in known_absent_targets:
                return False
            return frozenset({(f"__unknown__::TARGET {target_name}", False)})
        pos += 1
        return _lookup_var(_clean_var(tok))

    result = parse_or()
    if pos != len(tokens):
        raise Unresolvable(f"trailing tokens in condition {expr_text!r}: {tokens[pos:]}")
    return result


def safe_condition(expr_text):
    """eval_condition(), but degrades unsupported syntax (MATCHES, STREQUAL,
    VERSION_GREATER, ...) to an opaque tag instead of crashing the whole
    scan. These show up in if() guards unrelated to our feature flags
    (compiler checks, etc.); condition_key() still raises loudly if one
    ever ends up mattering for real output."""
    try:
        return eval_condition(expr_text)
    except Unresolvable:
        return frozenset({(f"__unknown__::{expr_text.strip()}", False)})


def condition_key(cond):
    """Normalize an eval_condition() result to a JSON-friendly value."""
    if cond is True:
        return True
    if cond is False:
        return False
    unknown = [name for name, _negated in cond if name.startswith("__unknown__::")]
    if unknown:
        raise Unresolvable(
            f"condition depends on unresolvable term(s) {unknown} that reached real "
            "output; teach eval_condition/_lookup_var about it or check EXTERNAL_TOKEN_MAP"
        )
    negated = [name for name, negated in cond if negated]
    if negated:
        # package_info() does not support negations yet
        raise Unresolvable(
            f"condition depends on negated term(s) {negated} (e.g. `if (NOT APPLE)`) "
            "that reached real output; review this case manually and add support "
            "for negated conditions to both this script and the recipe's "
            "_condition_is_true() if it is legitimate"
        )
    return sorted(name for name, _negated in cond)


# ---------------------------------------------------------------------------
# CMakeLists.txt scanning: strip comments, track an if/else/endif condition
# stack, collect add_subdirectory() reachability edges and in-file
# set()/list(APPEND) variable definitions.
# ---------------------------------------------------------------------------

IF_RE = re.compile(r"^\s*if\s*\((.*)\)\s*$")
ELSEIF_RE = re.compile(r"^\s*elseif\s*\((.*)\)\s*$")
ELSE_RE = re.compile(r"^\s*else\s*\(\s*\)\s*$")
ENDIF_RE = re.compile(r"^\s*endif\s*\(")
ADD_SUBDIR_RE = re.compile(r"add_subdirectory\s*\(\s*([\w${}]+)\s*\)")
SET_RE = re.compile(r"^\s*set\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s+(.*)\)\s*$", re.S)
LIST_APPEND_RE = re.compile(r"^\s*list\s*\(\s*APPEND\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.*)\)\s*$", re.S)
RETURN_RE = re.compile(r"^\s*return\s*\(\s*\)")
FUNCTION_OPEN_RE = re.compile(r"^\s*(function|macro)\s*\(", re.I)
FUNCTION_CLOSE_RE = re.compile(r"^\s*(endfunction|endmacro)\s*\(", re.I)
FOREACH_RE = re.compile(r"^\s*foreach\s*\(\s*(\w+)\s+(.*)\)\s*$", re.S)
ENDFOREACH_RE = re.compile(r"^\s*endforeach\s*\(")


def strip_comments(text):
    out = []
    for line in text.splitlines():
        in_q = False
        buf = []
        for c in line:
            if c == '"':
                in_q = not in_q
                buf.append(c)
            elif c == "#" and not in_q:
                break
            else:
                buf.append(c)
        out.append("".join(buf))
    return "\n".join(out)


def split_statements(text):
    """Split cleaned CMakeLists.txt text into (line_no, statement_text) for
    simple single-line constructs, and handle multi-line set()/list()/if()
    calls by joining until parens balance."""
    lines = text.splitlines()
    stmts = []
    buf = ""
    start = None
    depth = 0
    for i, line in enumerate(lines, 1):
        if not buf:
            start = i
        buf += line + "\n"
        depth += line.count("(") - line.count(")")
        if depth <= 0 and buf.strip():
            stmts.append((start, buf.strip()))
            buf = ""
            depth = 0
    return stmts


def find_call_body(text, macro_names):
    """Find the first pxr_library(...)/pxr_plugin(...) call and return
    (macro_name, body_text) or None."""
    for m in re.finditer(r"\b(" + "|".join(macro_names) + r")\s*\(", text):
        open_idx = m.end() - 1
        depth = 0
        in_q = False
        i = open_idx
        while i < len(text):
            c = text[i]
            if c == '"':
                in_q = not in_q
            elif not in_q:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        return m.group(1), text[open_idx + 1:i]
            i += 1
        raise Unresolvable("unbalanced parens scanning macro call")
    return None


def tokenize_args(body):
    tokens = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and body[j] != '"':
                j += 1
            tokens.append(body[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and not body[j].isspace():
                j += 1
            tokens.append(body[i:j])
            i = j
    return tokens


def parse_sectioned_args(tokens, first_is_name=True):
    name = tokens[0] if first_is_name else None
    sections = {}
    current = None
    idx = 1 if first_is_name else 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in SECTION_KEYWORDS:
            current = tok
            sections.setdefault(current, [])
            if tok in ONE_VALUE_ARGS:
                idx += 1
                if idx < len(tokens):
                    sections[current].append(tokens[idx])
            idx += 1
            continue
        if current is not None:
            sections[current].append(tok)
        idx += 1
    return name, sections


class FileVars:
    """Resolves in-file set()/list(APPEND) variables to a flat list of
    (condition, item) leaf pairs, honoring the if/else stack active at each
    assignment. Nested ${OTHERVAR} references on the right-hand side are
    expanded immediately (against the variable's value *at that point in
    the file*), the same way CMake evaluates them, so a later
    `${THIS_VAR}` reference never has to recurse back into itself."""

    def __init__(self):
        self.vars = {}  # name -> list of (condition, item), items are plain leaf tokens

    def _expand(self, items, condition, self_name):
        flat = []
        for it in items:
            varname = it[2:-1] if it.startswith("${") and it.endswith("}") else None
            if varname == self_name:
                # `set(X ${X} newitem)` / `set(X ... ${X})`: refers to X's
                # value *before* this statement. If X was never assigned
                # earlier in the file, CMake treats it as empty, not as a
                # literal token to classify later.
                for item_cond, leaf in self.vars.get(self_name, []):
                    flat.append((_and(condition, item_cond), leaf))
            elif varname is not None and varname in self.vars:
                # Known local variable (set()/list(APPEND) seen earlier in
                # this file): inline its current value.
                for item_cond, leaf in self.vars[varname]:
                    flat.append((_and(condition, item_cond), leaf))
            else:
                # Plain leaf, or a ${VAR} this file never assigns (set by
                # find_package()/find_library() elsewhere, e.g.
                # ${OCIO_LIBRARIES}): keep it literal for EXTERNAL_TOKEN_MAP
                # / internal-name classification downstream.
                flat.append((condition, it))
        return flat

    def set(self, name, items, condition):
        self.vars[name] = self._expand(items, condition, self_name=name)

    def append(self, name, items, condition):
        self.vars.setdefault(name, [])
        self.vars[name].extend(self._expand(items, condition, self_name=name))

    def resolve(self, name):
        # An unset CMake variable is simply empty, not an error (this also
        # covers self-referential accumulation like
        # `set(X ${Y} ${X})` before X has ever been set()).
        return self.vars.get(name, [])


def scan_file_vars_and_call(text):
    """Walk a CMakeLists.txt tracking the if/else condition stack, recording
    set()/list(APPEND) assignments and the file's own early-return guard
    condition, then locate the pxr_library/pxr_plugin call."""
    stmts = split_statements(text)
    stack = []  # list of resolved condition values (True/False/frozenset) for active branch
    fvars = FileVars()
    own_guard = True  # AND of all "return()-guarded" if-conditions seen before the call
    package_name = None
    call_stmt_idx = None
    func_depth = 0  # inside function()/macro() ... endfunction()/endmacro(): body doesn't
                    # execute at file-parse time, so ignore it entirely (its own if/return
                    # statements are not top-level control flow for this file).

    def active_condition():
        cond = True
        for c in stack:
            cond = _and(cond, c)
        return cond

    for idx, (_lineno, stmt) in enumerate(stmts):
        if FUNCTION_OPEN_RE.match(stmt):
            func_depth += 1
            continue
        if FUNCTION_CLOSE_RE.match(stmt):
            func_depth = max(0, func_depth - 1)
            continue
        if func_depth > 0:
            continue
        m = SET_RE.match(stmt)
        if m and m.group(1) == "PXR_PACKAGE":
            val_tokens = tokenize_args(m.group(2).rstrip(")"))
            if val_tokens:
                package_name = val_tokens[0]
            continue
        m = IF_RE.match(stmt)
        if m:
            stack.append(safe_condition(m.group(1)))
            continue
        m = ELSEIF_RE.match(stmt)
        if m:
            if stack:
                stack[-1] = _not(stack[-1])
                stack.append(safe_condition(m.group(1)))
            continue
        if ELSE_RE.match(stmt):
            if stack:
                stack[-1] = _not(stack[-1])
            continue
        if ENDIF_RE.match(stmt):
            if stack:
                stack.pop()
            continue
        if RETURN_RE.match(stmt):
            # An early return() while conditions are active on the stack
            # means: reaching the rest of the file (incl. the pxr_library
            # call) requires the NEGATION of the currently active branch.
            own_guard = _and(own_guard, _not(active_condition()))
            continue
        m = SET_RE.match(stmt)
        if m:
            items = tokenize_args(m.group(2).rstrip(")"))
            fvars.set(m.group(1), items, active_condition())
            continue
        m = LIST_APPEND_RE.match(stmt)
        if m:
            items = tokenize_args(m.group(2).rstrip(")"))
            fvars.append(m.group(1), items, active_condition())
            continue
        if re.search(r"\b(pxr_library|pxr_plugin)\s*\(", stmt):
            call_stmt_idx = idx
            break

    if call_stmt_idx is None:
        return None

    # Re-extract the call body from the *original* text (not the
    # statement-split copy) so nested parens inside it are handled exactly
    # once, robustly.
    call = find_call_body(text, ("pxr_library", "pxr_plugin"))
    if call is None:
        return None
    macro, body = call
    tokens = tokenize_args(body)
    if tokens and tokens[0] == "${PXR_PACKAGE}":
        if package_name is None:
            raise Unresolvable("pxr_library uses ${PXR_PACKAGE} but it was never set()")
        tokens[0] = package_name
    name, sections = parse_sectioned_args(tokens)
    return {
        "name": name,
        "is_plugin": macro == "pxr_plugin",
        "own_guard": own_guard,
        "libraries_raw": sections.get("LIBRARIES", []),
        "fvars": fvars,
    }


# ---------------------------------------------------------------------------
# Directory reachability: walk add_subdirectory() edges from pxr/ down,
# honoring the same if/else stack logic as above.
# ---------------------------------------------------------------------------

def scan_subdirectory_edges(text):
    """Return list of (child_name_or_None_if_unresolvable, condition).

    Most directories in this tree are wired up via
    `set(DIRS a b c) foreach(d ${DIRS}) add_subdirectory(${d}) endforeach()`
    rather than literal add_subdirectory(name) calls, so DIRS-style
    variables are tracked (via the same FileVars machinery used for
    LIBRARIES) and the loop is unrolled into concrete per-directory edges.
    A directory that exists on disk but was never added to such a list
    (e.g. an orphaned/not-yet-wired-in module) must NOT be treated as
    reachable just because sibling directories are -- see the
    usdLodValidators case in 26.08, which sits right next to
    usdGeomValidators etc. on disk but is missing from usdValidation's own
    DIRS list, so it's simply never built.
    """
    stmts = split_statements(text)
    stack = []
    edges = []
    func_depth = 0
    file_guard = True  # AND of NOT(condition) for every top-level return() seen so far
    dir_vars = FileVars()
    foreach_stack = []  # list of (loopvar, [(condition, name), ...] or None)

    def active_condition():
        cond = True
        for c in stack:
            cond = _and(cond, c)
        return _and(cond, file_guard)

    def resolve_foreach_source(expr_tokens):
        # expr_tokens is e.g. ["${DIRS}"] or a literal list ["a", "b", "c"]
        items = []
        for tok in expr_tokens:
            if tok.startswith("${") and tok.endswith("}"):
                varname = tok[2:-1]
                resolved = dir_vars.resolve(varname)
                if not resolved and varname not in dir_vars.vars:
                    return None  # genuinely unknown source, can't unroll safely
                items.extend(resolved)
            else:
                items.append((True, tok))
        return items

    for _lineno, stmt in stmts:
        if FUNCTION_OPEN_RE.match(stmt):
            func_depth += 1
            continue
        if FUNCTION_CLOSE_RE.match(stmt):
            func_depth = max(0, func_depth - 1)
            continue
        if func_depth > 0:
            continue
        m = IF_RE.match(stmt)
        if m:
            stack.append(safe_condition(m.group(1)))
            continue
        m = ELSEIF_RE.match(stmt)
        if m:
            if stack:
                stack[-1] = _not(stack[-1])
                stack.append(safe_condition(m.group(1)))
            continue
        if ELSE_RE.match(stmt):
            if stack:
                stack[-1] = _not(stack[-1])
            continue
        if ENDIF_RE.match(stmt):
            if stack:
                stack.pop()
            continue
        if RETURN_RE.match(stmt):
            # A top-level return() ends processing of the *rest of this
            # file* whenever the currently active branch is taken, so
            # everything from here on (incl. later add_subdirectory calls)
            # is implicitly gated behind NOT(that branch).
            branch = True
            for c in stack:
                branch = _and(branch, c)
            file_guard = _and(file_guard, _not(branch))
            continue
        m = SET_RE.match(stmt)
        if m:
            items = tokenize_args(m.group(2).rstrip(")"))
            dir_vars.set(m.group(1), items, active_condition())
            continue
        m = LIST_APPEND_RE.match(stmt)
        if m:
            items = tokenize_args(m.group(2).rstrip(")"))
            dir_vars.append(m.group(1), items, active_condition())
            continue
        m = FOREACH_RE.match(stmt)
        if m:
            loopvar = m.group(1)
            source_tokens = tokenize_args(m.group(2).rstrip(")"))
            foreach_stack.append((loopvar, resolve_foreach_source(source_tokens)))
            continue
        if ENDFOREACH_RE.match(stmt):
            if foreach_stack:
                foreach_stack.pop()
            continue
        for m in ADD_SUBDIR_RE.finditer(stmt):
            child = m.group(1)
            if child.startswith("${"):
                varname = child[2:-1]
                resolved = None
                for loopvar, items in reversed(foreach_stack):
                    if loopvar == varname:
                        resolved = items
                        break
                if resolved is not None:
                    for item_cond, name in resolved:
                        edges.append((name, _and(active_condition(), item_cond)))
                else:
                    # Couldn't resolve the loop's source list: fail loud
                    # rather than silently guessing "every sibling
                    # directory", which is exactly wrong when a directory
                    # exists on disk but was never wired into the list.
                    raise Unresolvable(
                        f"add_subdirectory(${{{varname}}}) with no resolvable source "
                        f"list (foreach source not a tracked set()/list(APPEND) "
                        f"variable) in statement: {stmt!r}"
                    )
            else:
                edges.append((child, active_condition()))
    return edges


def compute_reachability(pxr_root):
    """BFS from pxr_root, returning {absolute_dir_path: condition}."""
    reach = {pxr_root: True}
    queue = [pxr_root]
    while queue:
        d = queue.pop()
        cond_d = reach[d]
        cmakelists = d / "CMakeLists.txt"
        if not cmakelists.is_file():
            continue
        text = strip_comments(cmakelists.read_text(encoding="utf-8", errors="replace"))
        for child, cond in scan_subdirectory_edges(text):
            combined = _and(cond_d, cond)
            child_dir = d / child
            if child_dir not in reach:
                reach[child_dir] = combined
            else:
                reach[child_dir] = _or(reach[child_dir], combined) if isinstance(reach[child_dir], bool) else combined
            queue.append(child_dir)
    return reach


# ---------------------------------------------------------------------------
# LIBRARIES token resolution -> (base_requires, base_frameworks,
# conditional_requires: list of (condition, requires, frameworks))
# ---------------------------------------------------------------------------

def resolve_tokens(tokens, condition, fvars, internal_names, out):
    if condition is False:
        return  # dead branch (e.g. gated on a feature this recipe always disables)
    for tok in tokens:
        if tok == "":
            continue  # e.g. set(optionalLibraries "") placeholder for an empty list
        if tok.startswith("-framework "):
            fw_name = tok.split(" ", 1)[1]
            if fw_name.startswith("${") and fw_name.endswith("}"):
                # e.g. "-framework ${APPLE_UI_FRAMEWORK}" where the file
                # itself set()s APPLE_UI_FRAMEWORK conditionally: resolve
                # it the same way as any other in-file variable.
                for item_cond, leaf in fvars.resolve(fw_name[2:-1]):
                    out.append((_and(condition, item_cond), {"frameworks": [leaf]}))
            else:
                out.append((condition, {"frameworks": [fw_name]}))
        elif tok in internal_names:
            out.append((condition, {"requires": [tok]}))
        elif tok in EXTERNAL_TOKEN_MAP:
            out.append((condition, EXTERNAL_TOKEN_MAP[tok]))
        elif tok.startswith("${") and tok.endswith("}"):
            varname = tok[2:-1]
            for item_cond, item in fvars.resolve(varname):
                resolve_tokens([item], _and(condition, item_cond), fvars, internal_names, out)
        else:
            raise Unresolvable(
                f"unrecognized LIBRARIES token {tok!r}; add it to EXTERNAL_TOKEN_MAP "
                "or check if it's a new internal component name"
            )


def build_component(parsed, internal_names, comp_cond=()):
    resolved = []
    resolve_tokens(parsed["libraries_raw"], True, parsed["fvars"], internal_names, resolved)

    base_requires, base_frameworks = [], []
    conditional = {}  # condition_key (tuple) -> {"requires": set, "frameworks": set}
    for cond, contrib in resolved:
        ck = condition_key(cond)
        if ck is False:
            continue
        if ck is not True:
            # symbols already required by the component itself are redundant here
            ck = [name for name in ck if name not in comp_cond] or True
        reqs = contrib.get("requires", [])
        frms = contrib.get("frameworks", [])
        if ck is True:
            base_requires.extend(reqs)
            base_frameworks.extend(frms)
        else:
            bucket = conditional.setdefault(tuple(ck), {"requires": [], "frameworks": []})
            bucket["requires"].extend(reqs)
            bucket["frameworks"].extend(frms)

    def dedup(seq):
        seen = []
        for x in seq:
            if x not in seen:
                seen.append(x)
        return seen

    comp = {"requires": dedup(base_requires)}
    if base_frameworks:
        comp["frameworks"] = dedup(base_frameworks)
    if parsed["is_plugin"]:
        comp["is_plugin"] = True
    if conditional:
        comp["conditional"] = [
            {"condition": list(ck), **{k: dedup(v) for k, v in bucket.items() if v}}
            for ck, bucket in conditional.items()
        ]
    return comp


def to_compact_json(obj, level=0, indent=4):
    """Like json.dumps(obj, indent=indent, sort_keys=True), but a list whose
    items are all plain scalars (str/bool/int/float) is printed on a single
    line instead of one item per line. Keeps components/<version>.json
    readable without the multi-hundred-line blow-up of one-token-per-line
    arrays."""
    pad = " " * indent * level
    pad_in = " " * indent * (level + 1)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = [
            f"{pad_in}{json.dumps(k)}: {to_compact_json(v, level + 1, indent)}"
            for k, v in sorted(obj.items())
        ]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        if all(isinstance(x, (str, bool, int, float)) for x in obj):
            return json.dumps(obj)
        items = [f"{pad_in}{to_compact_json(x, level + 1, indent)}" for x in obj]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return json.dumps(obj)


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    src_root = Path(sys.argv[1])
    version = sys.argv[2]
    out_path = Path(sys.argv[3])
    if out_path.stem != version:
        raise SystemExit(
            f"output filename {out_path.name!r} doesn't match version {version!r}; "
            f"package_info() looks up components/{version}.json by name"
        )

    pxr_root = src_root / "pxr"
    reach = compute_reachability(pxr_root)

    # Pass 1: parse every CMakeLists.txt that actually contains a
    # pxr_library/pxr_plugin call, regardless of reachability (we need the
    # full name universe to distinguish "internal component" from
    # "external token" during LIBRARIES resolution).
    parsed_by_dir = {}
    for cmakelists in pxr_root.rglob("CMakeLists.txt"):
        text = strip_comments(cmakelists.read_text(encoding="utf-8", errors="replace"))
        if not re.search(r"\b(pxr_library|pxr_plugin)\s*\(", text):
            continue
        parsed = scan_file_vars_and_call(text)
        if parsed:
            parsed_by_dir[cmakelists.parent] = parsed

    internal_names = {p["name"] for p in parsed_by_dir.values()}

    components = {}
    skipped = []
    for d, parsed in sorted(parsed_by_dir.items(), key=lambda kv: kv[1]["name"]):
        dir_cond = reach.get(d, False)
        full_cond = _and(dir_cond, parsed["own_guard"])
        ck = condition_key(full_cond)
        if ck is False:
            skipped.append(parsed["name"])
            continue
        comp = build_component(parsed, internal_names, ck if ck is not True else ())
        if ck is not True:
            comp["condition"] = list(ck)
        components[parsed["name"]] = comp

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(to_compact_json(components) + "\n")
    print(f"Wrote {len(components)} components to {out_path}")
    print(f"Skipped (unreachable under this recipe's fixed config): {sorted(skipped)}")


if __name__ == "__main__":
    main()
