# DAP Remote v0.1 线上协议

本文件描述原有可靠控制与文件协议。v0.2 新增测速扩展：kind=3、flags=1 的 BENCH_DATA 仅保护帧头，不校验数据块 CRC，详见 [BENCHMARK.md](BENCHMARK.md)。普通 REQUEST/RESPONSE 帧保持原格式和全帧 CRC。保留的 channel 2 尚未启用；未实现 EVENT/STREAM 推送、认证、滑动窗口或掉电文件续传。

## 1. 帧与边界

```
wire = COBS(header || payload || crc32c_le) || 00
```

| 解码后偏移 | 字段 | 类型 | 说明 |
|---:|---|---|---|
| 0 | version | u8 | 固定 1 |
| 1 | kind | u8 | 1 REQUEST，2 RESPONSE |
| 2 | channel | u8 | 0 控制，1 文件数据，2 保留，3 RTT |
| 3 | flags | u8 | 固定 0 |
| 4 | session_id | u64 LE | HELLO 请求为 0，成功响应携带新会话 |
| 12 | request_id | u32 LE | 非零，单连接内严格递增，不允许回绕 |
| 16 | opcode | u16 LE | 指令编号 |
| 18 | payload_len | u16 LE | 0..4096 |
| 20 | payload | bytes | 指令体 |
| 20+payload_len | CRC32C | u32 LE | 覆盖前面全部解码后字节 |

原始帧固定开销 24 B，最大原始帧长 4120 B。最大 COBS 累积长度保守限制为 `MAX_RAW + MAX_RAW//254 + 2`，超过后一直丢弃到下一个 00。空分隔符忽略；坏 COBS、非法头、长度不符或 CRC 错误均丢弃，不执行、不根据坏帧编号回包。

FILE_DATA 和 FILE_READ 使用 channel=1；RTT_* 使用 channel=3；其余指令使用 channel=0。响应沿用请求的 channel。CRC 合法但已知指令通道不对，返回 INVALID_ARGUMENT。

发送端不能假设 write 或 read 边界等于帧边界。应用不依赖蓝牙一次透传长度；接收支持任意切块和多帧粘连。若链路在分隔符之前截断，接收端可能丢弃后续第一个被拼接的帧，后续重试应继续恢复；不能直接按接收空闲时间当作成功帧。

### CRC32C

CRC-32C / Castagnoli：正常多项式 `0x1EDC6F41`，反射实现 `0x82F63B78`，init=`FFFFFFFF`，refin/refout=true，xorout=`FFFFFFFF`。`123456789` 的结果为 `E3069283`，线上按 `83 92 06 E3` 发送。

COBS 与 CRC 不提供认证或保密。真实无线产品需要另行设计身份绑定、权限与防重放。

## 2. TLV 控制体与响应

除 FILE_DATA 外，请求体为一串 TLV：

```
tag:u16 LE | length:u16 LE | value:length bytes
```

固定字段类型由 tag 确定，不在线上传类型名字。见 [完整字段表](REFERENCE.md)。

| 类型 | 编码 |
|---|---|
| str | 严格 UTF-8，无终止 NUL |
| bytes | 原始字节，允许 00/FF |
| u64 | 固定 8 B 小端；不允许负值 |
| bool | 长度 1，且只能 00 或 01 |
| records | 重复的 `record_length:u32 LE + record_TLV_bytes`，无额外条目数 |

TLV 字段顺序不影响业务含义，字段重复会被拒绝。未知 tag：最高位未置位时可忽略，最高位置位时返回 INVALID_ARGUMENT。本版已定义的 tag 均不设置最高位。必填项缺失、长度非法、UTF-8 非法均返回 INVALID_ARGUMENT。

控制响应 payload：

```
status:u16 LE | TLV fields
```

OK=0、ACCEPTED=1。其他状态为错误，可附 `message` 和结构化字段，例如 OFFSET_MISMATCH 携带 next_offset。FILE_READ 的响应也采用 TLV，data 字段承载原始文件字节；本版响应不是另一种裸数据布局。

SHA-256 在线上 sha256 字段为 32 原始字节；version 字段为 64 个小写十六进制字符。两者均针对原始文件字节，包括 HEX 的换行，不做规范化。目录 FS_STAT 的 version 为空字符串，FS_LIST 的 version 是目录快照摘要，语义不同。

例：`path='/a',size=3` 的控制体：

```
02 00 02 00 2F 61
05 00 08 00 03 00 00 00 00 00 00 00
```

本实现中 FS_STAT 和 FS_LIST 为了检测外部修改会实际计算文件 SHA-256，因此列表不是低成本操作；实际固件可维护版本令牌，但必须同时更新协议版本语义，不能静默替换现有哈希定义。

## 3. 会话与去重

1. 新连接必须发送 HELLO，session=0、channel=0、protocol TLV=1。
2. 设备返回非零随机 session_id 和本次 boot_id。同一连接重复相同 HELLO 返回同一 session。
3. 一条连接只允许一次 HELLO 工作流，重连建立新会话。其他连接的 session 不可挪用。
4. 同一会话同一 request_id、相同 channel/opcode/payload，返回原始业务响应。
5. 同一 request_id 改变参数，返回 REQUEST_CONFLICT。
6. 最近 256 条响应缓存在该连接内；已淘汰且 request_id 不大于已处理最大编号时，返回 RESULT_UNKNOWN，绝不重新执行。

响应缓存要求**重试时 TLV 顺序也保持不变**，因为缓存比较的是原始请求字节。客户端每次 RPC 只编码一次。

连接断开立即丢失该连接的 request 缓存，但文件事务和 RTT 流在设备进程内继续存在。FLASH_START 使用独立的 operation_token 与持久化任务记录完成跨连接去重。其他有副作用的命令不允许在重连后盲目重放，尤其 RTT_WRITE。

超时重传只发生在当前连接，连接错误不会自动重连。旧请求不能跨新会话直接重试。查询结果未知不等于操作没有执行。

## 4. 文件状态机

上传：

```
FILE_BEGIN → ACTIVE → FILE_DATA* → FILE_END → COMMITTED
                    └→ FILE_ABORT → ABORTED
                    └→ 闲置 600 秒 → EXPIRED
```

FILE_DATA 请求体（不是 TLV）：

| 偏移 | 字段 | 类型 |
|---:|---|---|
| 0 | transfer_id | u64 LE |
| 8 | offset | u64 LE |
| 16 | data_len | u16 LE，1..512 |
| 18 | data | 与 data_len 完全相同的原始字节 |

offset 是原文件字节偏移，不能使用 HEX 目标地址。`offset+data_len` 必须在文件大小内，C 实现应先做溢出检查。

- offset 等于连续已接受偏移：检查写入结果，成功后推进 next_offset。
- 整块处于已接受范围：回读比对完全一致才返回当前 next_offset；不同则 REQUEST_CONFLICT。
- offset 超前，或块跨过当前连续边界：返回 OFFSET_MISMATCH。
- CRC 错误：没有 ACK；发送方超时重传同一帧。
- 空文件不发 FILE_DATA，直接 FILE_END。

FILE_END：验证长度、同步、回读 SHA-256，确认目的文件版本没有被外部改变，再替换目的路径。哈希错误不提交。即使 FILE_END 响应丢失，FILE_STATUS 仍可返回 COMMITTED，重复 FILE_END 安全。

`next_offset` 是设备运行中的接受进度，`checkpoint_offset` 在本版始终为 0，因为没有实现可承诺的设备掉电恢复。普通数据 ACK 不代表 SD 断电持久化。上传源文件不允许修改；下载源文件版本固定，设备每块验证版本。

取回：FILE_BEGIN(direction=download) 返回稳定版本和大小；主机重复 FILE_READ(offset,length<=512)，落盘成功后再请求下一块。主机校验整文件哈希，然后 FILE_END 结束事务并提交本地文件。

活动事务受文件锁保护。模拟器重启后上传临时文件被清理，旧 transfer_id 返回 NOT_FOUND；本地恢复记录 boot_id 不匹配时必须显式处理。

对于真实 FatFs，建议临时文件使用内部保留目录，并设计版本化提交记录。PC 上 os.replace 的行为不能当作 FAT 的掉电原子保证。

## 5. 烧录任务与电池

FLASH_START 先检查完整文件和目标，持久化操作记录后才返回 ACCEPTED。任务轮询返回状态和阶段，不发送主动完成事件。

```
QUEUED → CONNECTING → ERASING → PROGRAMMING → VERIFYING
                                              ├→ RESETTING → DONE
                                              └→ DONE
```

终态还包括 FAILED、CANCELLED、INTERRUPTED；任一终态都不得误当作 DONE。

operation_token 长度 1..80 字符，同 token 参数不一致返回 REQUEST_CONFLICT。跨重连查询推荐 JOB_STATUS(operation_token)。记录保存到 .dap/jobs.json；启动发现非终态记录时标记 INTERRUPTED。 journal 损坏时启动失败，不默默丢弃记录后允许重复烧录。

本版电量阈值仅用于模拟：外接电源或电量>=20%允许开始；运行中继续。实际产品需要测定擦除峰值电流、最低电压、剩余工作时长与不可中断区间。

## 6. RTT 与日志文件

destinations 位 0 表示 GUI，位 1 表示 SD。一次源数据采集分别投递到两个输出；GUI 缓存溢出计数，不拖住 SD 输出。SD 错误停用该输出并报告，GUI 可继续。GUI 断线后两个输出的策略不自动改变。

RTT 为拉取模式：RTT_READ 每次取出有限数据，响应包含非空批次序号。缓存丢新数据，默认 8192 B。源数据包含 00/FF 测试字节，不保证 UTF-8。

SD 文件头为 ASCII `DRT1`；之后记录：

```
timestamp_ms:u64 LE | channel_id:u16 LE | data_len:u16 LE | raw_data
```

timestamp_ms 是 DAP 采集服务时间。日志没有整文件 footer；读取器遇到截断记录应报告不完整，不能把尾部垃圾当作有效数据。空日志可能是零字节文件，因为头在首条记录到达时写入。

本版最多单活动流、通道 0、日志不轮转。SD 输出限额包含记录头。取回日志前先 RTT_STOP，释放业务锁并形成静态版本。

## 7. 向 FreeRTOS 移植

- UART DMA/ISR 仅入环形缓冲，解析在线程中完成。
- 先限制帧长，再解析头和 TLV；固定内存上限，禁止依据信任前的长度直接分配。
- 文件服务独占 FatFs 对象，所有写入检查返回码和实际字节数。
- SWD 管理器序列化目标访问；本版烧录和 RTT 互斥。
- 单 TX 调度器序列化完整帧。优先发送控制响应；有界队列和背压不能省略。
- request 缓存及最高已处理编号需要按 session 管理；operation_token 是另一层去重。
- 每个字段 tag、状态码、op 编号对应 REFERENCE.md；不使用编译器裸结构体布局发送帧，逐字段编码以避免对齐和字节序问题。
- 复用 tests/vectors.json 检验 C 实现，之后加入 SD、SWD 与实际蓝牙压力测试。
