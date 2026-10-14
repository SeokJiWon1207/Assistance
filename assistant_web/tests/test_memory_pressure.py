import tempfile
import unittest
from pathlib import Path

from backend.memory_pressure import available_memory_bytes


class MemoryPressureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.cgroup = self.root / "cgroup"
        (self.proc / "self").mkdir(parents=True)
        self.cgroup.mkdir()

    def host(self, kib):
        (self.proc / "meminfo").write_text(f"MemTotal: 2097152 kB\nMemAvailable: {kib} kB\n")

    def limit(self, directory, maximum, current):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "memory.max").write_text(str(maximum))
        (directory / "memory.current").write_text(str(current))

    def test_host_available_memory_is_used_when_cgroup_is_unlimited(self):
        self.host(500000)
        (self.proc / "self/cgroup").write_text("0::/system.slice/app.service\n")
        self.limit(self.cgroup / "system.slice/app.service", "max", 200000000)
        self.assertEqual(available_memory_bytes(self.proc, self.cgroup), 512000000)

    def test_cgroup_headroom_caps_host_free_memory(self):
        self.host(500000)
        (self.proc / "self/cgroup").write_text("0::/service\n")
        self.limit(self.cgroup / "service", 300000000, 200000000)
        self.assertEqual(available_memory_bytes(self.proc, self.cgroup), 100000000)

    def test_ancestor_limit_is_respected_when_child_is_unlimited(self):
        self.host(500000)
        (self.proc / "self/cgroup").write_text("0::/parent/child\n")
        self.limit(self.cgroup / "parent/child", "max", 50000000)
        self.limit(self.cgroup / "parent", 300000000, 275000000)
        self.assertEqual(available_memory_bytes(self.proc, self.cgroup), 25000000)

    def test_cgroup_root_namespace_and_overlimit_report_zero(self):
        (self.proc / "self/cgroup").write_text("0::/\n")
        self.limit(self.cgroup, 100000000, 120000000)
        self.assertEqual(available_memory_bytes(self.proc, self.cgroup), 0)

    def test_missing_or_malformed_metrics_fall_back_to_available_source(self):
        (self.proc / "meminfo").write_text("MemAvailable: invalid kB\n")
        (self.proc / "self/cgroup").write_text("0::/service\n")
        self.limit(self.cgroup / "service", 300000000, 200000000)
        self.assertEqual(available_memory_bytes(self.proc, self.cgroup), 100000000)
        (self.cgroup / "service/memory.current").write_text("invalid")
        self.assertIsNone(available_memory_bytes(self.proc, self.cgroup))

    def test_cgroup_path_cannot_escape_mount(self):
        self.host(500000)
        (self.proc / "self/cgroup").write_text("0::/../outside\n")
        self.limit(self.root / "outside", 1000, 999)
        self.assertEqual(available_memory_bytes(self.proc, self.cgroup), 512000000)
