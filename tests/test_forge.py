"""Entrance identity checks, independent of Nintendo data and Docker."""
import unittest
from unittest.mock import patch
import test_regressions as baseline
import guard_roms


class EntranceGuardTests(unittest.TestCase):
    setUp = baseline.RegressionTests.setUp

    def check(self, expected, actual, world=1):
        report = guard_roms.Report("fixture")
        with patch.object(guard_roms, "parse_spoiler", return_value=({}, expected, set())):
            guard_roms.check_entrances(report, {"entrances": actual}, "fixture.txt", world)
        return report.rows[0][1]

    def row(self, src, dst):
        return dict(src_name=src, dst_name=dst)

    def test_equal_count_swapped_destinations_fails(self):
        expected = {1: [("Door (OOT_A)", "Exit (MM_B)"), ("Door (MM_C)", "Exit (OOT_D)")]}
        self.assertEqual(self.check(expected, [self.row("OOT_A", "OOT_D"), self.row("MM_C", "MM_B")]), "FAIL")

    def test_symbols_ignore_display_renames_and_order(self):
        expected = {1: [("New name (OOT_A)", "New destination (MM_B)"), ("X (MM_C)", "Y (OOT_D)")]}
        self.assertEqual(self.check(expected, [self.row("MM_C", "OOT_D"), self.row("OOT_A", "MM_B")]), "ok")

    def test_missing_extra_and_duplicate_pairs_fail(self):
        expected = {1: [("A (OOT_A)", "B (MM_B)")]}
        row = self.row("OOT_A", "MM_B")
        for actual in ([], [row, row], [row, self.row("MM_C", "OOT_D")]):
            with self.subTest(actual=actual):
                self.assertEqual(self.check(expected, actual), "FAIL")

    def test_unlabelled_pair_never_certifies_success(self):
        self.assertEqual(self.check({1: [("old format", "no symbol")]}, [self.row("OOT_A", "MM_B")]), "WARN")
        self.assertEqual(self.check({1: [("A (OOT_A)", "B (MM_B)")]}, [self.row("?0x12", "MM_B")]), "WARN")

    def test_partial_coverage_still_catches_wrong_destination(self):
        self.assertEqual(self.check({1: [("A (OOT_A)", "B (MM_B)"), ("old", "unknown")]},
                                    [self.row("OOT_A", "MM_C"), self.row("?0x12", "?0x13")]), "FAIL")

    def test_unknown_pairs_cannot_hide_count_disagreements(self):
        expected = {1: [("A (OOT_A)", "B (MM_B)")]}
        self.assertEqual(self.check(expected, [self.row("OOT_A", "MM_B"), self.row("OOT_C", "MM_D"),
                                              self.row("?0x12", "?0x13")]), "FAIL")
        expected[1] += [("C (OOT_C)", "D (MM_D)"), ("E (OOT_E)", "F (MM_F)")]
        self.assertEqual(self.check(expected, [self.row("?0x12", "?0x13")]), "FAIL")
        self.assertEqual(self.check(expected, [self.row("OOT_A", "MM_B"), self.row("OOT_X", "MM_Y"),
                                              self.row("?0x12", "?0x13")]), "FAIL")

    def test_all_mq_means_all_twinned_dungeons_not_one_named_all(self):
        rows = [dict(game="oot", name="MQ Chest", scene="DEKU_TREE", mq=True, item="Bow"),
                dict(game="oot", name="Chest", scene="DEKU_TREE", mq=False)]
        data = dict(checks=rows, mq=dict(scenes=["DEKU_TREE"]))
        log = self.home / "guard.log"
        truth = {1: {("oot", "MQ Chest"): ("Bow", 1)}}
        with patch.object(guard_roms, "parse_spoiler", return_value=(truth, {}, {"all"})):
            rep = guard_roms.Report("all MQ")
            guard_roms.check_placement(rep, data, self.home / "spoiler.txt", 1, log)
        self.assertEqual(rep.rows[0][1], "ok")
        rows += [dict(game="oot", name="MQ Temple", scene="TEMPLE_FIRE", mq=True),
                 dict(game="oot", name="Temple", scene="TEMPLE_FIRE", mq=False)]
        with patch.object(guard_roms, "parse_spoiler", return_value=(truth, {}, {"all"})):
            rep = guard_roms.Report("missed MQ")
            guard_roms.check_placement(rep, data, self.home / "spoiler.txt", 1, log)
        self.assertEqual(rep.rows[0][1], "WARN")
        self.assertIn("2 MQ dungeons", rep.rows[0][2])

    def test_uses_requested_world(self):
        expected = {1: [("A (OOT_A)", "B (MM_B)")], 2: [("A (OOT_A)", "C (MM_C)")]}
        self.assertEqual(self.check(expected, [self.row("OOT_A", "MM_C")], world=2), "ok")

    def test_parser_handles_world_sections_and_game_symbols(self):
        file = self.home / "spoiler.txt"
        file.write_text("Entrances\n  World 1\n    Old (OOT_A) -> To (MM_B)\n  World 2\n    Old (OOT_A) -> To (OOT_C)\nHints\n  Ignore -> This\n")
        _, actual, _ = guard_roms.parse_spoiler(file)
        self.assertEqual(actual, {1:[("Old (OOT_A)", "To (MM_B)")], 2:[("Old (OOT_A)", "To (OOT_C)")]})


if __name__ == "__main__":
    unittest.main()
