"""仅使用 Windows 桌面截图、OCR 和系统鼠标完成页面交互。"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import random
import time

from .core import TimerReading, parse_timer


class _Rect(ctypes.Structure):
    """接收 Windows API 返回的窗口边界。"""

    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


@dataclass(frozen=True)
class WindowInfo:
    """当前前台窗口的句柄、类型和位置。"""

    handle: int
    class_name: str
    rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class Calibration:
    """固定窗口布局下，读取计时和执行点击所需的位置。"""

    window_class: str
    window_rect: tuple[int, int, int, int]
    screen_size: tuple[int, int]
    timer_rect: tuple[int, int, int, int]
    hover_point: tuple[int, int]
    next_point: tuple[int, int]
    play_point: tuple[int, int]

    def save(self, path: Path) -> None:
        """保存本机标定结果，供下次启动时复用。"""

        data = {"version": 1, **self.__dict__}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Calibration:
        """读取并校验标定文件，拒绝缺失或越界的坐标。"""

        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError("标定文件版本不受支持，请重新运行 calibrate")
        try:
            config = cls(
                window_class=str(data["window_class"]),
                window_rect=_ints(data["window_rect"], 4),
                screen_size=_ints(data["screen_size"], 2),
                timer_rect=_ints(data["timer_rect"], 4),
                hover_point=_ints(data["hover_point"], 2),
                next_point=_ints(data["next_point"], 2),
                play_point=_ints(data["play_point"], 2),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("标定文件内容不完整，请重新运行 calibrate") from exc
        config.validate()
        return config

    def validate(self) -> None:
        """阻止区域错误或指向窗口外的鼠标点击。"""

        width, height = self.screen_size
        left, top, right, bottom = self.timer_rect
        if not self.window_class or width <= 0 or height <= 0:
            raise ValueError("标定窗口信息无效")
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise ValueError("计时区域无效，请重新标定")
        wl, wt, wr, wb = self.window_rect
        if wl >= wr or wt >= wb:
            raise ValueError("标定窗口边界无效")
        # 【修改】标定时逐点核对实际所属窗口；此处只查屏幕范围，避免缩放造成的矩形误判。
        # 正式点击前还会重新确认按钮坐标仍属于目标浏览器。
        for name, (x, y) in (
            ("悬停", self.hover_point),
            ("下一节", self.next_point),
            ("播放", self.play_point),
        ):
            if not (0 <= x < width and 0 <= y < height):
                raise ValueError(f"{name}坐标不在主屏幕内")


def _ints(value: object, count: int) -> tuple[int, ...]:
    """将配置数组限制为指定数量的整数，避免错误坐标进入鼠标操作。"""

    if not isinstance(value, list) or len(value) != count or any(type(item) is not int for item in value):
        raise ValueError("标定文件坐标格式无效")
    return tuple(value)


def enable_dpi_awareness() -> None:
    """在导入截图库前统一 Windows 逻辑坐标与物理像素。"""

    try:
        # 【修改】API 返回失败时也尝试旧接口，避免截图与鼠标取点使用不同缩放坐标。
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def foreground_window() -> WindowInfo:
    """获取当前真正接受鼠标点击的顶层窗口。"""

    user32 = ctypes.windll.user32
    # 【新增】显式声明 64 位句柄类型，避免默认 c_int 截断窗口句柄。
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(_Rect))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    handle = user32.GetForegroundWindow()
    if not handle:
        raise RuntimeError("找不到前台窗口")
    rect = _Rect()
    if not user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect)):
        raise RuntimeError("无法读取前台窗口位置")
    name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(handle), name, len(name))
    return WindowInfo(handle, name.value, (rect.left, rect.top, rect.right, rect.bottom))


# 【新增】标定依据鼠标实际指向的窗口，避免终端仍为前台时误绑定终端边界。
# 参数: point (tuple[int, int]) 主显示器上的鼠标坐标。
# 返回: 鼠标下方顶层窗口的句柄、类型和边界。
def window_at_point(point: tuple[int, int]) -> WindowInfo:
    """查找屏幕坐标对应的顶层窗口，供标定流程使用。"""

    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(_Rect))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    child = user32.WindowFromPoint(wintypes.POINT(*point))
    if not child:
        raise RuntimeError(f"坐标 {point} 下没有可识别的窗口")
    handle = user32.GetAncestor(wintypes.HWND(child), 2) or child
    rect = _Rect()
    if not user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect)):
        raise RuntimeError(f"无法读取坐标 {point} 对应的窗口边界")
    name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(handle), name, len(name))
    return WindowInfo(handle, name.value, (rect.left, rect.top, rect.right, rect.bottom))


def primary_screen_size() -> tuple[int, int]:
    """读取主显示器尺寸；宏仅在主显示器上标定和运行。"""

    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


class WindowsDesktop:
    """集中管理 OCR、截图、窗口检查及系统鼠标操作。"""

    def __init__(self) -> None:
        # 【新增】MSS 先于 PyAutoGUI 导入，避免屏幕缩放设置造成坐标不一致。
        import mss
        import numpy as np
        import cv2
        import pyautogui
        from rapidocr import RapidOCR

        self.mss = mss.mss()
        self.np = np
        self.cv2 = cv2
        self.mouse = pyautogui
        self.mouse.FAILSAFE = True
        self.ocr = RapidOCR()

    def point(self) -> tuple[int, int]:
        """读取当前系统鼠标所在的屏幕坐标。"""

        # 【修改】用与窗口边界同源的 Win32 坐标取点，避免缩放时 PyAutoGUI 坐标与窗口矩形不一致。
        position = wintypes.POINT()
        user32 = ctypes.windll.user32
        user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        user32.GetCursorPos.restype = wintypes.BOOL
        if not user32.GetCursorPos(ctypes.byref(position)):
            raise RuntimeError("无法读取鼠标位置")
        return position.x, position.y

    def click(self, point: tuple[int, int]) -> None:
        """在标定点发送一次系统鼠标点击。"""

        # 【修改】先经过按钮附近的随机位置，再移动到已校验的点击点，避免每次采用同一路径。
        approach = self.jitter_point(point, 5)
        if approach == point:
            width, _height = primary_screen_size()
            approach = (point[0] + 1 if point[0] + 1 < width else point[0] - 1, point[1])
        self.mouse.moveTo(*approach, duration=random.uniform(0.16, 0.36))
        self.mouse.moveTo(*point, duration=random.uniform(0.12, 0.27))
        self.mouse.click(*point)

    # 【新增】在指定像素半径内给 X、Y 分别取偏移，同时限制在主屏幕内。
    # 参数: point (tuple[int, int]) 中心坐标，radius (int) 最大像素偏移。
    # 返回: tuple[int, int] 可用于悬停或点击的屏幕坐标。
    def jitter_point(self, point: tuple[int, int], radius: int) -> tuple[int, int]:
        """生成不越出主屏幕的随机邻近点。"""

        width, height = primary_screen_size()
        x = max(0, min(width - 1, point[0] + random.randint(-radius, radius)))
        y = max(0, min(height - 1, point[1] + random.randint(-radius, radius)))
        return x, y

    # 【新增】播放器空闲时会隐藏控制栏，因此每次截图前都要产生真实的系统鼠标移动。
    # 参数: point (tuple[int, int]) 标定的控制栏空白位置。
    # 返回: 无；鼠标回到原位置并等待控制栏显现。
    def reveal_controls(self, point: tuple[int, int]) -> None:
        """在控制栏空白处来回移动少量像素，使计时文字重新显示。"""

        x, y = point
        width, _height = primary_screen_size()
        # 【修改】X、Y 每轮独立随机偏移；最终仍停在中心附近，防止越出标定的空白区域。
        outer = self.jitter_point(point, 7)
        if max(abs(outer[0] - x), abs(outer[1] - y)) < 3:
            outer = (x + 4 if x + 4 < width else x - 4, y)
        inner = self.jitter_point(point, 2)
        self.mouse.moveTo(*outer, duration=random.uniform(0.15, 0.32))
        self.mouse.moveTo(*inner, duration=random.uniform(0.12, 0.26))
        time.sleep(random.uniform(0.18, 0.32))

    def _image(self, rect: tuple[int, int, int, int], *, timer: bool):
        """截取给定屏幕区域，并将小字放大以提高识别率。"""

        left, top, right, bottom = rect
        shot = self.mss.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
        image = self.np.asarray(shot)[:, :, :3].copy()
        if timer:
            image = self.cv2.resize(image, None, fx=3, fy=3, interpolation=self.cv2.INTER_CUBIC)
        elif image.shape[1] > 1280:
            scale = 1280 / image.shape[1]
            image = self.cv2.resize(image, None, fx=scale, fy=scale, interpolation=self.cv2.INTER_AREA)
        return image

    def _ocr_text(self, rect: tuple[int, int, int, int], *, timer: bool) -> str:
        """从截图提取文字；不读取网页 DOM 或网络数据。"""

        image = self._image(rect, timer=timer)
        result = self.ocr(image, use_det=not timer, use_cls=False)
        return " ".join(getattr(result, "txts", None) or ())

    def read_timer(self, config: Calibration) -> tuple[TimerReading | None, str]:
        """让播放器控件显现后，仅识别标定的计时文字区域。"""

        # 【修改】每轮都左右移动，不能只移到固定点，否则鼠标静止后控制栏可能已隐藏。
        self.reveal_controls(config.hover_point)
        text = self._ocr_text(config.timer_rect, timer=True)
        return parse_timer(text), text

    def find_blocker(self, config: Calibration) -> str | None:
        """在可见窗口中查找明确的验证或答题提示，发现后交还人工。"""

        wl, wt, wr, wb = config.window_rect
        width, height = config.screen_size
        rect = max(0, wl), max(0, wt), min(width, wr), min(height, wb)
        text = self._ocr_text(rect, timer=False).lower().replace(" ", "")
        for marker in ("验证码", "人机验证", "请完成验证", "开始测验", "提交答案", "captcha", "startquiz"):
            if marker in text:
                return marker
        return None


def capture_point(desktop: WindowsDesktop, label: str) -> tuple[WindowInfo, tuple[int, int]]:
    """倒计时后记录用户所指位置，避免在课程页面运行脚本。"""

    input(f"\n按 Enter 后切回浏览器，把鼠标放到【{label}】；5 秒后记录：")
    for remaining in range(5, 0, -1):
        print(f"  {remaining}...", flush=True)
        time.sleep(1)
    # 【修改】鼠标悬停不会切换前台窗口，按光标所在位置绑定浏览器才可靠。
    point = desktop.point()
    window = window_at_point(point)
    print(f"已记录 {label}: {point}；所属窗口 {window.class_name} {window.rect}")
    return window, point


# 【新增】在冻结的浏览器截图上框选计时，避免视频控件自动隐藏及五秒取点不准。
# 参数: desktop (WindowsDesktop) 提供截图、OCR 与屏幕尺寸。
# 返回: 截图时的浏览器窗口及经 OCR 验证的计时区域。
def capture_timer_rect(desktop: WindowsDesktop) -> tuple[WindowInfo, tuple[int, int, int, int]]:
    """显示主屏幕截图供拖框，并在确认前验证框内计时可识别。"""

    input("\n按 Enter 后切回浏览器，把鼠标放在控制栏空白处让计时显示；5 秒后冻结屏幕：")
    for remaining in range(5, 0, -1):
        print(f"  {remaining}...", flush=True)
        time.sleep(1)
    # 【修改】截图可能在终端仍为前台时进行，按悬停坐标找播放器所属窗口。
    point = desktop.point()
    window = window_at_point(point)
    print(f"计时截图所属窗口：{window.class_name} {window.rect}")
    width, height = primary_screen_size()
    # 【新增】五秒倒计时后再次轻移鼠标，确保冻结截图里有可见的计时文字。
    desktop.reveal_controls(point)
    shot = desktop.mss.grab({"left": 0, "top": 0, "width": width, "height": height})

    # 【新增】只在本机内存显示截图；拖框时网页无需继续显示播放器控件。
    import tkinter as tk
    from PIL import Image, ImageTk

    root = tk.Tk()
    root.title("框选计时区域")
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    canvas = tk.Canvas(root, width=width, height=height, highlightthickness=0, cursor="crosshair")
    canvas.pack()
    photo = ImageTk.PhotoImage(Image.frombytes("RGB", (width, height), shot.rgb))
    canvas.create_image(0, 0, anchor="nw", image=photo)
    canvas.image = photo
    banner = canvas.create_rectangle(0, 0, width, 42, fill="#17202b", outline="")
    status = canvas.create_text(
        12, 21,
        anchor="w",
        fill="white",
        font=("Microsoft YaHei UI", 12),
        text="拖框圈住完整的 当前时间/总时间；识别成功后按 Enter，Esc 重新截屏",
    )
    start: list[int] = []
    selection: list[tuple[int, int, int, int]] = []
    outline: list[int] = []

    # 【新增】画框时即时显示选区，松开鼠标后才能进行 OCR，防止误选小区域。
    def on_press(event) -> None:
        """记录框选起点并清除上一次失败的选区。"""

        start[:] = [event.x, event.y]
        selection.clear()
        if outline:
            canvas.delete(outline.pop())
        outline.append(canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00e0a4", width=2))

    def on_drag(event) -> None:
        """拖动时更新计时区域的可视边界。"""

        if start and outline:
            canvas.coords(outline[0], start[0], start[1], event.x, event.y)

    def on_release(event) -> None:
        """对冻结截图中的选区做 OCR，只有有效计时允许确认。"""

        if not start:
            return
        left, right = sorted((max(0, min(width, start[0])), max(0, min(width, event.x))))
        top, bottom = sorted((max(0, min(height, start[1])), max(0, min(height, event.y))))
        if right - left < 40 or bottom - top < 12:
            canvas.itemconfigure(status, text="框选区域太小；请完整圈住 当前时间/总时间")
            return
        image = desktop.np.asarray(shot)[top:bottom, left:right, :3].copy()
        image = desktop.cv2.resize(image, None, fx=3, fy=3, interpolation=desktop.cv2.INTER_CUBIC)
        try:
            result = desktop.ocr(image, use_det=False, use_cls=False)
            raw = " ".join(getattr(result, "txts", None) or ())
        except Exception as exc:
            # 【新增】OCR 异常在界面内提示，允许重新框选或退出，不让 Tk 回调静默失败。
            canvas.itemconfigure(status, text=f"识别出错：{exc}；请重框或按 Esc 退出")
            return
        reading = parse_timer(raw)
        if reading is None:
            canvas.itemconfigure(status, text=f"无法识别：{raw!r}；请重框，或按 Esc 重新截屏")
            return
        selection[:] = [(left, top, right, bottom)]
        canvas.itemconfigure(status, text=f"已识别：{raw}（{reading.current}/{reading.total} 秒）；按 Enter 确认")

    # 【新增】确认键仅接受 OCR 已通过的区域；Esc 退出以便重新显示控制栏截图。
    def on_confirm(_event) -> None:
        """确认有效选区后关闭截图窗口。"""

        if selection:
            root.destroy()

    def on_cancel(_event) -> None:
        """取消本次截图选择，不保存可能错误的坐标。"""

        selection.clear()
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Return>", on_confirm)
    root.bind("<Escape>", on_cancel)
    root.after(100, root.focus_force)
    root.mainloop()
    if not selection:
        raise ValueError("已取消计时区域框选；请让计时可见后重新运行 calibrate")
    print(f"计时区域已确认：{selection[0]}")
    return window, selection[0]


def make_calibration(desktop: WindowsDesktop) -> Calibration:
    """引导用户标定文字框、显现控件的位置和两个按钮。"""

    # 【修改】先在冻结截图上框选并验证计时，避免最后才发现前两次取点无效。
    first, (x1, y1, x2, y2) = capture_timer_rect(desktop)
    steps = (
        "播放器控制栏空白处（悬停显示计时，避开按钮和提示气泡）",
        "下一个视频按钮中心",
        "播放按钮中心",
    )
    records = [capture_point(desktop, label) for label in steps]
    windows = [item[0] for item in records]
    if any(window.handle != first.handle or window.rect != first.rect for window in windows):
        raise ValueError("各标定点指向的窗口不一致，请把鼠标放在同一个课程窗口内重新标定")
    hover, next_point, play_point = [item[1] for item in records]
    config = Calibration(
        window_class=first.class_name,
        window_rect=first.rect,
        screen_size=primary_screen_size(),
        timer_rect=(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
        hover_point=hover,
        next_point=next_point,
        play_point=play_point,
    )
    config.validate()
    print("\n保持课程页面在前台，5 秒后验证计时识别...")
    time.sleep(5)
    # 【修改】标定验证仅截图与 OCR，不发送点击；终端偶尔仍为前台不应让标定失败。
    # 正式运行仍由 check_window 严格要求课程浏览器处于前台。
    reading, raw = desktop.read_timer(config)
    print(f"OCR 原文：{raw!r}；解析结果：{reading}")
    if reading is None:
        raise ValueError("截图中的计时可识别，但实时计时不可见；请调整控制栏悬停点后重新标定")
    return config


def check_window(config: Calibration, expected_handle: int) -> None:
    """每次截屏或点击前核对窗口，防止错点到别的程序。"""

    window = foreground_window()
    if window.handle != expected_handle or window.class_name != config.window_class:
        raise RuntimeError("浏览器已失去前台焦点")
    if window.rect != config.window_rect or primary_screen_size() != config.screen_size:
        raise RuntimeError("窗口位置、大小或屏幕分辨率已变化，请重新标定")


# 【新增】每次点击前验证按钮位置仍指向课程窗口，避免布局变化后误点其他应用。
# 参数: expected_handle (int) 运行时绑定的浏览器窗口，point (tuple[int, int]) 待点击坐标。
# 返回: 无；坐标指向其他窗口时抛出异常以停止宏。
def check_target_point(expected_handle: int, point: tuple[int, int]) -> None:
    """拒绝对不属于目标课程窗口的坐标发送系统点击。"""

    window = window_at_point(point)
    if window.handle != expected_handle:
        raise RuntimeError(f"按钮坐标 {point} 已不在课程窗口上，请重新标定")
