# v0.2 编号与字段索引

由 `scripts/export_spec.py` 从源码生成；已有编号不可重新排序。

## 指令编号

| 名称 | 十六进制 |
|---|---:|
| `HELLO` | `0x0001` |
| `PING` | `0x0003` |
| `GET_CAPS` | `0x0004` |
| `DEV_INFO` | `0x0100` |
| `DEV_STATUS` | `0x0101` |
| `POWER_STATUS` | `0x0102` |
| `FS_INFO` | `0x0200` |
| `FS_LIST` | `0x0201` |
| `FS_STAT` | `0x0202` |
| `FS_MKDIR` | `0x0203` |
| `FS_RENAME` | `0x0204` |
| `FS_DELETE` | `0x0205` |
| `FS_HASH` | `0x0206` |
| `FILE_BEGIN` | `0x0300` |
| `FILE_DATA` | `0x0301` |
| `FILE_STATUS` | `0x0302` |
| `FILE_END` | `0x0303` |
| `FILE_ABORT` | `0x0304` |
| `FILE_READ` | `0x0305` |
| `TARGET_INFO` | `0x0400` |
| `FLASH_CHECK` | `0x0500` |
| `FLASH_START` | `0x0501` |
| `JOB_STATUS` | `0x0600` |
| `JOB_LIST` | `0x0601` |
| `JOB_CANCEL` | `0x0602` |
| `RTT_START` | `0x0701` |
| `RTT_STOP` | `0x0702` |
| `RTT_READ` | `0x0703` |
| `RTT_WRITE` | `0x0704` |
| `RTT_STATS` | `0x0705` |
| `BENCH_BEGIN` | `0x0900` |
| `BENCH_DATA` | `0x0901` |
| `BENCH_END` | `0x0902` |
| `BENCH_STATUS` | `0x0903` |
| `BENCH_ABORT` | `0x0904` |
| `SIM_CONFIG` | `0x7F00` |

## 响应状态

| 名称 | 十进制 |
|---|---:|
| `OK` | 0 |
| `ACCEPTED` | 1 |
| `INVALID_ARGUMENT` | 2 |
| `UNSUPPORTED` | 3 |
| `NOT_FOUND` | 4 |
| `ALREADY_EXISTS` | 5 |
| `BUSY` | 6 |
| `NO_SPACE` | 7 |
| `SD_NOT_READY` | 8 |
| `IO_ERROR` | 9 |
| `FILE_CHANGED` | 10 |
| `OFFSET_MISMATCH` | 11 |
| `HASH_MISMATCH` | 12 |
| `INVALID_HEX` | 13 |
| `TARGET_MISMATCH` | 14 |
| `TARGET_ERROR` | 15 |
| `VERIFY_FAILED` | 16 |
| `CANCELLED` | 17 |
| `RESULT_UNKNOWN` | 18 |
| `REQUEST_CONFLICT` | 19 |
| `SESSION_INVALID` | 20 |
| `POWER_INSUFFICIENT` | 21 |

## TLV 字段

类型为 `u64` 的数值均占 8 字节，即使其业务取值很小。

| 字段 | Tag | 类型 |
|---|---:|---|
| `message` | 1 | `str` |
| `path` | 2 | `str` |
| `new_path` | 3 | `str` |
| `direction` | 4 | `str` |
| `size` | 5 | `u64` |
| `sha256` | 6 | `bytes` |
| `overwrite` | 7 | `bool` |
| `transfer_id` | 8 | `u64` |
| `next_offset` | 9 | `u64` |
| `block_size` | 10 | `u64` |
| `state` | 11 | `str` |
| `boot_id` | 12 | `u64` |
| `device_id` | 13 | `str` |
| `protocol` | 14 | `u64` |
| `max_payload` | 15 | `u64` |
| `features` | 16 | `str` |
| `capacity` | 17 | `u64` |
| `free` | 18 | `u64` |
| `cursor` | 19 | `u64` |
| `limit` | 20 | `u64` |
| `next_cursor` | 21 | `u64` |
| `version` | 22 | `str` |
| `entries` | 23 | `records` |
| `name` | 24 | `str` |
| `is_dir` | 25 | `bool` |
| `offset` | 26 | `u64` |
| `data` | 27 | `bytes` |
| `length` | 28 | `u64` |
| `job_id` | 29 | `str` |
| `operation_token` | 30 | `str` |
| `phase` | 31 | `str` |
| `completed_bytes` | 32 | `u64` |
| `total_bytes` | 33 | `u64` |
| `error` | 34 | `str` |
| `run_after` | 35 | `bool` |
| `target_id` | 36 | `str` |
| `base_address` | 37 | `u64` |
| `flash_size` | 38 | `u64` |
| `destinations` | 39 | `u64` |
| `stream_id` | 40 | `u64` |
| `sequence` | 41 | `u64` |
| `timestamp_ms` | 42 | `u64` |
| `dropped_bytes` | 43 | `u64` |
| `sd_path` | 44 | `str` |
| `rate` | 45 | `u64` |
| `sd_max_bytes` | 46 | `u64` |
| `sd_error` | 47 | `str` |
| `written_bytes` | 48 | `u64` |
| `battery_mv` | 49 | `u64` |
| `battery_percent` | 50 | `u64` |
| `charging` | 51 | `bool` |
| `external_power` | 52 | `bool` |
| `can_flash` | 53 | `bool` |
| `sd_ready` | 54 | `bool` |
| `io_fail` | 55 | `bool` |
| `verify_fail` | 56 | `bool` |
| `rtt_capacity` | 57 | `u64` |
| `jobs` | 58 | `records` |
| `checkpoint_offset` | 59 | `u64` |
| `channel_id` | 60 | `u64` |
| `media_epoch` | 61 | `u64` |
| `min_address` | 62 | `u64` |
| `max_address` | 63 | `u64` |
| `gui_bytes` | 64 | `u64` |
| `sd_bytes` | 65 | `u64` |
| `input_data` | 66 | `bytes` |
| `retries` | 67 | `u64` |
| `crc_errors` | 68 | `u64` |
| `disconnect_policy` | 69 | `str` |
| `transfers` | 70 | `records` |
| `streams` | 71 | `records` |
| `bench_id` | 72 | `u64` |
| `sink` | 73 | `str` |
| `received_bytes` | 74 | `u64` |
| `elapsed_us` | 75 | `u64` |
| `verify_us` | 76 | `u64` |
| `passed` | 77 | `bool` |
| `sequence_errors` | 78 | `u64` |
| `framing_errors` | 79 | `u64` |
