import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import workspace_data


class WorkspaceDataCacheTests(unittest.TestCase):
    def tearDown(self):
        workspace_data.clear_workspace_snapshot_cache()

    def test_snapshot_is_parsed_once_until_its_files_change(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "chroma.sqlite3"
            index_path = Path(directory) / "mantis_index.sqlite3"
            database_path.write_bytes(b"first")
            index_path.write_bytes(b"index")
            snapshot = {
                "available": True,
                "matched_count": 1,
                "issues": [{"id": 1}],
                "reason": "",
            }
            with (
                patch.object(workspace_data, "CHROMA_DB_PATH", database_path),
                patch.dict(
                    os.environ,
                    {"MANTIS_INDEX_DB_PATH": str(index_path), "MANTIS_INDEX_MODE": "primary"},
                ),
                patch.object(workspace_data, "scan_mantis_documents", return_value=snapshot) as scan,
            ):
                first = workspace_data.load_workspace_snapshot()
                second = workspace_data.load_workspace_snapshot()
                database_path.write_bytes(b"second-version")
                third = workspace_data.load_workspace_snapshot()

        self.assertEqual(first, second)
        self.assertEqual(third[0], [{"id": 1}])
        self.assertEqual(scan.call_count, 2)
