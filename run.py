"""便捷入口：等价于 `python -m airelay`。

给不习惯 `-m` 写法的同学用；参数完全一致，例如
    python run.py --mode server --port 8000
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from airelay.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
