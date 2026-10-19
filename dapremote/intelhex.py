"""Strict Intel HEX parser for the simulated 32-bit target."""


class HexError(ValueError):
    pass


def parse_hex(data, base_address=0x08000000, flash_size=256 * 1024):
    try:
        text = data.decode("ascii")
    except UnicodeError as exc:
        raise HexError("HEX must be ASCII") from exc
    image, address_base, eof = {}, 0, False
    for number, line in enumerate(text.splitlines(), 1):
        if not line:
            continue
        if eof:
            raise HexError("record after EOF at line %d" % number)
        try:
            if not line.startswith(":") or len(line) < 11 or any(c not in "0123456789abcdefABCDEF" for c in line[1:]):
                raise ValueError()
            record = bytes.fromhex(line[1:])
            if len(record) != record[0] + 5 or sum(record) & 0xFF:
                raise ValueError()
        except ValueError as exc:
            raise HexError("bad HEX length/checksum at line %d" % number) from exc
        count, address, kind = record[0], int.from_bytes(record[1:3], "big"), record[3]
        payload = record[4:-1]
        if kind == 0:
            if address + count > 0x10000:
                raise HexError("data record crosses 64 KiB boundary")
            for i, byte in enumerate(payload):
                absolute = address_base + address + i
                if not base_address <= absolute < base_address + flash_size:
                    raise HexError("address outside target flash: 0x%08X" % absolute)
                if absolute in image:
                    raise HexError("overlapping data at 0x%08X" % absolute)
                image[absolute] = byte
        elif kind == 1:
            if count or address:
                raise HexError("invalid EOF")
            eof = True
        elif kind in (2, 4):
            if count != 2 or address:
                raise HexError("invalid extended address")
            address_base = int.from_bytes(payload, "big") << (4 if kind == 2 else 16)
        elif kind in (3, 5):
            if count != 4 or address:
                raise HexError("invalid start address")
            # Validated but not used as a reset vector by this simulated target.
        else:
            raise HexError("unsupported record type %d" % kind)
    if not eof or not image:
        raise HexError("EOF and at least one data byte required")
    return image
