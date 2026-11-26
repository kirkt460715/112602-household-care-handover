"""服务快照的原子化 JSON 存储。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .service import CareService


class JsonStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self, service: CareService) -> bool:
        if not self.path.exists():
            return False
        data = json.loads(self.path.read_text(encoding="utf-8"))
        service.restore(data)
        return True

    def save(self, service: CareService) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(service.snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
