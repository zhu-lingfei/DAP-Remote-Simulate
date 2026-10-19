# v0.2 串口测速与波特率扫描

本功能只操作普通串口，不识别蓝牙模块，不发送 AT 指令。它测试当前连接条件下的传输稳定性和吞吐，并报告测试通过的最高候选设置。TCP 只用来验证工作流。

## 1. GUI 使用

1. 启动模拟器或实现本扩展的设备固件。
2. GUI 顶部填写连接端口（例如 COM8）和当前可通信波特率，连接。
3. 进入“串口测速”页。
4. 设置候选波特率、每轮 KiB、重复轮数、memory/sd；可选择本地文件，留空则生成伪随机数据。
5. “测当前连接”不改波特率，只测试一轮；“扫描波特率”逐档重复测试。
6. 测试期间可点击“停止测试”。单次串口写入和控制超时仍有等待上限，不保证瞬间停止。

与文件传输不同，测速数据不发逐块确认、不重传、不做数据块 CRC 校验。数据帧保留边界和帧头 CRC；完成后核对整份数据的接收长度与 SHA-256。正式文件传输的可靠性机制没有关闭。

仅设置本机串口不会让对端 UART 自动变速。对端必须独立匹配或具备自动识别；固定波特率设备通常只会有一个档位通过。本功能没有偷偷发送对端切换指令。

## 2. Python API

### `Client.benchmark`

```python
from dapremote.client import Client

with Client.open("COM8", baudrate=115200, timeout=2.0) as dap:
    result = dap.benchmark(size=64*1024, sink="memory", seed=1)
    print(result["passed"], result["bytes_per_s"])
    print(result["expected_sha256"], result["received_sha256"])

    # 文件路径存在时使用文件实际长度，size 参数不参与数据生成。
    saved = dap.benchmark(file_path="test.bin", sink="sd")
```

签名：

```python
benchmark(size=65536, file_path=None, sink="memory", seed=1,
          progress=None, cancel_event=None) -> dict
```

| 参数 | 说明 |
|---|---|
| size | 生成数据字节数，1..16 MiB；空测试不能测吞吐，因此拒绝 |
| file_path | 可选本地文件，大小 1 B..16 MiB；发送期间不得修改 |
| sink | memory：边收边哈希，不保存整个文件；sd：写内部临时文件，结束后同步并回读哈希 |
| seed | 生成数据种子；数据准备和预先计算哈希不计入发送时间 |
| progress | 在调用线程执行的回调；event 为 data 或 complete，不可直接操作 GUI 控件 |
| cancel_event | 可选 threading.Event；置位后在数据块边界发起取消 |

返回：

| 字段 | 含义 |
|---|---|
| bench_id | 本轮测试标识，u64 |
| state | PASSED / FAILED 等状态 |
| sink,size | 接收模式与期望长度 |
| sent_bytes,received_bytes | 电脑发送的源数据长度、设备实际接收长度，不含帧头 |
| sha256 | 设备实际数据 SHA-256，bytes |
| expected_sha256,received_sha256 | 方便显示的 64 字符哈希 |
| passed | 长度、哈希、序号、分帧检查全部通过才为 True |
| elapsed_us | 接收窗口耗时：BENCH_BEGIN 建立后到处理 BENCH_END，包括开始响应和结束控制到达时间，不含最终 SD 回读校验时间 |
| verify_us | 最终同步与哈希核对准备耗时，SD 模式包含回读 |
| bytes_per_s | received_bytes × 1,000,000 / elapsed_us，失败轮次也会有值，不可把失败吞吐当有效传输速度 |
| send_seconds | 电脑逐帧写入接口的耗时，数据可能仍在驱动/设备缓冲中，因此不作为链路吞吐结论 |
| total_seconds | 电脑从开始写数据到收到最终结果的耗时，包含结果响应和校验 |
| sequence_errors,framing_errors | 序号异常次数、设备解码器检测的坏帧数 |
| error | 失败诊断文本，不作为机器可读状态码 |

SHA-256 只能说明整份内容是否一致，不能单独推导误码率。序号异常帮助定位缺帧/重复帧，不修复数据；接收端按到达顺序哈希，不能因去重而掩盖重复数据。

测试失败（内容不一致、SD 写入错误）返回 passed=False；控制业务错误抛 RemoteError；链路异常抛 OSError/ConnectionError/RpcTimeout；取消抛 BenchmarkCancelled（RuntimeError 子类）。没有测试数据的自动续传或自动补发。

### `Client.scan_baudrates`

```python
with Client.open("COM8", baudrate=115200, timeout=2.0) as dap:
    summary = dap.scan_baudrates(
        [115200, 230400, 460800, 921600], rounds=3,
        size=64*1024, sink="memory", probe_timeout=0.5,
    )
    print(summary["highest_passed_baudrate"])
    print(summary["recommended_baudrate"])
    print(summary["restored"], summary["restore_error"])
```

完整参数：`baudrates, rounds=3, size=65536, file_path=None, sink='memory', probe_timeout=0.5, settle_s=0.1, progress=None, cancel_event=None`。

- 候选数 1..32，整数波特率 1..4000000，去重后升序；实际驱动可能拒绝某些值。
- rounds 为每档 1..20 轮，任何一轮失败则该档失败，不把一次偶然成功当稳定通过。
- probe_timeout 允许 0.05..10 秒，只用于每档 PING；PING 不重试，以免不匹配档位长时间阻塞。
- settle_s 允许 0..5 秒，是本机串口设置后的等待时间。
- 测试阶段控制请求仍使用客户端原来的超时与重试参数；数据从不重试。
- finally 中恢复原波特率、清空接收缓存、重置本地解码器并发送分隔符及 PING；超时、取消和无通过档位也走恢复。
- 恢复仅修改本机端口，不会把已失配或复位的对端配置修好。restored=False 时应重新连接并检查双方配置。
- 同一会话必须持续有效；如果你的设备在每次改变串口参数时复位，应由设备适配层解决，不能把这种扫描当作透明功能。

summary 字段：

| 字段 | 说明 |
|---|---|
| simulated | True 表示承载不支持实际设置波特率（如 TCP） |
| original_baudrate | 扫描前的本机串口设置；TCP 为 None |
| results | 每档结果：baudrate,baudrate_applied,rounds_requested,rounds_passed,rounds,passed,bytes_per_s,error |
| highest_passed_baudrate | 全部指定轮次通过的最高候选档位；TCP 或没有通过项时为 None |
| recommended_baudrate | 在通过档位中，吞吐达到实测最好值 90% 的最低档位，用来避免吞吐平台上无收益地提高设置；不是硬件安全认证 |
| cancelled | 是否中途取消；不足指定轮数的档位不算通过 |
| restored,restore_error | 是否恢复原配置并重新验证通信，以及错误原因 |

progress event 为 candidate、round、result、summary，在调用线程执行。取消返回保留已完成轮次的 summary，而不是丢掉所有结果。回调抛异常仍会尝试恢复原设置，然后传播异常。

这些值是**本次条件下的候选结果**。测试数据量、运行时长、RTT 负载、流控设置和接收任务负载都会影响结论。若使用小文件，控制往返占比高，不宜用其结果判断链路峰值。长时间验证应增加数据量和重复轮数。

## 3. CLI

```powershell
python -m dapremote --endpoint COM8 --baudrate 115200 --timeout 2 bench --size-kib 64 --sink memory
python -m dapremote --endpoint COM8 --baudrate 115200 baud-scan --rates 115200,230400,460800,921600 --rounds 3 --size-kib 64
python -m dapremote bench --file examples/blink.hex --sink sd
```

`--endpoint/--baudrate/--timeout` 在子命令前。bench 校验失败返回退出码 1；baud-scan 无通过项或恢复失败也返回 1。GUI 中开始测试后可停止；CLI 可 Ctrl+C，扫描的 finally 仍尝试恢复。

## 4. 线上协议扩展

能力字符串增加 `bench-raw-v1`。设备不支持 BENCH_BEGIN 时，应返回 UNSUPPORTED；客户端不会向不支持的设备先发送裸数据。

| 指令 | channel | 请求 | 响应 |
|---|---:|---|---|
| BENCH_BEGIN 0x0900 | 0 | size:u64,sha256:32B,sink:str | bench_id 与初始状态 |
| BENCH_DATA 0x0901 | 1 | 专用单向帧，见下文 | 无响应 |
| BENCH_END 0x0902 | 0 | bench_id | 状态、接收哈希和统计；校验失败是 OK 响应中的 passed=False |
| BENCH_STATUS 0x0903 | 0 | bench_id | 当前状态及统计 |
| BENCH_ABORT 0x0904 | 0 | bench_id | 释放临时资源，标记 CANCELLED；已结束的结果不改变 |

控制指令仍使用普通 REQUEST/RESPONSE、TLV 和全帧 CRC32C。新 tag 只能追加，编号见 REFERENCE.md。

### BENCH_DATA 专用帧

```
COBS(header20 || bench_id:u64 || sequence:u64 || data || header_crc32c:u32) || 00
```

普通头布局不变，但必须同时满足：kind=3、channel=1、flags=1、request_id=0、opcode=0x0901。session_id 必须匹配当前连接。

末尾 CRC32C **只覆盖 20 字节普通帧头**，不覆盖 bench_id/sequence/data。data 长度 1..4080 B（4096 B payload 减去 16 B 测试元数据）。BENCH_DATA_HEADER 及数据本身的错误由最后的长度/哈希/序号检查发现。任何普通业务指令带 flags=1 都被拒绝，不能借测速关闭删除、烧录或正式上传的 CRC。

发送端连续写帧，不等 ACK，不重传；END 前额外发送一个 00，帮助在末尾分隔符丢失时恢复边界。坏 COBS/帧头导致的数据丢失计入 framing_errors；整个数据 payload 的修改不被帧 CRC 拦截，由整文件 SHA-256 报告。

接收端以内存流式哈希或内部 SD 临时文件接收，最大声明长度 16 MiB，实际超额输入受限，不按报文声明无限分配内存。测试与活动文件事务、烧录互斥；RTT 可保持开启，用于带负载测试，但 GUI 扫描期间不轮询 RTT，GUI 队列可能溢出；SD RTT 输出可继续。

测试句柄属于当前会话，不跨重连恢复。断线、取消或 10 秒无有效数据时释放临时文件。最终结果只保留最近一轮，重复 END 在当前连接的响应缓存内安全；下一轮 BEGIN 会替换旧结果。SD 测试文件从不改名为正式文件，并在结束后删除。

## 5. 承载接口补充

SerialTransport 暴露 `supports_baudrate=True`、`baudrate`、`set_baudrate(value)` 和 `reset_input()`。自定义可扫描承载也需要提供这些属性；baudrate 必须能返回原来的正整数，便于恢复。

SocketTransport 暴露 `supports_baudrate=False`、`baudrate=None`。同样运行测试数据流，但不会声称实际应用了候选波特率。

本版只实现电脑→设备方向。反方向连续流测试、远端 UART 协商切换、长期自动调速、误码定位和生产用自适应窗口仍属于后续扩展。真实 UART 与固件尚需实机验证。
