"""Pin the screen-then-resolve-by-name class, enumerated from source by condition.

THE CONDITION this gate enumerates:

    a function asks the filesystem whether path P is a link (the SCREEN), and
    then hands P -- or a name derived from P -- to something that resolves it,
    follows it, or reads through it (the USE).

The screen answers about a NAME at one instant. A USE that resolves that same
name afterwards can traverse a link planted in between, so the screen's verdict
does not carry. The cure is to hold one descriptor per component across both
halves, which makes the proof and the use the same walk.

Two things this gate deliberately does.

It finds sites by the CONDITION, never by a list of file names, because a key
shaped like "places that look like the ones already known" grows a defect one
site at a time. The list below is the declared BASELINE the condition's output is
compared against, not the search key.

And a USE is not only an ``os`` or ``pathlib`` call. A project helper that
resolves internally is equally a resolve at the call site, so the resolver set is
computed from source over the call graph. ``is_sensitive_path`` builds its
candidate forms with ``realpath``, so handing it a screened name is the probe.

Why the resolver set is bounded at ONE call hop. Depth 0 is the primitives alone
and misses a site whose USE is ``atomic_write`` rather than a primitive, so it
under-reports. Taken to a fixpoint the set saturates at about 20000 names --
essentially every function in the package, because nearly everything eventually
touches the filesystem -- and a saturated set makes the USE predicate vacuous and
the count meaningless. Depth 1 is the smallest hop count at which every control
module is found, so it is the honest one.

A run in which the controls do not all hit is UNKNOWN, not a pass: the condition
failed to find sites known to satisfy it, so its silence about everything else
carries no weight. :func:`test_control_modules_are_all_found` prints the count so
a reader sees the number rather than inferring it from a green tick.

The class divides into two kinds wanting OPPOSITE treatment, which is why this
gate pins a shape rather than converting callers. A site that VETS a link target
and then continues needs the hold. A site that refuses every link never traverses
one, so a hold buys it nothing -- and tightening it further costs an ordinary
Windows or macOS layout that puts a junction or symlink on the path to a config
directory, for no privilege gain. :data:`HELD_SITES` therefore names only the
sites whose contract is to hold, and those are the ones whose shape is asserted.
"""

from __future__ import annotations

import ast
import functools
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from link_screen_sites import DECLARED_SITES
from source_corpus import candidate_sources, parsed_candidates, src_root

#: Predicates that ask the filesystem whether a path is a link. Each one marks
#: the pattern, so keying on a single symbol under-reports: the ancestor walk and
#: the single-path check reach the same shape by different routes.
SCREENS = frozenset({"first_linked_ancestor", "is_link_or_junction", "is_reparse_point"})

#: By-name resolves in ``os``, ``os.path``, ``pathlib`` and ``shutil``. Both
#: spellings of each question, because ``pathlib`` renames all of them.
PRIMITIVES = frozenset(
    {
        "realpath",
        "readlink",
        "resolve",
        "samefile",
        "stat",
        "lstat",
        "exists",
        "getsize",
        "getmtime",
        "isdir",
        "isfile",
        "islink",
        "is_dir",
        "is_file",
        "is_symlink",
        "open",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "listdir",
        "scandir",
        "iterdir",
        "glob",
        "rglob",
        "walk",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "move",
        "rmtree",
        "unlink",
        "mkdir",
        "makedirs",
        "rename",
        "touch",
    }
)

#: Names that are never a path resolve, however the call-graph hop admits them.
#: The resolver set keys on the UNQUALIFIED name, so a project helper called
#: ``append`` or ``run`` that touches the filesystem would otherwise make every
#: ``list.append`` and ``subprocess.run`` read as a resolve.
NEVER = frozenset(
    {
        "append",
        "extend",
        "add",
        "update",
        "insert",
        "pop",
        "remove",
        "discard",
        "close",
        "run",
        "get",
        "set",
        "put",
        "send",
        "format",
        "join",
        "split",
        "encode",
        "decode",
        "strip",
        "lower",
        "upper",
        "keys",
        "values",
        "items",
    }
)

#: A USE carrying this keyword is anchored to a descriptor, not to a name.
FD_KWARGS = frozenset({"dir_fd"})

#: One call hop. See the module docstring for why this is not a tunable knob.
RESOLVER_DEPTH = 1

#: Modules known to satisfy the condition. The gate must find every one, or its
#: report on the rest of the tree is UNKNOWN rather than clean.
CONTROL_MODULES = (
    "acp/prompt_blocks.py",
    "apps/builtins/auto_improvement/backend/clone_setup.py",
    "apps/builtins/aws_control/backend/backup.py",
    "apps/builtins/issue_radar/backend/crew_store.py",
    "apps/plugin_import.py",
    "dashboard/handlers/themes.py",
    "image_artifacts.py",
    "member_essential_context.py",
    "memory.py",
    "messaging/outbound_files.py",
)

#: Sites whose contract is to hold the chain across the screen AND the use. The
#: shape of each is asserted structurally below. A site earns a place here by
#: holding, not by being important.
HELD_SITES = frozenset(
    {
        ("apps/builtins/issue_radar/backend/crew_store.py", "_write_unit_order"),
    }
)

#: The helper a held site walks the chain with, and the one that releases it.
HOLD_CALL = "_hold_chain_no_follow"
RELEASE_CALL = "_release_held"


def _verb(node: ast.Call) -> str:
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


def _qual(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f"{ast.unparse(f.value)}.{f.attr}"
    return f.id if isinstance(f, ast.Name) else ""


def _names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _anchored(node: ast.Call) -> bool:
    """Whether this call addresses a descriptor rather than a name."""
    qual = _qual(node)
    if qual.startswith("pinned_fs.") or "fstat" in qual:
        return True
    return any(kw.arg in FD_KWARGS for kw in node.keywords)


def _rel(path: Path) -> str:
    return path.relative_to(src_root()).as_posix()


#: Path fragments this gate does not police. Which files a gate covers is the
#: gate's own contract, so the corpus does not apply these. A test that plants a
#: junction on purpose screens and then resolves it BY DESIGN -- that is the
#: fixture, not the defect -- and vendored code is not ours to reshape.
UNPOLICED = ("tests/", "/testing/", "_vendor")


def _policed(path: Path) -> bool:
    rel = _rel(path)
    if path.name.startswith("test_") or path.name == "conftest.py":
        return False
    return not any(fragment in f"/{rel}" for fragment in UNPOLICED)


@functools.cache
def _resolver_names() -> frozenset[str]:
    """Names that resolve a path by name, within :data:`RESOLVER_DEPTH` hops."""
    callees: dict[str, set[str]] = {}
    for path, _text, tree in parsed_candidates():
        if not _policed(path):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seen = callees.setdefault(fn.name, set())
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and not _anchored(node):
                    seen.add(_verb(node))
    resolvers = set(PRIMITIVES)
    for _ in range(RESOLVER_DEPTH):
        grown = {n for n, calls in callees.items() if (calls & resolvers) and n not in NEVER}
        if grown <= resolvers:
            break
        resolvers |= grown
    return frozenset(resolvers)


class _Sites(ast.NodeVisitor):
    """Collect screens, the names they taint, and later uses of a tainted name."""

    def __init__(self, resolvers: frozenset[str]) -> None:
        self.resolvers = resolvers
        self.screen_lines: list[int] = []
        self.tainted: set[str] = set()
        self.uses: list[tuple[int, str, set[str]]] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call) and _verb(node.value) in SCREENS:
            # The ancestor a screen RETURNS is itself a name, so reading through
            # it re-walks just as the argument does.
            for target in node.targets:
                self.tainted |= _names(target)
        elif self.tainted & _names(node.value):
            for target in node.targets:
                self.tainted |= _names(target)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        verb = _verb(node)
        if verb in SCREENS:
            arg = node.args[0] if node.args else None
            self.tainted |= _names(arg)
            self.screen_lines.append(node.lineno)
        elif verb in self.resolvers and verb not in NEVER and not _anchored(node):
            subject: ast.AST | None = node.args[0] if node.args else None
            if subject is None and isinstance(node.func, ast.Attribute):
                subject = node.func.value
            self.uses.append((node.lineno, _qual(node) or verb, _names(subject)))
        self.generic_visit(node)


def _sites_in(tree: ast.Module, resolvers: frozenset[str]) -> Iterator[tuple[str, int]]:
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scan = _Sites(resolvers)
        for child in fn.body:
            scan.visit(child)
        if not scan.screen_lines:
            continue
        first = min(scan.screen_lines)
        if any(line >= first and (names & scan.tainted) for line, _verb_, names in scan.uses):
            yield fn.name, first


@functools.cache
def discovered_sites() -> frozenset[tuple[str, str]]:
    """Every ``(module, function)`` in the package that satisfies the condition."""
    resolvers = _resolver_names()
    found: set[tuple[str, str]] = set()
    for path, _text, tree in parsed_candidates(require_any=tuple(SCREENS)):
        if not _policed(path):
            continue
        for func, _line in _sites_in(tree, resolvers):
            found.add((_rel(path), func))
    return frozenset(found)


def test_control_modules_are_all_found() -> None:
    """Every module known to satisfy the condition is found, and the count is shown."""
    modules = {module for module, _func in discovered_sites()}
    hit = [m for m in CONTROL_MODULES if m in modules]
    missed = [m for m in CONTROL_MODULES if m not in modules]
    # Printed rather than only asserted: a reader judging this gate needs the
    # number, and a green tick alone does not carry it.
    print(
        f"link-screen condition: {len(hit)}/{len(CONTROL_MODULES)} control modules, "
        f"{len(discovered_sites())} sites in {len(modules)} modules, "
        f"resolver depth {RESOLVER_DEPTH} ({len(_resolver_names())} names)"
    )
    assert not missed, (
        "the condition does not find these control modules, so its report on the "
        f"rest of the tree is UNKNOWN rather than clean: {sorted(missed)}"
    )


def test_every_site_is_declared() -> None:
    """A new screen-then-resolve site declares itself in :data:`DECLARED_SITES`."""
    undeclared = sorted(discovered_sites() - DECLARED_SITES)
    assert not undeclared, (
        "these functions screen a path for links and then resolve, follow or read "
        "the same name afterwards, which is the window a link planted in between "
        "slips through. Hold the chain across both halves, or add the site to "
        "DECLARED_SITES with the reason a hold is not the answer there:\n"
        + "\n".join(f"    {module}::{func}" for module, func in undeclared)
    )


def test_declared_sites_still_exist() -> None:
    """Every name in the baseline is a site the condition still finds."""
    stale = sorted(DECLARED_SITES - discovered_sites())
    assert not stale, (
        "DECLARED_SITES names sites the condition does not find. Remove them so "
        "the baseline keeps meaning what it says:\n"
        + "\n".join(f"    {module}::{func}" for module, func in stale)
    )


def _held_try_blocks(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Try]:
    """``try`` blocks that run while a chain walked by :data:`HOLD_CALL` is held."""
    held_names: set[str] = set()
    blocks: list[ast.Try] = []
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            if _verb(stmt.value) == HOLD_CALL:
                for target in stmt.targets:
                    held_names |= _names(target)
        elif isinstance(stmt, ast.Try):
            released = any(
                isinstance(node, ast.Call) and _verb(node) == RELEASE_CALL
                for node in ast.walk(ast.Module(body=stmt.finalbody, type_ignores=[]))
            )
            if released and held_names:
                blocks.append(stmt)
    return blocks


@pytest.mark.parametrize(("module", "func"), sorted(HELD_SITES))
def test_held_site_screens_and_resolves_inside_one_hold(module: str, func: str) -> None:
    """The screen and the by-name use both run while the chain is held."""
    path = src_root() / module
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    target = next(
        (
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == func
        ),
        None,
    )
    assert target is not None, f"{module} defines no {func}"

    blocks = _held_try_blocks(target)
    assert blocks, (
        f"{module}::{func} names a held site, but no try block runs while a chain "
        f"walked by {HOLD_CALL} is held and released by {RELEASE_CALL}"
    )

    resolvers = _resolver_names()
    for block in blocks:
        body = ast.Module(body=block.body, type_ignores=[])
        calls = [node for node in ast.walk(body) if isinstance(node, ast.Call)]
        screened = [c for c in calls if _verb(c) in SCREENS]
        used = [
            c for c in calls if _verb(c) in resolvers and _verb(c) not in NEVER and not _anchored(c)
        ]
        if screened and used:
            return

    raise AssertionError(
        f"{module}::{func} holds a chain, but its screen and its by-name use are not "
        "both inside that hold, so the window between them is open again. Keep the "
        f"{', '.join(sorted(SCREENS))} check and the write in the same held block."
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink layout; junctions differ")
def test_held_write_accepts_an_ordinary_symlinked_layout(tmp_path: Path) -> None:
    """A held write still works where a symlink carries an ordinary directory layout.

    The passing control for this gate. A hold that refused any layout reached
    through a link would read as correct against the refusal direction alone,
    while breaking every host whose config directory is placed by a dotfile
    manager -- which is a supported layout, not an attack.
    """
    from kiro_crew.apps.builtins.issue_radar.backend import crew_store

    real = tmp_path / "real"
    (real / "crew").mkdir(parents=True)
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)

    # The leaf's PARENT is a real directory; an ancestor above it is the symlink,
    # which is the shape a dotfile manager produces.
    target = link / "crew" / "unit-order.txt"
    crew_store._write_unit_order(target, ("alpha", "beta"))

    assert (real / "crew" / "unit-order.txt").read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_gate_reads_the_package_it_claims_to() -> None:
    """The corpus filter reaches files holding a screen, so the scan is not empty."""
    assert candidate_sources(require_any=tuple(SCREENS)), (
        "no source file mentions any screen predicate, so the corpus filter is "
        "wrong and every gate above is vacuously green"
    )
    assert discovered_sites(), "the condition found no sites at all, which is not credible"
