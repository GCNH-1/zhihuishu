"""验证计时解析和切换状态机不会提前或重复点击。"""

import unittest

from course_switcher.core import SwitchController, TimerReading, parse_timer


class TimerParsingTests(unittest.TestCase):
    """覆盖播放器可能显示的冒号、点号及无效数字。"""

    def test_common_formats(self) -> None:
        """两种分隔符都应解析成相同的秒数。"""

        for value in ("10:11/10:11", "10.11 / 10.11", "播放 10：11／10：11"):
            self.assertEqual(parse_timer(value), TimerReading(611, 611))
        self.assertEqual(parse_timer("1:02:03/1:05:00"), TimerReading(3723, 3900))

    def test_rejects_unreliable_values(self) -> None:
        """不完整、越界或倒退的总时长不能触发鼠标操作。"""

        for value in ("10:11", "10:61/11:00", "11:00/10:59", "0:00/0:00", "not a timer"):
            self.assertIsNone(parse_timer(value))


class SwitchControllerTests(unittest.TestCase):
    """验证结束、切换、播放和异常停止的关键路径。"""

    def test_complete_switch_cycle(self) -> None:
        """完成需要两次确认；新计时重置后只点一次播放。"""

        controller = SwitchController(started_at=0)
        self.assertEqual(controller.update(TimerReading(8, 10), 1).action, "none")
        self.assertEqual(controller.update(TimerReading(10, 10), 3).action, "none")
        self.assertEqual(controller.update(TimerReading(9, 10), 5).action, "none")
        self.assertEqual(controller.update(TimerReading(10, 10), 7).action, "none")
        self.assertEqual(controller.update(TimerReading(10, 10), 9).action, "click_next")
        self.assertEqual(controller.update(TimerReading(10, 10), 11).action, "none")
        self.assertEqual(controller.update(TimerReading(0, 20), 13).action, "click_play")
        self.assertEqual(controller.update(TimerReading(0, 20), 15).action, "none")
        self.assertEqual(controller.update(TimerReading(1, 20), 17).action, "none")
        self.assertEqual(controller.state, "watching")

    def test_stops_if_next_video_does_not_load(self) -> None:
        """末节按钮无效时不得继续重复点击。"""

        controller = SwitchController(started_at=0)
        controller.update(TimerReading(10, 10), 1)
        self.assertEqual(controller.update(TimerReading(10, 10), 3).action, "click_next")
        self.assertEqual(controller.update(TimerReading(10, 10), 24).action, "stop")
        self.assertEqual(controller.update(TimerReading(0, 10), 25).action, "stop")

    def test_stops_if_playback_does_not_advance(self) -> None:
        """新一节没有成功播放时及时交给人工。"""

        controller = SwitchController(started_at=0)
        controller.update(TimerReading(10, 10), 1)
        controller.update(TimerReading(10, 10), 3)
        self.assertEqual(controller.update(TimerReading(0, 20), 5).action, "click_play")
        self.assertEqual(controller.update(TimerReading(0, 20), 18).action, "stop")

    def test_stops_on_missing_or_stalled_timer(self) -> None:
        """弹窗遮挡会停止；暂停只尝试一次播放，失败后停止。"""

        missing = SwitchController(started_at=0)
        self.assertEqual(missing.update(None, 16).action, "stop")
        stalled = SwitchController(started_at=0)
        stalled.update(TimerReading(1, 20), 1)
        # 【修改】暂停的视频先尝试点一次播放，验证仍无进度后才停止。
        self.assertEqual(stalled.update(TimerReading(1, 20), 62).action, "click_play")
        self.assertEqual(stalled.update(TimerReading(1, 20), 75).action, "stop")

    # 【新增】覆盖启动时已暂停和播放中途停住的两种情况。
    def test_resumes_initial_and_mid_video_pause(self) -> None:
        """暂停后只点一次播放，计时恢复后重新进入观察状态。"""

        initial = SwitchController(started_at=0)
        self.assertEqual(initial.update(TimerReading(0, 120), 1).action, "none")
        self.assertEqual(initial.update(TimerReading(0, 120), 11).action, "none")
        self.assertEqual(initial.update(TimerReading(0, 120), 12).action, "click_play")
        self.assertEqual(initial.update(TimerReading(1, 120), 14).action, "none")
        self.assertEqual(initial.state, "watching")

        middle = SwitchController(started_at=0)
        middle.update(TimerReading(2, 120), 1)
        middle.update(TimerReading(4, 120), 3)
        self.assertEqual(middle.update(TimerReading(4, 120), 23).action, "none")
        self.assertEqual(middle.update(TimerReading(4, 120), 24).action, "click_play")
        self.assertEqual(middle.update(TimerReading(4, 120), 25).action, "none")
        self.assertEqual(middle.update(TimerReading(5, 120), 27).action, "none")
        self.assertEqual(middle.state, "watching")

    # 【新增】答题 API 可能耗时较长，关闭弹窗后重新起算 12 秒播放确认。
    def test_quiz_resume_resets_progress_timeout(self) -> None:
        """答题后只验证新播放点击，避免沿用弹窗前的超时。"""

        controller = SwitchController(started_at=0)
        controller.update(TimerReading(12, 100), 1)
        controller.after_quiz_play(TimerReading(12, 100), 100)
        self.assertEqual(controller.update(TimerReading(12, 100), 105).action, "none")
        self.assertEqual(controller.update(TimerReading(13, 100), 107).action, "none")
        self.assertEqual(controller.state, "watching")


if __name__ == "__main__":
    unittest.main()
