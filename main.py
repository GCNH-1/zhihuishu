"""网课本机鼠标宏的标定、试运行与正式运行入口。"""

from __future__ import annotations

import argparse
from pathlib import Path
import threading
import time
import traceback
import winsound

from course_switcher.core import SwitchController, TimerReading
from course_switcher.desktop import (
    Calibration,
    WindowsDesktop,
    check_target_point,
    check_window,
    enable_dpi_awareness,
    foreground_window,
    make_calibration,
    make_quiz_calibration,
)
from course_switcher.quiz import (
    MiMoSolver,
    QuizCalibration,
    QuizSnapshot,
    QuizView,
    feedback_result,
    is_quiz_visible,
    option_text_identity,
    parse_quiz_view,
    question_fingerprint,
    question_still_visible,
    quiz_feedback_badge,
    revealed_quiz_answer,
)


DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"
# 【新增】弹窗标定与视频标定独立保存，已有 config.json 无需迁移。
DEFAULT_QUIZ_CONFIG = Path(__file__).resolve().parent / "quiz_config.json"


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


# 【新增】所有答题点击走同一窗口检查和紧急停止检查，避免 API 等待后误点别的程序。
def _quiz_click(
    desktop: WindowsDesktop,
    video_config: Calibration,
    target_handle: int,
    point: tuple[int, int],
    stop_event: threading.Event,
    label: str,
) -> None:
    """校验并点击当前浏览器中的一个答题控件。"""

    if stop_event.is_set():
        raise RuntimeError("收到紧急停止快捷键")
    check_window(video_config, target_handle)
    marker = desktop.find_blocker(video_config)
    if marker in ("验证码", "人机验证", "请完成验证", "captcha"):
        raise RuntimeError(f"画面出现“{marker}”，请手动处理")
    actual = desktop.jitter_point(point, 2)
    check_target_point(target_handle, actual)
    print(f"答题点击 {label}: {actual}", flush=True)
    desktop.click(actual)


# 【新增】只接受点击后新增的明确反馈；未知页面变化或多题界面立即停止。
def _wait_quiz_feedback(
    desktop: WindowsDesktop,
    video_config: Calibration,
    quiz_config: QuizCalibration,
    before: QuizSnapshot,
    target_handle: int,
    stop_event: threading.Event,
) -> tuple[str, str, QuizSnapshot]:
    """等待正确或错误反馈，必要时只点击一次明确的提交按钮。"""

    deadline = time.monotonic() + 9
    submitted = False
    started = time.monotonic()
    while time.monotonic() < deadline:
        if stop_event.wait(0.7):
            raise RuntimeError("收到紧急停止快捷键")
        check_window(video_config, target_handle)
        after = desktop.read_quiz(quiz_config)
        if not is_quiz_visible(after):
            raise RuntimeError("答题弹窗在确认反馈前消失，请手动检查")
        # 【新增】反馈期间题干变化意味着已切题，不能把新题文字当成上一题结果。
        if not question_still_visible(before, after):
            raise RuntimeError("答题期间题目已切换，请手动处理")
        result, feedback = feedback_result(before, after)
        if result is not None:
            return result, feedback, after
        # 【修改】反馈尚未出现时才解析选项和提交按钮；瞬时 OCR 漏检继续等待。
        try:
            view = parse_quiz_view(after)
        except ValueError as exc:
            if "无法可靠定位" in str(exc):
                continue
            raise
        if view.multi_page:
            raise RuntimeError("检测到多题弹窗，此版本停止自动答题")
        # 【新增】多选题可能需要提交；仅在明确识别按钮且未出现即时反馈时点一次。
        if not submitted and view.submit is not None and time.monotonic() - started >= 1.5:
            _quiz_click(desktop, video_config, target_handle, view.submit.center, stop_event, "提交")
            submitted = True
    raise RuntimeError("点击选项后未识别到明确对错反馈，请手动处理")


# 【新增】短暂重读尚未完整显示的选项，避免弹窗刚出现时因一次 OCR 漏检而停止。
# 参数: desktop (WindowsDesktop) 截图 OCR；quiz_config (QuizCalibration) 弹窗位置；
# snapshot (QuizSnapshot) 当前截图；video_config (Calibration) 固定窗口；
# target_handle (int) 浏览器句柄；stop_event (Event) 紧急停止信号。
# 返回: tuple[QuizSnapshot, QuizView] 带完整选项的稳定截图和解析结果。
def _ready_quiz_view(
    desktop: WindowsDesktop,
    quiz_config: QuizCalibration,
    snapshot: QuizSnapshot,
    video_config: Calibration,
    target_handle: int,
    stop_event: threading.Event,
) -> tuple[QuizSnapshot, QuizView]:
    """选项尚未加载或偶发 OCR 漏检时短暂重读，始终不发送点击。"""

    for attempt in range(3):
        try:
            return snapshot, parse_quiz_view(snapshot)
        except ValueError as exc:
            if "无法可靠定位" not in str(exc) or attempt == 2:
                raise
            if stop_event.wait(0.8):
                raise RuntimeError("收到紧急停止快捷键") from exc
            check_window(video_config, target_handle)
            snapshot = desktop.read_quiz(quiz_config)
    raise RuntimeError("答题选项仍未显示")


# 【新增】正常答对与程序重启后接管已答对页面共用关闭流程，保证关闭只点一次。
# 参数: desktop/video_config/quiz_config 提供截图及位置；after 为答后截图；
# target_handle 为窗口句柄；stop_event 为停止信号。返回: TimerReading 播放恢复前的计时。
def _close_quiz_and_resume(
    desktop: WindowsDesktop,
    video_config: Calibration,
    quiz_config: QuizCalibration,
    after: QuizSnapshot,
    target_handle: int,
    stop_event: threading.Event,
) -> TimerReading:
    """点击底部关闭，确认弹窗消失后恢复视频。"""

    close_lines = [line for line in after.lines if line.text.strip() == "关闭"]
    if len(close_lines) != 1:
        raise RuntimeError("无法定位答题弹窗的关闭按钮")
    _quiz_click(desktop, video_config, target_handle, close_lines[0].center, stop_event, "关闭")
    if stop_event.wait(1):
        raise RuntimeError("收到紧急停止快捷键")
    check_window(video_config, target_handle)
    if is_quiz_visible(desktop.read_quiz(quiz_config)):
        raise RuntimeError("点击关闭后弹窗仍存在，请手动处理")
    reading, raw = desktop.read_timer(video_config)
    if reading is None:
        raise RuntimeError(f"答题后无法识别播放器计时：{raw!r}")
    _quiz_click(desktop, video_config, target_handle, video_config.play_point, stop_event, "恢复播放")
    return reading


# 【新增】模型只负责返回选项；点击、一次重试和播放恢复仍由本机状态检查控制。
def _handle_quiz(
    desktop: WindowsDesktop,
    video_config: Calibration,
    quiz_config: QuizCalibration,
    snapshot: QuizSnapshot,
    solver: MiMoSolver,
    target_handle: int,
    stop_event: threading.Event,
    dry_run: bool,
) -> TimerReading | None:
    """处理一题弹窗；试运行仅报告模型答案，正式运行后返回视频计时。"""

    snapshot, view = _ready_quiz_view(desktop, quiz_config, snapshot, video_config, target_handle, stop_event)
    initial_question = question_fingerprint(snapshot)
    if view.multi_page:
        raise RuntimeError("检测到多题弹窗，此版本停止自动答题")
    # 【新增】用户可能在已答对的弹窗上重启程序；明确识别“正确”后直接关闭，避免再次选答案。
    if quiz_feedback_badge(snapshot) == "correct":
        if dry_run:
            print("试运行：当前题已答对，将点击关闭并恢复播放；未发送点击。", flush=True)
            return None
        return _close_quiz_and_resume(desktop, video_config, quiz_config, snapshot, target_handle, stop_event)
    # 【修改】重启时已显示错误的题目，也优先读取页面答案；只允许再改选一次。
    already_wrong = quiz_feedback_badge(snapshot) == "wrong"
    if already_wrong and view.question_type == "multiple":
        raise RuntimeError("无法确认已答错多选题的选中状态，请手动处理")
    answer = revealed_quiz_answer(snapshot, view) if already_wrong else None
    source = "页面正确答案" if answer is not None else "MiMo 判断"
    if answer is None:
        answer = solver.solve(snapshot, view)
    print(f"{source}：{answer.question_type}，选择 {','.join(answer.answers)}", flush=True)
    if dry_run:
        print("试运行：未点击选项、关闭或播放。", flush=True)
        return None
    previous: tuple[str, ...] = ()
    max_attempts = 1 if already_wrong else 2
    for attempt in range(max_attempts):
        check_window(video_config, target_handle)
        current = desktop.read_quiz(quiz_config)
        current, current_view = _ready_quiz_view(
            desktop, quiz_config, current, video_config, target_handle, stop_event
        )
        # 【新增】网络请求可能耗时，点击前复核题干以避免旧答案落到新题。
        if question_fingerprint(current) != initial_question:
            raise RuntimeError("MiMo 返回前题目已变化，请手动处理")
        if current_view.multi_page or sorted(current_view.options) != sorted(view.options):
            raise RuntimeError("答题界面已变化，请手动处理")
        # 【修改】选中后 OCR 的空格、字母标点变化不等于题目变化；仍核对实际选项内容。
        if any(option_text_identity(current_view.options[letter].text)
               != option_text_identity(view.options[letter].text) for letter in view.options):
            raise RuntimeError("题目选项已变化，请手动处理")
        # 【新增】多选重试只切换与上次不同的选项；单选与判断题直接改选。
        letters = set(answer.answers) ^ set(previous) if answer.question_type == "multiple" else set(answer.answers)
        for letter in sorted(letters):
            _quiz_click(desktop, video_config, target_handle, current_view.options[letter].center, stop_event, f"选项 {letter}")
        outcome, feedback, after = _wait_quiz_feedback(
            desktop, video_config, quiz_config, current, target_handle, stop_event
        )
        print(f"答题反馈：{feedback or outcome}", flush=True)
        if outcome == "correct":
            # 【修改】识别结果后统一关闭，避免答后界面再次进入选项解析。
            return _close_quiz_and_resume(desktop, video_config, quiz_config, after, target_handle, stop_event)
        if attempt == max_attempts - 1:
            raise RuntimeError("改选一次后仍显示错误，请手动处理")
        previous = answer.answers
        retry_snapshot, retry_view = _ready_quiz_view(
            desktop, quiz_config, after, video_config, target_handle, stop_event
        )
        # 【修改】页面已公布答案时直接校验并采用；没有公布时才再次请求模型。
        answer = revealed_quiz_answer(retry_snapshot, retry_view)
        source = "页面正确答案" if answer is not None else "MiMo 重试"
        if answer is None:
            answer = solver.solve(retry_snapshot, retry_view, previous, feedback)
        if answer.answers == previous:
            raise RuntimeError("正确答案与刚才的错误选择相同，请手动确认页面反馈")
        print(f"{source}：选择 {','.join(answer.answers)}", flush=True)
    raise RuntimeError("答题状态异常")


def run(config_path: Path, *, dry_run: bool) -> int:
    """识别计时并按状态机操作；试运行只报告拟执行的点击。"""

    config = Calibration.load(config_path)
    # 【新增】没有答题标定时保留旧的视频流程；有标定时启动即校验密钥和窗口布局。
    quiz_config = QuizCalibration.load(DEFAULT_QUIZ_CONFIG) if DEFAULT_QUIZ_CONFIG.exists() else None
    if quiz_config is not None and (
        quiz_config.window_class != config.window_class
        or quiz_config.window_rect != config.window_rect
        or quiz_config.screen_size != config.screen_size
    ):
        raise ValueError("答题标定与视频窗口不一致，请重新运行 calibrate-quiz")
    solver = MiMoSolver() if quiz_config is not None else None
    desktop = WindowsDesktop()
    _focus_countdown()
    target = foreground_window()
    check_window(config, target.handle)
    print("紧急停止：Ctrl+Alt+Q，或将鼠标移到主屏幕左上角；关闭终端也可停止。")

    controller = SwitchController(time.monotonic())
    stop_event = threading.Event()
    from pynput.keyboard import GlobalHotKeys

    listener = GlobalHotKeys({"<ctrl>+<alt>+q": stop_event.set})
    listener.start()
    last_text = ""
    last_scan = time.monotonic() - 10
    try:
        # 【新增】启动时先检查弹窗，允许程序在课程已经暂停出题时接管。
        initial_quiz = desktop.read_quiz(quiz_config) if quiz_config is not None else None
        if initial_quiz is not None and is_quiz_visible(initial_quiz):
            reading = _handle_quiz(
                desktop, config, quiz_config, initial_quiz, solver, target.handle, stop_event, dry_run
            )
            if dry_run:
                return 0
            controller.after_quiz_play(reading, time.monotonic())
        else:
            marker = desktop.find_blocker(config)
            if marker:
                _alert(f"画面出现“{marker}”，请手动处理或运行 calibrate-quiz")
                return 0
            reading, raw = desktop.read_timer(config)
            if reading is None:
                raise ValueError(f"启动时未识别到计时：{raw!r}。请重新标定")
            print(f"已识别课程计时：{raw!r} -> {reading.current}/{reading.total} 秒")
        while not stop_event.is_set():
            if not listener.is_alive():
                _alert("紧急停止快捷键监听已失效")
                return 1
            check_window(config, target.handle)
            # 【新增】弹题优先于计时与旧的测验阻断词，避免遮挡计时后误判为播放故障。
            if quiz_config is not None:
                quiz_snapshot = desktop.read_quiz(quiz_config)
                check_window(config, target.handle)
                if is_quiz_visible(quiz_snapshot):
                    reading = _handle_quiz(
                        desktop, config, quiz_config, quiz_snapshot, solver, target.handle, stop_event, dry_run
                    )
                    if dry_run:
                        return 0
                    controller.after_quiz_play(reading, time.monotonic())
                    last_scan = time.monotonic()
                    stop_event.wait(2)
                    continue
            now = time.monotonic()
            if now - last_scan >= 10:
                marker = desktop.find_blocker(config)
                check_window(config, target.handle)
                last_scan = time.monotonic()
                if marker:
                    suggestion = "；请运行 calibrate-quiz" if marker == "AI助教小智" and quiz_config is None else ""
                    _alert(f"画面出现“{marker}”，请手动处理{suggestion}")
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
    # 【修改】增加单独的弹窗标定命令，因答题弹窗出现时无法同时标定视频计时。
    parser.add_argument("command", choices=("calibrate", "calibrate-quiz", "dry-run", "run"))
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
        if args.command == "calibrate-quiz":
            # 【新增】视频标定先存在，才能验证弹窗是否属于同一固定浏览器窗口。
            desktop = WindowsDesktop()
            video_config = Calibration.load(args.config)
            quiz_config = make_quiz_calibration(desktop, video_config)
            quiz_config.save(DEFAULT_QUIZ_CONFIG)
            print(f"答题标定完成：{DEFAULT_QUIZ_CONFIG}")
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
