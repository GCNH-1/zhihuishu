"""解析播放器计时，并在确认状态后发出单次鼠标操作指令。"""

from __future__ import annotations

from dataclasses import dataclass
import re


_TIME_PAIR = re.compile(
    r"(?<!\d)(\d{1,3}(?:[:.]\d{1,2}){1,2})\s*[/／]\s*"
    r"(\d{1,3}(?:[:.]\d{1,2}){1,2})(?!\d)"
)


@dataclass(frozen=True)
class TimerReading:
    """当前播放秒数和视频总秒数。"""

    current: int
    total: int


@dataclass(frozen=True)
class Decision:
    """状态机返回的操作与供用户查看的原因。"""

    action: str = "none"
    reason: str = ""


def _seconds(value: str) -> int | None:
    """将 OCR 得到的分秒或时分秒转换为秒。"""

    parts = [int(part) for part in re.split(r"[:.]", value)]
    if len(parts) == 2 and parts[1] < 60:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3 and parts[1] < 60 and parts[2] < 60:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def parse_timer(text: str) -> TimerReading | None:
    """从画面文字中找出可信的“当前/总时长”数字。"""

    normalized = text.translate(str.maketrans({"：": ":", "．": ".", "／": "/"}))
    for match in _TIME_PAIR.finditer(normalized):
        current = _seconds(match.group(1))
        total = _seconds(match.group(2))
        if current is not None and total is not None and 0 < total and current <= total:
            return TimerReading(current=current, total=total)
    return None


class SwitchController:
    """根据连续计时决定何时点下一节、播放或停止。"""

    def __init__(self, started_at: float) -> None:
        self.state = "watching"
        self.state_at = started_at
        self.last_valid_at = started_at
        self.last_progress_at = started_at
        self.last_current: int | None = None
        self.last_total: int | None = None
        self.saw_progress = False  # 【新增】区分刚启动的暂停视频和播放中途的短暂缓冲。
        self.end_hits = 0
        self.previous_total: int | None = None
        self.reset_current: int | None = None

    def update(self, reading: TimerReading | None, now: float) -> Decision:
        """每次屏幕采样调用一次；任何异常均返回停止或保持等待。"""

        if self.state == "stopped":
            return Decision("stop", "程序已停止")
        if self.state == "waiting_reset" and now - self.state_at > 20:
            return self._stop("切换后 20 秒内计时未从头开始；可能已到最后一节或出现弹窗")
        if self.state == "waiting_progress" and now - self.state_at > 12:
            return self._stop("点击播放后 12 秒内计时未推进")
        if reading is None:
            if now - self.last_valid_at > 15:
                return self._stop("连续 15 秒无法识别计时，可能有弹窗、测验或验证码")
            return Decision()

        self.last_valid_at = now
        if self.state == "waiting_reset":
            # 【新增】只有时间明显回退，才认为下一节已加载，避免重复点“下一节”。
            if self.previous_total is not None and reading.current < self.previous_total - 1:
                self.state = "waiting_progress"
                self.state_at = now
                self.reset_current = reading.current
                self.saw_progress = False
                return Decision("click_play", "下一节计时已重置")
            return Decision()

        if self.state == "waiting_progress":
            if self.reset_current is not None and reading.current > self.reset_current:
                self.state = "watching"
                self.state_at = now
                self.last_progress_at = now
                self.last_current = reading.current
                self.last_total = reading.total
                self.saw_progress = True
                self.end_hits = 0
                return Decision("none", "视频已开始播放")
            if self.reset_current is not None and reading.current < self.reset_current:
                self.reset_current = reading.current
            return Decision()

        if reading.current >= reading.total:
            # 【新增】两次采样必须显示相同总时长且都到终点，才允许切换。
            self.end_hits = self.end_hits + 1 if self.last_total == reading.total else 1
            self.last_total = reading.total
            if self.end_hits >= 2:
                self.state = "waiting_reset"
                self.state_at = now
                self.previous_total = reading.total
                self.end_hits = 0
                return Decision("click_next", "连续两次确认视频播放完成")
            return Decision()

        self.end_hits = 0
        if self.last_current is None:
            self.last_progress_at = now
        elif reading.current != self.last_current:
            self.last_progress_at = now
            self.saw_progress = reading.current > self.last_current
        self.last_current = reading.current
        self.last_total = reading.total
        # 【修改】启动时或播放中途计时停住时，先尝试一次播放，再由 waiting_progress 验证结果。
        # 已见过进度的章节给缓冲留更长时间；一次点击无效则停止，避免反复切换播放状态。
        stall_limit = 20 if self.saw_progress else 10
        if now - self.last_progress_at > stall_limit:
            self.state = "waiting_progress"
            self.state_at = now
            self.reset_current = reading.current
            return Decision("click_play", "计时未推进，尝试恢复播放")
        return Decision()

    def _stop(self, reason: str) -> Decision:
        """锁定停止状态，确保之后的采样不会再次产生点击指令。"""

        self.state = "stopped"
        return Decision("stop", reason)

    # 【新增】答题期间视频计时不推进；关闭弹窗后重新起算播放确认超时。
    # 参数: reading (TimerReading) 关闭弹窗后的计时；now (float) 当前单调时钟。
    # 返回: 无；下一轮 update 只验证视频是否重新推进。
    def after_quiz_play(self, reading: TimerReading, now: float) -> None:
        """将答题后的播放点击纳入原有单次确认状态。"""

        self.state = "waiting_progress"
        self.state_at = now
        self.last_valid_at = now
        self.last_progress_at = now
        self.reset_current = reading.current
        self.last_current = reading.current
        self.last_total = reading.total
        self.end_hits = 0
