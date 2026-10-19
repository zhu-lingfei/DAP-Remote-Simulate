import random
import struct
import unittest

from dapremote.intelhex import HexError, parse_hex
from dapremote.protocol import (Decoder, Frame, Kind, MAX_ENCODED, Op, ProtocolError,
                                cobs_decode, cobs_encode, crc32c, decode_fields, encode_fields)


class ProtocolTests(unittest.TestCase):
    def test_frozen_full_frame_vectors(self):
        import json
        from pathlib import Path
        vectors = json.loads(Path("tests/vectors.json").read_text(encoding="utf-8"))
        for vector in vectors:
            with self.subTest(name=vector["name"]):
                frame = Frame(vector["kind"], vector["channel"], vector["session"],
                              vector["request"], vector["opcode"], bytes.fromhex(vector["payload_hex"]))
                self.assertEqual(frame.encode().hex(), vector["wire_hex"])
                self.assertEqual(cobs_decode(bytes.fromhex(vector["wire_hex"])[:-1]).hex(), vector["raw_hex"])
                self.assertEqual(Frame.decode(bytes.fromhex(vector["wire_hex"])[:-1]), frame)

    def test_generated_reference_is_current(self):
        from pathlib import Path
        from scripts.export_spec import reference
        self.assertEqual(Path("docs/REFERENCE.md").read_text(encoding="utf-8"), reference())

    def test_independent_crc_and_cobs_vectors(self):
        self.assertEqual(crc32c(b"123456789"), 0xE3069283)
        vectors = [(b"", "01"), (b"\0", "0101"), (b"\0\0", "010101"),
                   (b"\x11\x22\0\x33", "0311220233"), (b"abc", "04616263")]
        for raw, encoded in vectors:
            self.assertEqual(cobs_encode(raw).hex(), encoded)
            self.assertEqual(cobs_decode(bytes.fromhex(encoded)), raw)

    def test_tlv_fixed_bytes(self):
        self.assertEqual(encode_fields(dict(path="/a", size=3)).hex(),
                         "020002002f61050008000300000000000000")
        self.assertEqual(decode_fields(bytes.fromhex("020002002f61050008000300000000000000")),
                         dict(path="/a", size=3))

    def test_seeded_roundtrip_all_lengths_and_fragmentation(self):
        rng = random.Random(20261018)
        for size in (0, 1, 253, 254, 255, 512, 4096):
            payload = bytes(rng.randrange(256) for _ in range(size))
            frame = Frame(Kind.REQUEST, 1, 0x1234, 7, Op.FILE_DATA, payload)
            encoded = frame.encode()
            decoder, found, offset = Decoder(), [], 0
            while offset < len(encoded):
                count = rng.randrange(1, 51)
                found.extend(decoder.feed(encoded[offset:offset + count]))
                offset += count
            self.assertEqual(found, [frame])

    def test_recovery_after_crc_truncation_garbage_and_oversize(self):
        frame = Frame(1, 0, 9, 1, Op.PING)
        good = frame.encode()
        raw = bytearray(cobs_decode(good[:-1]))
        raw[-1] ^= 1
        stream = cobs_encode(raw) + b"\0" + good[:4] + b"\0" + b"x" * (MAX_ENCODED + 10) + b"\0" + good + good
        decoder = Decoder()
        self.assertEqual(decoder.feed(stream), [frame, frame])
        self.assertEqual(decoder.errors, 3)
        self.assertLessEqual(len(decoder.buffer), MAX_ENCODED)

    def test_tlv_rejects_duplicate_truncation_invalid_bool_and_mandatory(self):
        for raw in (b"\1", bytes.fromhex("0200050061"), bytes.fromhex("0700010002"),
                    bytes.fromhex("ff800000"), encode_fields(dict(path="a")) * 2):
            with self.assertRaises(ProtocolError):
                decode_fields(raw)
        self.assertEqual(decode_fields(bytes.fromhex("ff7f010061")), {})

    def test_nested_records_binary_unicode(self):
        fields = dict(entries=[dict(name="固件.hex", size=3, data=b"\0\xff\x13")])
        self.assertEqual(decode_fields(encode_fields(fields)), fields)

    def test_hex_checks_real_format_and_addresses(self):
        from pathlib import Path
        data = Path("examples/blink.hex").read_bytes()
        image = parse_hex(data)
        self.assertEqual(bytes(image[a] for a in sorted(image)), bytes(range(1, 17)))
        for bad in (data.replace(b"F2", b"F3"), data.replace(b":00000001FF", b""),
                    data + b"\n:00000001FF", data.replace(b"0800", b"0900")):
            with self.assertRaises(HexError):
                parse_hex(bad)


if __name__ == "__main__":
    unittest.main()
