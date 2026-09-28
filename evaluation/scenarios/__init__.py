"""场景登记表：评测规格 §4.4 基线清单的实现入口，逐个落地中。"""

from __future__ import annotations

from ..harness.model import Scenario
from .s01_insurance import SCENARIO as S1

REGISTRY: dict[str, Scenario] = {
    S1.id: S1,
}
