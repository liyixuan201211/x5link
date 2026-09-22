"""用合成数据验证「视觉时间戳」这条路算得对、且扛得住真实拍摄的劣化。

三件事：
  1. 编解码往返：任意毫秒数 -> 40 bit -> 毫秒数，必须一模一样。
  2. 抗劣化：模拟相机斜着拍、画面只占一角、模糊、噪声、有损压缩，仍要解得出来。
  3. 延迟数学：造一个「相机固定慢 D 毫秒」的假世界，量出来的中位数必须等于 D。

没有硬件也能跑，用来证明工具本身是对的 —— 等 X5 接上，量到的数字就可信。
"""

from __future__ import annotations

import sys

import numpy as np

from .latency import (N_CELLS, PAGE_H, PAGE_W, WRAP, LatencyStats, bits_for_ms,
                      decode, ms_from_bits, render_card)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


# ------------------------------------------------------------------ 1. 编解码

def test_roundtrip() -> None:
    print("\n[1] 编解码往返")
    rng = np.random.default_rng(0)
    vals = [0, 1, 2, 255, 256, 65535, 1_000_000, WRAP - 1] + \
           list(rng.integers(0, WRAP, size=500))
    bad = [v for v in vals if ms_from_bits(bits_for_ms(int(v))) != int(v) % WRAP]
    check("往返 508 个随机毫秒数", not bad, f"失败 {len(bad)} 个")

    bits = bits_for_ms(123456)
    check("bit 数固定为 40", bits.size == N_CELLS, f"实际 {bits.size}")
    check("篡改 header 会被拒", ms_from_bits(np.roll(bits, 1)) is None)


# ------------------------------------------------------------------ 2. 抗劣化

def simulate_camera_view(ms: int, rng: np.random.Generator,
                         out_size=(720, 1280), scale: float = 0.45,
                         max_tilt: float = 0.10, blur: int = 3,
                         noise: float = 6.0, quality: int = 70) -> np.ndarray:
    """把卡片放进一个「像相机拍到的」画面：斜着、缩过、糊过、有噪声、有压缩。"""
    import cv2

    card = render_card(ms)
    ch, cw = card.shape[:2]
    oh, ow = out_size

    # 目标四角：随机偏移 + 轻微倾斜
    bw, bh = ow * scale, oh * scale
    jitter = lambda: rng.uniform(-max_tilt, max_tilt) * bw  # noqa: E731
    cx, cy = ow / 2, oh / 2
    src = np.array([[0, 0], [cw - 1, 0], [cw - 1, ch - 1], [0, ch - 1]], np.float32)
    dst = np.array([
        [cx - bw / 2 + jitter(), cy - bh / 2 + jitter()],
        [cx + bw / 2 + jitter(), cy - bh / 2 + jitter()],
        [cx + bw / 2 + jitter(), cy + bh / 2 + jitter()],
        [cx - bw / 2 + jitter(), cy + bh / 2 + jitter()],
    ], np.float32)

    m = cv2.getPerspectiveTransform(src, dst)
    canvas = np.zeros((oh, ow, 3), np.uint8)
    warp = cv2.warpPerspective(card, m, (ow, oh), flags=cv2.INTER_AREA)
    mask = cv2.warpPerspective(np.full((ch, cw), 255, np.uint8), m, (ow, oh))
    canvas[mask > 127] = warp[mask > 127]

    if blur:
        k = blur * 2 + 1
        canvas = cv2.GaussianBlur(canvas, (k, k), 0)
    if noise:
        canvas = np.clip(canvas.astype(np.float32) +
                         rng.normal(0, noise, canvas.shape), 0, 255).astype(np.uint8)
    if quality:
        ok, enc = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok:
            canvas = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    return canvas


def test_robustness() -> None:
    print("\n[2] 抗劣化（斜拍 / 缩小 / 模糊 / 噪声 / JPEG）")
    rng = np.random.default_rng(7)

    clean = render_card(424242)
    d = decode(clean)
    check("干净渲染帧可解", d.ok and d.ms == 424242, d.reason)

    ms = 987654
    view = simulate_camera_view(ms, rng)
    d = decode(view)
    check("模拟相机视角可解", d.ok and d.ms == ms, d.reason or f"ms={d.ms}")

    # 连续 20 帧，看成功率（真实拍摄会抖）
    hits = 0
    for i in range(20):
        v = simulate_camera_view(100000 + i * 33, rng)
        r = decode(v)
        hits += int(r.ok and r.ms == 100000 + i * 33)
    check("20 帧随机视角成功率 >= 90%", hits >= 18, f"{hits}/20")

    # 极端：卡片只占画面很小一块
    small = simulate_camera_view(555001, rng, scale=0.22, blur=5, quality=50)
    d = decode(small)
    check("卡片只占画面 22% 仍可解", d.ok and d.ms == 555001, d.reason or f"ms={d.ms}")


# ------------------------------------------------------------------ 3. 延迟数学

def test_latency_math() -> None:
    print("\n[3] 延迟数学（造一个已知慢 D 毫秒的假相机）")
    rng = np.random.default_rng(11)

    for true_delay in (33.0, 100.0, 250.0, 700.0):
        stats = LatencyStats()
        for i in range(60):
            # 时刻 t 抓到的帧，画面是 (t - D) 那一刻画出来的
            t_ms = 1_700_000_000_000.0 + i * 33.0
            frame = render_card(int(t_ms - true_delay))
            stats.add_frame(frame, t_ms)
        med = stats.summary()["median_ms"]
        check(f"D={true_delay:.0f}ms 量出来一致", abs(med - true_delay) < 0.51,
              f"实得 {med:.2f}ms")

    # 跨越 2**24 回绕：屏幕值的循环不能把答案带偏
    stats = LatencyStats()
    base = float(WRAP - 1000)
    for i in range(40):
        t_ms = base + i * 33.0
        frame = render_card(int(t_ms - 120.0) % WRAP)
        stats.add_frame(frame, t_ms % WRAP + (1 << 40))  # 大步走，强制回绕
    check("跨 mod 2**24 回绕仍正确",
          abs(stats.summary().get("median_ms", -1) - 120.0) < 0.51,
          f"实得 {stats.summary().get('median_ms')}")

    # 坏帧（全黑）必须被拒，不能污染统计
    stats = LatencyStats()
    stats.add_frame(np.zeros((720, 1280, 3), np.uint8), 0.0)
    check("全黑帧被拒", stats.n == 0 and stats.rejects.get("no-card", 0) == 1,
          str(stats.rejects))


def main() -> int:
    print("=" * 68)
    print("x5link 自检 —— 不需要相机")
    print("=" * 68)
    try:
        import cv2
        print(f"OpenCV {cv2.__version__}")
    except ImportError:
        print("!! 没有 opencv-python，无法运行")
        return 2

    test_roundtrip()
    test_robustness()
    test_latency_math()

    print("\n" + "=" * 68)
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败 -> {', '.join(FAILS)}")
        return 1
    print("结果：全部通过。解码器与延迟算法可信，等 X5 接上即可量真实数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
