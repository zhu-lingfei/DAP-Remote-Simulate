# DAP Remote v0.2

用于开发蓝牙串口 DAPLink 远程交互系统的 Python 参考实现。现在不需要 DAP 硬件即可运行：**GUI / CLI → 真实二进制协议 → TCP 设备模拟器**。

支持文件上传与取回、断线续传、SD 管理、HEX 检查、异步模拟烧录、任务查询与取消、RTT 回传 GUI / 写入 SD / 同时输出、电池状态，以及确定性故障注入。

## 快速运行（Windows）

需要 Python **3.9+**，建议 3.12。核心和测试不依赖第三方库。当前电脑默认 `python` 为 3.6，请使用项目提供的 `run.ps1`，它会优先选择项目虚拟环境或本机已有的 Codex Python 3.12。

在项目目录打开两个 PowerShell 终端。终端 1：

```powershell
.\run.ps1 sim --root sim_sd --port 8765
```

终端 2：

```powershell
.\run.ps1 info
.\run.ps1 upload examples/blink.hex /blink.hex
.\run.ps1 ls
.\run.ps1 check /blink.hex
.\run.ps1 flash /blink.hex --token review-flash-001 --wait
.\run.ps1 download /blink.hex retrieved.hex
```

如果系统阻止运行脚本，可使用 `powershell -ExecutionPolicy Bypass -File .\run.ps1 info`，或用你自己的 Python 3.9+ 执行 `python -m dapremote info`。

启动 GUI 测试台：

```powershell
.\run.ps1 gui
```

GUI 使用标准库 Tkinter，提供文件管理、上传/续传、取回/续传、HEX 检查、烧录、任务令牌查询、RTT 输出选择和设备状态。通信在工作线程运行。Tkinter 是第一版可直接运行的测试前端；协议与客户端不依赖 GUI，后续可替换为 PySide6。GUI 中的本地文件选择、覆盖、删除采用显式操作。

GUI 不会自动启动模拟器，请先运行终端 1。断开 GUI 不等于停止设备的烧录或 SD 日志。GUI 的烧录编号由程序自动生成并保存，用户无需填写或保留；重新连接相同端口与设备时，会查询上次任务。

GUI 使用可读的中文操作日志，不再显示响应 JSON。错误以红色 ERROR 标记，用户操作失败默认弹窗，可通过“错误弹窗”开关关闭；后台错误不会连续弹窗。RTT 显示普通文本，二进制字节转义显示。界面说明见 [GUI.md](docs/GUI.md)。

## RTT 示例

```powershell
.\run.ps1 rtt --dest gui --seconds 3
.\run.ps1 rtt --dest sd --sd-path /session-1.log --seconds 3
.\run.ps1 rtt --dest both --sd-path /session-2.log --seconds 3
```

SD RTT 文件为带通道与时间信息的二进制记录，不能直接当作 UTF-8 文本；格式见 [协议文档](docs/PROTOCOL.md)。SD 文件默认不覆盖，重复测试请换路径或先删除旧日志。

## 故障测试

停止旧模拟器后，用以下配置启动：

```powershell
.\run.ps1 sim --root sim_sd --fault drop:FILE_DATA:1 --fault corrupt:FILE_DATA:3 --fragment-size 7 --trace trace.jsonl
```

再进行上传。设备第一次已接收数据但不返回 ACK；第三次 FILE_DATA 响应 CRC 损坏。客户端自动使用同一请求编号重试。

`--fault` 支持 `drop / corrupt / truncate / disconnect`，格式为 `动作:指令名:第几次响应`，计数跨连接且包含重试。同一次响应只执行第一条匹配规则。

```powershell
.\run.ps1 test
```

测试使用临时目录和本机随机 TCP 端口，不需要运行常驻模拟器。覆盖正常传输、边界大小、坏帧、重复块、丢 ACK、断线恢复、文件提交、任务去重、模拟 Flash 内容、RTT 分流与资源冲突。

## 连接真实串口

在你使用的 Python 环境安装可选依赖：

```powershell
python -m pip install ".[serial]"
python -m dapremote --endpoint COM8 --baudrate 115200 info
```

**需要设备端实现同一协议后才能使用。** 当前并没有 DAP 固件，也没有验证实际串口或蓝牙模块。`SerialTransport` 支持配置 RTS/CTS，CLI 默认关闭；软件流控关闭。

## 串口测速与波特率扫描

GUI 新增“串口测速”页：连接后选择“测当前连接”或“扫描波特率”。每轮可使用生成数据或本地文件，接收端可选择内存或 SD；测试数据不逐块 ACK、不重传、不校验数据块 CRC，最后比对长度与 SHA-256。控制指令和数据帧头仍受 CRC 保护。测试不会发布可烧录文件。

```powershell
python -m dapremote --endpoint COM8 --baudrate 115200 bench --size-kib 64 --sink memory
python -m dapremote --endpoint COM8 --baudrate 115200 baud-scan --rates 115200,230400,460800,921600 --rounds 3
python -m dapremote bench --file examples/blink.hex --sink sd
```

最后一条默认连接 TCP 模拟器。TCP 扫描仅验证流程，不设置物理波特率，不给出真实串口最高波特率。扫描**仅设置本机串口**；不发送蓝牙 AT 指令、不改变对端配置。对端需自行匹配或支持自动识别，否则仅匹配档位能通信。扫描结束会尝试恢复原波特率并 PING 确认；恢复失败会明确报告。

API、线上帧格式、结果解释和限制见 [BENCHMARK.md](docs/BENCHMARK.md)。

## 文档与目录

- [Python API 与指令语义](docs/API.md)：参数、返回值、异常、续传和任务调用示例。
- [线上协议](docs/PROTOCOL.md)：帧布局、TLV、CRC、状态机及固件移植约定。
- [编号和字段表](docs/REFERENCE.md)：全部 opcode、状态码和 TLV tag。
- [测试与验收](docs/TESTING.md)：故障场景、测试边界和硬件验证路径。
- [串口测速 API 与协议](docs/BENCHMARK.md)：无数据块校验的连续传输、波特率扫描和恢复机制。
- `dapremote/client.py`：可供其他 Python 程序直接调用的 API。
- `dapremote/simulator.py`：设备服务与故障注入。
- `tests/vectors.json`：冻结的帧向量，供后续 C 实现对照。

## 当前边界

- 正式文件采用 512 B 停等传输；每个设备同时只允许一个活动文件传输。
- 支持设备进程不重启时的断线续传；不支持设备掉电后的文件续传。临时上传文件会在模拟器重新启动时清理。
- RTT 和任务状态使用**轮询请求**，本版没有主动 EVENT / STREAM 帧推送；GUI 忙于传文件时 RTT 在设备侧有界缓存，SD 输出继续。
- 仅模拟一个目标和 RTT 通道 0；Flash 操作不是真实 SWD/JTAG 烧录。
- 模拟 Flash 擦除涉及的 1 KiB 扇区，不默认整片擦除。
- 模拟器只监听本机 IPv4 回环；没有应用认证、加密或生产设备权限管理。
- 本机文件替换和任务日志不等同于真实 FatFs / SD 的掉电保证。
- 文件版本为原始字节的 SHA-256；用户从操作系统直接改动模拟 SD 会绕开业务锁，版本检查可发现相关变化，但无法模拟全部真实并发情况。

这是可运行的开发基线，不应直接当作量产固件协议实现的安全或掉电可靠性证明。
