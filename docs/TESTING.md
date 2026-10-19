# 测试与验收

## 自动测试

```powershell
.\run.ps1 test
```

或使用 Python 3.9+：

```text
python -m unittest discover -v
```

没有 pytest 或硬件依赖。测试使用 TemporaryDirectory、本机 TCP 临时端口；通过真正的编码帧收发，不直接用函数返回值冒充链路。关闭并重新创建客户端用于测试新会话与断线恢复。

| 类别 | 覆盖内容 |
|---|---|
| 帧 | CRC32C 标准校验向量、COBS 已知向量、固定 TLV 字节、冻结整帧向量 |
| 边界 | 0/1/511/512/513/8193 字节文件、4096 B payload、逐字节分片 |
| 解码恢复 | CRC 错误、截断、超长垃圾、粘包、重新同步 |
| 指令 | 坏请求不执行、同编号改参冲突、旧会话拒绝 |
| 传输 | 丢 ACK、坏 ACK、重复块、超前偏移、错误 SHA-256 不发布 |
| 续传 | 数据已执行后断线、取回断线、提交回复丢失、本地文件变化拒绝 |
| 文件服务 | 分页、过期目录版本、路径越界、保留目录、版本条件删除/改名 |
| SD | 空间不足、I/O 故障、未就绪、旧文件保留 |
| 烧录 | 真实 HEX 解析、模拟 Flash 字节、保留未涉及扇区、断线继续、token 去重 |
| 任务 | 低电量拒绝、取消、校验失败、重启标记中断 |
| RTT | GUI/SD/二者、原始二进制、缓存溢出、SD 故障、断线继续、与传输并行 |

随机分片测试固定种子为 20261018，便于复现。故障规则按响应次数精确触发，不依赖随机“碰巧出错”。

## 手工故障实验

以下为独立实验；每次先停止之前的模拟器。不要同时启动两个进程使用同一模拟 SD 目录。

### 丢数据 ACK / 损坏响应

```powershell
.\run.ps1 sim --root sim_sd --fault drop:FILE_DATA:1 --fault corrupt:FILE_DATA:3 --fragment-size 1
.\run.ps1 upload examples/blink.hex /blink.hex
```

预期上传成功、内容不重复。对于小于 512 B 的 blink.hex，第三次 FILE_DATA 可能不会触发，若要观察所有规则请使用几 KiB 以上的文件。

### 写完第二块后断线

```powershell
.\run.ps1 sim --root sim_sd --fault disconnect:FILE_DATA:2
.\run.ps1 upload bigger.bin /bigger.bin
# 首次报连接断开，保留 bigger.bin.dap-upload.json；再次执行原命令。
.\run.ps1 upload bigger.bin /bigger.bin
```

预期从设备确认的偏移恢复，最终 SHA-256 相同。规则计数跨连接，仅在指定次数触发。

### 开始烧录后响应丢失

```powershell
.\run.ps1 sim --root sim_sd --fault disconnect:FLASH_START:1
.\run.ps1 upload examples/blink.hex /blink.hex
.\run.ps1 flash /blink.hex --token same-operation-001
.\run.ps1 job --token same-operation-001
```

预期只有一个任务，任务继续完成。需要重试开始操作时使用相同 token、路径、版本和选项；新 token 表示新的烧录请求。

### 电池、SD 和校验故障

PowerShell 中通过 Python API 注入，避免不同 shell 对 JSON 引号的处理差异：

```python
from dapremote.client import Client
from dapremote.protocol import Op
with Client.open() as dap:
    dap.call(Op.SIM_CONFIG, battery_percent=5)
    # 此时 FLASH_START 应返回 POWER_INSUFFICIENT。
    dap.call(Op.SIM_CONFIG, external_power=True, verify_fail=True)
    # 此后新烧录任务应在 VERIFYING 阶段失败。
```

## GUI 检查清单

GUI 代码随核心一起做语法编译；自动测试不创建桌面窗口。运行 `.\run.ps1 gui` 后手工检查：

1. 模拟器未启动时连接失败有提示，界面不冻结。
2. 正常连接、目录刷新、中文文件名、上传与取回。
3. 断线后点击断开再连接，用同一个文件重新发起续传。
4. RTT 三个输出选择、二进制转义显示、停止后日志可取回。
5. 烧录状态更新、操作令牌查询、失败任务显示。
6. 传输期间界面仍能重绘；关闭窗口不自动停止设备任务。

当前 GUI 是功能性测试台，不包含生产级任务历史、自动重连、日志轮转或多设备管理。

## v0.2 测速测试补充

`tests/test_benchmark.py` 增加内存/SD 最终哈希、实际源文件、仅帧头 CRC、数据损坏/丢失/截断不重传、END 回复丢失、取消释放资源、SD 故障、扫描参数应用、驱动拒绝、波特率失配、原设置恢复、恢复失败以及 TCP 模拟标签。

RateTransport 是经过实际 TCP 协议交换的串口设置模型，用来测试扫描逻辑，不代表真实串口性能。冻结的旧协议向量继续通过，普通可靠文件传输不会因测速扩展关闭 CRC。

桌面环境可运行以下隐藏窗口集成检查，构建真实 Tk 控件、连接临时模拟器并完成测速和模拟扫描：

```powershell
python scripts/gui_smoke.py
```

它不连接真实串口，不展示窗口，不替代最终的屏幕布局和实机测试。

GUI 可读性回归新增 `tests/test_gui_text.py`：检查结果不被字典序列化、中文错误提示、级别、失败测速不显示有效吞吐、空推荐档位及跨块 UTF-8 / 二进制显示。隐藏窗口检查另外覆盖真实日志 ERROR 颜色、弹窗开关、后台错误不弹窗、任务进度更新不重复刷日志、实际 RTT 文本显示和清空日志。

一键烧录回归 `tests/test_gui_flash.py` 使用真实模拟器验证连续两次明确操作创建两个任务、活动任务不重复启动、发送前保存恢复记录、超时或断线后查询同一任务、GUI 控制器重建恢复、拒绝请求可修正后重试、恢复文件损坏或不可写时不启动任务。隐藏窗口还验证没有编号输入框、实际点击烧录两次、自动重连查询，以及编号不会出现在可见日志里。

## 后续硬件验证

1. 将嵌入式 C 编解码与状态机编译到 PC，运行同一组冻结向量。
2. 两只 USB-UART 交叉连接，验证 UART 分片、缓冲和 RTS/CTS。
3. 替换为实际蓝牙模块，测量延迟分布、拥塞、距离和持续吞吐，调整超时。
4. 接入 FreeRTOS 与 FatFs，注入 SD 写入错误、空间不足和拔卡。
5. 实际 SWD 烧录校验、目标保护状态和取消边界。
6. 在上传提交、擦除、编程、日志同步阶段做掉电试验，并针对真实介质实现恢复。

Python 模拟器证明的是协议工作流在被建模故障下的行为，不证明未建模的硬件时序、介质掉电安全或无线吞吐。
