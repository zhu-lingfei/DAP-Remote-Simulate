"""Bounded HEX text presentation; not a replacement for target flash validation."""
from dataclasses import dataclass

MAX_PREVIEW_LINES = 4000


@dataclass(frozen=True)
class PreviewLine:
    number: int
    text: str
    kind: str


def preview_lines(data, truncated=False, max_lines=MAX_PREVIEW_LINES):
    """Classify records for display; do not reject a deliberately incomplete tail."""
    source = data.decode("ascii", errors="backslashreplace").splitlines(keepends=True)
    result = []
    for index, line in enumerate(source[:max_lines]):
        partial = truncated and index == len(source) - 1 and not line.endswith(("\n", "\r"))
        text = line.rstrip("\r\n")
        # Preview is untrusted text: display controls as escapes, never send to Tk raw.
        text = "".join("\\x%02x" % ord(c) if ord(c) < 32 or ord(c) == 127 else c for c in text)
        kind = "partial" if partial else "blank" if not text else "error"
        if text and not partial:
            try:
                if (not text.startswith(":") or any(c not in "0123456789abcdefABCDEF" for c in text[1:])):
                    raise ValueError()
                record = bytes.fromhex(text[1:])
                if len(record) < 5 or len(record) != record[0] + 5 or sum(record) & 0xFF:
                    raise ValueError()
                address = int.from_bytes(record[1:3], "big")
                record_type, count = record[3], record[0]
                if record_type == 0:
                    kind = "data"
                elif record_type == 1 and count == 0 and address == 0:
                    kind = "eof"
                elif record_type in (2, 4) and count == 2 and address == 0:
                    kind = "address"
                elif record_type in (3, 5) and count == 4 and address == 0:
                    kind = "start"
            except ValueError:
                pass
        result.append(PreviewLine(index + 1, text, kind))
    return result
