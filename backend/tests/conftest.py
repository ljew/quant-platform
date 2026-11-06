"""pytest 配置：让 tests/ 能直接 import app.* 并共享路径常量。"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
