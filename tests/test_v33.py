"""v33 format regressions, using synthetic ROM records and save buffers."""
import os
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_regressions as baseline
import inventory
import mkchecks
import overlay
import paths
import placement
import payload
import xflags


def dictionary():
    # IDs deliberately differ from XML order and actor numbers.
    records = {
        xflags.match_key("oot", dict(scene=0x51, actor=3)): 25,
        xflags.match_key("oot", dict(scene=0x51, setup=1, actor=4)): 25,
        xflags.SIBLING | (1 << 16) | 25: 33,
        xflags.match_key("mm", dict(scene=7, room=2, actor=6)): 4,
    }
    return b"".join(struct.pack(">IHH", key, ident, 0) for key, ident in sorted(records.items()))


class IndexedCheckTests(unittest.TestCase):
    setUp = baseline.RegressionTests.setUp

    def test_actor_aliases_siblings_and_games_use_rom_ids(self):
        index = xflags.Index(dictionary())
        self.assertEqual(index.lookup("oot", dict(scene=0x51, actor=3)), 25)
        self.assertEqual(index.lookup("oot", dict(scene=0x51, setup=1, actor=4, slice=1)), 33)
        self.assertEqual(index.lookup("mm", dict(scene=7, room=2, actor=6)), 4)
        self.assertIsNone(index.lookup("mm", dict(scene=0x51, actor=3)))
        self.assertEqual(len(index.identities("oot", 33)), 2)
        self.assertFalse(index.identities("mm", 33))

    def test_bad_dictionary_cannot_be_mistaken_for_v33(self):
        blob = dictionary()
        for bad in (blob[:-1], blob[:8] * 2, blob[:6] + b"xx" + blob[8:],
                    struct.pack(">IHH", xflags.SIBLING | 0x10000 | 99, 3, 0),
                    struct.pack(">IHHIHH", 1, 3, 0, xflags.SIBLING | 0x10000 | 3, 3, 0),
                    struct.pack(">IHH", 6, 0xffff, 0)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    xflags.Index(bad)
        with patch.object(xflags.rom, "read_extra_vrom", return_value=bytes.fromhex("05010001000100010000000000000000") * 70):
            self.assertIsNone(xflags.read(b"legacy placement"))

    def test_nested_xml_resolves_through_dictionary_not_file_order(self):
        root = self.home / "checks"
        root.mkdir()
        (root / "oot.xml").write_text('''<checks game="oot"><scene id="FIELD" room="0">
          <actor><match actor="3"/><match setup="1" actor="4"/>
            <xflag type="fairy" location="Fairy" item="FAIRY"/>
            <xflag type="fairy" slice="1" location="Big Fairy" item="FAIRY_BIG"/>
          </actor><chest location="Chest" flag="1" item="RUPEE"/>
        </scene></checks>''')
        scenes = {"OOT_FIELD": 0x51}
        with patch.object(mkchecks, "DATA", self.home), patch.object(mkchecks, "XFLAG_INDEX", xflags.Index(dictionary())):
            rows = list(mkchecks.load_rows("oot"))
            index, skipped = mkchecks.name_index(scenes, {})
        self.assertEqual(len(rows), 3)
        self.assertEqual(skipped, 0)
        self.assertEqual(index["oot", 0x10000019][0]["name"], "Fairy")
        self.assertEqual(index["oot", 0x10000021][0]["name"], "Big Fairy")

    def test_same_id_alias_disagreement_refuses_label(self):
        index = xflags.Index(dictionary())
        row = dict(type="fairy", scene="FIELD", matches=[dict(scene="FIELD", actor=3), dict(scene="FIELD", actor=5)])
        index.matches[xflags.match_key("oot", dict(scene=0x51, actor=5))] = 26
        with patch.object(mkchecks, "XFLAG_INDEX", index):
            self.assertIsNone(mkchecks.row_key("oot", row, {"OOT_FIELD": 0x51}, 0x51, 3, dict(actor=3)))

    def test_shared_bits_cows_and_stray_fairies_follow_new_save(self):
        with patch.object(mkchecks, "XFLAG_INDEX", xflags.Index(dictionary())), \
             patch.object(mkchecks, "LAYOUT_FROM_ROM", True), \
             patch.object(mkchecks, "CUSTOM_OOT", dict(xflags=0x1e0)), \
             patch.object(mkchecks, "COWS_OFF", 0x44c):
            check = mkchecks.check_from_key("oot", 0x10000019)
            self.assertEqual(check["off"], 0x1e0 + 25 // 8)
            self.assertEqual((check["bit"], check["scene_id"]), (1, 0x51))
            self.assertEqual(check["xflag"]["setups"], [0, 1])
            cow = mkchecks.check_from_key("mm", 0x0600000b)
            self.assertEqual((cow["anchor"], cow["off"], cow["bit"]), ("custom", 0x44c, 11))
            for flag, field, bit in ((0x10, "collect", 16), (0x21, "switch1", 1), (0x31, "switch1", 17)):
                fairy = mkchecks.check_from_key("mm", 0x05000000 | (0x16 << 16) | flag)
                self.assertEqual((fairy["field"], fairy["bit"]), (field, bit))
        blob = bytearray(0x500)
        blob[check["off"]] = 1 << check["bit"]
        self.assertEqual(overlay.read_flags(blob, [check, cow]), {("oot", check["name"])})

    def test_shared_actor_is_pending_in_each_scene_and_room_only_once(self):
        index = xflags.Index(dictionary())
        index.by_id[25].extend([
            dict(game="oot", scene=0x52, setup=1, room=2, actor=8, slice=0),
            dict(game="oot", scene=0x52, setup=1, room=3, actor=9, slice=0),
        ])
        with patch.object(mkchecks, "XFLAG_INDEX", index), \
             patch.object(mkchecks, "LAYOUT_FROM_ROM", True), \
             patch.object(mkchecks, "CUSTOM_OOT", dict(xflags=0x1e0)):
            check = mkchecks.check_from_key("oot", 0x10000019)
        tracker = overlay.Tracker(None, dict(checks=[check], rom="alias.z64"))
        self.assertEqual(tracker.scene_rooms["oot", 0x52], {2, 3})
        for scene, room, setup in ((0x51, 0, 0), (0x51, 0, 1), (0x52, 2, 1), (0x52, 3, 1)):
            with self.subTest(scene=scene, room=room, setup=setup):
                tracker.poll_once = lambda: ("oot", set(), 1.0, {}, scene, room, True, setup)
                tracker.refresh()
                state = json.loads(tracker.snapshot())
                self.assertEqual(state["totals"]["oot"], 1)
                self.assertEqual(len(state["pending_here"]["list"]), 1)
                self.assertTrue(state["pending_here"]["list"][0]["here"])
        tracker.poll_once = lambda: ("oot", {("oot", check["name"])}, 1.0, {}, 0x52, 2, True, 1)
        tracker.refresh()
        self.assertEqual(json.loads(tracker.snapshot())["pending_here"]["list"], [])

    def test_confidence_counts_shared_bitmap_once(self):
        table = dict(xflag_format="indexed", custom_save=dict(oot=dict(xflags=0x1e0, npc=0),
                     mm=dict(xflags=0x1e0, npc=0x80), xflag_bytes=0x26b))
        self.assertEqual(overlay.xflag_ranges(table), [(0x1e0, 0x44b)])

    def test_v33_selection_resets_and_respects_explicit_data(self):
        with patch.dict(os.environ), patch.object(paths, "_ROM_DATA_VERSION", None), \
             patch.object(paths, "active_data_tag", return_value=None):
            os.environ.pop(paths.DATA_ENV, None)
            with patch.object(xflags, "read", return_value=xflags.Index(dictionary())):
                paths.select_rom_data(b"new seed")
            self.assertEqual(paths.data_version(), "v33.0")
            self.assertEqual(paths.data_dir(), paths.res("data-v33"))
            with patch.object(xflags, "read", return_value=None):
                paths.select_rom_data(b"old seed")
            self.assertEqual(paths.data_version(), "v32.0")
            os.environ[paths.DATA_ENV] = paths.res("data")
            with patch.object(xflags, "read", return_value=xflags.Index(dictionary())):
                paths.select_rom_data(b"new seed")
            self.assertEqual(paths.data_dir(), paths.res("data"))

    def test_owl_record_and_both_mask_ownership_bits(self):
        oot, mm = bytearray(0x1500), bytearray(0x1500)
        mm[0x68:0x98] = b"\xff" * 48
        struct.pack_into(">I", oot, 0xe4 + 9 * 0x1c, 1 << 4)
        struct.pack_into(">I", oot, inventory.MM_OWL_FLAGS_ADDR, 1 << 1)
        for mask in range(4):
            struct.pack_into(">I", oot, 0x154, mask << 14)
            snap = inventory.mm_snapshot(mm, oot, indexed=True)
            self.assertEqual(snap["eq:Adult Mask"], int(bool(mask & 2)))
            self.assertEqual(snap["item:Fierce Deity's Mask"], 0x35 if mask & 1 else 0xff)
            self.assertEqual([v for k,v in snap.items() if k.startswith("owl:")], [0,0,0,0,1,0,0,0,0,0])
        old = inventory.mm_snapshot(mm, oot)
        self.assertNotIn("eq:Adult Mask", old)
        self.assertEqual([v for k,v in old.items() if k.startswith("owl:")], [0,1,0,0,0,0,0,0,0,0])

    def test_getter_recognition_requires_both_payloads_and_unique_code(self):
        import collections
        scans, located = {}, {}
        for game, base in (("oot", 0x80449000), ("mm", 0x80769000)):
            ram = payload.PAYLOAD[game]["ram"]
            words = (0x3403ffff, 0x10830009, 0x24020001,
                     0x3c030000 | ((base + 0x8000) >> 16), 0x000410c2,
                     0x24630000 | (base & 0xffff), 0x00431021, 0x904201e0,
                     0x30840007, 0x00821007, 0x30420001, payload.JR_RA, 0)
            refs = [payload.Ref(ram + 28, "lbu", base + 0x1e0, base, 2, True),
                    payload.Ref(ram + 60, "lw", base + 0x1e8, base, 2, False)]
            scans[game] = SimpleNamespace(words=words, refs=refs, ram=ram)
            located[game] = dict(custom=(base, 0x400))
        idx = collections.Counter({0:3, 0x20:3, 0x28:3, 0x30:3, 0x80:3, 0xa0:3})
        any_ = collections.Counter({0xa4:3})
        with patch.object(payload, "_pooled_refs", return_value=(idx, any_)):
            result = payload.indexed_layout(scans, located, xflags.Index(dictionary()))
            self.assertEqual(result["cows"], 0x1e8)
            self.assertEqual(result["mm"]["halfDays"], 0xa4)
            scans["mm"].words = tuple([0] + list(scans["mm"].words[1:]))
            self.assertIsNone(payload.indexed_layout(scans, located, xflags.Index(dictionary())))

    def test_capacity_and_adult_mask_are_visible_in_inventory(self):
        grid = overlay.item_grid("mm", {"up:deku sticks": 1, "up:deku nuts": 0, "eq:Adult Mask": 1})
        groups = {g["name"]: g["items"] for g in grid}
        self.assertEqual({i["label"] for i in groups["Upgrades"]}, {"deku sticks", "deku nuts"})
        self.assertEqual(groups["Masks"][0]["label"], "Adult Mask")
        self.assertTrue(groups["Masks"][0]["on"])

    def test_v33_item_spellings_preserve_distinct_upgrades(self):
        self.assertTrue(placement.same_item("Deku Nut Upgrade", "Deku Nut Capacity"))
        self.assertFalse(placement.same_item("Deku Nut Upgrade", "Second Deku Nut Upgrade"))
        self.assertFalse(placement.same_item("Deku Stick Upgrade", "Deku Nut Capacity"))
        self.assertFalse(placement.same_item("Ocarina (MM)", "Bomb Bag"))


if __name__ == "__main__":
    unittest.main()
