"""相机取流与同时录制。

取流用 AVFoundation（macOS 原生），录制交给 ffmpeg 子进程吃原始帧。
两者共用同一条帧通路，所以「预览的同时还能存下素材」是被证明的，不是声称的。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass


# ---------------------------------------------------------------- 设备枚举

@dataclass
class Device:
    index: int
    name: str
    kind: str            # "video" | "audio"


_SECTION_RE = re.compile(r"AVFoundation (video|audio) devices:")
_ENTRY_RE = re.compile(r"\]\s*\[(\d+)\]\s*(.+?)\s*$")


def list_devices() -> list[Device]:
    """列出 AVFoundation 设备。

    OpenCV 在 macOS 上拿不到设备名，所以这里问 ffmpeg。
    注意 ffmpeg 列完设备后会以非零码退出，属于正常现象。
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("找不到 ffmpeg")

    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation",
         "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    devices: list[Device] = []
    kind = None
    for line in proc.stderr.splitlines():
        sec = _SECTION_RE.search(line)
        if sec:
            kind = sec.group(1)
            continue
        m = _ENTRY_RE.search(line)
        if m and kind:
            devices.append(Device(int(m.group(1)), m.group(2), kind))
    return devices


def match_device(name: str | None = None, kind: str = "video") -> Device | None:
    """按名字片段找设备（不区分大小写）。name=None 时优先找影石相机。"""
    wanted = (name or "insta360").lower()
    candidates = [d for d in list_devices() if d.kind == kind]
    for d in candidates:
        if wanted in d.name.lower():
            return d
    return None


# ---------------------------------------------------------------- 取流

def open_capture(index: int, timeout_s: float | None = None):
    """打开一路设备。

    timeout_s=None（默认）：在**主线程**打开。
      这一点很重要 —— macOS 的摄像头授权弹窗只有主线程能弹，
      放到子线程会让 OpenCV 报
      "can not spin main run loop from other thread"，于是永远拿不到授权。

    timeout_s=秒数：放到子线程并限时，超时给出明确报错（不会卡死），
      但**不会弹授权窗**，所以只适合「权限已经给过了」的无人值守场景。
    """
    import threading

    import cv2

    if timeout_s is None:
        cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            raise RuntimeError(
                f"打不开视频设备 #{index}。\n"
                "  如果是第一次用，先不带 --open-timeout 跑一次，让系统弹摄像头授权窗并点允许；\n"
                "  或到 系统设置 > 隐私与安全性 > 摄像头 里给运行本程序的 App 打勾。")
        return cap

    # 子线程模式：跳过 OpenCV 自己的授权申请（子线程弹不出来，只会刷错误日志）
    os.environ.setdefault("OPENCV_AVFOUNDATION_SKIP_AUTH", "1")
    box: dict = {}

    def _open():
        try:
            box["cap"] = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        except Exception as e:  # pragma: no cover
            box["err"] = e

    t = threading.Thread(target=_open, daemon=True)
    t.start()
    t.join(timeout_s)

    if t.is_alive():
        raise RuntimeError(
            f"打开设备 #{index} 超时（{timeout_s:.0f} 秒无响应）。\n"
            "  macOS 十有八九是没给摄像头权限：\n"
            "  系统设置 > 隐私与安全性 > 摄像头 -> 勾上运行本程序的 App，然后重开程序。\n"
            "  （设备被别的程序独占时也会这样。）")
    if "err" in box:
        raise RuntimeError(f"打开设备 #{index} 失败：{box['err']}")
    cap = box.get("cap")
    if cap is None or not cap.isOpened():
        raise RuntimeError(f"打不开视频设备 #{index}")
    return cap


class FrameSource:
    """一路相机取流。"""

    def __init__(self, index: int, width: int | None = None,
                 height: int | None = None, fps: int | None = None,
                 open_timeout: float | None = None):
        import cv2

        self._cv2 = cv2
        self.index = index
        self.requested = (width, height, fps)  # 请求值
        self.cap = open_capture(index, open_timeout)
        if width:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height:
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if fps:
            self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.actual = (int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                       int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                       self.cap.get(cv2.CAP_PROP_FPS))

    def read(self):
        """返回 (ok, frame, timestamp_ms)。时间戳取帧回到内存的那一刻。"""
        ok, frame = self.cap.read()
        return ok, frame, time.time() * 1000.0

    def close(self):
        try:
            self.cap.release()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------- 录制

class Recorder:
    """把帧写成 H.264 mp4。帧从 stdin 以 bgr24 原始数据喂进去。

    默认用 h264_videotoolbox（Apple 媒体引擎硬件编码）。实测用 libx264 编
    2880x1440 跑不到实时，会把整条链反压到 ~13fps、录出来的时长也对不上。
    """

    def __init__(self, path: str, width: int, height: int, fps: float,
                 crf: int = 18, preset: str = "veryfast",
                 encoder: str = "h264_videotoolbox", bitrate: str = "40M"):
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("找不到 ffmpeg")
        self.path = path
        self.width, self.height, self.fps = width, height, fps
        self.crf, self.preset = crf, preset
        self.encoder, self.bitrate = encoder, bitrate
        self.proc: subprocess.Popen | None = None
        self.frames = 0

    def start(self):
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}", "-r", f"{self.fps:.3f}",
            "-i", "pipe:0",
            "-an",
        ]
        if self.encoder == "libx264":
            cmd += ["-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf)]
        else:
            # 硬件编码用码率控质；allow_sw 允许它在不支持时退回软件
            cmd += ["-c:v", self.encoder, "-b:v", self.bitrate, "-allow_sw", "1"]
        cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-y", self.path]

        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        return self

    def write(self, frame):
        if self.proc is None or self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write(frame.tobytes())
            self.frames += 1
        except BrokenPipeError:
            self.proc = None

    def close(self):
        if self.proc is None:
            return self.path
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            rc = self.proc.wait(timeout=30)
            if rc != 0:
                err = (self.proc.stderr.read() or b"").decode(errors="replace")
                raise RuntimeError(f"ffmpeg 录制失败 (rc={rc}): {err.strip()[:400]}")
        finally:
            self.proc = None
        return self.path

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


class FfmpegSource:
    """取流 + （可选）同时录制，用**两个独立的 ffmpeg 进程**。

    为什么要两个进程：
      - X5 的 UVC 允许两路同时打开（实测 A 录 2880x1440 拿到 33.8fps 实时、
        B 同时拿到 27.8fps）。
      - 录制那条路**完全不经过 Python**，全画质直写文件，所以不会因为
        Python 中转 12.4MB/帧而把实时性拖垮（实测中转会掉到 ~12.5fps，
        录出来的时长还对不上真实时间）。
      - 预览这条路只吐缩小的帧（默认宽 1280），Python 处理起来很便宜。

    为什么不用 OpenCV：macOS 上它不枚举 UVC 模式，只按宽高猜。
    X5 只暴露两档 —— 1920x1080(16:9) 和 2880x1440(2:1 全景)，而 OpenCV 要
    2880x1440 时给了 1552x1552。ffmpeg 用 -video_size 才能精确锁定档位。

    为什么不让一个 ffmpeg 同时输出管道和文件：avfoundation 报的输入时基是
    「1000k fps」，多输出会触发 vsync 无限补帧（dup=71799、时间戳卡 0、进程卡死）。
    """

    def __init__(self, index: int, width: int, height: int, fps: int = 30,
                 scale: tuple[int, int] | None = None, record: str | None = None,
                 crf: int = 18, preset: str = "veryfast",
                 encoder: str = "h264_videotoolbox", bitrate: str = "40M",
                 pixel_format: str | None = None):
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("找不到 ffmpeg")

        self.requested = (width, height)
        # 预览管道默认按宽 1280 等比缩小，够看也够解码；要全画质用 --scale 指定
        if scale:
            self.out_w, self.out_h = scale
        elif width > 1280:
            self.out_w = 1280
            self.out_h = max(2, round(1280 * height / width))
        else:
            self.out_w, self.out_h = width, height

        self.frames = 0
        self.frame_bytes = self.out_w * self.out_h * 3
        self.record = record
        self.returncode: int | None = None
        self.stderr_text = ""

        def _input(idx):
            c = ["-f", "avfoundation", "-framerate", str(fps)]
            if pixel_format:
                c += ["-pixel_format", pixel_format]
            return c + ["-video_size", f"{width}x{height}", "-i", str(idx)]

        # --- 路 A：全画质直写文件，Python 完全不碰 ---
        # 先起它、并等文件真的出现，确认它抢到了设备，再去起预览那路。
        # （实测两路几乎同时开会撞车，第二路打不开设备。）
        self.rec_proc: subprocess.Popen | None = None
        if record:
            rec_cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error"]
                       + _input(index)
                       + ["-map", "0:v", "-an"])
            if encoder == "libx264":
                rec_cmd += ["-c:v", "libx264", "-preset", preset,
                            "-crf", str(crf)]
            else:
                rec_cmd += ["-c:v", encoder, "-b:v", bitrate, "-allow_sw", "1"]
            rec_cmd += ["-pix_fmt", "yuv420p", "-y", record]
            self.rec_cmd = rec_cmd
            self.rec_proc = subprocess.Popen(rec_cmd, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.PIPE)
            deadline = time.time() + 8.0
            while time.time() < deadline and self.rec_proc.poll() is None:
                if os.path.exists(record) and os.path.getsize(record) > 0:
                    break
                time.sleep(0.1)
            if self.rec_proc.poll() is not None:
                err = b""
                if self.rec_proc.stderr:
                    err = self.rec_proc.stderr.read() or b""
                self.rec_proc = None
                raise RuntimeError("录制那一路 ffmpeg 启动失败："
                                   + err.decode(errors="replace").strip()[:400])

        # --- 路 B：缩小的原始帧走管道，给预览 / 延迟解码用 ---
        preview_cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error"]
                       + _input(index)
                       + ["-map", "0:v",
                          "-vf", f"scale={self.out_w}:{self.out_h}",
                          # -r 必须有，否则假时基会让 ffmpeg 无限补帧
                          "-r", str(fps),
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"])
        self.cmd = preview_cmd
        self.proc = subprocess.Popen(preview_cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)

    def read(self):
        """返回 (ok, frame, timestamp_ms)。read(n) 会读到 n 字节或 EOF。"""
        import numpy as np

        if self.proc is None or self.proc.stdout is None:
            return False, None, time.time() * 1000.0
        data = self.proc.stdout.read(self.frame_bytes)
        now = time.time() * 1000.0
        if not data or len(data) < self.frame_bytes:
            return False, None, now
        self.frames += 1
        frame = np.frombuffer(data, np.uint8).reshape(self.out_h, self.out_w, 3)
        return True, frame, now

    @staticmethod
    def _stop(proc, graceful: bool, timeout: float = 20.0):
        """graceful=True 时先 SIGINT 让 ffmpeg 把 mp4 的 moov 写好。"""
        if proc is None:
            return None
        try:
            if graceful:
                proc.send_signal(signal.SIGINT)
            else:
                proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        except Exception:
            pass
        return proc.returncode

    def close(self):
        def _drain(p):
            """进程已经退出后再读 stderr 才不会阻塞。"""
            if p is None or not p.stderr:
                return ""
            try:
                return (p.stderr.read() or b"").decode(errors="replace").strip()
            except Exception:
                return ""

        # 顺序很重要：先停进程，再读它的 stderr。
        # 反过来写会在进程还活着时阻塞在 read() 上，永远退不出来（踩过）。
        rec, self.rec_proc = self.rec_proc, None
        self.rec_rc = self._stop(rec, graceful=True)
        rec_err = _drain(rec)

        prev, self.proc = self.proc, None
        self.returncode = self._stop(prev, graceful=False, timeout=10)
        prev_err = _drain(prev)

        self.rec_error = rec_err
        self.stderr_text = prev_err

        # "Selected pixel format ... not supported" 是 avfoundation 的启动协商噪音，
        # 会自动退回可用格式，不是错误，别报出来吓人。
        import sys
        for tag, msg in (("录制", rec_err), ("预览", prev_err)):
            if msg and "Selected pixel format" not in msg:
                print(f"[ffmpeg {tag}] {msg[:300]}", file=sys.stderr)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FpsMeter:
    """滑动窗口帧率。"""

    def __init__(self, window: int = 30):
        self.window = window
        self.stamps: list[float] = []

    def tick(self, now_s: float | None = None) -> float:
        self.stamps.append(now_s if now_s is not None else time.time())
        if len(self.stamps) > self.window:
            self.stamps.pop(0)
        if len(self.stamps) < 2:
            return 0.0
        span = self.stamps[-1] - self.stamps[0]
        return (len(self.stamps) - 1) / span if span > 0 else 0.0
