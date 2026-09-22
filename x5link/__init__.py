"""x5link —— 一期工作流第 1 步：把 X5 画面接进电脑、量延迟、同时存素材。"""

from .capture import Device, FrameSource, FpsMeter, Recorder, list_devices, match_device
from .latency import Decode, LatencyStats, decode, render_card, verdict

__all__ = [
    "Device", "FrameSource", "FpsMeter", "Recorder", "list_devices", "match_device",
    "Decode", "LatencyStats", "decode", "render_card", "verdict",
]
__version__ = "0.1.0"
