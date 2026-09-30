"""网课本机鼠标宏的标定、试运行与正式运行入口。"""

from __future__ import annotations

import argparse
from pathlib import Path
import threading
import time
import traceback
import winsound

from course_switcher.core import SwitchController
from course_switcher.desktop import (
    Calibration,
    WindowsDesktop,
    check_target_point,
    check_window,
    enable_dpi_awareness,
    foreground_window,
    make_calibration,
)


DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"


def _alert(message: str) -> None:
    """在终端显示停止原因，并用系统提示音提醒用户接管。"""

    print(f"\n已停止：{message}", flush=True)
    try:
        winsound.MessageBeep()
    except RuntimeError:
        pass


def _focus_countdown() -> None:
    """给用户切回已登录课程浏览器的时间。"""

    print("请在 5 秒内切到已登录的课程页面，并保持窗口位置与缩放不变。")
    for remaining in range(5, 0, -1):
        print(f"  {remaining}...", flush=True)
        time.sleep(1)


def run(config_path: Path, *, dry_run: bool) -> int:
    """识别计时并按状态机操作；试运行只报告拟执行的点击。"""

    config = Calibration.load(config_path)
    desktop = WindowsDesktop()
    _focus_countdown()
    target = foreground_window()
    check_window(config, target.handle)
    reading, raw = desktop.read_timer(config)
    if reading is None:
        raise ValueError(f"启动时未识别到计时：{raw!r}。请重新标定")
    print(f"已识别课程计时：{raw!r} -> {reading.current}/{reading.total} 秒")
    print("紧急停止：Ctrl+Alt+Q，或将鼠标移到主屏幕左上角；关闭终端也可停止。")

    controller = SwitchController(time.monotonic())
    stop_event = threading.Event()
    from pynput.keyboard import GlobalHotKeys

    listener = GlobalHotKeys({"<ctrl>+<alt>+q": stop_event.set})
    listener.start()
    last_text = ""
    last_scan = time.monotonic() - 10
    try:
        while not stop_event.is_set():
            if not listener.is_alive():
                _alert("紧急停止快捷键监听已失效")
                return 1
            check_window(config, target.handle)
            now = time.monotonic()
            if now - last_scan >= 10:
                marker = desktop.find_blocker(config)
                check_window(config, target.handle)
                last_scan = time.monotonic()
                if marker:
                    _alert(f"画面出现“{marker}”，请手动处理")
                    return 0

            reading, raw = desktop.read_timer(config)
            check_window(config, target.handle)
            if raw != last_text:
                print(f"计时：{raw!r} -> {reading}", flush=True)
                last_text = raw
            decision = controller.update(reading, time.monotonic())
            if decision.action == "stop":
                _alert(decision.reason)
                return 0
            if decision.action in ("click_next", "click_play"):
                if dry_run:
                    print(f"试运行：此刻将执行 {decision.action}（{decision.reason}）；未发送点击。")
                    return 0
                if stop_event.is_set():
                    _alert("收到紧急停止快捷键")
                    return 0
                check_window(config, target.handle)
                point = config.next_point if decision.action == "click_next" else config.play_point
                # 【修改】点击点在标定中心附近随机偏移 2 像素，并核对实际点仍属课程窗口。
                point = desktop.jitter_point(point, 2)
                check_target_point(target.handle, point)
                print(f"{decision.reason}，点击 {decision.action}: {point}", flush=True)
                desktop.click(point)
            stop_event.wait(2)
        _alert("收到紧急停止快捷键")
        return 0
    finally:
        listener.stop()


def main() -> int:
    """解析命令并在所有错误下安全退出，不留下后台点击任务。"""

    parser = argparse.ArgumentParser(description="仅用本机画面识别和鼠标切换网课视频")
    parser.add_argument("command", choices=("calibrate", "dry-run", "run"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="标定文件路径")
    args = parser.parse_args()
    enable_dpi_awareness()
    try:
        if args.command == "calibrate":
            desktop = WindowsDesktop()
            config = make_calibration(desktop)
            config.save(args.config)
            print(f"标定完成：{args.config}")
            return 0
        return run(args.config, dry_run=args.command == "dry-run")
    except KeyboardInterrupt:
        _alert("终端收到 Ctrl+C")
        return 1
    except (FileNotFoundError, ModuleNotFoundError, ValueError, RuntimeError) as exc:
        _alert(str(exc))
        return 1
    except Exception as exc:
        # 【新增】包括鼠标移到屏幕角落的安全异常在内，任何错误都立即终止循环。
        _alert(f"桌面操作已中止：{exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
