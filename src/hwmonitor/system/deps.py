"""源码运行时缺少 Python 依赖 (pythonnet) 时, 在程序目录执行 pip install -e . 补装; 便携 EXE 已内置, 用不到。"""
import importlib
import logging
import subprocess
import sys

from hwmonitor import paths

log = logging.getLogger(__name__)


def command():
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-e", str(paths.BASE)]


def install():
    """用当前解释器的 pip 安装 pyproject.toml 里的依赖 (不弹控制台), 返回是否成功。"""
    try:
        done = subprocess.run(command(), capture_output=True, text=True, errors="replace", timeout=900,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        log.exception("pip install failed")
        return False
    if done.returncode:
        log.error("pip install exited with %s:\n%s\n%s", done.returncode, done.stdout, done.stderr)
        return False
    importlib.invalidate_caches()  # 新装的包出现在已扫描过的 site-packages 里, 否则 import 仍找不到
    return True
