"""A seed opened straight from its .zip: the ROM inside is what gets tracked.

Project64 opens archives and writes the archive's path to `Recent Rom 0`;
on 22 oct 2026 that took auto-detection down and left the previous seed's
tables on the page. No ROMs, emulator or downloads needed here.

Run: python -B -m unittest discover -s tests -v
"""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
with tempfile.TemporaryDirectory(prefix="tracker-test-import-") as import_home:
    with patch.dict(os.environ, OOTMM_TRACKER_HOME=import_home, OOTMM_TRACKER_DATA=str(ROOT / "data")):
        import discover
        import overlay
        import paths

SEED = "abcd1234"
ROM_NAME = f"OoTMM-{SEED}-R29.z64"
ROM_BYTES = bytes(range(256)) * 16          # 4 KB, a multiple of four, like a ROM


def swapped_md5(raw):
    sw = bytearray(raw)
    sw[0::4], sw[1::4], sw[2::4], sw[3::4] = raw[3::4], raw[2::4], raw[1::4], raw[0::4]
    return hashlib.md5(bytes(sw)).hexdigest().upper()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tracker-archive-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.downloads = Path(self.temp.name) / "Downloads"
        self.downloads.mkdir()
        for p in (patch.dict(os.environ, OOTMM_TRACKER_HOME=str(self.home),
                             OOTMM_TRACKER_DATA=str(ROOT / "data")),
                  patch.object(paths, "USER_DIR", str(self.home)),
                  patch.object(discover, "CACHE", str(self.home / "discover-cache.json"))):
            p.start()
            self.addCleanup(p.stop)

    def make_zip(self, name=f"OoTMM-{SEED} (2).zip", rom=ROM_NAME, extra=()):
        path = self.downloads / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            if rom:
                z.writestr(rom, ROM_BYTES)
            z.writestr(f"OoTMM-{SEED}-R29-comprobaciones.json", "{}")
            for n, data in extra:
                z.writestr(n, data)
        return path

    def make_emu(self, open_path, save_hash=None):
        emu = Path(self.temp.name) / "emu"
        (emu / "Config").mkdir(parents=True)
        (emu / "Config" / "Project64.cfg").write_text(
            f"[Recent Files]\nRecent Rom 0={open_path}\n", encoding="utf-8")
        if save_hash:
            (emu / "Save" / f"OOT+MM COMBO-{save_hash}").mkdir(parents=True)
        return str(emu)

    def test_a_loose_rom_is_still_itself(self):
        rom = self.downloads / f"OoTMM-{SEED}.z64"
        rom.write_bytes(ROM_BYTES)
        emu = self.make_emu(rom, swapped_md5(ROM_BYTES))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(discover.find_rom(emu), str(rom))
        self.assertIsNone(discover.archive_origin(str(rom)))

    def test_zip_the_emulator_opened_yields_the_rom_inside(self):
        zpath = self.make_zip()
        emu = self.make_emu(zpath, swapped_md5(ROM_BYTES))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rom = discover.find_rom(emu)
        self.assertIsNotNone(rom)
        self.assertEqual(os.path.basename(rom), ROM_NAME)
        self.assertEqual(Path(rom).parents[1], self.home / discover.ARCHIVES_DIRNAME)
        self.assertEqual(Path(rom).read_bytes(), ROM_BYTES)
        # the save folder was matched through the zip, by the ROM inside it
        self.assertIn("its save agrees", out.getvalue())
        self.assertIn("tracking the ROM inside it", out.getvalue())
        self.assertEqual(discover.archive_origin(rom), str(zpath))
        # the second start finds it there and does not extract again
        before = os.stat(rom)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(discover.find_rom(emu), rom)
        after = os.stat(rom)
        self.assertEqual((before.st_mtime_ns, before.st_ino), (after.st_mtime_ns, after.st_ino))

    def test_spoiler_beside_the_archive_is_found(self):
        zpath = self.make_zip()
        folder = self.downloads / f"OoTMM-{SEED}"
        folder.mkdir()
        spoiler = folder / f"OoTMM-Spoiler-{SEED}.txt"
        spoiler.write_text("Seed: x\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            rom = discover.rom_file(str(zpath))
            self.assertEqual(discover.find_spoiler(rom), str(spoiler))

    def test_spoiler_packed_in_the_zip_comes_out_with_the_rom(self):
        zpath = self.make_zip(name=f"OoTMM-{SEED}.zip", rom=f"OoTMM-{SEED}.z64",
                              extra=[(f"OoTMM-Spoiler-{SEED}.txt", "Seed: y\n")])
        with contextlib.redirect_stdout(io.StringIO()):
            rom = discover.rom_file(str(zpath))
            found = discover.find_spoiler(rom)
        self.assertEqual(os.path.dirname(found), os.path.dirname(rom))
        self.assertEqual(Path(found).read_text(encoding="utf-8"), "Seed: y\n")

    def test_a_zip_without_a_rom_is_refused_and_nothing_crashes(self):
        zpath = self.make_zip(rom=None)
        emu = self.make_emu(zpath, "0" * 32)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(discover.find_rom(emu))
        self.assertIn("holds no ROM", out.getvalue())
        self.assertIsNone(discover.rom_hash_cached(str(zpath)))
        # not a multiple of four: not a ROM, whatever it is called
        odd = self.downloads / "odd.z64"
        odd.write_bytes(b"abc")
        self.assertIsNone(discover.rom_hash_cached(str(odd)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(discover.rom_file(str(self.downloads / "seed.7z")))
        self.assertIn("seed.7z", out.getvalue())
        self.assertFalse((self.home / discover.ARCHIVES_DIRNAME).exists())

    def test_overlay_compares_the_rom_inside_the_open_archive(self):
        zpath = self.make_zip()
        with contextlib.redirect_stdout(io.StringIO()):
            rom = discover.rom_file(str(zpath))
        tracker = overlay.Tracker(None, dict(checks=[], rom=rom))
        tracker._rom_check_at = 0
        with patch.object(discover, "find_emulator", return_value="emu"), \
                patch.object(discover, "recent_roms", return_value=[str(zpath)]):
            tracker.check_rom_open()
        self.assertFalse(tracker.state["rom_mismatch"])
        self.assertEqual(tracker.state["rom_open"], rom)
        other = self.make_zip(name="OoTMM-zzzz9999 (1).zip", rom="OoTMM-zzzz9999.z64")
        tracker._rom_check_at = 0
        out = io.StringIO()
        with patch.object(discover, "find_emulator", return_value="emu"), \
                patch.object(discover, "recent_roms", return_value=[str(other)]), \
                contextlib.redirect_stdout(out):
            tracker.check_rom_open()
        self.assertTrue(tracker.state["rom_mismatch"])
        # named without extracting: that is the follower's job
        self.assertEqual(os.path.basename(tracker.state["rom_open"]), "OoTMM-zzzz9999.z64")
        self.assertFalse(os.path.exists(tracker.state["rom_open"]))
        self.assertIn(str(other), out.getvalue())

    def test_only_the_newest_extracted_seeds_are_kept(self):
        foreign = self.home / discover.ARCHIVES_DIRNAME / "not-ours"
        foreign.mkdir(parents=True)
        (foreign / "keep.txt").write_text("x", encoding="utf-8")
        roms = []
        for i in range(discover.ARCHIVES_KEEP + 2):
            z = self.make_zip(name=f"OoTMM-seed{i}.zip", rom=f"OoTMM-seed{i}.z64")
            with contextlib.redirect_stdout(io.StringIO()):
                rom = discover.rom_file(str(z))
            roms.append(rom)
            # distinct ages, whatever the clock did in between
            os.utime(os.path.dirname(rom), ns=((i + 1) * 10**9,) * 2)
        kept = [r for r in roms if os.path.isfile(r)]
        self.assertEqual(kept, roms[-discover.ARCHIVES_KEEP:])
        self.assertTrue((foreign / "keep.txt").is_file())
