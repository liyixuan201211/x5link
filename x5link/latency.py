"""视觉时间戳编解码 —— 用来测相机的端到端延迟。

原理
----
屏幕上画一张卡片，卡片里的黑白格子编码「这张卡片被画出来的那一刻」的毫秒数。
相机对着屏幕拍。抓到的每一帧到达电脑时，我们从像素里把那个毫秒数解出来，
再和本地时钟相减：

    latency = now_ms - decoded_ms   (mod 2**24)

不需要 OCR，也不需要给两台设备对表 —— 屏幕自己发光的像素就是基准。

时间精度说明
------------
量到的是「卡片被绘制」→「该帧已回到电脑内存」的总耗时，包含：
  - 显示器渲染/扫描输出（通常 < 1 帧）
  - X5 机内拼接 + USB 传输 + 驱动缓冲
  - 解码到内存
所以它是略微偏保守（偏大）的估计，作为「能不能玩」的判据是安全的。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------- 卡片规格

PAGE_W, PAGE_H = 1600, 1000      # 白页尺寸（解码后的标准坐标系）
COLS, ROWS = 8, 5                # 8 列 x 5 行 = 40 个格子
N_CELLS = COLS * ROWS
CELL_W, CELL_H = PAGE_W // COLS, PAGE_H // ROWS
MARGIN = 48                      # 白页外的黑边，保证轮廓检测干净

HEADER = (1, 0, 1, 0, 1, 0, 1, 0)   # 8 bit，用于校验方向与有效性
FOOTER = (0, 1, 0, 1, 0, 1, 0, 1)   # 8 bit，与 header 反相
PAYLOAD_BITS = 24                    # 毫秒数 mod 2**24（约 4.66 小时一个循环）
WRAP = 1 << PAYLOAD_BITS

# 取样时只看格子正中间这一小块，避开边缘的过曝/串色
SAMPLE_W, SAMPLE_H = CELL_W // 4, CELL_H // 4


# ---------------------------------------------------------------- 编码

def bits_for_ms(ms: int) -> np.ndarray:
    """把毫秒数编成 40 个 bit（header | payload | footer）。"""
    v = int(ms) & (WRAP - 1)
    payload = [(v >> (PAYLOAD_BITS - 1 - i)) & 1 for i in range(PAYLOAD_BITS)]
    return np.array(list(HEADER) + payload + list(FOOTER), dtype=np.uint8)


def ms_from_bits(bits) -> int | None:
    """解出毫秒数；header/footer 对不上就返回 None（说明这帧不可信）。"""
    bits = np.asarray(bits, dtype=np.uint8).ravel()
    if bits.size != N_CELLS:
        return None
    if tuple(int(b) for b in bits[:8]) != HEADER:
        return None
    if tuple(int(b) for b in bits[32:]) != FOOTER:
        return None
    v = 0
    for b in bits[8:32]:
        v = (v << 1) | int(b)
    return v


def render_card(ms: int, margin: int = MARGIN) -> np.ndarray:
    """画出一帧卡片（BGR）。白页 + 黑格表示 0，白格表示 1。"""
    img = np.zeros((PAGE_H + 2 * margin, PAGE_W + 2 * margin, 3), np.uint8)
    img[margin:margin + PAGE_H, margin:margin + PAGE_W] = 255

    bits = bits_for_ms(ms)
    # 黑格往内缩一圈，留出白色间隔，避免相机的高光溢出糊在一起
    pad_x, pad_y = CELL_W // 8, CELL_H // 8
    for i, b in enumerate(bits):
        if b:
            continue
        r, c = divmod(int(i), COLS)
        y0, x0 = margin + r * CELL_H, margin + c * CELL_W
        img[y0 + pad_y:y0 + CELL_H - pad_y,
            x0 + pad_x:x0 + CELL_W - pad_x] = 0
    return img


# ---------------------------------------------------------------- 解码

def _order_corners(pts: np.ndarray) -> np.ndarray:
    """把 4 个角点排成 左上、右上、右下、左下。"""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def _quad_from_contour(cnt, gray_shape):
    """把一个轮廓变成 4 个角点；不合适的返回 None。"""
    import cv2

    h, w = gray_shape
    area = cv2.contourArea(cnt)
    if area < 0.004 * h * w:            # 太小，不可能是卡片
        return None
    # 注意：这里**不能**要求 isContourConvex —— 模糊/噪声会让轮廓边缘出现
    # 微小凹陷，直接判死。真正的校验交给 header/footer。

    peri = cv2.arcLength(cnt, True)
    approx = cv2.approxPolyDP(cnt, 0.03 * peri, True)
    if len(approx) == 4:
        quad = _order_corners(approx)
    else:
        # 兜底：旋转矩形（透视下近似四边形）
        quad = _order_corners(cv2.boxPoints(cv2.minAreaRect(cnt)))

    # 长宽比粗筛：白页是 1.6:1，透视下放宽到 0.6~3.0
    (tl, tr, br, bl) = quad
    wtop = np.linalg.norm(tr - tl)
    wbot = np.linalg.norm(br - bl)
    hleft = np.linalg.norm(bl - tl)
    hright = np.linalg.norm(br - tr)
    ww = (wtop + wbot) / 2.0
    hh = (hleft + hright) / 2.0
    if hh <= 1 or ww <= 1:
        return None
    ar = ww / hh
    if not (0.6 <= ar <= 3.0):
        return None
    if min(ww, hh) < 40:                # 太小读不出格子
        return None
    return quad


def find_card_candidates(image: np.ndarray, max_n: int = 12):
    """找出所有可能的白页候选（按面积从大到小）。

    为什么需要「多个候选」：真实房间里，天花板、白墙这些亮块往往比卡片还大，
    只取最大轮廓会选中墙（实测就是这么翻车的）。所以这里返回一批，
    由 decode() 逐个试，谁能通过 header/footer 校验就用谁。
    """
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:max_n]

    quads = []
    for cnt in contours:
        q = _quad_from_contour(cnt, gray.shape[:2])
        if q is not None:
            quads.append(q)
    return quads


def find_card(image: np.ndarray):
    """兼容旧接口：返回最大的那个候选。"""
    quads = find_card_candidates(image)
    return quads[0] if quads else None


def warp_card(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """把卡片透视校正到标准 PAGE_W x PAGE_H。"""
    import cv2

    dst = np.array([[0, 0], [PAGE_W - 1, 0],
                    [PAGE_W - 1, PAGE_H - 1], [0, PAGE_H - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(corners.astype(np.float32), dst)
    return cv2.warpPerspective(image, m, (PAGE_W, PAGE_H))


def read_bits(card: np.ndarray) -> np.ndarray:
    """从校正后的卡片里读 40 个 bit（取每格中心的灰度中位数）。"""
    gray = card if card.ndim == 2 else card.mean(axis=2)
    pad_y, pad_x = max(1, SAMPLE_H // 2), max(1, SAMPLE_W // 2)
    bits = np.zeros(N_CELLS, np.uint8)
    for i in range(N_CELLS):
        r, c = divmod(i, COLS)
        cy, cx = r * CELL_H + CELL_H // 2, c * CELL_W + CELL_W // 2
        patch = gray[cy - pad_y:cy + pad_y + 1, cx - pad_x:cx + pad_x + 1]
        bits[i] = 1 if float(np.median(patch)) > 127.0 else 0
    return bits


@dataclass
class Decode:
    ok: bool
    ms: int | None = None
    reason: str = ""
    corners: np.ndarray | None = None
    card: np.ndarray | None = None
    contrast: float = 0.0     # 黑白格的实际亮度差，用来判断拍摄质量
    candidates: int = 0       # 这一帧一共试了几个候选四边形


def decode(image: np.ndarray) -> Decode:
    """从一整帧里解出时间戳：逐个候选试，谁通过 header/footer 校验就用谁。"""
    quads = find_card_candidates(image)
    if not quads:
        return Decode(False, reason="no-card")

    first = None
    for corners in quads:
        card = warp_card(image, corners)
        ms = ms_from_bits(read_bits(card))
        if first is None:
            first = (corners, card)          # 留一个，便于失败时出证据图
        if ms is None:
            continue
        gray = card.mean(axis=2)
        return Decode(True, ms=ms, corners=corners, card=card,
                      contrast=float(gray.max() - gray.min()),
                      candidates=len(quads))

    c, cd = first
    return Decode(False, reason="bad-header", corners=c, card=cd,
                  candidates=len(quads))


# ---------------------------------------------------------------- 延迟统计

@dataclass
class LatencyStats:
    """收集样本并给出统计量。单位毫秒。"""

    max_accept_ms: int = 5000
    samples: list[float] = field(default_factory=list)
    rejects: dict[str, int] = field(default_factory=dict)

    def add_frame(self, image: np.ndarray, now_ms: float) -> float | None:
        d = decode(image)
        if not d.ok:
            self.rejects[d.reason] = self.rejects.get(d.reason, 0) + 1
            return None
        raw = (now_ms - d.ms) % WRAP
        # 延迟不可能超过 max_accept_ms；超了说明解错了或没对准
        if raw > self.max_accept_ms:
            self.rejects["out-of-range"] = self.rejects.get("out-of-range", 0) + 1
            return None
        self.samples.append(raw)
        return raw

    # -- 统计量 ---------------------------------------------------------
    @property
    def n(self) -> int:
        return len(self.samples)

    def _pct(self, p: float) -> float:
        return float(np.percentile(self.samples, p))

    def summary(self) -> dict:
        if not self.samples:
            return {"n": 0, "rejects": dict(self.rejects)}
        return {
            "n": self.n,
            "median_ms": self._pct(50),
            "p95_ms": self._pct(95),
            "min_ms": float(np.min(self.samples)),
            "max_ms": float(np.max(self.samples)),
            "jitter_std_ms": float(np.std(self.samples)),
            "rejects": dict(self.rejects),
        }


def verdict(median_ms: float) -> str:
    """给一个人话结论。体感游戏对延迟的容忍度参考值。"""
    if median_ms < 100:
        return "优秀 —— 可以放心做实时的体感控制"
    if median_ms < 160:
        return "可用 —— 体感控制手感正常，快速动作略有滞后"
    if median_ms < 250:
        return "偏慢 —— 能玩，但闪避类操作会有明显延迟感，需要靠预测补偿"
    return "太慢 —— 不适合直接驱动实时玩法，建议改用相机本机录制 + 事后对齐"


def annotate(image: np.ndarray, d: Decode, now_ms: float | None = None) -> np.ndarray:
    """把检测结果画到帧上，用于人工确认解码器有没有锁住。"""
    import cv2

    out = image.copy()
    if d.corners is not None:
        cv2.polylines(out, [d.corners.astype(np.int32)], True, (0, 255, 0), 4)
    label = f"decoded={d.ms}ms" if d.ok else f"FAIL:{d.reason}"
    if d.ok and now_ms is not None:
        label += f"  latency={(now_ms - d.ms) % WRAP:.0f}ms"
    cv2.putText(out, label, (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                (0, 255, 0) if d.ok else (0, 0, 255), 3)
    if d.card is not None:
        thumb = cv2.resize(d.card, (PAGE_W // 4, PAGE_H // 4))
        out[out.shape[0] - thumb.shape[0]:, :thumb.shape[1]] = thumb
    return out
