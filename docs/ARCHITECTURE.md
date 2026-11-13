# 架构说明

本文说明代码的分层、各模块职责和数据流，便于定位修改位置。使用说明见 [README](../README.md)。

## 分层与依赖方向

```text
bootstrap ──> ui ──> core ──> sensors
                │       │
                └───────┴──> system
paths / settings: 各层共用的基础模块
```

- 依赖只能从上往下：`ui` 可以用 `core`、`sensors`、`system`；`core`、`sensors`、`system` 不引用 `ui`。
- 所有导入都写成绝对路径 `from hwmonitor.xxx import ...`，不使用相对导入。
- 第三方二进制只放在 `vendor/`，许可证放在 `licenses/`，不与源码混放。

## 模块一览

| 位置 | 职责 |
|---|---|
| `__main__.py` | 入口。以脚本方式运行时（`start-windows.bat`、开机自启任务、UAC 提权重启）把 `src/` 加入导入路径 |
| `bootstrap.py` | 启动流程：日志、管理员权限、单实例、DPI 感知、旧数据迁移、创建主窗口；`--self-test` 自检 |
| `paths.py` | 程序目录、数据目录、内置硬件库目录、图标位置；兼容源码运行和 PyInstaller 打包 |
| `settings.py` | 设置的默认值、字段校验、原子写入 `data/settings.json` |
| `core/collector.py` | 唯一的采集线程：刷新硬件、解析读数、写历史、发布给传感器数据接口；设备（路由器/远程电脑）在线程池里各自读取，采集循环只取最近一次结果，不等网络 |
| `core/alerts.py` | 持续高温与风扇异常告警（持续时间、恢复温差、冷却） |
| `core/device_presence.py` | TP-Link 终端状态变化确认，首次/恢复静默建立基准；事件经独立队列送到 UI，避免采样队列覆盖而丢失提醒 |
| `core/history.py` | SQLite 历史存储、显卡身份合并、曲线精简、CSV 导出 |
| `core/diagnostics.py` | 滚动日志与诊断报告（不含环境变量和私有路径） |
| `sensors/lhm.py` | 加载 LibreHardwareMonitorLib（pythonnet），打开/刷新/关闭硬件，输出与 LHM `data.json` 相同结构的树 |
| `sensors/reading.py` | 解析传感器树，归一化成界面使用的读数（CPU/显卡/内存/网速/风扇/各类温度） |
| `sensors/nvidia.py` | 硬件库不可用时用 `nvidia-smi` 兜底 |
| `sensors/openwrt.py` | OpenWrt 路由器指标（prometheus-node-exporter-lua） |
| `sensors/tplink.py` | TP-Link 原厂固件登录、会话复用、WAN 状态/在线时长与终端排行只读查询，密码错误后停止重试 |
| `sensors/hwinfo.py` | 硬件信息窗口与诊断用的静态硬件清单（WMI） |
| `system/elevation.py` | 以管理员身份重新启动 |
| `system/credentials.py` | Windows DPAPI 加密/解密设备密码，当前 Windows 用户范围 |
| `system/autostart.py` | 任务计划程序开机自启，程序移动后自动修正路径 |
| `system/tray.py` | 托盘图标、气泡通知、单实例互斥量（纯 ctypes） |
| `system/pawnio.py` | PawnIO 驱动检测与安装 |
| `system/webserver.py` | 兼容 `/data.json` 的传感器数据接口、局域网防火墙规则、本机地址 |
| `system/management_server.py` / `webbridge.py` | 独立局域网手机管理服务（默认 7076）：HTTP、固定开放局域网、独立防火墙规则、HTTP 与 Tk 主线程之间的有界 RPC 队列 |
| `system/auth.py` / `captcha.py` / `mailer.py` | 管理端登录：图形验证码（纯 SVG 路径）→ 配对码（邮件 + 本机显示 + 已登录设备批准）→ 长期设备令牌（仅存哈希，`management_devices.json`）；全局发信限流；SMTP 配置独立保存在 `mail.json`，网页不可修改 |
| `system/web/` | 自包含手机网页、样式、脚本、主屏幕图标与 Web App manifest；不依赖 CDN |
| `core/management.py` | 对外配置投影、密码隐藏、严格更新校验和版本冲突检测 |
| `ui/web_management.py` | 在 Tk 主线程读取状态与历史、保存并应用网页设置；硬件概览后台读取 |
| `ui/main_window.py` | 主窗口：CPU/显卡卡片、可选分组、提示条、底部工具栏、托盘联动、渲染 |
| `ui/device_cards.py` | 设备卡片（OpenWrt / 远程电脑 / TP-Link）：单行标题、详情、终端流量榜、在线状态变更提醒 |
| `ui/settings_dialog.py` 等 | 设置、自定义显示、历史曲线、硬件信息、悬浮窗、通用对话框，各为一个混入类 |
| `ui/theme.py` | 配色、字体、ttk 样式、窗口图标与深色标题栏、自绘开关和勾选框 |
| `ui/display.py` | 指标/分组默认名称、数值格式化、按温度着色 |

## 主窗口的组成

`MainWindow` 由多个混入类组合而成，每个对话框或子窗口的代码在各自文件中：

```text
MainWindow(SettingsDialogMixin, CustomizeDialogMixin, HistoryDialogMixin,
           FloatingWindowMixin, InfoWindowMixin, DeviceCardsMixin, DialogMixin)
```

混入类共享主窗口的状态（`self.root`、`self.settings`、`self.data`、`self.collector` 等），新增窗口时按同样方式加一个文件。

## 数据流

```text
LibreHardwareMonitorLib ─(lhm.Sensors.sample)─> data.json 结构的树 ─┬─> reading.read() ─> 读数 ─> 采集结果队列 ─> MainWindow.tick()
                                                                    └─> WebServer.publish() ─> /data.json
```

- 所有 .NET 调用都在采集线程里进行；界面线程只从队列取结果，不直接访问硬件。
- 采集线程退出时关闭硬件库，释放驱动。
- 内网数据服务与手机管理服务是两个独立实例，互不提供对方的路由。管理请求通过有界队列由 `MainWindow.tick()` 处理；超时且尚未执行的修改会取消，不会稍后静默应用。网页不能修改管理服务自身的端口和开关，也不能修改配对码邮件。
- 管理端除静态文件和 `/api/auth/status|captcha|request|verify` 外都要求设备令牌 Cookie。鉴权在 HTTP 线程内完成（`Auth` 自带锁），不经过 Bridge；配对事件经 `Auth.events` 由桌面 `tick()` 显示到提示条和托盘，服务器模式写入日志。
- 历史每 5 秒取样一次，写入 SQLite，并通过队列同步给界面的内存历史。

## 测试

- `tests/unit/`：按模块分文件，不依赖真实硬件和 .NET（硬件库用模拟对象）。
- `tests/ui/smoke_ui.py`：隔离数据目录、模拟传感器的界面集成测试。
- `tests/support.py`：共用的模拟传感器树和模拟硬件对象。
- 打包后的 EXE 用 `--self-test` 自检，确认能加载 .NET 硬件库。

## Linux 服务器路径

`bootstrap` 先解析模式，Windows 延迟导入 `desktop`，Linux 选择 `server.Controller`，服务器路径不导入 Tk、托盘、Win32 或 pythonnet。桌面和后台通过 `core.actions.ManagementActions` 共享网页动作，通过 `core.runtime.RuntimeState` 共享历史与告警消费；各自控制循环串行处理 Bridge 请求，桌面副作用保留在 UI。

`Collector` 接受 `sensors.backend.Backend`，每次生成 `Snapshot(data, tree)`；Windows 包装现有 LHM，Linux 直接生成归一化读数，再从同一读数生成兼容树。采集线程仍是唯一历史写入者。Linux 以 `linux_credentials`、`linux_info` 替换平台服务，部署见 [LINUX.md](LINUX.md)。
