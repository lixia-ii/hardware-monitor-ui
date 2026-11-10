"""程序入口: python -m hwmonitor, 也可直接运行本文件 (start.bat、开机自启任务、UAC 提权重启都这样启动)。"""
import sys

if __package__ in (None, "") and not getattr(sys, "frozen", False):
    # 作为脚本运行时 sys.path[0] 是本包目录, 换成 src 目录才能按 hwmonitor.* 导入
    from pathlib import Path
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from hwmonitor.bootstrap import main  # noqa: E402

if __name__ == "__main__":
    main()
