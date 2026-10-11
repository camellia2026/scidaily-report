#!/usr/bin/env python
"""入口脚本。

    python run.py --smoke                    # 纯离线冒烟
    python run.py --check-sources            # 逐个源体检
    python run.py --seed                     # 抓一次固化成离线固件
    python run.py --no-llm --dry-run         # 规则模式出一份报告
    python run.py --dry-run                  # 真跑但不推送
    python run.py                            # 真跑并推送
    python run.py weekly                     # 每周顶刊盘点

为什么需要这个文件而不是直接 `python -m scidaily.main`：
  common/ 包与 scidaily/ 都不安装，靠 sys.path 注入。
  把 sys.path 处理集中在这一个地方，其他模块就不用各自关心了。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scidaily.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
