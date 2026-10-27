"""The direct link: RDRAM read out of another process, with no script in the emulator.

A child Python process stands in for the emulator. It VirtualAllocs an 8 MiB block,
writes the N64 boot globals and a few known bytes into it the way Project64 stores them
(each 32-bit word reversed) or byte for byte, and waits until its stdin closes. No ROM,
emulator or network. Windows only, like the link itself.

Run: python -B -m unittest discover -s tests -v
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
with tempfile.TemporaryDirectory(prefix="tracker-test-import-") as import_home:
    with patch.dict(os.environ, OOTMM_TRACKER_HOME=import_home, OOTMM_TRACKER_DATA=str(ROOT / "data")):
        import ootmm
        import proclink

PATTERN = b"OoTMM-direct-link-test-pattern!!"

# The stand-in emulator. argv[1] is the word order of the storage: "<" reverses every
# 4-byte word as Project64 does, ">" keeps the bytes as the N64 would see them.
CHILD = r'''
import array, ctypes, struct, sys
from ctypes import wintypes
order = sys.argv[1]
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.VirtualAlloc.restype = ctypes.c_void_p
k.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
SIZE = 0x800000
base = k.VirtualAlloc(None, SIZE, 0x3000, 0x04)      # MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
mem = (ctypes.c_char * SIZE).from_address(base)
def put(off, data):
    if order == "<":
        w = array.array("I", data)
        w.byteswap()
        data = w.tobytes()
    mem[off:off + len(data)] = data
put(0x300, struct.pack(">7I", 1, 0, 0xB0000000, 0, 0x17D9, 2, 0x800000))
put(0x1000, b"OoTMM-direct-link-test-pattern!!")
put(0x2000, struct.pack(">IHBB", 0x11223344, 0x5566, 0x77, 0))
sys.stdout.write("READY %d" % base + chr(10))
sys.stdout.flush()
sys.stdin.readline()
'''

# A process with nothing that looks like RDRAM in it.
EMPTY_CHILD = "import sys; sys.stdout.write('READY 0' + chr(10)); sys.stdout.flush(); sys.stdin.readline()"


def start_child(code, *argv):
    p = subprocess.Popen([sys.executable, "-B", "-c", code, *argv],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline()
    if not line.startswith("READY "):
        p.kill()
        raise RuntimeError(f"the stand-in did not start: {line!r}")
    return p, int(line.split()[1])


def stop_child(p):
    if p.poll() is None:
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=5)
    for pipe in (p.stdin, p.stdout):
        if pipe is not None and not pipe.closed:
            pipe.close()


@unittest.skipUnless(os.name == "nt", "ReadProcessMemory is Windows only")
class DirectLinkTests(unittest.TestCase):
    def setUp(self):
        self.children = []

    def tearDown(self):
        for p in self.children:
            stop_child(p)

    def child(self, order):
        p, base = start_child(CHILD, order)
        self.children.append(p)
        return p, base

    def check_reads(self, link):
        self.assertEqual(link.read(0x80000318, 4), 0x00800000)
        self.assertEqual(link.read(0x80000308, 4), 0xB0000000)
        self.assertEqual(link.read_block(0x80001000, 32), PATTERN)
        self.assertEqual(link.read_block(0x80001003, 7), PATTERN[3:10])      # unaligned, odd length
        self.assertEqual(link.read(0x80002000, 4), 0x11223344)
        self.assertEqual(link.read(0x80002004, 2), 0x5566)
        self.assertEqual(link.read(0x80002006, 1), 0x77)
        self.assertEqual(link.read_block(0xA0001000, 4), PATTERN[:4])       # KSEG1 alias

    def test_finds_rdram_and_undoes_the_word_swap(self):
        p, base = self.child("<")
        link = proclink.ProcLink.attach(p.pid, sys.executable)
        try:
            self.assertEqual(link.base, base)
            self.assertEqual(link.order, "<")
            self.check_reads(link)
            self.assertTrue(link.alive())
            self.assertEqual(link.stats()["word_order"], "little")
            self.assertGreater(link.stats()["reads"], 0)
            with self.assertRaises(OSError):
                link.read_block(0x807FFFFC, 8)       # runs past the end of RDRAM
            with self.assertRaises(OSError):
                link.read_block(0x7FFFFFFC, 4)       # below it
        finally:
            link.close()

    def test_byte_exact_storage_is_read_as_is(self):
        p, base = self.child(">")
        link = proclink.ProcLink.attach(p.pid, sys.executable)
        try:
            self.assertEqual((link.base, link.order), (base, ">"))
            self.check_reads(link)
        finally:
            link.close()

    def test_a_process_that_exits_takes_its_link_down(self):
        p, _ = self.child("<")
        link = proclink.ProcLink.attach(p.pid, sys.executable)
        try:
            self.assertEqual(link.read_block(0x80001000, 4), PATTERN[:4])
            stop_child(p)
            self.assertFalse(link.alive())
            with self.assertRaises(ConnectionError):
                link.read_block(0x80001000, 4)
        finally:
            link.close()

    def test_a_process_without_rdram_is_reported_not_guessed(self):
        p, _ = start_child(EMPTY_CHILD)
        self.children.append(p)
        with self.assertRaises(proclink.ProcDown):
            proclink.ProcLink.attach(p.pid, sys.executable)

    def test_the_default_wait_adopts_the_process_with_no_script_at_all(self):
        """What the exe does with no flags: `auto` has to find the emulator's memory
        by itself, while the socket and shared-memory listeners sit idle."""
        import mmflink
        import socket
        self.assertTrue(ootmm.DIRECT_IN_AUTO)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        procs = []
        with patch.object(proclink, "emulator_processes", lambda: list(procs)), \
                patch.object(mmflink, "DEFAULT_NAME", "OoTMMTrackerTest-" + str(os.getpid())):
            p, _ = self.child("<")
            procs.append((p.pid, sys.executable))
            link = ootmm.wait_for_emulator("127.0.0.1", port, "test", "auto")
            self.assertIsInstance(link, proclink.ProcLink)
            self.assertEqual(link.read_block(0x80001000, 32), PATTERN)
            link.close()

    def test_wait_for_emulator_direct_adopts_the_process_and_relinks_after_a_reload(self):
        procs = []
        with patch.object(proclink, "emulator_processes", lambda: list(procs)):
            p1, _ = self.child("<")
            procs.append((p1.pid, sys.executable))
            links = []
            link = ootmm.wait_for_emulator("127.0.0.1", 0, "test", "direct", on_link=links.append)
            self.assertIs(link, links[0])
            self.assertEqual(link.read_block(0x80001000, 32), PATTERN)
            # the emulator reloads: that block is gone and another appears
            stop_child(p1)
            procs.clear()
            p2, _ = self.child(">")
            procs.append((p2.pid, sys.executable))
            deadline = time.time() + 15
            while len(links) < 2 and time.time() < deadline:
                time.sleep(0.1)
            self.assertEqual(len(links), 2, "the watcher did not hand over a new link")
            self.assertEqual(links[1].order, ">")
            self.assertEqual(links[1].read_block(0x80001000, 32), PATTERN)
            stop_child(p2)


if __name__ == "__main__":
    unittest.main()
