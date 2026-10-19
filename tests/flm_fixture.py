"""Only an ELF envelope for simulator transport tests; not an executable FLM."""
import struct


def flm_envelope(marker=b"simulator test only"):
    ident = b"\x7fELF\x01\x01\x01" + b"\0" * 9
    return ident + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, 0, 0, 52, 0, 0, 0, 0, 0) + marker
