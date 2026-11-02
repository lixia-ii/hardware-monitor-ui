# 硬件监控界面

一个简单的 Windows 桌面界面,实时显示 CPU 温度、显卡温度、CPU 风扇、显卡风扇、机箱(主板)风扇转速。

## 说明

本项目**只是读取 [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) 提供的数据并展示**,自己不直接访问硬件、不带任何驱动。传感器的读取全部由 LibreHardwareMonitor 完成,本项目通过它内置 Remote Web Server 的 `http://127.0.0.1:8085/data.json` 获取数据。

- LibreHardwareMonitor 开源地址:https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
- `LibreHardwareMonitor.zip` 是该项目的程序文件打包,版权与许可证归原作者所有,请以其开源仓库为准。你也可以自行从上面的仓库下载新版本,按相同的目录结构打包替换。

## 使用

1. 双击 `start.bat`。会弹出一次 UAC 管理员确认:
   - 如果当前目录下没有 `LibreHardwareMonitor/`,会先自动解压 `LibreHardwareMonitor.zip`(压缩包内已预置:开启 Web Server、启动最小化到托盘,仅监听 127.0.0.1);
   - 如果 LibreHardwareMonitor 没在运行,会自动最小化启动它;
   - 关闭监控窗口时,会一并关闭由 `start.bat` 启动的 LibreHardwareMonitor。
2. 需要已安装 Python 3(仅用标准库,无需安装依赖)。

## 注意

- 读取 CPU 温度和风扇转速需要管理员权限,这是 LibreHardwareMonitor 的要求。
- 未连接 LibreHardwareMonitor 时,显卡温度和风扇百分比会由 `nvidia-smi` 兜底(仅 NVIDIA)。
- 能显示哪些风扇,取决于主板是否被 LibreHardwareMonitor 识别。部分品牌机(OEM 主板)没有风扇传感器,CPU 风扇和机箱风扇会显示为空。
- 主板风扇名称含 "CPU" 才算 CPU 风扇,其余转速不为 0 的归为机箱风扇。
- 调试传感器读取:`python app\sensors.py`。

## 目录结构

```
start.bat                双击启动
README.md
app/
  monitor.py             界面
  sensors.py             读取 LibreHardwareMonitor 数据
  app.ico                窗口图标
  make_icon.py           重新生成图标
LibreHardwareMonitor.zip 第三方程序压缩包(见上文链接)
LibreHardwareMonitor/    首次启动时自动解压生成,已被 git 忽略
```
