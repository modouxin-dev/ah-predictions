import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import os
import tempfile

# 测试中的客户端调用不得改写仓库里的 data/source_status.json
os.environ["AH_SOURCE_STATUS_FILE"] = str(Path(tempfile.mkdtemp()) / "source_status.json")
