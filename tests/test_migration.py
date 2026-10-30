"""Offline migration regression tests. Run: python3 -m unittest discover -s tests -v"""

import fcntl
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("manifest.json", "Service.qml", "BarWidget.qml", "Panel.qml",
         "scenula.py", "tessella.py", "palettes.json")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class MigrationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="scenula-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.plugins = self.home / ".config/omarchy/plugins"
        self.old = self.plugins / "goldbarth.tessella"
        self.new = self.plugins / "goldbarth.scenula"
        self.old.mkdir(parents=True)
        for name in FILES:
            shutil.copy2(ROOT / name, self.old / name)
        self.shell = self.home / ".config/omarchy/shell.json"
        self.config = {"version": 1, "bar": {"centerAnchor": "goldbarth.tessella", "layout": {
            "left": [{"id": "unrelated", "custom": "goldbarth.tessella"}], "center": [],
            "right": [{"id": "goldbarth.tessella", "custom": {"keep": True}}]}},
            "plugins": [{"id": "goldbarth.tessella", "keep": 123}],
            "disabledPlugins": [], "unrelated": {"id": "goldbarth.tessella"}}
        write_json(self.shell, self.config)
        self.original = self.shell.read_bytes()
        self.shell.chmod(0o600)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.shim("omarchy-shell", "exit 1")
        self.shim("hyprctl", "echo '[{\"width\":640,\"height\":360,\"transform\":0}]'")
        self.shim("omarchy-theme-bg-set", 'echo "$1" > "$HOME/background-set"')
        self.env = dict(os.environ, HOME=str(self.home),
                        PATH=f"{self.bin}:/usr/share/omarchy/bin:{os.environ['PATH']}")

    def shim(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    def cli(self, *args, script=None, ok=True):
        script = script or (self.new if self.new.exists() else self.old) / "scenula.py"
        result = subprocess.run([sys.executable, "-I", str(script), *args],
                                env=self.env, text=True, capture_output=True, timeout=20)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def populate(self, brand="tessella"):
        config = self.home / f".config/omarchy/{brand}/config.json"
        data = self.home / f".local/share/{brand}"
        state = self.home / f".local/state/{brand}"
        write_json(config, {"enabled": True, "paused": True, "artists": ["demo"],
                            "intervalMinutes": 60, "resumedAt": 123, "palette": "nord",
                            "hiddenImages": ["demo/4"], "futureSetting": {"kept": True}})
        entries = {}
        for i in range(5):
            path = data / f"images/demo/{i}.ppm"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"P6\n80 100\n255\n" + bytes((i * 40, 80, 120)) * 8000)
            entries[f"demo/{i}"] = {"id": str(i), "artist": "demo", "title": f"Scene {i}",
                                    "project": f"https://example.invalid/{i}", "path": str(path),
                                    "w": 80, "h": 100, "tags": ["day"], "tagVersion": 4,
                                    "swatches": [[50, 0, 0, 1]], "swatchVersion": 1}
        write_json(data / "library.json", {"images": entries, "futureMetadata": 42})
        collage = data / "collages/collage-1.jpg"
        collage.parent.mkdir()
        collage.write_bytes(b"existing-collage")
        current = {"themes": ["day"], "images": [dict(key=f"demo/{i}", **entries[f"demo/{i}"])
                                                   for i in range(2)],
                   "screen": [640, 360], "layout": "portrait-cols-2", "at": 123456789,
                   "path": str(collage)}
        write_json(state / "state.json", {"history": ["demo/0", "demo/1"],
                                           "current": current, "previous": current, "futureState": 9})
        write_json(state / "sync.json", {"lastSync": 123456789, "phase": "done", "running": False})
        current_dir = self.home / ".local/state/omarchy/current"
        current_dir.mkdir(parents=True, exist_ok=True)
        (current_dir / "background").symlink_to(collage)
        return config, data, state

    def snapshot(self, paths):
        return {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                for root in paths for p in root.rglob("*") if p.is_file()}


class MigrationTests(MigrationFixture):
    def test_populated_upgrade_is_lossless_and_idempotent(self):
        config, data, state = self.populate()
        paths = [config.parent, data, state]
        before = self.snapshot(paths)
        self.cli("migrate")
        self.assertFalse(self.old.exists())
        self.assertTrue(self.new.is_dir())
        migrated = json.loads(self.shell.read_text())
        self.assertEqual(migrated["bar"]["layout"]["right"][0],
                         {"id": "goldbarth.scenula", "custom": {"keep": True}})
        self.assertEqual(migrated["bar"]["centerAnchor"], "goldbarth.scenula")
        self.assertEqual(migrated["unrelated"], self.config["unrelated"])
        self.assertEqual(migrated["bar"]["layout"]["left"], self.config["bar"]["layout"]["left"])
        backup = self.shell.with_name("shell.json.before-scenula")
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(self.shell.stat().st_mode & 0o777, 0o600)
        for parent in (".config/omarchy", ".local/share", ".local/state"):
            self.assertTrue((self.home / parent / "scenula").samefile(self.home / parent / "tessella"))
        self.cli("migrate")
        self.assertEqual(before, self.snapshot(paths))
        self.assertEqual(backup.read_bytes(), self.original)
        status = json.loads(self.cli("status").stdout)
        self.assertEqual((status["images"], status["tagged"], status["pendingColors"]), (4, 4, 0))
        self.assertTrue(status["back"]["available"])
        self.assertEqual(status["state"]["futureState"], 9)
        self.assertTrue((self.home / ".local/state/omarchy/current/background").is_file())

    def test_conflicting_data_fails_before_any_changes(self):
        config, data, state = self.populate()
        (self.home / ".local/share/scenula").mkdir()
        before = self.snapshot([config.parent, data, state])
        self.assertIn("Nothing was merged", self.cli("migrate", ok=False).stderr)
        self.assertEqual(self.shell.read_bytes(), self.original)
        self.assertTrue(self.old.is_dir())
        self.assertFalse((self.home / ".config/omarchy/scenula").exists())
        self.assertEqual(before, self.snapshot([config.parent, data, state]))

    def test_two_checkouts_are_not_merged_or_removed(self):
        shutil.copytree(self.old, self.new)
        self.cli("migrate", ok=False, script=self.old / "scenula.py")
        self.assertTrue(self.old.is_dir() and self.new.is_dir())
        self.assertEqual(self.shell.read_bytes(), self.original)

    def test_invalid_shell_json_is_untouched(self):
        self.shell.write_text("{broken")
        self.cli("migrate", ok=False)
        self.assertEqual(self.shell.read_text(), "{broken")
        self.assertTrue(self.old.is_dir())

    def test_invalid_shell_shapes_fail_without_mutation(self):
        for invalid in (None, [], {"version": 1, "bar": []},
                        {"version": 1, "bar": {"layout": {"right": None}}}):
            write_json(self.shell, invalid)
            before = self.shell.read_bytes()
            self.cli("migrate", ok=False)
            self.assertEqual(self.shell.read_bytes(), before)
            self.assertTrue(self.old.is_dir())

    def test_shell_config_symlink_is_preserved(self):
        target = self.home / "dotfiles/shell.json"
        target.parent.mkdir()
        self.shell.rename(target)
        self.shell.symlink_to(target)
        self.cli("migrate")
        self.assertTrue(self.shell.is_symlink())
        self.assertEqual(json.loads(target.read_text())["plugins"][0]["id"], "goldbarth.scenula")

    def test_duplicate_bar_placements_are_rejected(self):
        self.config["bar"]["layout"]["left"].append({"id": "goldbarth.scenula", "other": True})
        write_json(self.shell, self.config)
        self.cli("migrate", ok=False)
        self.assertEqual(json.loads(self.shell.read_text()), self.config)
        self.assertTrue(self.old.is_dir())

    def test_disabled_and_unplaced_plugin_stays_disabled_and_unplaced(self):
        self.config["bar"]["layout"]["right"] = []
        self.config["plugins"] = []
        self.config["disabledPlugins"] = ["goldbarth.tessella", "unrelated"]
        write_json(self.shell, self.config)
        self.cli("migrate")
        updated = json.loads(self.shell.read_text())
        self.assertEqual(updated["disabledPlugins"], ["goldbarth.scenula", "unrelated"])
        self.assertEqual(updated["bar"]["layout"]["right"], [])
        self.assertEqual(updated["plugins"], [])

    def test_interrupted_checkout_rename_is_resumable(self):
        self.populate()
        self.old.rename(self.new)
        self.cli("migrate")
        self.assertEqual(json.loads(self.shell.read_text())["plugins"][0]["id"], "goldbarth.scenula")

    def test_broken_directory_symlink_is_rejected(self):
        (self.home / ".local/share").mkdir(parents=True)
        (self.home / ".local/share/scenula").symlink_to("missing")
        self.cli("init", ok=False)
        self.assertFalse((self.home / ".config/omarchy/scenula").exists())

    def test_fresh_start_creates_only_scenula_data(self):
        self.old.rename(self.new)
        self.cli("init")
        self.assertFalse((self.home / ".local/share/tessella").exists())
        config = self.home / ".config/omarchy/scenula/config.json"
        self.assertTrue(config.is_file())
        status = json.loads(self.cli("status").stdout)
        self.assertEqual(status["images"], 0)
        self.assertEqual(config.read_text(), "")

    def test_old_cli_entry_point_and_render_lock_are_shared(self):
        _, _, state = self.populate()
        self.cli("migrate")
        self.assertEqual(json.loads(self.cli("status", script=self.new / "tessella.py").stdout)["images"], 4)
        with open(state / "render.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.cli("next", ok=False)
            self.assertEqual(result.returncode, 3)

    def test_parallel_initializations_share_one_store(self):
        self.populate()
        processes = [subprocess.Popen([sys.executable, "-I", str(self.old / "scenula.py"), "init"],
                                      env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for _ in range(5)]
        for process in processes:
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err)

    def test_cancellation_recognizes_both_backend_names(self):
        spec = importlib.util.spec_from_file_location("scenula_backend", ROOT / "scenula.py")
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        backend.SYNC_PATH = self.home / "sync.json"
        for name in ("scenula.py", "tessella.py"):
            script = self.home / name
            script.write_text("import time; time.sleep(60)")
            process = subprocess.Popen([sys.executable, str(script), "sync"])
            try:
                write_json(backend.SYNC_PATH, {"pid": process.pid, "running": True})
                self.assertEqual(backend.running_sync_pid(), process.pid)
                backend.cmd_cancel()
                self.assertEqual(process.wait(timeout=5), -15)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_known_upstream_remote_updates_and_fork_stays(self):
        subprocess.run(["git", "init", "-q", str(self.old)], check=True)
        subprocess.run(["git", "-C", str(self.old), "remote", "add", "origin",
                        "git@github.com:goldbarth/omarchy-tessella.git"], check=True)
        self.cli("migrate")
        remote = subprocess.check_output(["git", "-C", str(self.new), "remote", "get-url", "origin"], text=True)
        self.assertEqual(remote.strip(), "git@github.com:goldbarth/omarchy-scenula.git")
        subprocess.run(["git", "-C", str(self.new), "remote", "set-url", "origin",
                        "https://example.invalid/my-fork"], check=True)
        self.cli("migrate")
        remote = subprocess.check_output(["git", "-C", str(self.new), "remote", "get-url", "origin"], text=True)
        self.assertEqual(remote.strip(), "https://example.invalid/my-fork")


if __name__ == "__main__":
    unittest.main()
