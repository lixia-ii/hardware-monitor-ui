# 硬件监控界面

一个简单的 Windows 桌面界面,实时显示 CPU/显卡的温度、利用率、内存/显存占用,网卡实时上传/下载速度,以及 CPU 风扇、显卡风扇、机箱(主板)风扇转速。

## 说明

本项目**只是读取 [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) 提供的数据并展示**,自己不直接访问硬件、不带任何驱动。传感器的读取全部由 LibreHardwareMonitor 完成,本项目通过它内置 Remote Web Server 的 `http://127.0.0.1:8085/data.json` 获取数据。

- LibreHardwareMonitor 开源地址:https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
- `LibreHardwareMonitor.zip` 是该项目的程序文件打包,放在本仓库的 [Releases](https://github.com/lixia-ii/hardware-monitor-ui/releases) 页面(不在 git 里),版权归原作者所有,以 [MPL-2.0](THIRD_PARTY/LibreHardwareMonitor-MPL-2.0.txt) 许可证发布,源码见上面的仓库。你也可以自行从上面的仓库下载新版本,按相同的目录结构打包替换。

## 使用

1. 双击 `start.bat`。会弹出一次 UAC 管理员确认:
   - 如果当前目录下没有 `LibreHardwareMonitor/`,会先从 Releases 自动下载 `LibreHardwareMonitor.zip` 并解压(下载失败时按提示手动下载,放到 `start.bat` 同目录即可)(压缩包内已预置:开启 Web Server、启动最小化到托盘,仅监听 127.0.0.1);
   - 如果 LibreHardwareMonitor 没在运行,会自动最小化启动它;
   - 关闭监控窗口时,会一并关闭由 `start.bat` 启动的 LibreHardwareMonitor。
2. 最小化窗口会隐藏到右下角托盘;单击托盘图标恢复窗口,右键可退出,鼠标悬停可直接看 CPU/显卡温度。点关闭按钮时默认询问"退出"还是"最小化到托盘",可勾选记住,之后在底栏 **设置** 里修改。程序只允许运行一个,重复启动会唤出已打开的窗口。
3. **设置 → 开机自动启动**:登录后自动在托盘运行,并一并启动 LibreHardwareMonitor。通过任务计划程序(任务名 `HardwareMonitorUI`,以最高权限运行)实现,开启或关闭时确认一次 UAC,之后开机不再弹出。移动程序目录后,下次启动会自动修正任务里的路径。
4. 需要已安装 Python 3(运行监控界面仅用标准库,无需安装依赖;仅重新生成图标的 `make_icon.py` 需要 Pillow)。

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

```
start.bat                双击启动
README.md
app/
  monitor.py             界面
  tray.py                托盘图标、单实例
  autostart.py           开机自启 (计划任务)
  sensors.py             读取 LibreHardwareMonitor 数据
  hwinfo.py              读取型号/系统/处理器等硬件概览
  app.ico                窗口图标
  make_icon.py           重新生成图标
settings.json            本地设置(关闭按钮行为、分组收起状态),已被 git 忽略
LICENSE                  本项目许可证(MIT)
THIRD_PARTY/             第三方许可证
LibreHardwareMonitor.zip 首次启动时从 Releases 下载,已被 git 忽略
LibreHardwareMonitor/    由上面的压缩包解压生成,已被 git 忽略
```

## 许可证

本项目代码以 [MIT](LICENSE) 许可证发布。LibreHardwareMonitor 以 [MPL-2.0](THIRD_PARTY/LibreHardwareMonitor-MPL-2.0.txt) 发布,不包含在本仓库代码中。
