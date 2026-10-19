"""Bounded COBS framing, CRC32C and typed TLV control payloads."""
import struct
from dataclasses import dataclass
from enum import IntEnum

VERSION = 1
MAX_PAYLOAD = 4096
HEADER = struct.Struct("<BBBBQIHH")
MAX_RAW = HEADER.size + MAX_PAYLOAD + 4
MAX_ENCODED = MAX_RAW + MAX_RAW // 254 + 2
FILE_HEADER = struct.Struct("<QQH")
BENCH_HEADER = struct.Struct("<QQ")  # benchmark ID and sequence; no payload CRC


class Kind(IntEnum):
    REQUEST = 1
    RESPONSE = 2
    BENCH_DATA = 3


class Op(IntEnum):
    HELLO = 0x0001
    PING = 0x0003
    GET_CAPS = 0x0004
    DEV_INFO = 0x0100
    DEV_STATUS = 0x0101
    POWER_STATUS = 0x0102
    FS_INFO = 0x0200
    FS_LIST = 0x0201
    FS_STAT = 0x0202
    FS_MKDIR = 0x0203
    FS_RENAME = 0x0204
    FS_DELETE = 0x0205
    FS_HASH = 0x0206
    FILE_BEGIN = 0x0300
    FILE_DATA = 0x0301
    FILE_STATUS = 0x0302
    FILE_END = 0x0303
    FILE_ABORT = 0x0304
    FILE_READ = 0x0305
    TARGET_INFO = 0x0400
    FLASH_CHECK = 0x0500
    FLASH_START = 0x0501
    JOB_STATUS = 0x0600
    JOB_LIST = 0x0601
    JOB_CANCEL = 0x0602
    RTT_START = 0x0701
    RTT_STOP = 0x0702
    RTT_READ = 0x0703
    RTT_WRITE = 0x0704
    RTT_STATS = 0x0705
    BENCH_BEGIN = 0x0900
    BENCH_DATA = 0x0901
    BENCH_END = 0x0902
    BENCH_STATUS = 0x0903
    BENCH_ABORT = 0x0904
    SIM_CONFIG = 0x7F00


class Status(IntEnum):
    OK = 0
    ACCEPTED = 1
    INVALID_ARGUMENT = 2
    UNSUPPORTED = 3
    NOT_FOUND = 4
    ALREADY_EXISTS = 5
    BUSY = 6
    NO_SPACE = 7
    SD_NOT_READY = 8
    IO_ERROR = 9
    FILE_CHANGED = 10
    OFFSET_MISMATCH = 11
    HASH_MISMATCH = 12
    INVALID_HEX = 13
    TARGET_MISMATCH = 14
    TARGET_ERROR = 15
    VERIFY_FAILED = 16
    CANCELLED = 17
    RESULT_UNKNOWN = 18
    REQUEST_CONFLICT = 19
    SESSION_INVALID = 20
    POWER_INSUFFICIENT = 21


class ProtocolError(ValueError):
    pass


def crc32c(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def cobs_encode(data):
    out = bytearray([0])
    index, code = 0, 1
    for byte in data:
        if byte == 0:
            out[index] = code
            index, code = len(out), 1
            out.append(0)
        else:
            out.append(byte)
            code += 1
            if code == 255:
                out[index] = code
                index, code = len(out), 1
                out.append(0)
    out[index] = code
    return bytes(out)


def cobs_decode(data):
    if not data or 0 in data:
        raise ProtocolError("invalid COBS input")
    out, index = bytearray(), 0
    while index < len(data):
        code = data[index]
        index += 1
        end = index + code - 1
        if end > len(data):
            raise ProtocolError("truncated COBS block")
        out.extend(data[index:end])
        index = end
        if code != 255 and index < len(data):
            out.append(0)
    return bytes(out)


@dataclass(frozen=True)
class Frame:
    kind: int
    channel: int
    session: int
    request: int
    opcode: int
    payload: bytes = b""
    flags: int = 0

    def _is_benchmark(self):
        special = (self.kind == Kind.BENCH_DATA and self.channel == 1 and
                   self.opcode == Op.BENCH_DATA and self.request == 0 and self.flags == 1)
        if not special and (self.flags or self.kind not in (Kind.REQUEST, Kind.RESPONSE)):
            raise ProtocolError("invalid kind/flags combination")
        return special

    def encode(self):
        if len(self.payload) > MAX_PAYLOAD:
            raise ProtocolError("payload exceeds limit")
        special = self._is_benchmark()
        raw = HEADER.pack(VERSION, self.kind, self.channel, self.flags, self.session,
                          self.request, self.opcode, len(self.payload)) + self.payload
        checksum = crc32c(raw[:HEADER.size] if special else raw)
        return cobs_encode(raw + struct.pack("<I", checksum)) + b"\0"

    @classmethod
    def decode(cls, encoded):
        raw = cobs_decode(encoded)
        if not HEADER.size + 4 <= len(raw) <= MAX_RAW:
            raise ProtocolError("invalid frame size")
        version, kind, channel, flags, session, request, opcode, size = HEADER.unpack_from(raw)
        if version != VERSION or channel not in (0, 1, 2, 3):
            raise ProtocolError("unsupported header")
        if len(raw) != HEADER.size + size + 4:
            raise ProtocolError("payload size mismatch")
        frame = cls(kind, channel, session, request, opcode, raw[HEADER.size:-4], flags)
        special = frame._is_benchmark()
        if crc32c(raw[:HEADER.size] if special else raw[:-4]) != struct.unpack_from("<I", raw, len(raw) - 4)[0]:
            raise ProtocolError("CRC mismatch")
        return frame


class Decoder:
    """Incremental decoder. Oversize input discards until the next delimiter."""
    def __init__(self):
        self.buffer = bytearray()
        self.discard = False
        self.errors = 0

    def feed(self, data):
        frames = []
        for byte in data:
            if byte == 0:
                if self.buffer and not self.discard:
                    try:
                        frames.append(Frame.decode(bytes(self.buffer)))
                    except (ValueError, struct.error):
                        self.errors += 1
                self.buffer.clear()
                self.discard = False
            elif not self.discard:
                self.buffer.append(byte)
                if len(self.buffer) > MAX_ENCODED:
                    self.buffer.clear()
                    self.discard = True
                    self.errors += 1
        return frames


# Frozen v0.1 tag order. Existing IDs MUST NOT be renumbered.
_FIELDS = [
    ("message", "str"), ("path", "str"), ("new_path", "str"),
    ("direction", "str"), ("size", "u64"), ("sha256", "bytes"),
    ("overwrite", "bool"), ("transfer_id", "u64"), ("next_offset", "u64"),
    ("block_size", "u64"), ("state", "str"), ("boot_id", "u64"),
    ("device_id", "str"), ("protocol", "u64"), ("max_payload", "u64"),
    ("features", "str"), ("capacity", "u64"), ("free", "u64"),
    ("cursor", "u64"), ("limit", "u64"), ("next_cursor", "u64"),
    ("version", "str"), ("entries", "records"), ("name", "str"),
    ("is_dir", "bool"), ("offset", "u64"), ("data", "bytes"),
    ("length", "u64"), ("job_id", "str"), ("operation_token", "str"),
    ("phase", "str"), ("completed_bytes", "u64"), ("total_bytes", "u64"),
    ("error", "str"), ("run_after", "bool"), ("target_id", "str"),
    ("base_address", "u64"), ("flash_size", "u64"), ("destinations", "u64"),
    ("stream_id", "u64"), ("sequence", "u64"), ("timestamp_ms", "u64"),
    ("dropped_bytes", "u64"), ("sd_path", "str"), ("rate", "u64"),
    ("sd_max_bytes", "u64"), ("sd_error", "str"), ("written_bytes", "u64"),
    ("battery_mv", "u64"), ("battery_percent", "u64"), ("charging", "bool"),
    ("external_power", "bool"), ("can_flash", "bool"), ("sd_ready", "bool"),
    ("io_fail", "bool"), ("verify_fail", "bool"), ("rtt_capacity", "u64"),
    ("jobs", "records"), ("checkpoint_offset", "u64"), ("channel_id", "u64"),
    ("media_epoch", "u64"), ("min_address", "u64"), ("max_address", "u64"),
    ("gui_bytes", "u64"), ("sd_bytes", "u64"), ("input_data", "bytes"),
    ("retries", "u64"), ("crc_errors", "u64"), ("disconnect_policy", "str"),
    ("transfers", "records"), ("streams", "records"),
    ("bench_id", "u64"), ("sink", "str"), ("received_bytes", "u64"),
    ("elapsed_us", "u64"), ("verify_us", "u64"), ("passed", "bool"),
    ("sequence_errors", "u64"), ("framing_errors", "u64"),
]
SCHEMA = {name: (i + 1, kind) for i, (name, kind) in enumerate(_FIELDS)}
BY_TAG = {tag: (name, kind) for name, (tag, kind) in SCHEMA.items()}


def encode_fields(fields):
    result = bytearray()
    for name, value in fields.items():
        if name not in SCHEMA:
            raise ProtocolError("unknown field: " + name)
        tag, kind = SCHEMA[name]
        if kind == "str":
            raw = value.encode("utf-8")
        elif kind == "bytes":
            raw = bytes(value)
        elif kind == "bool":
            if type(value) is not bool:
                raise ProtocolError("boolean required: " + name)
            raw = bytes([int(value)])
        elif kind == "u64":
            if type(value) is not int or not 0 <= value < 2**64:
                raise ProtocolError("u64 required: " + name)
            raw = struct.pack("<Q", value)
        else:
            records = [encode_fields(record) for record in value]
            raw = b"".join(struct.pack("<I", len(record)) + record for record in records)
        if len(raw) > 65535:
            raise ProtocolError("field too large")
        result.extend(struct.pack("<HH", tag, len(raw)))
        result.extend(raw)
    if len(result) > MAX_PAYLOAD:
        raise ProtocolError("control payload exceeds limit")
    return bytes(result)


def decode_fields(raw, depth=0):
    if depth > 2:
        raise ProtocolError("TLV nesting too deep")
    result, offset = {}, 0
    while offset < len(raw):
        if offset + 4 > len(raw):
            raise ProtocolError("truncated TLV header")
        tag, size = struct.unpack_from("<HH", raw, offset)
        offset += 4
        if offset + size > len(raw):
            raise ProtocolError("truncated TLV value")
        value = raw[offset:offset + size]
        offset += size
        if tag not in BY_TAG:
            if tag & 0x8000:
                raise ProtocolError("unknown mandatory TLV")
            continue
        name, kind = BY_TAG[tag]
        if name in result:
            raise ProtocolError("duplicate TLV: " + name)
        if kind == "str":
            value = value.decode("utf-8", errors="strict")
        elif kind == "u64":
            if size != 8:
                raise ProtocolError("invalid u64")
            value = struct.unpack("<Q", value)[0]
        elif kind == "bool":
            if value not in (b"\0", b"\1"):
                raise ProtocolError("invalid bool")
            value = value == b"\1"
        elif kind == "records":
            records, pos = [], 0
            while pos < len(value):
                if pos + 4 > len(value):
                    raise ProtocolError("truncated record")
                length = struct.unpack_from("<I", value, pos)[0]
                pos += 4
                if pos + length > len(value):
                    raise ProtocolError("truncated record body")
                records.append(decode_fields(value[pos:pos + length], depth + 1))
                pos += length
            value = records
        result[name] = value
    return result


def response_payload(status=Status.OK, **fields):
    raw = struct.pack("<H", status) + encode_fields(fields)
    if len(raw) > MAX_PAYLOAD:
        raise ProtocolError("response too large")
    return raw


def parse_response(payload):
    if len(payload) < 2:
        raise ProtocolError("missing response status")
    return Status(struct.unpack_from("<H", payload)[0]), decode_fields(payload[2:])
