"""量 MJPEG 推流的真实能力：相机实际能吐多少帧、各档参数下多少带宽。

必须在 X5Link.app 里跑（裸进程拿不到摄像头权限）：
    open -n X5Link.app --args --exec <venv>/bin/python tools/measure_stream.py
"""

import os
import subprocess
import time

DUR = 6            # 每档测多久
SRC_W, SRC_H = 2880, 1440

CONFIGS = [
    ("1920x960  q5 (现在)", 1920, 960, 5),
    ("1920x960  q2",        1920, 960, 2),
    ("2560x1280 q3",        2560, 1280, 3),
    ("2880x1440 q5",        2880, 1440, 5),
    ("2880x1440 q3",        2880, 1440, 3),
    ("2880x1440 q2",        2880, 1440, 2),
]


def run(cmd, out):
    if os.path.exists(out):
        os.remove(out)
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        p.wait(timeout=DUR + 25)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    dt = time.time() - t0
    data = open(out, "rb").read() if os.path.exists(out) else b""
    n = data.count(b"\xff\xd8")
    err = (p.stderr.read() or b"").decode(errors="replace") if p.stderr else ""
    return n, dt, len(data), err


# --- 1) 相机在这档 2:1 模式下到底能吐多少帧（passthrough，不让 ffmpeg 补帧）---
print("=" * 78)
print("1) 相机真实帧率（-fps_mode passthrough，不补帧，所以帧数就是真相）")
print("=" * 78)
cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
       "-f", "avfoundation", "-framerate", "30",
       "-video_size", f"{SRC_W}x{SRC_H}", "-i", "0",
       "-map", "0:v", "-fps_mode", "passthrough",
       "-f", "mjpeg", "-q:v", "3", "-y", "/tmp/m_true.mjpeg"]
n, dt, size, err = run(cmd, "/tmp/m_true.mjpeg")
media = n / 30.0 if n else 0
print(f"  收到 {n} 帧，墙钟 {dt:.2f}s  ->  实际 {n / dt:.1f} fps")
print(f"  （若按 30fps 时间轴算，这些帧只值 {media:.2f}s，"
      f"说明真实速度是 {n / dt:.1f} fps）")
if err.strip():
    print("  stderr:", err.strip()[:200])

# --- 2) 各档 MJPEG 的帧大小与带宽（用生产参数 -r 30）---
print()
print("=" * 78)
print("2) 各档 MJPEG 推流：帧大小 / 带宽（生产用的 -r 30 恒定帧率）")
print("=" * 78)
print(f"{'档位':<20} {'帧数':>5} {'fps':>6} {'KB/帧':>8} {'MB/s':>7}")
print("-" * 78)

rows = []
for name, w, h, q in CONFIGS:
    out = f"/tmp/m_{w}x{h}_q{q}.mjpeg"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "avfoundation", "-framerate", "30",
           "-video_size", f"{SRC_W}x{SRC_H}", "-i", "0",
           "-map", "0:v", "-vf", f"scale={w}:{h}", "-r", "30",
           "-f", "mjpeg", "-q:v", str(q), "-y", out]
    n, dt, size, err = run(cmd, out)
    kpf = size / n / 1024 if n else 0
    mbs = size / dt / 1e6 if dt else 0
    print(f"{name:<20} {n:>5} {n / dt:>6.1f} {kpf:>8.1f} {mbs:>7.2f}")
    rows.append((name, n / dt, kpf, mbs))
    if err.strip() and "Selected pixel format" not in err:
        print("   stderr:", err.strip()[:160])

print()
print("=" * 78)
print("参考：本机 localhost 传输不是瓶颈；瓶颈在浏览器端的 JPEG 解码 + 纹理上传。")
print("     2880x1440 的 RGBA 纹理每帧 16.6MB，30fps 就是约 500MB/s 的上传量。")
print("=" * 78)
