"""能不能同时开两路摄像头？

如果能：A 进程全画质录 mp4，B 进程只吐缩小的原始帧给预览/解码 —— 各司其职，
不用 Python 中转到 12.4MB/帧，也就不会把实时性拖垮。

必须跑在 X5Link.app 里（裸 node/终端进程拿不到摄像头权限）：
    open -n X5Link.app --args --exec <venv>/bin/python tools/dual_open_test.py
"""

import os
import subprocess
import time

CAM = "0"
W, H = 960, 480

print("启动 A：全画质录 mp4（6 秒）")
A = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error",
     "-f", "avfoundation", "-framerate", "30", "-video_size", "2880x1440",
     "-i", CAM, "-t", "6",
     "-c:v", "h264_videotoolbox", "-b:v", "40M", "-allow_sw", "1",
     "-pix_fmt", "yuv420p", "-y", "/tmp/dualA.mp4"],
    stderr=subprocess.PIPE)

time.sleep(2)

print("启动 B：缩小原始帧走管道，看能不能在 A 占用时打开")
B = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error",
     "-f", "avfoundation", "-framerate", "30", "-video_size", "2880x1440",
     "-i", CAM, "-vf", f"scale={W}:{H}", "-r", "30",
     "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE)

frame_bytes = W * H * 3
n = 0
t0 = time.time()
while time.time() - t0 < 6:
    data = B.stdout.read(frame_bytes)
    if not data or len(data) < frame_bytes:
        break
    n += 1
dt = time.time() - t0
print(f"B 收到 {n} 帧 / {dt:.1f}s = {n / dt:.1f} fps")

try:
    B.terminate()
    B.wait(timeout=5)
except Exception:
    B.kill()
if B.stderr:
    print("B stderr:", (B.stderr.read() or b"").decode()[:400])

A.wait(timeout=25)
print("A rc =", A.returncode)
if A.stderr:
    print("A stderr:", (A.stderr.read() or b"").decode()[:400])
print("A 文件存在:", os.path.exists("/tmp/dualA.mp4"),
      os.path.getsize("/tmp/dualA.mp4") if os.path.exists("/tmp/dualA.mp4") else 0)
print("结论:", "两路可以共存 ✅" if n > 30 else "B 打不开，设备是独占的 ❌")
