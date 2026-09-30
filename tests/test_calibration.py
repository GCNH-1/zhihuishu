"""验证错误的标定坐标不会进入实际鼠标操作。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from course_switcher.desktop import Calibration


class CalibrationTests(unittest.TestCase):
    """覆盖合法坐标及窗口外点击点。"""

    def test_valid_and_invalid_geometry(self) -> None:
        """标定坐标必须在主屏幕内，窗口边界由实际所指窗口另行确认。"""

        common = dict(
            window_class="Chrome_WidgetWin_1",
            window_rect=(0, 0, 1200, 800),
            screen_size=(1920, 1080),
            timer_rect=(100, 600, 250, 630),
            hover_point=(500, 600),
            next_point=(350, 600),
            play_point=(300, 600),
        )
        Calibration(**common).validate()
        # 【新增】计时文字可能被播放器绘在窗口装饰边界外；只要在主屏幕内应允许 OCR 验证。
        Calibration(**{**common, "timer_rect": (1200, 600, 1300, 630)}).validate()
        # 【修改】窗口矩形可能与截图缩放不一致；点击点的实际窗口归属在运行前另行核对。
        Calibration(**{**common, "next_point": (1400, 600)}).validate()
        with self.assertRaises(ValueError):
            Calibration(**{**common, "next_point": (1940, 600)}).validate()
        with self.assertRaises(ValueError):
            Calibration(**{**common, "timer_rect": (250, 600, 100, 630)}).validate()

    # 【新增】验证标定写入文件后能在下一次独立运行中完整读回。
    def test_saved_calibration_can_be_reused(self) -> None:
        """保存和加载应保留计时区域、悬停点及两个按钮坐标。"""

        config = Calibration(
            window_class="Chrome_WidgetWin_1",
            window_rect=(-13, -13, 3085, 1837),
            screen_size=(3072, 1920),
            timer_rect=(252, 1680, 476, 1725),
            hover_point=(219, 1693),
            next_point=(143, 1703),
            play_point=(46, 1698),
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.save(path)
            self.assertEqual(Calibration.load(path), config)


if __name__ == "__main__":
    unittest.main()
