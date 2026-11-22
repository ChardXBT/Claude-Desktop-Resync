import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import resync


A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SPACE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "Claude"
        self.code = self.root / resync.MAIN / A / ORG
        self.cow = self.root / resync.COWORK / A / ORG
        self.target = self.root / resync.MAIN / B / ORG
        self.code.mkdir(parents=True)
        self.cow.mkdir(parents=True)
        self.write(self.code / "local_one.json", {"sessionId": "local_one", "title": "source"})
        self.write(self.cow / "local_two.json", {"sessionId": "local_two"})
        self.write(self.cow / "local_two" / "output.json", {"result": "kept"})
        self.space = {"id": SPACE, "name": "Synthetic", "folders": [], "projects": [],
                      "createdAt": 1, "updatedAt": 2}
        self.write(self.cow / resync.SPACES, {"spaces": [self.space]})
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(resync, "PASS_LOCK", self.base / "pass.lock").start()
        mock.patch.object(resync, "claude_running", return_value=False).start()

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def snapshot(self, path):
        return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}

    def test_prepares_only_target_history_and_projects_and_is_repeatable(self):
        for name in ("scheduled-tasks.json", "backlog/tasks.json", "bridge-state.json"):
            self.write(self.code / name, {"do_not_copy": True})
        self.write(self.cow / resync.RSS, {"entries": [{"sessionId": "remote"}]})
        before = self.snapshot(self.root)
        result = resync.prepare_switch(self.root, B, ORG)
        self.assertTrue(result["ok"])
        self.assertEqual(result["protocol"], 1)
        self.assertEqual((result["code"], result["cowork"], result["projects"]), (1, 1, 1))
        target_cow = self.root / resync.COWORK / B / ORG
        self.assertEqual(json.loads((self.target / "local_one.json").read_text()),
                         {"sessionId": "local_one", "title": "source"})
        self.assertEqual(json.loads((target_cow / "local_two" / "output.json").read_text()), {"result": "kept"})
        self.assertEqual(json.loads((target_cow / resync.SPACES).read_text())["spaces"], [self.space])
        for path, content in before.items():
            self.assertEqual((self.root / path).read_bytes(), content)
        for name in ("scheduled-tasks.json", "backlog/tasks.json", "bridge-state.json"):
            self.assertFalse((self.target / name).exists())
        self.assertFalse((target_cow / resync.RSS).exists())
        self.assertEqual(resync.prepare_switch(self.root, B, ORG)["changes"], {})

    def test_dry_run_writes_no_session_files(self):
        before = self.snapshot(self.root)
        result = resync.prepare_switch(self.root, B, ORG, dry=True)
        self.assertTrue(result["changes"])
        self.assertEqual(self.snapshot(self.root), before)
        self.assertFalse(self.target.exists())

    def test_preexisting_temporary_hardlink_cannot_overwrite_outside_file(self):
        self.target.mkdir(parents=True)
        outside = self.base / "outside.txt"
        outside.write_text("kept")
        os.link(outside, self.target / ".resync-local_one.json.tmp")
        resync.prepare_switch(self.root, B, ORG)
        self.assertEqual(outside.read_text(), "kept")

    def test_explicit_old_root_is_read_only_source_for_gateway_history(self):
        target_root = self.base / "Claude-3p"
        target_root.mkdir()
        self.write(target_root / "config.json", {})
        before = self.snapshot(self.root)
        result = resync.prepare_switch(target_root, B, ORG, source_roots=[self.root])
        self.assertTrue(result["ok"])
        self.assertEqual(self.snapshot(self.root), before)
        self.assertTrue((target_root / resync.MAIN / B / ORG / "local_one.json").is_file())
        self.assertTrue((target_root / resync.COWORK / B / ORG / "local_two/output.json").is_file())

    def test_replaced_target_files_are_backed_up_and_sources_stay_unchanged(self):
        self.write(self.target / "local_one.json", {"title": "old-target"})
        os.utime(self.target / "local_one.json", (1, 1))
        source = self.snapshot(self.code)
        resync.prepare_switch(self.root, B, ORG)
        backups = list((self.root / "session-switch-backups").rglob("local_one.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), {"title": "old-target"})
        self.assertEqual(self.snapshot(self.code), source)

    def test_automatic_prepare_never_propagates_deletions(self):
        self.write(self.target / "local_one.json", {"title": "target-history"})
        (self.code / "deleted_one").write_text("1")
        before = (self.target / "local_one.json").read_bytes()
        resync.prepare_switch(self.root, B, ORG)
        self.assertEqual((self.target / "local_one.json").read_bytes(), before)
        self.assertFalse((self.target / "deleted_one").exists())

    def test_malformed_target_projects_refuse_before_any_history_write(self):
        target_cow = self.root / resync.COWORK / B / ORG
        self.write(target_cow / resync.SPACES, {"spaces": "not-a-list"})
        before = self.snapshot(self.root)
        with self.assertRaises(RuntimeError):
            resync.prepare_switch(self.root, B, ORG)
        self.assertEqual(self.snapshot(self.root), before)

    def test_existing_target_projects_are_preserved(self):
        target_cow = self.root / resync.COWORK / B / ORG
        other = dict(self.space, id="cccccccc-cccc-cccc-cccc-cccccccccccc", name="Target")
        self.write(target_cow / resync.SPACES, {"spaces": [other], "keep": "yes"})
        resync.prepare_switch(self.root, B, ORG)
        result = json.loads((target_cow / resync.SPACES).read_text())
        self.assertEqual(result["keep"], "yes")
        self.assertEqual([x["id"] for x in result["spaces"]], [other["id"], SPACE])

    def test_open_or_unknown_desktop_is_refused(self):
        for running in (True, None):
            with self.subTest(running=running), mock.patch.object(resync, "claude_running", return_value=running):
                with self.assertRaises(RuntimeError):
                    resync.prepare_switch(self.root, B, ORG)
        self.assertFalse(self.target.exists())

    def test_invalid_identity_is_refused(self):
        for acct, org in (("../escape", ORG), (B, ""), (B, "../escape")):
            with self.subTest(acct=acct, org=org), self.assertRaises(RuntimeError):
                resync.prepare_switch(self.root, acct, org)

    def test_pass_lock_excludes_another_merge(self):
        with resync.merge_lock():
            with self.assertRaises(RuntimeError):
                resync.prepare_switch(self.root, B, ORG)
        self.assertFalse(self.target.exists())

    def test_junction_destination_is_refused_without_following_it(self):
        real = Path.lstat
        def junction(path):
            if path == self.root / resync.MAIN / B:
                return SimpleNamespace(st_mode=0o40755, st_reparse_tag=0xA0000003)
            return real(path)
        with mock.patch.object(Path, "lstat", junction), self.assertRaises(RuntimeError):
            resync.prepare_switch(self.root, B, ORG)
        self.assertFalse(self.target.exists())

    def test_a_new_installation_with_no_local_chats_needs_no_repair(self):
        empty = self.base / "new-Claude"
        self.write(empty / "config.json", {"locale": "en-US"})
        result = resync.prepare_switch(empty, B, ORG)
        self.assertEqual((result["code"], result["cowork"], result["projects"]), (0, 0, 0))
        self.assertEqual(result["changes"], {})

    def test_target_link_is_refused(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.root / resync.MAIN / B
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks require OS permission")
        with self.assertRaises(RuntimeError):
            resync.prepare_switch(self.root, B, ORG)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
