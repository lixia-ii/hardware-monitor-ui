# Linux 服务器预览版

已加入无桌面入口、Linux 采集、共享历史/告警与管理接口。当前 Windows 开发机完成模拟与 HTTP 回归；Ubuntu 实机、systemd、24 小时运行和升级回退验收仍待执行，尚未标记正式支持。

## 直接运行

需要 Python 3.11+、venv、pip、Bash 和 util-linux（提供 flock）；无需 Tk、.NET 或图形会话。在独立目录解压或克隆项目后，一键后台启动：

```sh
sh start.sh
sh start.sh status
sh start.sh stop
sh start.sh restart
sh start.sh logs
```

首次运行自动创建项目内的 `.venv` 并联网安装依赖；以后复用环境，`pyproject.toml` 变化时自动重新安装。无需 sudo，等待服务就绪后返回命令行，关闭终端后继续运行；使用 `stop` 优雅退出，最多等待 30 秒，不强制杀进程。`status` 未运行时返回非零状态。`logs` 显示实时日志，Ctrl+C 只退出查看。Ubuntu / Debian 缺组件时执行 `sudo apt install python3-venv python3-pip util-linux`。可用 `PYTHON=python3.13 sh start.sh` 指定首次创建环境使用的 Python。

不带参数等同于 `start`，默认监听 `0.0.0.0:7076`，浏览器通过 `http://服务器IP:7076/` 访问；旧命令 `sh start.sh 1` 继续兼容。可以覆盖监听地址、端口或开启 NVIDIA 查询；`restart` 不带参数沿用上次参数，带参数则使用新的参数（未指定的项回到默认值）。`--check` 和 `--help` 在前台执行后退出，例如：

```sh
sh start.sh --check
sh start.sh start --nvidia
sh start.sh restart --host 127.0.0.1 --port 8080
```

默认数据保存在项目的 `data/`，可用 `HARDWARE_MONITOR_DATA_DIR="$HOME/.local/share/hwmonitor" sh start.sh` 指定其他目录，重启时也需传入同样的环境变量。脚本从任意工作目录调用均可，管理状态及控制台日志固定保存在项目的 `data/launcher/`（`hwmonitor.pid`、启动参数、`startup.log`），每次启动保留一份上次日志 `startup.log.previous`。程序自身的滚动日志仍为数据目录下的 `monitor.log`。脚本通过管理锁避免并发操作，使用前核对 PID 的命令行和工作目录，丢失 PID 文件也能找回本项目的服务，包括之前以前台模式启动的实例。启动失败会显示日志，30 秒内未就绪则返回非零状态，保留仍在初始化的进程供 `status` / `logs` 检查。启动过程请等命令返回后再关闭终端。

本脚本不提供崩溃重启或开机自启；需要这些功能时使用下文的 systemd 安装方式，不要混用两套启动方式管理同一个实例。

也可以手动安装并运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m hwmonitor --help
.venv/bin/python -m hwmonitor --check
HARDWARE_MONITOR_DATA_DIR="$HOME/.local/share/hwmonitor" .venv/bin/python -m hwmonitor --headless
```

Linux 直接运行 Python 默认进入前台服务器模式，管理端仅监听 `127.0.0.1:7076`。检查命令不会创建数据目录、密钥、数据库或监听器；CPU 首次读数处于预热状态。没有温度、风扇或显卡也可使用 CPU、内存、网速、历史和设备管理。

从电脑建立隧道 `ssh -L 7076:127.0.0.1:7076 user@server` 后，在浏览器打开 `http://127.0.0.1:7076`。

可信内网可显式使用 `--host 0.0.0.0 --port 7076`；可选 `--data-port 8085` 开启同一采样快照的 `/data.json`，`--nvidia` 启用 NVIDIA 查询（需要驱动与 nvidia-smi）。管理端和数据端不能使用同一端口。当前监听参数支持 IPv4 地址。

内存占用采用 total - available。默认网速采用一张默认路由接口，没有路由时选一张物理网卡；网页可手动选择，避免桥接与容器接口重复统计。预热、网卡断连、计数回退和选择变化时网速为空。温度/风扇从只读 hwmon 通道获取，以设备实际路径、芯片和通道标识；未知温度保留“其他温度”，未知风扇不作为 CPU 风扇告警。风扇的真实 0 RPM 保留。

## systemd

将可信源码放到 `/opt/hwmonitor` 等普通服务账户能读取的目录。不要放在受 ProtectHome 限制的 home 目录。Ubuntu 如缺 venv，先安装 `python3-venv`。

```sh
sudo sh /opt/hwmonitor/scripts/install-linux.sh /opt/hwmonitor
sudo systemctl status hwmonitor
sudo journalctl -u hwmonitor -n 50
```

安装器创建普通账户 hwmonitor，使用项目 `.venv` 安装包；重复安装保留设置与密码。数据保存在 `/var/lib/hwmonitor`，文件锁放在 `/run/hwmonitor`。日志进入 journal，并保留有大小上限的 monitor.log。修改 `/etc/default/hwmonitor` 后重启：

```sh
HWMONITOR_ARGS="--host=192.168.1.10 --port=7076 --nvidia"
```

程序不自动修改 Linux 防火墙。需要时自行限定来源，例如 `sudo ufw allow from 192.168.1.0/24 to any port 7076 proto tcp`。管理服务目前没有登录认证，只用于本机或可信内网；不直接暴露公网。Host/同源校验仅允许 IP 或 localhost。

停止/重启：`sudo systemctl stop hwmonitor` / `sudo systemctl restart hwmonitor`。启动冲突和目录不可写会以非零状态退出；配置损坏时记录日志并使用默认设置启动。SIGTERM/SIGINT 停止管理修改并清理采集与服务，systemd 对超过 15 秒的进程执行最终回收。

## 凭据与备份

Linux 使用 Fernet，`credentials.key` 与设置分开保存，目录 0700、密钥 0600。请同时备份密钥、settings.json 和 history.sqlite3；停止服务后再复制整个数据目录，避免遗漏 SQLite WAL。密钥丢失后不会自动生成新密钥；可以恢复原密钥，或在网页为每台设备重新输入密码（重新输入时才生成新密钥，旧密码随之失效）。Windows DPAPI 密码需要重新输入。此方案不抵御服务账户或 root 被控制。

升级前记录旧版本（Git commit 或源码包），保留旧安装目录/虚拟环境。安装器会停止已有服务，将数据完整备份到 `/var/backups/hwmonitor/upgrade-*` 后安装。升级失败时恢复旧代码及依赖，停止服务，将对应数据备份恢复到 `/var/lib/hwmonitor`，执行 `sudo chown -R hwmonitor:hwmonitor /var/lib/hwmonitor` 后再启动。不要把新版本数据库直接交给旧版本。请在实机完成一次回退演练后用于长期部署。

卸载服务：`sudo sh scripts/uninstall-linux.sh`。默认保留数据、密钥、备份、应用及账户。

## 验收记录

- Windows 本机已通过：145 项单元测试（Linux 文件锁 1 项按平台跳过）、桌面与网页集成、便携包构建及 EXE 自检（无回调错误、图标与 .NET 硬件库加载正常）；脚本语法和网页 JavaScript 语法检查通过。
- 自动回归覆盖：模拟 CPU/内存、网速差分/回退/断卡、同型号 hwmon、0 RPM、权限失败、GPU UUID、凭据和密钥丢失；真实 HTTP 设置冲突、保存失败、历史/CSV、诊断、能力拒绝、退出和端口冲突。
- Ubuntu CI：安装、完整平台适用单元测试、无 Tk 导入检查、帮助/检查、真实后台启动、HTTP 与 SIGTERM；CI 结果以实际工作流运行为准。
- 待实机：Ubuntu 26.04.1 x86_64 的 Python/内核/CPU/显卡/传感器记录，与系统工具核对数值，普通用户 systemd 自启/重启/停止、升级回退，以及默认 1 秒刷新/5 秒历史下 24 小时 CPU/内存/数据库增长记录。

接口参考：[psutil 官方 API](https://psutil.io/api/)、[Linux hwmon](https://www.kernel.org/doc/html/latest/hwmon/sysfs-interface.html)、[Fernet](https://cryptography.io/en/latest/fernet/)。
