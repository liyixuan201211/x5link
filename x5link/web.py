"""本地网页服务：把 X5 的实时画面喂给浏览器，做 360° 拖动预览。

为什么用 MJPEG 转发：
    ffmpeg 直接输出 JPEG 流，Python 只负责切包和转发，完全不碰 2880x1440 的
    原始帧（那样会把实时性拖垮）。浏览器用 <img src="/stream.mjpg"> 就能收。

为什么在浏览器里做「无畸变」：
    X5 给的是等距柱状（经纬图），直接看就是那种中间鼓、两边拉的鱼眼感。
    查看器里每个像素反推一条视线方向，再用经纬图公式去采样 —— 等价于
    x360/projection.py 里那套「等距柱状 -> 透视」，只是搬到 GPU 上实时跑，
    所以任何角度都是正常的直线透视，拖动就是在转视角。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VIEWER = Path(__file__).with_name("viewer.html")


class JpegSplitter:
    """从连续的字节流里切出一个个完整 JPEG（SOI FFD8 ... EOI FFD9）。"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buf += chunk
        out = []
        while True:
            start = self.buf.find(b"\xff\xd8")
            if start < 0:
                self.buf.clear()
                break
            end = self.buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start:
                    del self.buf[:start]      # 丢掉 SOI 之前的垃圾
                break
            out.append(bytes(self.buf[start:end + 2]))
            del self.buf[:end + 2]
        return out


class MjpegSource:
    """一路 MJPEG：ffmpeg 出图，读线程切包，转发给所有连着的浏览器。

    ffmpeg 挂了会自动重拉。但**光靠"进程退出就重拉"是不够的**，实测有两种它
    兜不住的死法，都在这里补上了：

    1. **设备序号会变**。AVFoundation 的枚举顺序不稳定（同一台机器上见过
       [0]=Insta360/[1]=FaceTime，过一会儿反过来）。序号只在启动时解析一次的话，
       顺序一翻 ffmpeg 就打开了错误的设备（FaceTime 不支持 2880x1440），报
       "Selected video size is not supported"，然后一帧都不出。
       -> 每次起 ffmpeg 前按**名字**重新解析序号（`_resolve_index`）。
    2. **ffmpeg 活着但不出帧**。这时 `stdout.read()` 会永久阻塞，循环再也回不到
       重启逻辑；加上 stderr 被丢进 DEVNULL，表现就是"服务在跑、状态正常、
       画面不动、没有任何日志"。
       -> 看门狗线程：`stall_timeout` 秒没有新帧就 kill 掉重来（`_watch`），
          并把 ffmpeg 的 stderr 落到文件里（`ffmpeg_log`）。
    """

    def __init__(self, index: int, width: int, height: int, fps: int = 30,
                 scale: tuple[int, int] | None = None, quality: int = 5,
                 pixel_format: str | None = None,
                 dev_name: str | None = None,
                 stall_timeout: float = 3.0,
                 ffmpeg_log: str | None = None):
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("找不到 ffmpeg")

        if scale:
            self.out_w, self.out_h = scale
        else:
            # 默认**不再缩小**，直接推原生分辨率。
            # 相机那边已经在压了，我们这层再缩一次就是白白丢掉真实细节。
            self.out_w, self.out_h = width, height

        self.index, self.width, self.height, self.fps = index, width, height, fps
        self.quality, self.pixel_format = quality, pixel_format
        self.dev_name = dev_name              # 按名字重新解析序号用
        self.stall_timeout = float(stall_timeout)
        #: 按名字找不到相机时的重试间隔。比稳态重试慢得多 —— 相机没插的时候
        #: 没必要每 3 秒开一次别的设备，那是纯浪费（实测会累积到 1024 次重启）。
        self.missing_backoff = 5.0
        self.camera_present: bool | None = None
        self.ffmpeg_log = ffmpeg_log or os.path.join(
            tempfile.gettempdir(), "x5link-ffmpeg.log")

        self.latest: bytes | None = None
        self.latest_t = 0.0
        self.frames = 0
        self._t0 = time.time()
        self._last_publish = time.time()
        self.restarts = 0
        self.notes: list[str] = []            # 序号变化 / 看门狗动作，给 /status 看
        self.clients: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._run, daemon=True)
        self._reader.start()
        self._watchdog = threading.Thread(target=self._watch, daemon=True)
        self._watchdog.start()

    def _note(self, msg: str):
        self.notes.append(f"[{time.strftime('%T')}] {msg}")
        del self.notes[:-20]

    def _resolve_index(self):
        """每次起 ffmpeg 前按名字重新问一次序号；**找不到就返回 None**。

        这里绝不能"问不到就退回上次那个序号" —— 退回旧序号等于去打开
        「现在恰好占着那个序号」的设备（通常是内置 FaceTime 摄像头），
        然后永远打不开 2880x1440、永远没有帧、看门狗无限重启。
        实测这样会把 restarts 累积到 1024 次，而画面一直不动、日志里只有
        FaceTime 的模式列表 —— 相机明明不在，程序却在跟另一台相机较劲。
        相机不在，就该老老实实说"相机不在"。
        """
        if not self.dev_name:
            return self.index
        try:
            from .capture import match_device
            dev = match_device(self.dev_name)
        except Exception:
            dev = None
        if dev is None:
            self._note(f"找不到「{self.dev_name}」：相机没接 / 没开机 / 不在 webcam 模式")
            return None
        if dev.index != self.index:
            self._note(f"设备序号变了：{self.index} -> {dev.index}（{dev.name}），已跟随")
            self.index = dev.index
        return self.index

    def _watch(self):
        """看门狗：超过 stall_timeout 没有新帧就把 ffmpeg 干掉，让 _run 重拉。"""
        while not self._stop.is_set():
            time.sleep(0.5)
            # 首帧要单独给宽限：2880x1440 这种大流量的相机，从打开到吐出第一帧
            # 可能要好几秒，拿"稳态 3 秒"去卡它会把正常的启动也当成卡死。
            grace = (max(self.stall_timeout * 4, 12.0) if self.latest_t <= 0
                     else self.stall_timeout)
            idle = time.time() - max(self._last_publish, self._t0)
            if idle > grace:
                p = self.proc
                if p is not None and p.poll() is None:
                    self._note(f"看门狗：{idle:.1f}s 没有新帧，重启采集")
                    self.restarts += 1
                    try:
                        p.kill()
                    except Exception:
                        pass
                    # 重启后重新开始计宽限（否则会连环触发）
                    self._last_publish = time.time()
                    self._t0 = self._last_publish

    # -- ffmpeg 命令 ----------------------------------------------------
    def _cmd(self):
        # 相机不在就干脆不起 ffmpeg（返回 None），别去打开别的设备。
        idx = self._resolve_index()
        if idx is None:
            return None
        # loglevel 用 warning 而不是 error：真正要排的那两类故障
        # （"Selected video size/pixel format ... not supported"）都是 **warning**，
        # 用 error 级别会被吞掉，于是日志文件是空的、什么都查不到。
        # warning 平时几乎不输出，代价可以忽略。
        c = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
             "-f", "avfoundation", "-framerate", str(self.fps)]
        if self.pixel_format:
            c += ["-pixel_format", self.pixel_format]
        c += ["-video_size", f"{self.width}x{self.height}",
              "-i", str(idx),
              "-map", "0:v",
              "-vf", f"scale={self.out_w}:{self.out_h}",
              # -r 不能省：avfoundation 报的时基是假的，不锁帧率 ffmpeg 会无限补帧
              "-r", str(self.fps),
              "-f", "mjpeg", "-q:v", str(self.quality),
              "pipe:1"]
        return c

    def _run(self):
        splitter = JpegSplitter()
        while not self._stop.is_set():
            errf = None
            try:
                cmd = self._cmd()
                if cmd is None:
                    # 相机不在：**不要**去开别的设备，也不要让看门狗空转 ——
                    # 重新计宽限、歇久一点再看。状态用 note 说清楚。
                    self.camera_present = False
                    self.proc = None
                    self._t0 = self._last_publish = time.time()
                    if not self._stop.is_set():
                        time.sleep(self.missing_backoff)
                    continue
                self.camera_present = True
                # stderr 不再丢进 DEVNULL —— 之前"没有任何日志"就是这么来的。
                # 每次重拉都覆盖写，文件里永远是最近一次尝试的报错。
                errf = open(self.ffmpeg_log, "wb")
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                             stderr=errf)
                while not self._stop.is_set():
                    chunk = self.proc.stdout.read(65536)
                    if not chunk:
                        break
                    for jpg in splitter.feed(chunk):
                        self._publish(jpg)
            except Exception as e:
                self._note(f"采集异常：{e!r}")      # 原来这里是 except: pass
            finally:
                if errf is not None:
                    try:
                        errf.close()
                    except Exception:
                        pass
                if self.proc:
                    try:
                        self.proc.terminate()
                        self.proc.wait(timeout=5)
                    except Exception:
                        try:
                            self.proc.kill()
                        except Exception:
                            pass
                    self.proc = None
            if not self._stop.is_set():
                time.sleep(1.0)            # 断了就歇一秒重来


    def _publish(self, jpg: bytes):
        self.latest = jpg
        self.latest_t = time.time()
        self._last_publish = self.latest_t     # 看门狗靠它判断"还活着"
        self.frames += 1
        with self.lock:
            for q in list(self.clients):
                if q.full():
                    try:
                        q.get_nowait()     # 丢掉旧的，保证低延迟
                    except queue.Empty:
                        pass
                try:
                    q.put_nowait(jpg)
                except queue.Full:
                    pass

    # -- 客户端 ---------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self.lock:
            self.clients.discard(q)

    @property
    def fps_actual(self) -> float:
        return self.frames / max(1e-6, time.time() - self._t0)

    def start(self):
        self._t0 = time.time()
        return self

    def close(self):
        self._stop.set()
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass


def make_handler(src: MjpegSource):
    # 浏览器把自己实测的「真实显示帧率」报回来，这样从服务端就能读到
    # 用户那块屏幕上到底跑了多少帧（不用去观察他的浏览器窗口）。
    state: dict = {"shown_fps": None, "shown_t": 0.0, "shown_res": None}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):        # 别刷屏
            pass

        def _send(self, code, ctype, body: bytes, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]

            if path in ("/", "/index.html"):
                # 每次读盘：改完 viewer.html 只要刷新浏览器就生效，不用重启服务
                self._send(200, "text/html; charset=utf-8", VIEWER.read_bytes())
                return

            if path == "/stream.mjpg":
                self.send_response(200)
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store, no-cache")
                self.send_header("Pragma", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                q = src.subscribe()
                try:
                    while True:
                        jpg = q.get(timeout=15)
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(b"Content-Length: " + str(len(jpg)).encode()
                                         + b"\r\n\r\n")
                        self.wfile.write(jpg)
                        self.wfile.write(b"\r\n")
                except Exception:
                    pass
                finally:
                    src.unsubscribe(q)
                return

            if path == "/snapshot.jpg":
                if src.latest is None:
                    self._send(503, "text/plain; charset=utf-8",
                               "还没有画面".encode())
                else:
                    self._send(200, "image/jpeg", src.latest)
                return

            if path == "/report":
                # 浏览器回传：它实测到的真实显示帧率（内容真的变了的次数/秒）
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                try:
                    if "shown" in qs:
                        state["shown_fps"] = float(qs["shown"][0])
                        state["shown_t"] = time.time()
                    if "res" in qs:
                        state["shown_res"] = qs["res"][0]
                except Exception:
                    pass
                self._send(200, "text/plain; charset=utf-8", b"ok")
                return

            if path == "/status":
                body = json.dumps({
                    "frames": src.frames,
                    "clients": len(src.clients),
                    "stream": [src.out_w, src.out_h],
                    "camera_mode": [src.width, src.height],
                    "last_frame_age_s": round(time.time() - src.latest_t, 2)
                    if src.latest_t else None,
                    # ↓ 来自浏览器实测（用户屏幕上真正显示出来的帧率）
                    "browser_shown_fps": state["shown_fps"],
                    "browser_shown_age_s": round(time.time() - state["shown_t"], 1)
                    if state["shown_t"] else None,
                    "browser_texture": state["shown_res"],
                    # ↓ 采集侧的自述：序号是否变过、看门狗是否动过手。
                    #   出问题先看这两个，别再去猜"为什么画面不动"。
                    "device_index": src.index,
                    "camera_present": src.camera_present,
                    "restarts": src.restarts,
                    "notes": src.notes[-5:],
                }, ensure_ascii=False).encode()
                self._send(200, "application/json; charset=utf-8", body)
                return

            self._send(404, "text/plain; charset=utf-8", b"not found")

    return Handler


def serve(dev, args) -> int:
    import webbrowser

    scale = tuple(args.stream_scale) if args.stream_scale else None
    src = MjpegSource(dev.index, args.width, args.height, args.fps,
                      scale=scale, quality=args.quality,
                      pixel_format=args.pixel_format,
                      # 把名字传进去：序号会在重拉时按名字重新解析（见类注释）
                      dev_name=dev.name).start()

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(src))
    url = f"http://{args.host}:{args.port}/"
    print(f"设备：[{dev.index}] {dev.name}")
    print(f"相机模式：{args.width}x{args.height} @ {args.fps}fps")
    print(f"推流尺寸：{src.out_w}x{src.out_h}（MJPEG q={args.quality}）")
    print(f"看门狗：{src.stall_timeout:.0f}s 没有新帧就重启采集")
    print(f"ffmpeg 日志：{src.ffmpeg_log}")
    print(f"打开：{url}")
    print("拖动看四周，滚轮缩放。Ctrl-C 结束。")

    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        src.close()
        httpd.server_close()
    return 0
