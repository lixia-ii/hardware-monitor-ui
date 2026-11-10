"""测试包: 无需安装即可从仓库根目录运行 (python -m unittest discover -s tests -t .)。"""
from pathlib import Path
import sys

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
