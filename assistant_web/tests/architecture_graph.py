"""구조 이전 검사용 정적 분석. 생산 코드를 import하거나 실행하지 않는다.

명시적 Python import 그래프와 문자열/동적 연결의 검토 목록을 구분한다.
임의 계산으로 만든 모듈 이름이나 런타임 별칭을 해석하는 도구는 아니다.
"""

from __future__ import annotations

import ast
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImportEdge:
    importer: str
    imported: str
    line: int
    scope: str = "module"
    type_checking: bool = False


@dataclass(frozen=True)
class Reference:
    source: str
    scope: str
    kind: str
    target: str
    line: int


IGNORED_DIRECTORIES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "runtime", "data",
    "artifacts", "output", "vendor", ".codex", ".agents", ".claude", ".superpowers", "ObsidianNotes",
    ".worktrees", "worktrees", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".aws",
}
REFERENCE_SUFFIXES = {".py", ".md", ".sh", ".command", ".yml", ".yaml", ".toml", ".json"}


def _files(root: Path, suffixes: set[str]):
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in IGNORED_DIRECTORIES
                            and not (Path(directory) / name).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix in suffixes and not path.is_symlink() and not name.startswith("firebase-service-account"):
                yield path


def _module(path: Path, root: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join((root.name, *parts)).removesuffix(".__init__")


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


class _Scanner(ast.NodeVisitor):
    def __init__(self, path, module, known_modules, source, pattern=None):
        self.path, self.module, self.known_modules = path, module, known_modules
        self.source, self.pattern = source, pattern
        self.package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        self.names, self.function_depth, self.type_checking = [], 0, False
        self.aliases = {}
        self.edges, self.references, self.dynamic = [], [], []
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.parents = {child: node for node in ast.walk(self.tree) for child in ast.iter_child_nodes(node)}

    @property
    def scope(self):
        if not self.names:
            return "module"
        return ("function:" if self.function_depth else "class:") + ".".join(self.names)

    def resolved(self, node):
        value = _dotted(node)
        first, dot, rest = value.partition(".")
        return self.aliases.get(first, first) + (dot + rest if dot else "")

    def add_dynamic(self, node, kind, target):
        self.dynamic.append(Reference(self.source, self.scope, kind, target, node.lineno))

    def import_target(self, node, target):
        if target in self.known_modules:
            self.edges.append(ImportEdge(self.module, target, node.lineno, self.scope, self.type_checking))

    def facade_target(self, node, kind, value):
        if self.pattern:
            for match in self.pattern.finditer(value):
                self.references.append(Reference(self.source, self.scope, kind, match.group(), node.lineno))

    def visit_Import(self, node):
        for alias in node.names:
            self.aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
            self.import_target(node, alias.name)
            self.facade_target(node, "import", alias.name)

    def visit_ImportFrom(self, node):
        prefix = node.module or ""
        if node.level:
            parts = self.package.split(".")
            prefix = ".".join(parts[:len(parts) - node.level + 1] + ([prefix] if prefix else []))
        # `from . import child` in __init__ loads the child, not the package again.
        targets = set() if prefix == self.module and self.path.name == "__init__.py" else {prefix}
        for alias in node.names:
            target = f"{prefix}.{alias.name}"
            targets.add(target)
            self.aliases[alias.asname or alias.name] = target
            self.facade_target(node, "import", prefix if alias.name == "*" else target)
        for target in sorted(targets):
            self.import_target(node, target)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            self.facade_target(node, "string", node.value)

    def visit_FunctionDef(self, node):
        if node.name in {"compat_dependency", "_compat_dependency"}:
            self.add_dynamic(node, "compat_definition", f"{self.module}.{node.name}")
        # Decorators and defaults are evaluated in the enclosing scope.
        for value in [*node.decorator_list, node.args, *([node.returns] if node.returns else [])]:
            self.visit(value)
        aliases = self.aliases.copy()
        parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        parameters += [arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None]
        for arg in parameters:
            self.aliases[arg.arg] = ""
        self.names.append(node.name)
        self.function_depth += 1
        for value in node.body:
            self.visit(value)
        self.function_depth -= 1
        self.names.pop()
        self.aliases = aliases

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        for value in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(value)
        aliases = self.aliases.copy()
        self.names.append(node.name)
        for value in node.body:
            self.visit(value)
        self.names.pop()
        self.aliases = aliases

    def type_guard(self, node):
        if self.resolved(node) == "typing.TYPE_CHECKING":
            return False
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = self.type_guard(node.operand)
            return None if value is None else not value
        if isinstance(node, ast.BoolOp):
            values = [self.type_guard(value) for value in node.values]
            if isinstance(node.op, ast.And):
                return False if False in values else True if all(value is True for value in values) else None
            return True if True in values else False if all(value is False for value in values) else None
        return None

    def visit_If(self, node):
        self.visit(node.test)
        original = self.type_checking
        guard = self.type_guard(node.test)
        for body, unreachable in ((node.body, guard is False), (node.orelse, guard is True)):
            self.type_checking = original or unreachable
            for value in body:
                self.visit(value)
        self.type_checking = original

    @staticmethod
    def target_value(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id == "__name__":
            return "<__name__>"
        return "<dynamic>"

    def registry(self, node):
        if self.resolved(node) != "sys.modules":
            return
        parent = self.parents.get(node)
        if isinstance(parent, ast.Subscript):
            target = self.target_value(parent.slice)
        else:
            call = self.parents.get(parent)
            target = self.target_value(call.args[0]) if isinstance(call, ast.Call) and call.args else "<dynamic>"
        self.add_dynamic(node, "module_registry", target)

    def visit_Attribute(self, node):
        self.registry(node)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            self.aliases[node.id] = ""
            return
        self.registry(node)

    def visit_Call(self, node):
        name = self.resolved(node.func)
        if name in {"importlib.import_module", "__import__"}:
            self.add_dynamic(node, "dynamic_import", self.target_value(node.args[0]) if node.args else "<dynamic>")
        if name.rsplit(".", 1)[-1] in {"compat_dependency", "_compat_dependency"}:
            target = name if "." in name else f"{self.module}.{name}"
            self.add_dynamic(node, "compat_call", target)
        if (isinstance(node.func, ast.Attribute) and node.func.attr in {"setdefault", "update", "__setitem__"}
                and isinstance(node.func.value, ast.Call) and _dotted(node.func.value.func) == "globals"):
            self.add_dynamic(node, "globals_injection", "<dynamic>")
        self.generic_visit(node)


def collect_python_edges(root: Path) -> list[ImportEdge]:
    modules = {_module(path, root): path for path in _files(root, {".py"})}
    edges = []
    for module, path in sorted(modules.items()):
        scanner = _Scanner(path, module, modules, module)
        scanner.visit(scanner.tree)
        edges.extend(scanner.edges)
    return edges


def strongly_connected_components(edges: list[ImportEdge]) -> list[set[str]]:
    graph = defaultdict(set)
    for edge in edges:
        if not edge.type_checking:
            graph[edge.importer].add(edge.imported)
            graph[edge.imported]
    indices, low, active, stack, groups = {}, {}, set(), [], []

    def visit(node):
        indices[node] = low[node] = len(indices)
        stack.append(node)
        active.add(node)
        for target in sorted(graph[node]):
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in active:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            group = set()
            while True:
                target = stack.pop()
                active.remove(target)
                group.add(target)
                if target == node:
                    break
            if len(group) > 1 or node in graph[node]:
                groups.append(group)

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(groups, key=lambda group: (-len(group), sorted(group)))


def cyclic_edges(edges: list[ImportEdge]) -> list[ImportEdge]:
    owners = {node: number for number, group in enumerate(strongly_connected_components(edges)) for node in group}
    return [edge for edge in edges if not edge.type_checking and edge.importer in owners
            and owners[edge.importer] == owners.get(edge.imported)]


def _matches(module, prefixes):
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def forbidden_references(edges: list[ImportEdge], rules: list[dict]) -> list[Reference]:
    return [Reference(edge.importer, edge.scope, rule["id"], edge.imported, edge.line)
            for edge in edges if not edge.type_checking for rule in rules
            if _matches(edge.importer, rule["importers"]) and _matches(edge.imported, rule["targets"])
            and edge.importer not in rule.get("exclude_importers", [])]


def collect_facade_references(root: Path, facades: list[str], *, exclude_paths: set[str] | None = None) -> list[Reference]:
    pattern = re.compile(r"(?<![\w.])(?:" + "|".join(re.escape(value) for value in sorted(facades, key=len, reverse=True))
                         + r")(?:\.[A-Za-z_][A-Za-z_0-9]*)*(?![\w.])")
    references = []
    for path in _files(root, REFERENCE_SUFFIXES):
        source = path.relative_to(root).as_posix()
        if source in (exclude_paths or set()):
            continue
        if path.suffix == ".py":
            parts = path.relative_to(root).parts
            module_root = root.joinpath(*parts[:parts.index("backend")]) if "backend" in parts else root.parent
            module = _module(path, module_root / "backend") if "backend" in parts else _module(path, module_root)
            scanner = _Scanner(path, module, set(), source, pattern)
            scanner.visit(scanner.tree)
            references.extend(scanner.references)
        else:
            kind = "text" if path.suffix == ".md" else "config_string"
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                references.extend(Reference(source, "module", kind, match.group(), number) for match in pattern.finditer(line))
    return references


def collect_dynamic_references(root: Path) -> list[Reference]:
    references = []
    for path in _files(root, {".py"}):
        scanner = _Scanner(path, _module(path, root), set(), path.relative_to(root).as_posix())
        scanner.visit(scanner.tree)
        references.extend(scanner.dynamic)
    return references


def _reference(item):
    if isinstance(item, ImportEdge):
        return Reference(item.importer, item.scope, "import", item.imported, item.line)
    return item


def inventory_records(items) -> list[dict]:
    counts, lines = Counter(), defaultdict(set)
    for item in map(_reference, items):
        key = (item.source, item.scope, item.kind, item.target)
        counts[key] += 1
        lines[key].add(item.line)
    return [dict(zip(("source", "scope", "kind", "target"), key, strict=True), count=count, lines=sorted(lines[key]))
            for key, count in sorted(counts.items())]


def inventory_changes(actual, expected) -> tuple[list[dict], list[dict]]:
    def counts(records):
        return {(r["source"], r["scope"], r["kind"], r["target"]): r["count"] for r in records}

    current, baseline = counts(inventory_records(actual)), counts(expected)
    added, stale = [], []
    for key in sorted(current.keys() | baseline.keys()):
        before, after = baseline.get(key, 0), current.get(key, 0)
        if before != after:
            record = dict(zip(("source", "scope", "kind", "target"), key, strict=True), before=before, after=after)
            (added if after > before else stale).append(record)
    return added, stale
