# 硬件监控界面

一个简单的 Windows 桌面界面,实时显示 CPU/显卡的温度、利用率、内存/显存占用,网卡实时上传/下载速度,以及 CPU 风扇、显卡风扇、机箱(主板)风扇转速。

## 说明

本项目**只是读取 [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) 提供的数据并展示**,自己不直接访问硬件、不带任何驱动。传感器的读取全部由 LibreHardwareMonitor 完成,本项目通过它内置 Remote Web Server 的 `http://127.0.0.1:8085/data.json` 获取数据。

- LibreHardwareMonitor 开源地址:https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
- `LibreHardwareMonitor.zip` 是该项目的程序文件打包,放在本仓库的 [Releases](https://github.com/lixia-ii/hardware-monitor-ui/releases) 页面(不在 git 里),版权归原作者所有,以 [MPL-2.0](THIRD_PARTY/LibreHardwareMonitor-MPL-2.0.txt) 许可证发布,源码见上面的仓库。你也可以自行从上面的仓库下载新版本,按相同的目录结构打包替换。

## 使用

1. 双击 `start.bat`。界面会请求一次 UAC 管理员确认，以便启动和关闭自己管理的 LibreHardwareMonitor:
   - 如果当前目录下没有 `runtime/LibreHardwareMonitor/`,会先从 Releases 自动下载到 `runtime/cache/LibreHardwareMonitor.zip` 并解压到 `runtime/`(下载失败时按提示手动下载,放到 `runtime/cache/` 即可)(压缩包内已预置:开启 Web Server、启动最小化到托盘,仅监听 127.0.0.1);
   - 界面先取得单实例锁，如果 LibreHardwareMonitor 没在运行，再最小化启动它；
   - 关闭监控窗口时，只关闭本次由界面启动并持有进程句柄的 LibreHardwareMonitor，先正常关闭，等待最多 4 秒后才终止；已在运行的实例不会被关闭。
2. 最小化窗口会隐藏到右下角托盘;单击托盘图标恢复窗口,右键可退出,鼠标悬停可直接看 CPU/显卡温度。点关闭按钮时默认询问"退出"还是"最小化到托盘",可勾选记住,之后在底栏 **设置** 里修改。程序只允许运行一个,重复启动会唤出已打开的窗口。
3. **设置 → 开机自动启动**:登录后自动在托盘运行,并一并启动 LibreHardwareMonitor。通过任务计划程序(任务名 `HardwareMonitorUI`,以最高权限运行)实现,开启或关闭时确认一次 UAC,之后开机不再弹出。移动程序目录后,下次启动会自动修正任务里的路径。
4. 源码运行需要 Python 3（推荐 3.13，运行界面仅用标准库，无需安装依赖）；便携 EXE 版本无需安装 Python。仅重新生成图标的 `make_icon.py` 需要 Pillow。

## 告警、历史与悬浮窗

- **温度告警**：在 **设置 → 告警** 中设置 CPU/显卡阈值、持续超温时间、恢复温差及提醒间隔。默认持续超温 10 秒才提醒，降到阈值以下 5°C 才解除；短暂回落和传感器缺失不会清除提醒冷却时间。托盘不可用时在主界面提示。
- **风扇异常检测**：在 **设置 → 告警** 中单独开启（默认关闭），设置最低 RPM 和持续时间。只有对应设备持续高温且风扇低于设定转速时才提醒；无 RPM 传感器、仅有百分比、显卡低温停转均不触发。
- **历史曲线**：点击底栏 **历史** 查看温度、利用率和网速。默认每 5 秒取样，刷新间隔更长时按实际采集节奏记录，保留最近 24 小时，支持 1 小时/24 小时范围、显卡选择、最高值、鼠标悬停读数及 CSV 导出。“显示间隔”默认自动：1 小时视图按 30 秒、24 小时视图按 5 分钟分段，每段保留首尾、最低与最高采样点，按真实时间顺序连成普通折线，减少点数并保留尖峰，不绘制菱形或气泡标记；也可手动选择原始、30 秒、1 分钟、5 分钟或 15 分钟。悬停曲线可查看原始读数和时间，最高值与 CSV 保留原始数据。缺失或长时间断连处留空，精简绘制不会跨越断点；切换显卡、网卡不会混用其他设备的曲线。
- 历史图按指标上下分图，CPU、显卡、内存、显存各占独立绘图区，避免曲线重叠；时间轴和悬停参考线对齐，各图单独显示最高值，利用率统一采用 0–100% 刻度。单显卡直接在图例中显示型号，多显卡选择仅影响显卡与显存曲线；网络视图分别显示上传、下载。能够唯一对应的 LHM 与 NVIDIA 历史记录归为同一个显卡选项，曲线、悬停读数和 CSV 导出会包含两种来源的数据；同型号多卡无法唯一对应时保持独立。
- **重启恢复**：默认将历史写入`data/history.sqlite3`，启动时自动恢复。可在 **设置 → 常规** 关闭持久化；关闭后仍保留本次运行的内存历史，已有本地数据保留并在超过 24 小时后淘汰。
- **悬浮窗**:点击底栏 **悬浮窗** 或在 **设置** 中开启。它会置顶显示 CPU/显卡温度与利用率;拖动标题栏可调整位置,右上角 × 关闭。位置和开关状态会在下次启动时恢复。

## OpenWrt 路由器

可选显示 OpenWrt 路由器的温度、CPU 利用率、内存、负载、运行时间和 WAN 上下行速度。数据来自路由器上的 [`prometheus-node-exporter-lua`](https://github.com/openwrt/packages/tree/master/utils/prometheus-node-exporter-lua)：它是 Prometheus node exporter 的 Lua 精简版，在 9100 端口以纯文本（每行"指标名 + 数值"）提供系统状态，只提供数据，不画图、不存历史。

**1. 在路由器上安装并启动**（温度需要 `-thermal` 附加包）：

```sh
opkg update
opkg install prometheus-node-exporter-lua prometheus-node-exporter-lua-thermal
/etc/init.d/prometheus-node-exporter-lua enable
/etc/init.d/prometheus-node-exporter-lua start
```

**2. 允许局域网访问。** 该服务默认可能只监听本机，Windows 访问不到。在路由器上检查：

```sh
netstat -lnt | grep 9100
```

- 显示 `0.0.0.0:9100` 或 `:::9100`：已可从局域网访问；仍打不开时检查防火墙。
- 显示 `127.0.0.1:9100`：只监听本机，改为监听 lan（想监听所有接口，把 `lan` 换成 `*`）：

```sh
uci set prometheus-node-exporter-lua.main.listen_interface='lan'
uci commit prometheus-node-exporter-lua
/etc/init.d/prometheus-node-exporter-lua restart
```

**3. 验证并启用。** 在 Windows 浏览器打开 `http://<路由器 IP>:9100/metrics`，能看到 `node_load1 0.15` 这类文本即可。然后在 **设置 → 采集与显示 → OpenWrt 路由器** 中勾选显示，并填写该地址（默认 `http://192.168.1.1:9100/metrics`）。WAN 网卡默认自动选择（优先 `pppoe-wan`/`wan`，否则取流量最大的物理网卡），也可以手动指定。路由器离线只影响路由器卡片，重试间隔逐步延长至 30 秒。

## 自定义、诊断与常驻开销

- **设置 → 采集与显示**：调整刷新间隔（0.5–30 秒）、固定默认显卡、选择参与网速统计的网卡。默认自动汇总物理网卡；手动选择时可包含 VPN/虚拟网卡，也可全部取消。显卡卡片上的切换选择也会保存。
- **自定义显示、名称和顺序**：先选择 CPU、显卡、可选分组或具体传感器分组，再修改名称、显示状态和顺序，最后点击“保存”。切换选中项会保留待保存的编辑，“取消”会放弃本次编辑。动态传感器按设备身份保存偏好，其他设备断连或顺序变化不会影响配置。隐藏不影响采集、告警和历史记录。
- **设置窗口**：支持缩放和滚动，常规设置按关闭行为、启动与悬浮窗、历史与诊断分区；设置即时保存，自定义显示单独确认保存。
- **多显卡断连恢复**：固定显卡缺失时保留空值，避免自动套用另一块显卡的数据。NVIDIA UUID 选择在 LHM 恢复后继续有效；同型号多卡无法确认身份时，仅使用 UUID 对应的读数。
- **设置 → 常规 → 导出诊断报告**：导出 JSON 或纯文本，包含程序版本、硬件概览、当前读数、最近 100 条本次运行的告警及最近采集耗时。报告不导出环境变量、异常中的私有路径或原始日志。
- 本地滚动日志为 `data/monitor.log`，最多 3 份，每份 512 KiB。配置用临时文件和原子替换保存；格式错误时使用默认值，写入失败会在界面提示。历史数据库不可用时退回内存历史并提示。
- 使用一个常驻采集线程；主窗口隐藏时停止更新主界面控件，但继续采集、告警和更新悬浮窗。连接失败后重试间隔逐步延长至 30 秒，进程状态查询缓存 10 秒，NVIDIA 兜底缓存 5 秒。新选择或设置可唤醒采集线程。
- 历史写入通过时间索引清理过期与超量记录，避免每次写入扫描全表；曲线缩放重绘合并处理，复用采样快照，鼠标悬停通过二分查找定位读数。
- 数据与配置默认集中保存在程序目录下的 `data/`，请放在有写入权限的位置。开发/测试可用 `HARDWARE_MONITOR_DATA_DIR` 指定隔离目录。

## 便携程序与开发验证

便携包解压后运行 `start.bat`，首次自动准备 LibreHardwareMonitor，之后可直接运行 `HardwareMonitorUI.exe`。移动时保留整个目录；任务计划程序同时兼容源码与 EXE。便携包不含 Python 安装程序，也不内置 LibreHardwareMonitor 驱动。

在 Windows PowerShell 中构建（首次需要联网下载构建依赖）：

```powershell
./scripts/build.ps1
```

输出 `dist/HardwareMonitorUI.exe` 与 `dist/HardwareMonitorUI-windows-x64.zip`。构建依赖单独安装到 `.build/venv/`；版本固定在 `scripts/requirements-build.txt`，源码运行仍无第三方依赖。

```powershell
python -m unittest discover -s tests -v
python tests/smoke_ui.py
./dist/HardwareMonitorUI.exe --self-test
```

GUI 集成测试使用隔离数据和模拟传感器，覆盖主界面、17,280 个历史点、设置、自定义、悬浮窗、导出和退出。EXE 自检使用独立实例标识，在 EXE 旁的 `.build/self-test/self-test.json` 写入结果，自动退出，不启动或关闭外部 LibreHardwareMonitor。GitHub Actions 在 Windows 上执行测试和构建，可从工作流产物下载便携包。

## 首次运行的 PawnIO 提示

首次启动会弹出 `PawnIO is not installed, do you want to install it?`,随后还有一个 `PawnIO Setup` 英文免责声明。

- **这是什么**:PawnIO 是 LibreHardwareMonitor 使用的开源驱动,用来读取 CPU 温度和主板风扇转速,不装这两项就读不到。英文免责声明是开源软件的通用条款("按现状提供,风险自负")。
- **怎么做**:两个窗口都点 **确定**,出现 UAC 提示选 **是**。只需装一次。
- **装完仍读不到**:关闭 LibreHardwareMonitor 后重新运行 `start.bat`;若还不行,检查 Windows 安全中心的"内存完整性"是否拦截了驱动。

## 注意

- 监控窗口提示"未连接 LibreHardwareMonitor"时,在 LibreHardwareMonitor 里勾选 **Options → Remote Web Server → Run**(压缩包内已预置开启,自行替换版本后需手动勾选)。
- 读取 CPU 温度和风扇转速需要管理员权限,这是 LibreHardwareMonitor 的要求。
- 未连接 LibreHardwareMonitor 时,显卡温度、利用率、显存占用和风扇百分比会由 `nvidia-smi` 兜底(仅 NVIDIA)。
- 网速是所有物理网卡的上传/下载之和,Hyper-V、WSL、虚拟机、VPN 等虚拟网卡不计入,避免重复统计。
- 能显示哪些风扇,取决于主板是否被 LibreHardwareMonitor 识别。部分品牌机(OEM 主板)没有风扇传感器,CPU 风扇和机箱风扇会显示为空。
- 硬盘/内存/网卡温度会自动排除厂商的告警、临界阈值,只显示当前温度。
- 多块显卡时优先显示独显的温度和风扇,核显不参与。
- CPU 温度优先取 `CPU Package`(Intel)/ `Core (Tdie)`(AMD);老款 Ryzen 的 Tctl 有 10~20℃ 偏移,有 Tdie 时不用 Tctl。
- LibreHardwareMonitor 设为华氏度时会自动换算,界面统一显示摄氏度。
- 主板风扇名称含 "CPU" 才算 CPU 风扇;名称含 "Pump" 的归为水冷泵,单独显示(检测到时才出现);其余转速不为 0 的归为机箱风扇。
- 硬件类别按 LibreHardwareMonitor 提供的 `HardwareId` 识别(如 `/intelcpu/0`、`/gpu-nvidia/0`、`/nvme/1`);旧版本没有该字段时退回按图标和名称识别。
- 调试传感器读取:`python app\sensors.py`。

## 目录结构

```text
hardware-monitor-ui/
├─ start.bat                 日常启动入口
├─ README.md                 使用说明
├─ LICENSE                   本项目 MIT 许可证
├─ app/                      程序源码和 app.ico
├─ scripts/                  开发工具
│  ├─ build.ps1              构建 Windows 便携包
│  ├─ requirements-build.txt 构建依赖
│  └─ make_icon.py           重新生成 app/app.ico（需要 Pillow）
├─ tests/                    核心回归与隔离 GUI 集成测试
├─ docs/
│  └─ PLAN.md                功能计划与完成状态
├─ THIRD_PARTY/              第三方许可证
├─ .github/workflows/        Windows CI
├─ data/                     本地设置、历史数据库和滚动日志，不提交 Git
├─ runtime/                  第三方运行文件，不提交 Git
│  ├─ LibreHardwareMonitor/  解压后的硬件监控程序
│  └─ cache/                 下载的 LibreHardwareMonitor.zip
├─ dist/                     最新 EXE、便携 ZIP 及随包文档，不提交 Git
└─ .build/                   构建环境、中间文件、自检数据和旧产物归档，不提交 Git
```

平时只需运行根目录的 `start.bat`。开发构建运行 `./scripts/build.ps1`，功能计划见 [docs/PLAN.md](docs/PLAN.md)。设置和历史已集中到 `data/`；旧布局的数据在启动取得单实例锁后自动迁移，新目录已有数据时不覆盖。构建和测试不会再在根目录生成数据库、日志或 spec 文件。

## 许可证

本项目代码以 [MIT](LICENSE) 许可证发布。LibreHardwareMonitor 以 [MPL-2.0](THIRD_PARTY/LibreHardwareMonitor-MPL-2.0.txt) 发布,不包含在本仓库代码中。
