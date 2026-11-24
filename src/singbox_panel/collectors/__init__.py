"""Traffic collectors for sing-box (v2ray_api, clash_api, and demo mock)."""

from .base import BaseCollector, CollectorManager
from .clash_api import ClashApiCollector
from .mock import MockTrafficCollector
from .v2ray_api import V2RayApiCollector

__all__ = [
    "BaseCollector",
    "CollectorManager",
    "ClashApiCollector",
    "MockTrafficCollector",
    "V2RayApiCollector",
]
