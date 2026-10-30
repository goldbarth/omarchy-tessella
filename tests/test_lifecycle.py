"""Installed Omarchy/Quickshell integration tests, with a temporary HOME.

SCENULA_INTEGRATION=1 python3 -m unittest discover -s tests -v
Set SCENULA_QPA_PLATFORM=wayland to check panel loading with closed windows.
Only compositor screen detection and the final wallpaper setter are substituted.
Plugin installation, updates, registry discovery, QML loading and IPC are real.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_migration import FILES, ROOT, MigrationFixture, write_json


@unittest.skipUnless(os.environ.get("SCENULA_INTEGRATION") == "1", "requires installed Omarchy and Quickshell")
class LifecycleTests(MigrationFixture):
    def setUp(self):
        super().setUp()
        self.host = self.home / "shell"
        self.host.mkdir()
        shutil.copy2(ROOT / "tests/shell.qml", self.host / "shell.qml")
        for name in ("Commons", "Ui", "services"):
            (self.host / name).symlink_to(Path("/usr/share/omarchy/shell") / name,
                                         target_is_directory=True)
        runtime = self.home / "runtime"
        runtime.mkdir(mode=0o700)
        platform = os.environ.get("SCENULA_QPA_PLATFORM", "offscreen")
        self.env.update(XDG_RUNTIME_DIR=str(runtime), QT_QPA_PLATFORM=platform,
                        QT_QPA_PLATFORMTHEME="generic", OMARCHY_PATH="/usr/share/omarchy",
                        SCENULA_TEST_SHELL=str(self.host))
        if platform == "wayland":
            display = Path(os.environ["WAYLAND_DISPLAY"])
            if not display.is_absolute():
                display = Path(os.environ["XDG_RUNTIME_DIR"]) / display
            self.env["WAYLAND_DISPLAY"] = str(display)
        self.shim("omarchy-shell", 'exec qs ipc -p "$SCENULA_TEST_SHELL" call -- "$@"')
        self.log = self.home / "quickshell.log"
        self.log_file = self.log.open("w")
        self.addCleanup(self.log_file.close)
        self.process = None

    def start_shell(self):
        self.process = subprocess.Popen(["qs", "-p", str(self.host), "--no-color"],
                                        env=self.env, stdout=self.log_file, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop_shell)
        self.wait_report(lambda r: True)

    def stop_shell(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def assert_no_qml_errors(self):
        self.log_file.flush()
        log = self.log.read_text()
        for marker in ("ReferenceError:", "TypeError:", "Cannot assign", "Failed to load configuration"):
            self.assertNotIn(marker, log, log)

    def assert_panel_loaded(self, report):
        if self.env["QT_QPA_PLATFORM"] == "wayland":
            self.assertTrue(report["panelLoaded"], self.log.read_text())

    def ipc(self, target, method, *args):
        result = subprocess.run(["omarchy-shell", target, method, *args], env=self.env,
                                text=True, capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    def wait_report(self, condition, timeout=15):
        deadline = time.monotonic() + timeout
        report = None
        while time.monotonic() < deadline:
            try:
                report = json.loads(self.ipc("test", "report"))
                if condition(report):
                    return report
            except (RuntimeError, ValueError):
                pass
            time.sleep(0.1)
        self.fail(f"Timed out: {report}\n{self.log.read_text()}")

    def command(self, *args):
        result = subprocess.run(args, env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result.stdout

    def git(self, directory, *args):
        return self.command("git", "-C", str(directory), *args)

    def make_upstream(self, legacy=False):
        upstream = self.home / "upstream"
        upstream.mkdir()
        for name in FILES:
            shutil.copy2(ROOT / name, upstream / name)
        if legacy:
            # Legacy identity fixture, independent of repository history.
            # Keep the real service API so hot-reloaded widgets can use it
            # while the manifest rescan is pending, as in the published plugin.
            manifest = json.loads((upstream / "manifest.json").read_text())
            manifest.update(id="goldbarth.tessella", name="Tessella", version="0.2.0")
            write_json(upstream / "manifest.json", manifest)
            for name in ("Service.qml", "BarWidget.qml", "Panel.qml"):
                text = (upstream / name).read_text().replace("scenula", "tessella")
                if name == "BarWidget.qml":
                    text = text.replace('  ScenulaIpc { target: "goldbarth.tessella" }', "")
                (upstream / name).write_text(text)
        self.git(upstream, "init", "-q", "-b", "main")
        self.git(upstream, "config", "user.name", "Migration test")
        self.git(upstream, "config", "user.email", "migration-test@example.invalid")
        self.git(upstream, "add", ".")
        self.git(upstream, "commit", "-qm", "Plugin fixture")
        return upstream

    def test_fresh_install_discovery_widget_ipc_and_retained_uninstall_data(self):
        shutil.rmtree(self.old)
        self.config["bar"]["layout"]["right"] = []
        self.config["plugins"] = []
        write_json(self.shell, self.config)
        upstream = self.make_upstream()
        self.start_shell()
        self.command("omarchy-plugin-add", str(upstream), "--enable", "--yes")
        report = self.wait_report(lambda r: r["loaded"] and r["widgetReady"])
        self.assertEqual(report["ids"], ["goldbarth.scenula"])
        self.assertEqual(report["error"], "")
        self.assert_panel_loaded(report)
        self.assertIn("enabled=false", self.ipc("goldbarth.scenula", "status"))
        self.assertEqual(self.ipc("goldbarth.scenula", "status"),
                         self.ipc("goldbarth.tessella", "status"))
        self.assertFalse((self.home / "background-set").exists())
        self.assertTrue((self.new / ".git").is_dir())
        self.assertFalse((self.home / ".config/omarchy/tessella").exists())
        self.command("omarchy-plugin-remove", "goldbarth.scenula", "--yes")
        self.wait_report(lambda r: r["ids"] == [] and not r["widgetLoaded"])
        self.assertTrue((self.home / ".config/omarchy/scenula/config.json").is_file())
        self.assert_no_qml_errors()

    def test_actual_update_reproduces_id_gap_then_migration_restores_widget_and_rotation(self):
        config_path, data, state = self.populate()
        config = json.loads(config_path.read_text())
        config.update(palette="", intervalMinutes=30)
        write_json(config_path, config)
        sync_path = state / "sync.json"
        write_json(sync_path, {"lastSync": int(time.time()), "phase": "done", "running": False})
        original_library = (data / "library.json").read_bytes()
        upstream = self.make_upstream(legacy=True)
        shutil.rmtree(self.old)
        self.command("git", "clone", "-q", str(upstream), str(self.old))
        self.start_shell()
        self.wait_report(lambda r: r["active"] == "goldbarth.tessella" and r["widgetLoaded"])
        for name in FILES:
            shutil.copy2(ROOT / name, upstream / name)
        self.git(upstream, "add", ".")
        self.git(upstream, "commit", "-qm", "Rename fixture to Scenula")
        self.command("omarchy-plugin-update", "goldbarth.tessella", "--yes")
        # Real registry: the new manifest does not match the old shell entry.
        self.wait_report(lambda r: r["ids"] == ["goldbarth.scenula"] and r["active"] == "")
        self.cli("migrate")
        report = self.wait_report(lambda r: r["loaded"] and r["widgetReady"])
        self.assertEqual(report["error"], "")
        self.assert_panel_loaded(report)
        self.assertTrue(report["config"]["paused"])
        self.assertEqual(report["current"]["at"], 123456789)
        self.assertEqual((data / "library.json").read_bytes(), original_library)
        self.assertFalse((self.home / "background-set").exists())
        self.assertEqual(self.ipc("goldbarth.scenula", "status"),
                         self.ipc("goldbarth.tessella", "status"))
        self.ipc("goldbarth.tessella", "next")
        report = self.wait_report(lambda r: r["current"] and r["current"]["at"] != 123456789)
        self.assertEqual(report["error"], "")
        output = Path((self.home / "background-set").read_text().strip())
        self.assertTrue(output.is_file())
        self.assertTrue(output.read_bytes().startswith(b"\xff\xd8"))
        self.ipc("goldbarth.scenula", "resume")
        self.wait_report(lambda r: r["config"].get("paused") is False)
        # Expire persisted rotation state; the real FileView/reconcile schedules
        # the next collage without an IPC next call.
        persisted = json.loads((state / "state.json").read_text())
        persisted["current"]["at"] = 1
        config = json.loads(config_path.read_text())
        config["resumedAt"] = 0
        write_json(config_path, config)
        write_json(state / "state.json", persisted)
        time.sleep(0.4)
        report = self.wait_report(lambda r: r["current"] and r["current"]["path"] != str(output))
        self.assertEqual(report["error"], "")
        self.ipc("goldbarth.scenula", "pause")
        self.wait_report(lambda r: r["config"].get("paused") is True)
        self.command("omarchy-plugin-update", "goldbarth.scenula", "--yes")
        # Restart the real QML service and verify persisted state is observed.
        self.stop_shell()
        self.start_shell()
        report = self.wait_report(lambda r: r["loaded"] and r["widgetReady"])
        self.assertTrue(report["config"]["paused"])
        self.assertEqual((data / "library.json").read_bytes(), original_library)
        self.assertEqual(report["ids"], ["goldbarth.scenula"])
        self.assert_no_qml_errors()

    def test_data_conflict_blocks_startup_without_overwriting_legacy_config(self):
        config_path, data, state = self.populate()
        self.old.rename(self.new)
        self.config["bar"]["layout"]["right"][0]["id"] = "goldbarth.scenula"
        self.config["plugins"] = []
        write_json(self.shell, self.config)
        (self.home / ".local/share/scenula").mkdir()
        before = self.snapshot([config_path.parent, data, state])
        self.start_shell()
        report = self.wait_report(lambda r: "exist independently" in r["error"])
        self.assertFalse(report["initialized"])
        self.assertFalse(report["widgetReady"])
        self.assertEqual(before, self.snapshot([config_path.parent, data, state]))
        self.assertFalse((self.home / ".config/omarchy/scenula").exists())


if __name__ == "__main__":
    unittest.main()
