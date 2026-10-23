import struct
import unittest
from unittest.mock import patch
import test_regressions as baseline
import payload
import placement
import provenance
import discover


class WorldIdentityTests(unittest.TestCase):
    setUp = baseline.RegressionTests.setUp
    seed_cache = baseline.RegressionTests.seed_cache

    def blob(self, game, world=2, offset=0x100, duplicate=False):
        address = payload.PAYLOAD[game]["ram"] + offset
        hi = ((address + 0x8000) >> 16) & 0xFFFF
        lo = address & 0xFFFF
        signature = struct.pack('>11I', 0x3C020000 | hi, 0x03E00008, 0x90420000 | lo,
                                0x3C028045, 0x2442C3C0, 0x8C430014, 0x24660001,
                                0xAC460014, 0xA8830000, 0x08102FB6, 0xB8830003)
        blob = bytearray(offset + 32)
        blob[:len(signature)] = signature
        if duplicate:
            blob[48:48+len(signature)] = signature
        blob[offset] = world
        return bytes(blob)

    def read(self, oot, mm):
        with patch.object(payload.romlib, 'read_extra_vrom', side_effect=[oot, mm]):
            return payload.world_id(b'fixture')

    def test_both_payloads_agree_even_with_negative_low_half(self):
        self.assertEqual(self.read(self.blob('oot', offset=0x9000), self.blob('mm', offset=0x9000)), 2)

    def test_conflict_missing_payload_or_ambiguous_code_refuses_identity(self):
        self.assertIsNone(self.read(self.blob('oot'), self.blob('mm', world=1)))
        self.assertIsNone(self.read(self.blob('oot', duplicate=True), self.blob('mm')))
        self.assertIsNone(self.read(self.blob('oot', world=0), self.blob('mm')))
        self.assertIsNone(self.read(self.blob('oot', world=65), self.blob('mm')))
        with patch.object(payload.romlib, 'read_extra_vrom', side_effect=KeyError):
            self.assertIsNone(payload.world_id(b'fixture'))

    def test_getter_alone_or_bad_packet_sequence_does_not_match(self):
        blob = bytearray(self.blob('oot'))
        blob[20:24] = bytes(4)
        self.assertIsNone(self.read(bytes(blob), self.blob('mm')))
        blob = bytearray(self.blob('oot'))
        blob[8:12] = struct.pack('>I', 0x90427FFF)  # target outside the file
        self.assertIsNone(self.read(bytes(blob), self.blob('mm')))

    def test_rom_identity_precedes_misleading_majority_and_spoiler(self):
        with patch.object(payload, 'world_id', return_value=2):
            world, source = placement.world_of_rom(b'fixture', {('oot',1):(1,1)}, {1:(99,100)})
        self.assertEqual(world, 2)
        self.assertEqual(source, "the ROM's world-id code")

    def test_generator_revision_invalidates_cache_without_changing_inputs(self):
        rom = self.seed_cache()
        with patch.object(provenance, 'CHECKS_REVISION', provenance.CHECKS_REVISION + 1), \
             patch.object(discover, '_generate', return_value=0):
            self.assertEqual(discover.ensure_tables(str(rom), None), ['checks.json'])


if __name__ == '__main__':
    unittest.main()
