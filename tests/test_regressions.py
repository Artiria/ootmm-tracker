"""Public regressions: no ROMs, emulator, downloads or existing tracker state needed.

Run: python -B -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "forge"))
with tempfile.TemporaryDirectory(prefix="tracker-test-import-") as import_home:
    with patch.dict(os.environ, OOTMM_TRACKER_HOME=import_home, OOTMM_TRACKER_DATA=str(ROOT / "data")):
        import dataupdate
        import discover
        import forge
        import mmflink
        import ootmm
        import overlay
        import paths
        import placement
        import provenance


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tracker-regression-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, OOTMM_TRACKER_HOME=str(self.home),
                              OOTMM_TRACKER_DATA=str(ROOT / "data"))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.user = patch.object(paths, "USER_DIR", str(self.home))
        self.user.start()
        self.addCleanup(self.user.stop)

    def tracker(self, rom="seed.z64", **kwargs):
        return overlay.Tracker(None, dict(checks=[], rom=str(rom), **kwargs))

    def seed_cache(self):
        rom = self.home / "seed.z64"
        rom.write_bytes(b"seed one")
        table = dict(rom=str(rom), rom_sha256=provenance.file_digest(rom),
                     generator_revision=provenance.CHECKS_REVISION,
                     data_sha256=provenance.data_digest(paths.data_dir()), version=paths.data_version(),
                     payload={"oot": {"triforce": None, "last_scene": None}}, souls=None,
                     scene_layers=None, shared_scene_rooms=None, mm_merged_scenes=None, mq=None, checks=[])
        (self.home / "checks.json").write_text(json.dumps(table))
        (self.home / "icons.json").write_text(json.dumps({"rom": str(rom), "rom_sha256": table["rom_sha256"]}))
        (self.home / "icons.png").write_bytes(b"fixture")
        return rom

    def test_unchanged_cache_is_reused(self):
        rom = self.seed_cache()
        with patch.object(discover, "_generate") as generate:
            self.assertEqual(discover.ensure_tables(str(rom), None), [])
        generate.assert_not_called()

    def test_adoption_and_revert_invalidate_only_checks(self):
        rom = self.seed_cache()
        os.environ.pop("OOTMM_TRACKER_DATA")
        adopted = Path(paths.updates_dir("v99.9"))
        adopted.mkdir(parents=True)
        (adopted / "pool_oot.csv").write_text("different labels")
        dataupdate.set_active("v99.9")
        with patch.object(discover, "_generate", return_value=0) as generate:
            self.assertEqual(discover.ensure_tables(str(rom), None), ["checks.json"])
            self.assertEqual(generate.call_args.args[0], "mkchecks")
        table_path = self.home / "checks.json"
        table = json.loads(table_path.read_text())
        table.update(version="v99.9", data_sha256=provenance.data_digest(adopted))
        table_path.write_text(json.dumps(table))
        dataupdate.set_active(None)
        with patch.object(discover, "_generate", return_value=0):
            self.assertEqual(discover.ensure_tables(str(rom), None), ["checks.json"])

    def test_same_path_same_size_same_mtime_rom_is_invalidated(self):
        rom = self.seed_cache()
        stat = rom.stat()
        rom.write_bytes(b"seed two")
        os.utime(rom, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with patch.object(discover, "_generate", return_value=0):
            self.assertEqual(discover.ensure_tables(str(rom), None), ["checks.json", "icons.png"])

    def test_data_fingerprint_is_independent_of_install_location(self):
        a, b = self.home / "a", self.home / "b"
        a.mkdir(); b.mkdir()
        for root in (a, b):
            (root / "pool_oot.csv").write_text("same")
        self.assertEqual(provenance.data_digest(a), provenance.data_digest(b))
        (b / "pool_oot.csv").write_text("changed")
        self.assertNotEqual(provenance.data_digest(a), provenance.data_digest(b))

    def test_update_discovers_emulator_or_reports_missing_rom(self):
        args = SimpleNamespace(what="update", rom=None)
        with patch.object(discover, "find_emulator", return_value=None):
            with self.assertRaisesRegex(SystemExit, "pass --rom"):
                ootmm.cmd_data(args)
        with patch.object(discover, "find_emulator", return_value="an emulator"), \
             patch.object(discover, "find_rom", return_value=None) as find:
            with self.assertRaises(SystemExit):
                ootmm.cmd_data(args)
            find.assert_called_once_with("an emulator", verbose=False)

    def test_frozen_measurement_dispatches_worker_and_cleans_failed_home(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            with patch.object(sys, "argv", command), patch("mkchecks.main", return_value=0) as generate:
                with self.assertRaises(SystemExit) as exit:
                    ootmm.main()
                self.assertEqual(exit.exception.code, 0)
                generate.assert_called_once_with(["--rom", "sample.z64"])
            return SimpleNamespace(returncode=1, stdout="", stderr="controlled generation failure")
        with patch.object(paths, "FROZEN", True), patch.object(sys, "executable", "ootmm-tracker.exe"), \
             patch.object(dataupdate.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "could not build"):
                dataupdate.build_with(None, "sample.z64", say=lambda _: None)
        command, kw = calls[0]
        self.assertEqual(command[:2], ["ootmm-tracker.exe", "--generate-checks"])
        self.assertNotEqual(kw["env"]["OOTMM_TRACKER_HOME"], str(self.home))
        self.assertFalse(Path(kw["env"]["OOTMM_TRACKER_HOME"]).exists())

    def test_all_visited_entrances_remain_in_state(self):
        entries = [dict(game="oot", src=i, dst_game="oot", dst=100+i,
                        from_area=str(i), to_area=str(i+100)) for i in range(41)]
        tracker = self.tracker(entrances=entries)
        tracker._entrances_found = {("oot", i): {"t": i, "sure": True} for i in range(41)}
        tracker.poll_once = lambda: ("oot", set(), 1.0, {}, None, None, False, None)
        tracker.refresh()
        state = json.loads(tracker.snapshot())
        self.assertEqual(len(state["entrances"]["found"]), 41)
        self.assertEqual(state["entrances"]["total"], 41)

    def test_distinct_roms_with_same_basename_keep_notes_separate(self):
        a, b = self.home / "a", self.home / "b"
        a.mkdir(); b.mkdir()
        (a / "seed.z64").write_bytes(b"seed A")
        (b / "seed.z64").write_bytes(b"seed B")
        first = self.tracker(a / "seed.z64")
        first.add_note("Only A")
        self.assertEqual(self.tracker(b / "seed.z64").notes, [])
        (b / "renamed.z64").write_bytes(b"seed A")
        self.assertEqual(self.tracker(b / "renamed.z64").notes[0]["text"], "Only A")

    def test_legacy_notes_need_explicit_assignment_and_survive_failed_import(self):
        old = {"seed.z64": [{"id": "old", "text": "legacy", "t": 1}]}
        file = self.home / "notes.json"
        file.write_text(json.dumps(old))
        tracker = self.tracker()
        self.assertEqual(tracker.notes, [])
        self.assertEqual(json.loads(tracker.snapshot())["notes_legacy_n"], 1)
        with patch.object(overlay.os, "replace", side_effect=PermissionError("test")):
            with self.assertRaises(OSError):
                tracker.import_legacy_notes()
        self.assertEqual(json.loads(file.read_text()), old)
        tracker.import_legacy_notes()
        self.assertEqual(tracker.notes[0]["text"], "legacy")
        self.assertNotIn("seed.z64", json.loads(file.read_text()))
        self.assertEqual(self.tracker(self.home / "other" / "seed.z64").notes, [])

    def test_note_add_and_delete_are_transactional_on_write_failure(self):
        tracker = self.tracker()
        note = tracker.add_note("keep me")
        with patch.object(overlay.os, "replace", side_effect=PermissionError("test")):
            with self.assertRaises(OSError):
                tracker.add_note("cannot save")
            with self.assertRaises(OSError):
                tracker.delete_note(note["id"])
        self.assertEqual(tracker.notes, [note])
        self.assertEqual(self.tracker().notes, [note])
        self.assertFalse(list(self.home.glob("notes-*.tmp")))

    @unittest.skipUnless(sys.platform == "win32", "Windows named memory protocol")
    def test_mmf_excludes_second_owner_and_releases_channel(self):
        name = "OoTMMRegression-" + uuid.uuid4().hex
        first = mmflink.MmfLink(name)
        try:
            first.req[0:4] = struct.pack("<I", 17)
            with self.assertRaises(mmflink.MmfBusy):
                mmflink.MmfLink(name)
            self.assertEqual(struct.unpack_from("<I", first.req, 0)[0], 17)
            code = "import mmflink,sys; mmflink.MmfLink(sys.argv[1])"
            run = subprocess.run([sys.executable, "-B", "-c", code, name], cwd=ROOT,
                                 capture_output=True, text=True, timeout=10)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("MmfBusy", run.stderr)
        finally:
            first.close()
        second = mmflink.MmfLink(name)
        second.close()
        second.close()

    def test_reset_during_handshake_allows_next_peer(self):
        class Peer:
            def setsockopt(self, *args): pass
            def settimeout(self, *args): pass
            def close(self): self.closed = True
        broken, good = Peer(), Peer()
        listener = SimpleNamespace(listen=lambda _: None, settimeout=lambda _: None,
                                   accept=unittest.mock.Mock(side_effect=[(broken, None), (good, None)]),
                                   close=unittest.mock.Mock())
        link = object()
        with patch.object(ootmm, "listen_for_script", return_value=listener), \
             patch.object(ootmm, "p64_handshake", side_effect=[ConnectionResetError("reset"), link]):
            self.assertIs(ootmm.wait_for_emulator("127.0.0.1", 0, "test", "p64em"), link)
        self.assertTrue(broken.closed)

    def test_terminal_listener_failure_reaches_caller(self):
        with patch.object(ootmm, "listen_for_script", return_value=None):
            with self.assertRaisesRegex(ConnectionError, "cannot listen"):
                ootmm.wait_for_emulator("127.0.0.1", 0, "test", "p64em")

    def test_handshake_deadline_does_not_apply_to_established_reads(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        errors = []
        def receive(sock, size):
            data = b""
            while len(data) < size:
                part = sock.recv(size - len(data))
                if not part:
                    raise ConnectionError("peer closed")
                data += part
            return data
        def peer():
            try:
                with socket.create_connection(listener.getsockname(), timeout=10) as sock:
                    self.assertEqual(receive(sock, 1), bytes([ootmm.OP_PING]))
                    sock.sendall(struct.pack(">I", ootmm.MAGIC))
                    receive(sock, 5)
                    sock.sendall(struct.pack(">I", 0x800000))
                    for addr, value in ((0x80000010, 0x11111111), (0x80000020, 0x22222222)):
                        self.assertEqual(receive(sock, 5), struct.pack(">BI", 4, addr))
                        sock.sendall(struct.pack(">I", value))
            except BaseException as ex:
                errors.append(ex)
        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        with patch.object(ootmm, "listen_for_script", return_value=listener):
            link = ootmm.wait_for_emulator("127.0.0.1", 0, "test", "p64em")
        try:
            self.assertIsNone(link.sock.gettimeout())
            self.assertEqual(link.read(0x80000010, 4), 0x11111111)
            self.assertEqual(link.read(0x80000020, 4), 0x22222222)
        finally:
            link.sock.close()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_fallback_unified_table_keeps_both_games(self):
        def record(key, value):
            return struct.pack(">IhHh6x", key, 1, value, 0)
        keys = [(1 << 24) | i for i in range(64)]
        blob = b"".join(record(k, i+1) for i, k in enumerate(keys))
        blob += b"".join(record(k | placement.KEY_MM, i+65) for i, k in enumerate(keys))
        blob += record(placement.KEY_END, 0)
        with patch.object(placement.rom, "extra_entries", return_value=[]), \
             patch.object(placement.rom, "read_extra_vrom", side_effect=[blob, KeyError()]):
            actual = placement.read_tables(b"fixture")
        self.assertEqual(len(actual), 128)
        for i, k in enumerate(keys):
            self.assertEqual(actual["oot", k], (i+1, 1))
            self.assertEqual(actual["mm", k], (i+65, 1))

    def test_forge_make_propagates_guard_failure(self):
        args = SimpleNamespace(refs=["fixture"], image=None, jobs=1, guard=True)
        with patch.object(forge, "OUT", self.home), patch.object(forge, "roms_ready", return_value=True), \
             patch.object(forge.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
             patch.object(forge, "resolve_ref", return_value=("fixture",)*3), \
             patch.object(forge, "run_ref", return_value=("fixture", 0, 0)), \
             patch.object(forge, "cmd_guard", return_value=1) as guard:
            self.assertEqual(forge.cmd_make(args), 1)
            guard.assert_called_once()


if __name__ == "__main__":
    unittest.main()
