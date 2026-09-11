import unittest
from pathlib import Path


DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy" / "aws"


class DeploymentScriptTests(unittest.TestCase):
    def test_deploy_uploads_to_immutable_release_and_versioned_snapshot(self):
        script = (DEPLOY_DIR / "deploy_to_ec2.sh").read_text(encoding="utf-8")

        self.assertIn('REMOTE_RELEASE="$APP_ROOT/releases/$RELEASE_ID"', script)
        self.assertIn('REMOTE_SNAPSHOT="$APP_ROOT/snapshots/$RELEASE_ID"', script)
        self.assertIn('"$REMOTE:$REMOTE_RELEASE/assistant_web/"', script)
        self.assertNotIn('"$REMOTE:$APP_ROOT/"', script)
        self.assertIn("mantis_index.sqlite3", script)

    def test_setup_arms_rollback_before_nginx_or_current_activation(self):
        script = (DEPLOY_DIR / "setup_server.sh").read_text(encoding="utf-8")

        trap_index = script.index("trap rollback_deployment ERR")
        certbot_index = script.index('certificate_path="/etc/letsencrypt')
        switch_index = script.index('atomic_switch_current "$RELEASE_DIR"')
        self.assertLess(trap_index, certbot_index)
        self.assertLess(trap_index, switch_index)
        self.assertIn("WorkingDirectory=$CURRENT_LINK/assistant_web", script)
        self.assertIn(
            "Environment=MANTIS_INDEX_MODE=$MANTIS_INDEX_MODE",
            script,
        )
        self.assertIn("Activation failed; rolling current back", script)

    def test_shared_database_changes_are_quiesced_backed_up_and_recoverable(self):
        script = (DEPLOY_DIR / "setup_server.sh").read_text(encoding="utf-8")
        preflight = script.index("scripts/runtime_databases.py check")
        trap = script.index("trap rollback_deployment ERR")
        backup = script.index('scripts/runtime_databases.py" backup')
        migration = script.index('scripts/runtime_databases.py" initialize')
        activation = script.index('atomic_switch_current "$RELEASE_DIR"')
        self.assertLess(preflight, trap)
        self.assertLess(trap, backup)
        self.assertLess(backup, migration)
        self.assertLess(migration, activation)
        self.assertIn('scripts/runtime_databases.py" restore', script)
        self.assertIn('scripts/runtime_databases.py" import', script)
        self.assertIn("database_change_started=1", script)
        self.assertIn("flock -n 9", script)

    def test_manual_rollback_checks_schema_before_switching_code(self):
        script = (DEPLOY_DIR / "rollback_release.sh").read_text(encoding="utf-8")
        self.assertLess(script.index('scripts/runtime_databases.py" check'), script.index('ln -s "$target"'))

    def test_deploy_passes_validated_index_mode_to_setup(self):
        script = (DEPLOY_DIR / "deploy_to_ec2.sh").read_text(encoding="utf-8")

        self.assertIn('MANTIS_INDEX_MODE="${MANTIS_INDEX_MODE:-primary}"', script)
        self.assertIn("MANTIS_INDEX_MODE='$MANTIS_INDEX_MODE'", script)
        self.assertIn("MANTIS_INDEX_MODE must be off, shadow, or primary", script)

    def test_manual_rollback_restores_previous_release_on_failed_readiness(self):
        script = (DEPLOY_DIR / "rollback_release.sh").read_text(encoding="utf-8")

        self.assertIn("trap restore_previous ERR", script)
        self.assertIn('mv -Tf "$temporary_link" "$CURRENT_LINK"', script)
        self.assertIn("/ready", script)


if __name__ == "__main__":
    unittest.main()
