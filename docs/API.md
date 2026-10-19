# Python API 与设备指令说明

版本：dapremote 0.2.0，线上协议主版本 1。示例在项目目录运行，Python 3.9+。新增测速 API 和单向无数据块 CRC 帧见 [BENCHMARK.md](BENCHMARK.md)；原文件上传 API 仍保持逐帧校验和停等确认。

## 1. 客户端生命周期

```python
from dapremote.client import Client
from dapremote.protocol import Op

with Client.open("tcp://127.0.0.1:8765", timeout=1.0, retries=5) as dap:
    print(dap.info)                    # HELLO 返回信息
    print(dap.call(Op.GET_CAPS))
    print(dap.call(Op.POWER_STATUS))
```

`Client.open(endpoint, baudrate=115200, timeout=1.0, retries=5)` 自动连接并握手。`endpoint` 为 `tcp://主机:端口` 或 `COM8`。`timeout` 是每次尝试的响应超时，`retries` 是首次发送之后的重试次数，允许 0..20。重试不换请求编号，不换参数。TCP/串口写超时另为 3 秒。连接被关闭时立即抛异常，不自动重连或重放业务操作。

`Client(transport, timeout=1.0, retries=5)` 用于自定义承载，之后必须调用 `hello()`。Transport 必须提供 `read(max_bytes, timeout)->bytes`、`write(data)`（完整发送或抛异常）、`close()`；读取超时返回 `b''`，连接断开抛 `ConnectionError`。不能把 EOF 当作读取超时。

客户端是同步 API，单次 RPC 通过锁串行化。一个设备的文件工作流应由一个所有者管理，**不要用多个线程同时调用 upload/download**。GUI 用一个工作线程提交操作，主线程只更新控件。

`close()` 或上下文退出只断开链路，不取消设备上的任务、文件传输或 RTT。

### 异常与不确定结果

| 异常 | 意义与处理 |
|---|---|
| `RemoteError` | 设备返回业务错误；读取 `.status` 和 `.fields`，不要解析错误文本 |
| `RpcTimeout` | 所有尝试超时；设备可能已经执行，先查文件或任务状态 |
| `ConnectionError` / `OSError` | 链路或本地文件错误；重新连接后恢复工作流 |
| `ResumeError` | 续传身份、文件版本、局部文件或设备 boot 不匹配，需显式处理 |
| `ProtocolError` | API 收到不符合约定的数据，停止当前工作流并诊断 |
| `FileExistsError` | 本地文件不允许覆盖，或无归属的 `.part` 文件已存在 |

GUI 文本仅供诊断；自动逻辑使用状态枚举。`OK` 与 `ACCEPTED` 都会作为正常字典返回；`ACCEPTED` 不代表任务完成。

## 2. 文件管理 API

```python
dap.mkdir("/firmware")
entries = dap.list_files("/firmware")
info = dap.stat("/firmware/app.hex")
dap.rename(info["path"], "/firmware/old.hex", info["version"])
dap.delete("/firmware/old.hex", info["version"])
```

| 方法 | 参数和返回 |
|---|---|
| `list_files(path='/')` | 自动分页，返回条目列表；目录变化时抛 `FILE_CHANGED`，由调用者决定重新列举 |
| `stat(path)` | 返回 `name,path,is_dir,size,version`；目录大小为 0，目录版本为空字符串 |
| `mkdir(path)` | 创建单层目录，父目录必须存在，已存在返回错误 |
| `rename(path,new_path,version)` | 不覆盖目的路径；普通文件需要匹配 SHA-256 版本；目录传空版本 |
| `delete(path,version)` | 普通文件需要匹配版本；目录传空版本且必须为空；不递归删除 |

路径为设备根目录下的 UTF-8 路径，推荐始终使用 `/firmware/app.hex`。最大 240 UTF-8 字节；禁止反斜线、冒号、空字符、`.`、`..`、`.dap` 保留组件，以及通过符号链接越出根目录。主机文件系统允许的名称不必然等于 FatFs 允许的名称，固件端还需实现对应限制。

## 3. 上传和断线续传

```python
result = dap.upload(
    "build/app.hex", "/firmware/app.hex",
    overwrite=False,
    resume_file="app-upload.json",
    progress=lambda done, total: print(done, total),
)
print(result["version"])  # 原文件 SHA-256 十六进制
```

`upload(local_path, remote_path, overwrite=False, resume_file=None, progress=None)`：

1. 计算本地文件大小和 SHA-256。
2. 新传输先执行 FILE_BEGIN，并在首块之前保存恢复记录。
3. 按 512 B 发送，确认连续字节偏移。
4. FILE_END 由设备同步、回读、校验并提交。
5. 成功后删除恢复记录，返回 `path,size,version,transfer_id`。

同一次 API 调用中的重试自动进行。断线后**创建新的 Client，再用相同源文件、设备路径、overwrite 和 resume_file 调用 upload**。恢复记录只存元信息；续传偏移以 FILE_STATUS 为准。源文件在上传期间不得被其他程序修改。

恢复记录包含 `direction,transfer_id,path,size,sha256,overwrite,boot_id,device_id`。远端与本地哈希均须吻合。FILE_END 已提交但回复丢失时，也能由该记录恢复到成功结果。

`resume_file=None` 不落本地恢复文件，调用者仍可直接使用底层 FILE_* 指令管理传输。CLI 默认在源文件旁创建 `<文件名>.dap-upload.json`。

如果 FILE_BEGIN 在设备执行后、回复前直接断线，客户端尚未得到 transfer_id，恢复记录也尚未建立。此时执行 `DEV_STATUS` 查看活动传输，核对路径、大小和 SHA-256 后，由调用者选择通过底层 API 接管或执行 FILE_ABORT；不要盲目再创建一个传输。超时未处理的活动传输在模拟器中 600 秒后失效。

设备进程重启后旧文件传输失效。旧恢复记录不会被静默删除或自动重传：明确检查设备状态后，再清理本地记录并重新开始。

## 4. 取回文件

```python
dap.download("/firmware/app.hex", "retrieved.hex", overwrite=False,
             progress=lambda done, total: print(done, total))
```

`download(remote_path,local_path,overwrite=False,progress=None)` 使用 `<目标>.part` 与 `<目标>.dap-download.json`。每块落盘后更新偏移和前缀哈希；重连时自动识别并验证恢复文件。完整 SHA-256 通过、FILE_END 确认后才替换最终文件。返回 `path,size,version`。

相同 API 再次调用即可续传。目标已存在时必须显式 `overwrite=True`。失败时保留恢复文件，便于诊断或续传。不要只删除其中一个文件；源文件或 boot 已变化时，要显式放弃旧恢复状态。

恢复文件提交并不是跨两个本地文件的掉电原子事务。例如最终文件已替换但恢复记录删除前电脑掉电，应核对最终文件哈希，再清理残留记录。

## 5. 检查与异步烧录

```python
import uuid

version = dap.stat("/firmware/app.hex")["version"]
check = dap.check_hex("/firmware/app.hex", version)
token = uuid.uuid4().hex
# 真实 GUI 应在发请求之前，把 token、path、version、run_after 保存到任务历史。
job = dap.start_flash("/firmware/app.hex", token, version=version, run_after=False)
result = dap.wait_job(job["job_id"], timeout=30, progress=print)
if result["state"] != "DONE":
    print("烧录没有成功：", result)
```

| 方法 | 说明 |
|---|---|
| `check_hex(remote_path,version=None)` | 不提供版本时先 stat；检查 HEX 记录、重叠、EOF、地址范围；返回 `total_bytes,min_address,max_address,target_id`，max 为包含边界 |
| `start_flash(remote_path,operation_token,version=None,run_after=False,target_id=None)` | token 必填；默认查询 TARGET_INFO；返回任务状态字典 |
| `job_status(job_id=None,operation_token=None)` | 至少提供一项；两项同时提供时 job_id 优先 |
| `wait_job(job_id,timeout=30,progress=None)` | 每 100 ms 查询，返回终态字典；任务失败也作为状态返回，调用者必须检查 `state` |

`wait_job` 超时不会取消任务。重连后使用 `job_status(operation_token=原token)` 查询。如果需要重发 FLASH_START，必须使用**相同 token 和相同参数**。参数变化返回 REQUEST_CONFLICT；一个新的烧录操作应生成新 token。

模拟器保留最多 128 条任务，不自动淘汰去重记录；达到上限拒绝新任务。记录保存在模拟 SD 的 `.dap/jobs.json`，进程重启后活动任务标记 INTERRUPTED。不要在任务进行中手动删除内部文件。任务记录被外部删除后，不再具有该操作的跨重启去重保证。

模拟目标：基址 `0x08000000`，Flash 256 KiB，扇区 1 KiB，只擦除涉及扇区。HEX 类型 00/01/02/03/04/05 受支持，03/05 起始地址只验证不用于替代复位向量。`run_after` 模拟复位阶段，不执行真实目标代码。Flash 数据保存在 `.dap/target.bin`。

## 6. RTT API

```python
stream = dap.start_rtt(destinations=3, sd_path="/capture-001.log",
                       rate=2048, sd_max_bytes=1024*1024)
sid = stream["stream_id"]
batch = dap.read_rtt(sid, length=512)
print(batch["data"], batch["dropped_bytes"], batch["sd_error"])
dap.call(Op.RTT_WRITE, stream_id=sid, data=b"status\n")
dap.stop_rtt(sid)
```

`destinations` 为位图：`1=GUI`、`2=SD`、`3=GUI+SD`。本版一次只允许一个活动流，仅通道 0；`rate` 是模拟源的生成速率，不是实际目标 RTT 的速率配置。允许 1..100000 B/s。GUI 缓存默认 8192 B，满时丢新数据并计数；SDK 返回原始 bytes，调用方自行决定编码。

`read_rtt(stream_id,length=512)` 允许读取 1..1024 B，返回 `data,sequence,channel_id,timestamp_ms,dropped_bytes,sd_bytes,gui_bytes,sd_error,destinations,sd_path,state,stream_id`。sequence 为本次取出的非空批次编号，从 0 开始；空读不推进。timestamp_ms 是设备 boot 后读取响应时的采集服务时间，并非目标日志产生时间。`gui_bytes` 表示此前已从设备队列取出的字节数，不能证明 GUI 已经展示。

RTT_READ 响应丢失时同请求重试会返回缓存批次；跨连接断开时最近被取出但未送达的批次可能丢失，`dropped_bytes` 仅统计设备 GUI 队列溢出，不保证能计入所有链路或目标端损失。本版不是可靠消息留存服务；需要完整日志时使用 SD 输出。

RTT_WRITE 最多 512 B，返回实际 `written_bytes`；模拟目标立即消费输入，仅保留最后一批用于诊断。同会话重试去重；跨连接不自动重发终端输入。

SD 输出使用新建文件、不覆盖；达到 `sd_max_bytes` 或写入错误时停止该输出并保留 `sd_error`，GUI 输出继续。日志上限包含文件头和记录头；本版没有日志轮转。RTT_STOP 后缓存可继续读取。断线不停止流，DEV_STATUS 返回 stream_id 可重新接入。每个模拟器 boot 最多保留 32 条流记录。

## 7. 底层指令参数表

所有下列指令使用 `dap.call(Op.名称, **参数)`，返回字典；FILE_DATA 例外，使用 `write_block`。`?` 表示可省略。枚举值与 tag 见 [REFERENCE.md](REFERENCE.md)。

| 指令 | 请求参数 | 主要返回值 |
|---|---|---|
| HELLO | protocol=1；由 hello 自动执行 | boot_id,protocol,device_id；session 在帧头 |
| PING | 无 | boot_id |
| GET_CAPS | 无 | protocol,max_payload,block_size,features |
| DEV_INFO | 无 | device_id,boot_id,protocol,target_id |
| DEV_STATUS | 无 | SD、电池、media_epoch、活动 transfers 和 streams |
| POWER_STATUS | 无 | battery_mv,battery_percent,charging,external_power,can_flash |
| FS_INFO | 无 | capacity,free,media_epoch |
| FS_LIST | path?='/',cursor?=0,limit?=8,version?='' | entries,version,next_cursor；0 表示结束 |
| FS_STAT | path | name,path,is_dir,size,version |
| FS_HASH | path,version | stat 结果及 sha256 原始 32 B |
| FS_MKDIR | path | 空字典 |
| FS_RENAME | path,new_path,version（普通文件必需） | 空字典 |
| FS_DELETE | path,version（普通文件必需） | 空字典 |
| FILE_BEGIN 上传 | direction='upload',path,size,sha256,overwrite?=False | transfer_id,state,next_offset,checkpoint_offset,size,sha256,block_size,direction |
| FILE_BEGIN 取回 | direction='download',path,version | 同上 |
| FILE_DATA | 二进制 transfer_id,offset,data_len,data | next_offset,checkpoint_offset |
| FILE_READ | transfer_id,offset,length?=512 | offset,data,next_offset |
| FILE_STATUS | transfer_id | FILE_BEGIN 的返回字段 |
| FILE_END | transfer_id | COMMITTED 状态及传输字段 |
| FILE_ABORT | transfer_id | 空字典；已提交文件不能回滚 |
| TARGET_INFO | 无 | target_id,base_address,flash_size |
| FLASH_CHECK | path,version,target_id? | HEX 检查结果 |
| FLASH_START | path,version,operation_token,run_after?=False,target_id? | ACCEPTED + job_id 等任务字段 |
| JOB_STATUS | job_id 或 operation_token | job_id,operation_token,state,phase,completed_bytes,total_bytes,error,run_after |
| JOB_LIST | cursor?=0,limit?=8 | jobs,next_cursor；不要在列表变化时依赖索引稳定 |
| JOB_CANCEL | job_id 或 operation_token | 任务状态；终态任务不改变 |
| RTT_START | destinations,channel_id?=0,rate?=1024,rtt_capacity?=8192,sd_path?,sd_max_bytes?=1048576 | stream_id,sd_path,destinations,disconnect_policy |
| RTT_READ | stream_id,length?=512 | 数据及统计，见上文 |
| RTT_STATS | stream_id | 流状态及统计，不移除数据 |
| RTT_STOP | stream_id | 流状态及统计 |
| RTT_WRITE | stream_id,data | written_bytes |
| SIM_CONFIG | 下表允许字段的任意非空子集 | 空字典，仅模拟器支持 |

FILE_READ 读取请求本身就是流控；下一请求表示主机已经处理前一块。服务器允许重读合法偏移，不使用响应中的 max 偏移替代主机已落盘偏移。FILE_END 对取回只关闭事务，最终文件哈希由电脑校验。

### SIM_CONFIG

| 字段 | 类型 | 说明 |
|---|---|---|
| battery_percent | u64，0..100 | 模拟电量 |
| battery_mv | u64 | 模拟电压 |
| charging,external_power | bool | 充电和外接电源状态 |
| sd_ready | bool | 模拟卡可用性；有活动文件事务时禁止切换 |
| io_fail | bool | 注入文件服务 I/O 错误 |
| verify_fail | bool | 让模拟烧录校验失败 |
| capacity | u64 | 模拟 SD 总容量，可缩小以测试空间不足 |

20% 是模拟器测试阈值，不是实际电池策略。运行中电量变低不会中断模拟烧录。实际硬件必须以实测电源预算实现该策略。

## 8. 状态与资源占用

文件状态：`ACTIVE → COMMITTED / ABORTED / EXPIRED`。FILE_END 校验失败时仍保持 ACTIVE，以便查询和主动放弃，不能当作可烧录文件。

任务状态：`QUEUED → RUNNING → DONE / FAILED / CANCELLED / INTERRUPTED`；phase 为 CONNECTING、ERASING、PROGRAMMING、VERIFYING、RESETTING 等。进度 completed_bytes/total_bytes 表示编程阶段字节数，不代表整体耗时百分比。

本版取消在模拟器锁保护的阶段边界立即生效；不模拟真实 Flash 控制器不可中断区间。目标可能已有部分擦写，取消后不自动运行。

活动文件事务、SD RTT 日志、烧录文件受业务锁保护。烧录与 RTT 互斥；文件传输和 RTT 可以同时运行。一个设备只允许一个活动文件事务；活动事务闲置 600 秒过期。每次 boot 最多保留 256 条传输记录，达到上限需重启模拟器。生产固件需要设计可查询的保留期限和清理策略。
