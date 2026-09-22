"""x5link 命令行。

    python -m x5link devices                 列出设备
    python -m x5link probe                   逐个分辨率试，看 X5 到底给不给 2880x1440
    python -m x5link preview --record a.mp4  预览 + 同时存素材
    python -m x5link latency                 全屏卡片 + 相机拍屏 -> 量延迟
    python -m x5link selftest                无相机自检
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from .capture import (FfmpegSource, FrameSource, FpsMeter, Recorder, list_devices,
                      match_device)
from .latency import LatencyStats, annotate, decode, render_card, verdict

# X5 官方文档给的参数：Webcam 模式要在采集端手动设成 2880x1440 才是全景 2:1
X5_NATIVE = (2880, 1440)
PROBE_LADDER = [X5_NATIVE, (2560, 1280), (1920, 1080), (1280, 720), (640, 480)]


# ---------------------------------------------------------------- 工具

def resolve_device(spec: str | None, kind: str = "video"):
    """--device 可以是序号，也可以是名字片段；留空则自动找影石。"""
    if spec is None:
        dev = match_device(None, kind)
        if dev is None:
            raise SystemExit(
                "没找到影石相机（名字里含 insta360）。\n"
                "先跑 `python -m x5link devices` 看列表，再用 --device 指定序号。")
        return dev
    if spec.lstrip("-").isdigit():
        for d in list_devices():
            if d.kind == kind and d.index == int(spec):
                return d
        return type("D", (), {"index": int(spec), "name": f"#{spec}", "kind": kind})()
    dev = match_device(spec, kind)
    if dev is None:
        raise SystemExit(f"没找到名字含 {spec!r} 的设备")
    return dev


def measure_fps(src: FrameSource, seconds: float, warmup: int = 5):
    """在给定设备上跑一小段，回报实际帧率与真实分辨率。"""
    meter = FpsMeter(window=60)
    got = 0
    t_end = time.time() + seconds
    while time.time() < t_end:
        ok, frame, _ = src.read()
        if not ok:
            continue
        got += 1
        if got > warmup:
            meter.tick()
    return meter.tick() if meter.stamps else 0.0, got


# ---------------------------------------------------------------- devices

def cmd_devices(args) -> int:
    devs = list_devices()
    video = [d for d in devs if d.kind == "video"]
    audio = [d for d in devs if d.kind == "audio"]

    print("视频设备：")
    if not video:
        print("  （无）")
    for d in video:
        tag = "  <-- 影石" if "insta360" in d.name.lower() else ""
        print(f"  [{d.index}] {d.name}{tag}")
    print("\n音频设备：")
    for d in audio:
        print(f"  [{d.index}] {d.name}")

    if not any("insta360" in d.name.lower() for d in video):
        print("\n提示：没看到 Insta360。X5 需要：开机 -> 用原装线连电脑 -> "
              "相机屏幕弹出选单里选「USB 摄像头」。")
    return 0


# ---------------------------------------------------------------- probe

def cmd_probe(args) -> int:
    dev = resolve_device(args.device)
    print(f"设备：[{dev.index}] {dev.name}\n")
    print(f"{'请求分辨率':>14} | {'实际分辨率':>14} | {'fps':>6} | 说明")
    print("-" * 66)

    results = []
    for (w, h) in PROBE_LADDER:
        try:
            src = FrameSource(dev.index, w, h, args.fps,
                              open_timeout=args.open_timeout)
        except RuntimeError as e:
            print(f"{f'{w}x{h}':>14} | {'打不开':>14} | {'-':>6} | {e}")
            results.append({"requested": [w, h], "ok": False, "error": str(e)})
            continue
        aw, ah, _ = src.actual
        fps, got = measure_fps(src, args.seconds)
        src.close()
        honored = (aw, ah) == (w, h)
        note = "请求被采纳" if honored else "被驱动改成了别的分辨率"
        if (aw, ah) == X5_NATIVE:
            note = "全景 2:1 已拿到 ✓"
        print(f"{f'{w}x{h}':>14} | {f'{aw}x{ah}':>14} | {fps:>6.1f} | {note}")
        results.append({"requested": [w, h], "actual": [aw, ah],
                        "fps": round(fps, 2), "ok": True, "frames": got})
        time.sleep(0.4)

    native = next((r for r in results if r.get("actual") == list(X5_NATIVE)), None)
    print()
    if native:
        print(f"结论：能拿到 {X5_NATIVE[0]}x{X5_NATIVE[1]} 全景 2:1，"
              f"约 {native['fps']} fps —— 满足第 1 步要求。")
    else:
        best = max((r for r in results if r.get("ok")),
                   key=lambda r: r["actual"][0] * r["actual"][1], default=None)
        if best:
            print(f"结论：拿不到 {X5_NATIVE[0]}x{X5_NATIVE[1]}；"
                  f"最高只到 {best['actual'][0]}x{best['actual'][1]}。"
                  "检查相机固件是否为最新、线是否为原装、以及相机端选的是不是「USB 摄像头」。")

    if args.json:
        print("\n" + json.dumps(results, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- preview

def cmd_preview(args) -> int:
    import cv2

    dev = resolve_device(args.device)
    use_ff = args.backend == "ffmpeg"

    if use_ff:
        src = FfmpegSource(dev.index, args.width, args.height, args.fps,
                           scale=tuple(args.scale) if args.scale else None,
                           record=args.record, crf=args.crf,
                           encoder=args.encoder, bitrate=args.bitrate)
        aw, ah, afps = src.out_w, src.out_h, float(args.fps)
        rec = None
    else:
        src = FrameSource(dev.index, args.width, args.height, args.fps,
                          open_timeout=args.open_timeout)
        aw, ah, afps = src.actual
        rec = None
        if args.record:
            rec = Recorder(args.record, aw, ah, afps or args.fps,
                           crf=args.crf, encoder=args.encoder,
                           bitrate=args.bitrate).start()

    print(f"设备：[{dev.index}] {dev.name}（后端 {args.backend}）")
    print(f"取流：请求 {args.width}x{args.height} @ {args.fps}fps -> 拿到 {aw}x{ah}"
          + ("（管道里缩过，减轻预览负担）" if args.scale else ""))
    if args.record:
        who = "独立 ffmpeg 进程全画质直写，不经过 Python" if use_ff else "Python 侧写"
        print(f"录制：{args.record}（{args.encoder}，{who}）")
    print("按 q 或 ESC 结束" + (f"，或 {args.seconds}s 后自动结束" if args.seconds else ""))

    meter = FpsMeter(window=30)
    t0 = time.time()
    frames = 0
    win = "x5link preview"
    if not args.no_window:
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    try:
        while True:
            ok, frame, t_read = src.read()
            if not ok:
                print("\n!! 读帧失败（相机断连，或 ffmpeg 已退出）", file=sys.stderr)
                break
            frames += 1
            fps = meter.tick(t_read / 1000.0)

            if rec is not None:
                rec.write(frame)           # 存的是干净帧，不带叠加信息

            written = src.frames if use_ff else frames
            if args.no_window:
                if frames % 30 == 0:
                    print(f"\r{frames} 帧  {fps:5.1f} fps  已写 {written}",
                          end="", flush=True)
            else:
                hud = frame.copy()
                cv2.putText(hud, f"{fps:5.1f} fps  {aw}x{ah}", (24, 56),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 255, 0), 3)
                if args.record:
                    cv2.circle(hud, (aw - 60, 56), 18, (0, 0, 255), -1)
                    cv2.putText(hud, f"REC {written}", (aw - 300, 66),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
                tw = min(aw, 1400)
                show = cv2.resize(hud, (tw, int(tw * ah / aw)))
                cv2.imshow(win, show)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break

            if args.seconds and time.time() - t0 >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        loop_end = time.time()          # 先记时间：下面的 close() 可能要好幾秒
        src.close()
        if rec is not None:
            path = rec.close()
            print(f"\n已保存：{path}（{rec.frames} 帧）")
        elif use_ff and args.record:
            print(f"\n已保存：{args.record}（{src.frames} 帧）")
        if not args.no_window:
            cv2.destroyAllWindows()

    dur = loop_end - t0                 # 只算采集循环本身，不含收尾
    print(f"结束：{frames} 帧 / {dur:.1f}s，平均 {frames / dur:.1f} fps")
    return 0


# ---------------------------------------------------------------- latency

def cmd_latency(args) -> int:
    import cv2

    dev = resolve_device(args.device)
    if args.backend == "ffmpeg":
        src = FfmpegSource(dev.index, args.width, args.height, args.fps,
                           scale=tuple(args.scale) if args.scale else None)
        aw, ah = src.out_w, src.out_h
    else:
        src = FrameSource(dev.index, args.width, args.height, args.fps,
                          open_timeout=args.open_timeout)
        aw, ah, _ = src.actual
    print(f"设备：[{dev.index}] {dev.name}  取流 {aw}x{ah}（后端 {args.backend}）")

    win = "x5link latency card"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    if args.window_pos:
        x, y = (int(v) for v in args.window_pos.split(","))
        cv2.moveWindow(win, x, y)
    cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    print(f"屏幕已全屏显示黑白时间戳卡片。把相机对准屏幕，"
          f"让卡片占满画面中间大部分区域。")
    print(f"采样 {args.seconds}s，按 q 或 ESC 提前结束。\n")

    stats = LatencyStats()
    meter = FpsMeter(window=60)
    intervals: list[float] = []
    t0 = time.time()
    last_draw = None
    evidence_dir = args.evidence
    if evidence_dir:
        from pathlib import Path
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)

    try:
        while time.time() - t0 < args.seconds:
            now_ms = time.time() * 1000.0
            if last_draw is not None:
                intervals.append(now_ms - last_draw)
            last_draw = now_ms

            cv2.imshow(win, render_card(int(now_ms)))
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break

            ok, frame, t_read = src.read()
            if not ok:
                continue
            meter.tick(t_read / 1000.0)
            lat = stats.add_frame(frame, t_read)

            if lat is not None and stats.n % 15 == 0:
                print(f"\r已采 {stats.n:4d} 帧  当前 {lat:6.1f}ms  "
                      f"中位 {stats.summary()['median_ms']:6.1f}ms", end="", flush=True)
            if evidence_dir and stats.n % args.evidence_every == 0:
                d = decode(frame)
                cv2.imwrite(f"{evidence_dir}/frame_{stats.n:05d}.png",
                            annotate(frame, d, t_read))
    except KeyboardInterrupt:
        pass
    finally:
        src.close()
        cv2.destroyAllWindows()

    print("\n")
    s = stats.summary()
    if not s.get("n"):
        print("一帧都没解出来。检查：")
        print("  - 相机是否对准了屏幕上的卡片")
        print("  - 卡片是否占画面足够大（至少 1/4 宽）")
        print("  - 卡片是否过曝或被环境光淹没（把屏幕调亮、房间调暗）")
        print(f"  拒收原因统计：{s.get('rejects')}")
        return 1

    # 卡片是每帧重画一次，所以量到的值含「0 ~ 一个刷新周期」的量化偏置，
    # 平均偏高半个周期。这里把已知偏置扣掉，给出更接近真值的估计。
    period = (sum(intervals) / len(intervals)) if intervals else 0.0
    bias = period / 2.0

    print("=" * 62)
    print(f"样本数        {s['n']}")
    print(f"中位延迟      {s['median_ms']:.1f} ms   （含 {bias:.1f}ms 刷新量化偏置）")
    print(f"去偏置估计    {s['median_ms'] - bias:.1f} ms   <-- 更接近真实端到端延迟")
    print(f"p95           {s['p95_ms']:.1f} ms")
    print(f"最小 / 最大   {s['min_ms']:.1f} / {s['max_ms']:.1f} ms")
    print(f"抖动(标准差)  {s['jitter_std_ms']:.1f} ms")
    print(f"卡片刷新周期  {period:.1f} ms（≈ 相机帧率 {1000 / period:.1f} fps）" if period else "")
    print(f"拒收          {s.get('rejects') or '无'}")
    print("=" * 62)
    print(f"判定：{verdict(s['median_ms'] - bias)}")

    if args.json:
        out = dict(s)
        out["bias_ms"] = bias
        out["debias_ms"] = s["median_ms"] - bias
        out["device"] = dev.name
        out["capture"] = [aw, ah]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- formats

def cmd_formats(args) -> int:
    """列出相机真正暴露给系统的 UVC 打包格式。

    OpenCV 在 macOS 上不会枚举 UVC 模式，只会按宽高去猜，经常猜错
    （实测：要 2880x1440，它给 1552x1552）。ffmpeg 能把真实模式列全。
    """
    dev = resolve_device(args.device)
    print(f"设备：[{dev.index}] {dev.name}\n")

    from pathlib import Path

    helper = (Path(__file__).resolve().parents[1]
              / "X5Link.app/Contents/Resources/avformats")
    if not helper.is_file():
        print(f"!! 找不到原生枚举器：{helper}")
        print("   编译它：")
        print("     clang -fobjc-arc -framework AVFoundation -framework Foundation \\")
        print("       -o X5Link.app/Contents/Resources/avformats tools/avformats.m")
        return 1

    proc = subprocess.run([str(helper), dev.name], capture_output=True, text=True)
    print(proc.stdout.rstrip() or proc.stderr.rstrip())

    print("\n解读：")
    print("  - 标了 <== 2:1 全景 的那一档才是经纬图（通常 2880x1440），游戏要用这一档。")
    print("    方形 / 16:9 的档位是双视图或单镜头，视野不够，带不动体感玩法。")
    print("  - 记下那一档的分辨率，以后 --width/--height 就填它。")
    return 0


# ---------------------------------------------------------------- web

def cmd_web(args) -> int:
    """起本地网页服务：浏览器里 360° 拖动预览，画面无畸变。"""
    dev = resolve_device(args.device)
    from .web import serve
    return serve(dev, args)


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="x5link", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="列出相机设备").set_defaults(func=cmd_devices)

    sp = sub.add_parser("formats", help="列出相机真正暴露的 UVC 打包格式（推荐先跑这个）")
    sp.add_argument("--device", help="序号或名字片段，默认自动找影石")
    sp.set_defaults(func=cmd_formats)

    sp = sub.add_parser("web", help="网页端 360° 拖动预览（无畸变）")
    sp.add_argument("--device")
    sp.add_argument("--width", type=int, default=2880)
    sp.add_argument("--height", type=int, default=1440)
    sp.add_argument("--fps", type=int, default=30)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--stream-scale", type=int, nargs=2, metavar=("W", "H"),
                    help="推流尺寸；默认压到宽 1920，浏览器更省力")
    sp.add_argument("--quality", type=int, default=2,
                    help="MJPEG 质量，2 最好 / 31 最差（默认 2，本机传输不吃带宽）")
    sp.add_argument("--pixel-format", default=None,
                    help="给 avfoundation 指定输入像素格式，消掉启动协商噪音")
    sp.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("probe", help="逐个分辨率试探，确认能否拿到 2880x1440")
    sp.add_argument("--device", help="序号或名字片段，默认自动找影石")
    sp.add_argument("--fps", type=int, default=30)
    sp.add_argument("--seconds", type=float, default=1.5, help="每个分辨率测多久")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("preview", help="实时预览，可同时录制")
    sp.add_argument("--device")
    sp.add_argument("--width", type=int, default=2880)
    sp.add_argument("--height", type=int, default=1440)
    sp.add_argument("--fps", type=int, default=30)
    sp.add_argument("--backend", choices=["ffmpeg", "opencv"], default="ffmpeg",
                    help="取流后端；只有 ffmpeg 能精确锁定 2880x1440 全景档（默认）")
    sp.add_argument("--scale", type=int, nargs=2, metavar=("W", "H"),
                    help="管道内缩放，只影响预览/解码，录制仍是全画质")
    sp.add_argument("--encoder", choices=["h264_videotoolbox", "libx264"],
                    default="h264_videotoolbox",
                    help="录制编码器。默认硬件编码；libx264 在 2880x1440 跑不到实时")
    sp.add_argument("--bitrate", default="40M",
                    help="硬件编码码率（默认 40M，2880x1440 够用）")
    sp.add_argument("--record", metavar="OUT.mp4", help="同时把素材存下来")
    sp.add_argument("--crf", type=int, default=18, help="越小画质越好（默认 18）")
    sp.add_argument("--seconds", type=float, help="跑多少秒后自动停")
    sp.add_argument("--no-window", action="store_true", help="不开预览窗口")
    sp.set_defaults(func=cmd_preview)

    sp = sub.add_parser("latency", help="量端到端延迟")
    sp.add_argument("--device")
    sp.add_argument("--width", type=int, default=2880)
    sp.add_argument("--height", type=int, default=1440)
    sp.add_argument("--fps", type=int, default=30)
    sp.add_argument("--backend", choices=["ffmpeg", "opencv"], default="ffmpeg",
                    help="取流后端；只有 ffmpeg 能精确锁定 2880x1440 全景档（默认）")
    sp.add_argument("--scale", type=int, nargs=2, metavar=("W", "H"),
                    help="管道内缩放。解码卡片够用就行，能显著降负载")
    sp.add_argument("--seconds", type=float, default=15.0)
    sp.add_argument("--window-pos", metavar="X,Y", help="卡片窗口位置，便于放到外接屏")
    sp.add_argument("--evidence", metavar="DIR", help="存带标注的抽帧，供人工确认")
    sp.add_argument("--evidence-every", type=int, default=30)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_latency)

    sp = sub.add_parser("selftest", help="不需要相机的自检")
    sp.set_defaults(func=lambda a: __import__("x5link.selftest", fromlist=["main"]).main())

    # 需要开设备的子命令统一加一个「打开超时」（默认主线程打开，能弹摄像头授权窗）
    for name in ("probe", "preview", "latency"):
        sub.choices[name].add_argument(
            "--open-timeout", type=float, default=None, metavar="SEC",
            help="在子线程限时打开设备（适合权限已给的无人值守；不会弹授权窗）")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
