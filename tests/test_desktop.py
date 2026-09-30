"""检查控制栏自动隐藏时，截屏前确实产生鼠标移动。"""

import unittest
from unittest.mock import patch

from course_switcher.desktop import WindowsDesktop


# 【新增】记录模拟鼠标轨迹，验证重复采样时不会只移动到同一个坐标。
class FakeMouse:
    """仅记录 moveTo 调用，不操作真实桌面。"""

    def __init__(self) -> None:
        """准备轨迹列表供断言。"""

        self.moves: list[tuple[int, int]] = []
        self.clicks: list[tuple[int, int]] = []  # 【新增】记录真实点击目标，避免轨迹测试误触桌面。

    def moveTo(self, x: int, y: int, *, duration: float) -> None:
        """记录坐标；duration 与真实鼠标接口保持一致。"""

        self.moves.append((x, y))

    # 【新增】记录按钮点击位置，不调用系统鼠标。
    def click(self, x: int, y: int) -> None:
        """保存点击点供安全边界断言。"""

        self.clicks.append((x, y))


# 【新增】验证控制栏唤醒轨迹，避免计时文字因鼠标静止而消失。
class RevealControlsTests(unittest.TestCase):
    """覆盖普通位置和屏幕右边缘的鼠标轻移。"""

    def test_moves_away_and_back_each_time(self) -> None:
        """每轮 X、Y 的偏移可变化，且坐标始终在主屏幕内。"""

        desktop = WindowsDesktop.__new__(WindowsDesktop)
        desktop.mouse = FakeMouse()
        with patch("course_switcher.desktop.primary_screen_size", return_value=(1000, 800)):
            with patch("course_switcher.desktop.random.randint", side_effect=[4, -5, 1, 2, 7, 7, -2, -2]):
                with patch("course_switcher.desktop.random.uniform", return_value=0.2):
                    with patch("course_switcher.desktop.time.sleep"):
                        desktop.reveal_controls((500, 600))
                        desktop.reveal_controls((995, 600))
        self.assertEqual(desktop.mouse.moves, [(504, 595), (501, 602), (999, 607), (993, 598)])

    # 【新增】按钮移动路线随机变化，但最终点击点仍由调用方验证并传入。
    def test_click_uses_nearby_waypoint(self) -> None:
        """点击前先经过随机邻近点，最后点击指定坐标。"""

        desktop = WindowsDesktop.__new__(WindowsDesktop)
        desktop.mouse = FakeMouse()
        with patch("course_switcher.desktop.primary_screen_size", return_value=(1000, 800)):
            with patch("course_switcher.desktop.random.randint", side_effect=[3, -4]):
                with patch("course_switcher.desktop.random.uniform", return_value=0.2):
                    desktop.click((500, 600))
        self.assertEqual(desktop.mouse.moves, [(503, 596), (500, 600)])
        self.assertEqual(desktop.mouse.clicks, [(500, 600)])


if __name__ == "__main__":
    unittest.main()
