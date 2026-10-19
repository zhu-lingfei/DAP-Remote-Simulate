"""Regenerate enum/TLV reference tables from source (not the wire vectors)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dapremote.protocol import Op, SCHEMA, Status


def reference():
    lines = ["# v0.2 编号与字段索引", "", "由 `scripts/export_spec.py` 从源码生成；已有编号不可重新排序。", "",
             "## 指令编号", "", "| 名称 | 十六进制 |", "|---|---:|"]
    lines += ["| `%s` | `0x%04X` |" % (op.name, op.value) for op in Op]
    lines += ["", "## 响应状态", "", "| 名称 | 十进制 |", "|---|---:|"]
    lines += ["| `%s` | %d |" % (status.name, status.value) for status in Status]
    lines += ["", "## TLV 字段", "", "类型为 `u64` 的数值均占 8 字节，即使其业务取值很小。", "",
              "| 字段 | Tag | 类型 |", "|---|---:|---|"]
    lines += ["| `%s` | %d | `%s` |" % (name, tag, kind) for name, (tag, kind) in SCHEMA.items()]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "docs" / "REFERENCE.md"
    target.parent.mkdir(exist_ok=True)
    target.write_text(reference(), encoding="utf-8")
