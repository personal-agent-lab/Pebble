"""场景登记表：评测规格 §4.4 基线清单的实现入口，逐个落地中。"""

from __future__ import annotations

from ..harness.model import Scenario
from .s01_insurance import SCENARIO as S1
from .s02_memory import SCENARIO as S2
from .s03_kb_intake import SCENARIO as S3
from .s04_kb_conflict import SCENARIO as S4
from .s07_health_attachments import SCENARIO as S7
from .s08_minutes_history import SCENARIO as S8

REGISTRY: dict[str, Scenario] = {
    S1.id: S1,
    S2.id: S2,
    S3.id: S3,
    S4.id: S4,
    S7.id: S7,
    S8.id: S8,
}
