"""실행 import 없이 의존 경계와 한시적 이전 예외를 검사한다."""

import json
import tempfile
import unittest
from pathlib import Path

from architecture_graph import (
    ImportEdge,
    Reference,
    collect_dynamic_references,
    collect_facade_references,
    collect_python_edges,
    cyclic_edges,
    forbidden_references,
    inventory_changes,
    inventory_records,
    strongly_connected_components,
)


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "assistant_web" / "backend"
FIXTURE = Path(__file__).parent / "fixtures" / "architecture_migration.json"


class GraphExtractionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backend = self.root / "backend"
        self.write("__init__.py", "")

    def write(self, name, source):
        path = self.backend / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def test_nested_and_relative_imports_are_resolved_without_executing_code(self):
        self.write("leaf.py", "raise RuntimeError('must not execute')")
        self.write("pkg/__init__.py", "")
        self.write("pkg/child.py", "")
        self.write("consumer.py", """from . import leaf
from backend.pkg import child, missing_attribute
def outer():
    if True:
        from .leaf import value
    class Inner:
        def run(self):
            import backend.pkg.child as other
""")
        edges = collect_python_edges(self.backend)
        selected = [(e.imported, e.scope) for e in edges if e.importer == "backend.consumer"]
        self.assertIn(("backend.leaf", "module"), selected)
        self.assertIn(("backend.pkg.child", "module"), selected)
        self.assertIn(("backend.leaf", "function:outer"), selected)
        self.assertIn(("backend.pkg.child", "function:outer.Inner.run"), selected)
        self.assertFalse(any("missing_attribute" in target for target, _ in selected))

    def test_package_relative_imports_and_reexports(self):
        self.write("leaf.py", "")
        self.write("pkg/__init__.py", "from .child import value")
        self.write("pkg/child.py", "from ..leaf import value")
        pairs = {(e.importer, e.imported) for e in collect_python_edges(self.backend)}
        self.assertIn(("backend.pkg", "backend.pkg.child"), pairs)
        self.assertIn(("backend.pkg.child", "backend.leaf"), pairs)

    def test_package_child_reexport_is_not_a_self_cycle(self):
        self.write("pkg/__init__.py", "from . import child")
        self.write("pkg/child.py", "")
        edges = collect_python_edges(self.backend)
        self.assertIn(("backend.pkg", "backend.pkg.child"), {(e.importer, e.imported) for e in edges})
        self.assertEqual(strongly_connected_components(edges), [])

    def test_type_checking_aliases_nested_guards_and_else_are_separate(self):
        self.write("leaf.py", "")
        self.write("consumer.py", """import typing as t
from typing import TYPE_CHECKING as TC
if t.TYPE_CHECKING:
    from .leaf import one
if TC and enabled:
    def hidden():
        from .leaf import two
if not TC:
    from .leaf import three
else:
    from .leaf import four
if TC or enabled:
    from .leaf import five
""")
        edges = [e for e in collect_python_edges(self.backend) if e.imported == "backend.leaf"]
        self.assertEqual([e.type_checking for e in edges], [True, True, False, True, False])
        self.assertEqual(edges[1].scope, "function:hidden")

    def test_shadowed_type_checking_name_does_not_hide_runtime_imports(self):
        self.write("leaf.py", "")
        self.write("consumer.py", """from typing import TYPE_CHECKING as TC
def run(TC):
    if TC:
        from .leaf import one
TC = runtime_flag
if TC:
    from .leaf import two
""")
        self.assertFalse(any(edge.type_checking for edge in collect_python_edges(self.backend)))

    def test_dag_three_node_cycle_and_self_cycle(self):
        edges = [ImportEdge("a", "b", 1), ImportEdge("b", "c", 1)]
        self.assertEqual(strongly_connected_components(edges), [])
        edges += [ImportEdge("c", "a", 1), ImportEdge("d", "d", 1)]
        self.assertEqual(strongly_connected_components(edges), [{"a", "b", "c"}, {"d"}])
        self.assertEqual(len(cyclic_edges(edges)), 4)
        self.assertEqual(strongly_connected_components([
            ImportEdge("a", "b", 1), ImportEdge("b", "a", 1, type_checking=True),
        ]), [])

    def test_new_cycle_edge_inside_existing_component_is_detected(self):
        old = [ImportEdge("a", "b", 1), ImportEdge("b", "c", 1), ImportEdge("c", "a", 1)]
        expected = inventory_records(cyclic_edges(old))
        actual = cyclic_edges([*old, ImportEdge("a", "c", 20, "function:new")])
        added, stale = inventory_changes(actual, expected)
        self.assertEqual(len(added), 1)
        self.assertEqual(stale, [])

    def test_lines_may_move_but_duplicate_sites_and_stale_exceptions_fail(self):
        expected = inventory_records([ImportEdge("a", "b", 1)])
        self.assertEqual(inventory_changes([ImportEdge("a", "b", 99)], expected), ([], []))
        added, _ = inventory_changes([ImportEdge("a", "b", 4), ImportEdge("a", "b", 5)], expected)
        self.assertEqual(len(added), 1)
        _, stale = inventory_changes([], expected)
        self.assertEqual(len(stale), 1)

    def test_forbidden_direction_uses_module_boundaries_and_exact_exclusions(self):
        rules = [{"id": "report-evaluation", "importers": ["backend.reports"],
                  "targets": ["backend.evaluation"], "exclude_importers": ["backend.reports.evaluation"]}]
        edges = [ImportEdge("backend.reports.aggregate", "backend.evaluation.check", 1),
                 ImportEdge("backend.reports.evaluation", "backend.evaluation.core", 1),
                 ImportEdge("backend.reports_extra", "backend.evaluation.check", 1)]
        violations = forbidden_references(edges, rules)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].kind, "report-evaluation")

    def test_facade_imports_symbols_patch_strings_and_relative_paths(self):
        self.write("legacy.py", "")
        self.write("consumer.py", """from . import legacy
from backend.legacy import work as action
import backend.legacy as old
patch('backend.legacy.work')
patch('backend.legacy_extra.work')
""")
        refs = collect_facade_references(self.root, ["backend.legacy"])
        self.assertEqual(sum(r.kind == "import" for r in refs), 3)
        self.assertEqual([r.target for r in refs if r.kind == "string"], ["backend.legacy.work"])
        self.assertIn("backend.legacy.work", [r.target for r in refs if r.kind == "import"])
        self.assertEqual(inventory_changes(refs, inventory_records(refs)), ([], []))
        old = inventory_records(refs)
        refs.append(Reference("backend/consumer.py", "function:other", "string", "backend.legacy.work", 10))
        self.assertTrue(inventory_changes(refs, old)[0])

    def test_dynamic_registry_aliases_import_calls_and_compat_are_visible(self):
        self.write("consumer.py", """import sys as system
from sys import modules as registry
from importlib import import_module as load
from backend.notion.compat import compat_dependency as dependency
def _compat_dependency(name, default):
    return system.modules.get('backend.legacy', default)
registry[__name__] = replacement
load('backend.legacy')
__import__(variable_name)
globals().setdefault(name, value)
dependency('work', default)
_compat_dependency('other', default)
""")
        refs = collect_dynamic_references(self.backend)
        kinds = [r.kind for r in refs]
        self.assertEqual(kinds.count("module_registry"), 2)
        self.assertEqual(kinds.count("dynamic_import"), 2)
        self.assertEqual(kinds.count("globals_injection"), 1)
        self.assertEqual(kinds.count("compat_definition"), 1)
        self.assertEqual(kinds.count("compat_call"), 2)
        self.assertTrue(any(r.target == "<dynamic>" for r in refs))

    def test_documentation_is_inventory_only_and_exclusions_are_explicit(self):
        (self.root / "notes.md").write_text("`backend.legacy.work` is deprecated", encoding="utf-8")
        self.write("consumer.py", "import backend.legacy")
        refs = collect_facade_references(self.root, ["backend.legacy"])
        self.assertEqual({r.kind for r in refs}, {"import", "text"})
        without = collect_facade_references(self.root, ["backend.legacy"], exclude_paths={"backend/consumer.py"})
        self.assertEqual([r.kind for r in without], ["text"])

    def test_shell_and_configuration_references_are_guarded(self):
        (self.root / "deploy.command").write_text("python -c 'import backend.legacy'", encoding="utf-8")
        (self.root / "config.toml").write_text('handler = "backend.legacy.work"', encoding="utf-8")
        refs = collect_facade_references(self.root, ["backend.legacy"])
        self.assertEqual(len(refs), 2)
        self.assertEqual({r.kind for r in refs}, {"config_string"})

    def test_other_worktrees_caches_and_credentials_are_outside_source_inventory(self):
        for name in (".worktrees/other/consumer.py", ".mypy_cache/metadata.json", "firebase-service-account-test.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('"backend.legacy.work"', encoding="utf-8")
        self.assertEqual(collect_facade_references(self.root, ["backend.legacy"]), [])


class ArchitectureMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.edges = collect_python_edges(BACKEND)

    def assertInventory(self, actual, key):
        added, stale = inventory_changes(actual, self.baseline[key])
        self.assertEqual(added, [], f"새 구조 위반({key}); 기준선을 자동 갱신하지 마세요: {added}")
        self.assertEqual(stale, [], f"해소된 예외({key})를 이전 목록에서 제거하세요: {stale}")

    def test_import_time_graph_stays_acyclic(self):
        eager = [edge for edge in self.edges if not edge.scope.startswith("function:")]
        self.assertEqual(strongly_connected_components(eager), [])

    def test_no_new_runtime_cycle_edges(self):
        self.assertInventory(cyclic_edges(self.edges), "cyclic_imports")

    def test_no_new_forbidden_layer_dependencies(self):
        self.assertInventory(forbidden_references(self.edges, self.baseline["rules"]), "forbidden_imports")

    def test_no_new_facade_imports_or_patch_targets(self):
        refs = collect_facade_references(ROOT, self.baseline["facades"],
                                         exclude_paths=set(self.baseline["reference_exclusions"]))
        self.assertInventory([r for r in refs if r.kind != "text"], "facade_references")

    def test_no_new_dynamic_dependency_dispatch(self):
        self.assertInventory(collect_dynamic_references(BACKEND), "dynamic_references")

    def test_no_new_retrieval_patch_targets(self):
        refs = collect_facade_references(ROOT, ["backend.retrieval"],
                                         exclude_paths=set(self.baseline["reference_exclusions"]))
        self.assertInventory([r for r in refs if r.kind == "string"], "retrieval_patch_targets")

    def test_migration_exceptions_have_an_owner_and_exit_condition(self):
        self.assertEqual(self.baseline["schema_version"], 1)
        for key in ("cyclic_imports", "forbidden_imports", "facade_references", "dynamic_references", "retrieval_patch_targets"):
            identities = [(r["source"], r["scope"], r["kind"], r["target"]) for r in self.baseline[key]]
            self.assertEqual(len(identities), len(set(identities)), f"중복 예외: {key}")
            for record in self.baseline[key]:
                with self.subTest(key=key, site=record["source"]):
                    self.assertRegex(record["stage"], r"^P[1-9](?:-[A-C])?$")
                    self.assertIn(record["exit_condition"], self.baseline["exit_conditions"])
                    self.assertGreater(record["count"], 0)
        self.assertEqual({r["id"] for r in self.baseline["rules"]}, {
            "intent-lower-boundary", "reports-lower-boundary", "sources-lower-boundary",
            "pure-lower-boundary", "notion-foundation-boundary", "chat-step-boundary",
            "configuration-foundation-boundary", "settings-no-notion-runtime",
        })


if __name__ == "__main__":
    unittest.main()
