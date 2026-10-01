"""【新增】验证弹窗选项、模型输出和一次重试流程不会产生盲目点击。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from course_switcher.core import TimerReading
from course_switcher.desktop import Calibration
from course_switcher.quiz import (
    OcrLine,
    MiMoSolver,
    QuizAnswer,
    QuizCalibration,
    QuizSnapshot,
    feedback_result,
    option_text_identity,
    parse_quiz_view,
    question_fingerprint,
    question_still_visible,
    quiz_feedback_badge,
    revealed_quiz_answer,
    validate_answer,
)
from main import _handle_quiz, _ready_quiz_view, _wait_quiz_feedback


# 【新增】读取用户两张截图的真实 OCR 结果，防止只测人工构造的完整反馈短语。
# 参数: name (str) 数据文件名；返回: QuizSnapshot 可直接驱动答题状态流程。
def screenshot_fixture(name: str) -> QuizSnapshot:
    """加载只有文字和坐标的截图回归数据。"""

    path = Path(__file__).parent / "fixtures" / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return QuizSnapshot(b"fixture-png", tuple(OcrLine(item["text"], tuple(item["box"])) for item in data))


# 【新增】构造与用户截图同形的 OCR 框，测试时不保存含课程画面的人像文件。
def sample_snapshot(feedback: str = "", *, visible: bool = True) -> QuizSnapshot:
    """返回判断题弹窗的简化 OCR 结果。"""

    lines = [
        OcrLine("AI助教小智给你出题啦！", (380, 160, 1200, 205)),
        OcrLine("【判断题】一套能够发挥功能的技术系统一定是一个产品", (520, 450, 1400, 490)),
        OcrLine("A. 对", (575, 640, 660, 678)),
        OcrLine("B. 错", (575, 738, 660, 779)),
        OcrLine("关闭", (920, 990, 990, 1032)),
    ] if visible else []
    if feedback:
        lines.append(OcrLine(feedback, (800, 850, 1000, 890)))
    return QuizSnapshot(b"test-png", tuple(lines))


class QuizParsingTests(unittest.TestCase):
    """【新增】验证截图 OCR 中的选项和反馈边界。"""

    # 【新增】同时覆盖正确答案整行、蓝色字母单独分框，以及实际错误反馈截图。
    def test_revealed_answer_from_actual_screenshots(self) -> None:
        for name in ("quiz_correct", "quiz_wrong"):
            snapshot = screenshot_fixture(name)
            self.assertEqual(revealed_quiz_answer(snapshot, parse_quiz_view(snapshot)),
                             QuizAnswer("judgement", ("B",)))
        self.assertEqual(quiz_feedback_badge(screenshot_fixture("quiz_wrong")), "wrong")
        self.assertIsNone(revealed_quiz_answer(sample_snapshot(), parse_quiz_view(sample_snapshot())))

    # 【新增】明确出现答案标签但字母不可靠时必须停止，不能截取部分答案。
    def test_invalid_revealed_answers_are_rejected(self) -> None:
        for value in ("C", "BB", "AB", "B 或 A", "", "B E"):
            with self.subTest(value=value):
                snapshot = sample_snapshot("正确答案：" + value)
                with self.assertRaises(ValueError):
                    revealed_quiz_answer(snapshot, parse_quiz_view(snapshot))

    # 【新增】多选答案按整组读取，不能仅取第一个字母。
    def test_revealed_multiple_answer(self) -> None:
        lines = list(sample_snapshot().lines)
        lines[1] = OcrLine("【多选题】选择正确说法", lines[1].box)
        lines.extend((OcrLine("C. 丙", (575, 790, 660, 830)),
                      OcrLine("正确答案：A、C", (575, 850, 800, 890))))
        snapshot = QuizSnapshot(b"png", tuple(lines))
        self.assertEqual(revealed_quiz_answer(snapshot, parse_quiz_view(snapshot)),
                         QuizAnswer("multiple", ("A", "C")))

    # 【新增】仅容忍 OCR 格式差异，实际选项文字变化仍可区分。
    def test_option_identity_ignores_label_format_only(self) -> None:
        for value in ("A. 对", "Ａ、对", "A对", "A 对"):
            self.assertEqual(option_text_identity(value), "对")
        self.assertNotEqual(option_text_identity("A. 对"), option_text_identity("A. 错"))

    def test_judgement_layout_and_answer_validation(self) -> None:
        """A/B 判断题不能被误认为固定 A–D 布局。"""

        view = parse_quiz_view(sample_snapshot())
        self.assertEqual(view.question_type, "judgement")
        self.assertEqual(sorted(view.options), ["A", "B"])
        self.assertEqual(view.close.center, (955, 1011))
        self.assertEqual(
            validate_answer({"question_type": "judgement", "answers": ["B"], "uncertain": False}, view),
            QuizAnswer("judgement", ("B",)),
        )
        for invalid in (
            {"question_type": "judgement", "answers": ["C"], "uncertain": False},
            {"question_type": "judgement", "answers": ["A", "B"], "uncertain": False},
            {"question_type": "judgement", "answers": ["A"], "uncertain": True},
        ):
            with self.assertRaises(ValueError):
                validate_answer(invalid, view)

    def test_judgement_accepts_ocr_spacing_and_unlabeled_true_false(self) -> None:
        """判断题 A/B 被 OCR 拆开、丢标点或只剩对错文字时仍能定位。"""

        original = sample_snapshot()
        variants = (
            ("A 对", "B 错"),
            ("Ａ．对", "Ｂ、错"),
            ("对", "错"),
            ("A", "B"),
        )
        for first, second in variants:
            with self.subTest(first=first, second=second):
                lines = list(original.lines)
                lines[2] = OcrLine(first, lines[2].box)
                lines[3] = OcrLine(second, lines[3].box)
                snapshot = QuizSnapshot(b"png", tuple(lines))
                view = parse_quiz_view(snapshot)
                self.assertEqual(sorted(view.options), ["A", "B"])
                self.assertTrue(question_fingerprint(snapshot))

    def test_judgement_reports_candidate_text_when_option_missing(self) -> None:
        """仍无法定位时给出有限的 OCR 候选，便于用户定位具体漏检。"""

        lines = list(sample_snapshot().lines)
        lines[3] = OcrLine("未识别文字", lines[3].box)
        with self.assertRaisesRegex(ValueError, "OCR 候选"):
            parse_quiz_view(QuizSnapshot(b"png", tuple(lines)))

    def test_feedback_only_uses_new_text(self) -> None:
        """题干原有的“正确”不能被当成答对反馈。"""

        before = sample_snapshot()
        self.assertEqual(feedback_result(before, before)[0], None)
        self.assertEqual(feedback_result(before, sample_snapshot("回答错误"))[0], "wrong")
        self.assertEqual(feedback_result(before, sample_snapshot("回答正确"))[0], "correct")

    # 【新增】用户答后截图同时有独立结果标记和答案说明，二者不能混为选项。
    def test_actual_correct_screenshot_preserves_question_and_options(self) -> None:
        before = screenshot_fixture("quiz_before")
        after = screenshot_fixture("quiz_correct")
        self.assertEqual(feedback_result(before, after)[0], "correct")
        self.assertTrue(question_still_visible(before, after))
        self.assertEqual(question_fingerprint(before), question_fingerprint(after))
        self.assertEqual(list(parse_quiz_view(after).options), ["A", "B"])
        self.assertEqual(parse_quiz_view(after).close.center, (637, 908))

    # 【新增】错误页面同样可能显示“正确答案”，不能因此提前关闭。
    def test_answer_explanation_alone_is_not_success(self) -> None:
        before = screenshot_fixture("quiz_before")
        after = screenshot_fixture("quiz_correct")
        explanation_only = QuizSnapshot(after.png, tuple(line for line in after.lines if line.text != "正确"))
        self.assertIsNone(feedback_result(before, explanation_only)[0])
        wrong = QuizSnapshot(after.png, tuple(
            OcrLine("错误", line.box) if line.text == "正确" else line for line in after.lines
        ))
        self.assertEqual(feedback_result(before, wrong)[0], "wrong")
        self.assertIsNone(quiz_feedback_badge(sample_snapshot("正确")))

    def test_feedback_can_cover_options_without_changing_question(self) -> None:
        """反馈覆盖 A/B 时仍可确认原题与对错，不把它当作选项识别失败。"""

        before = sample_snapshot()
        hidden = QuizSnapshot(b"png", tuple(
            line for line in sample_snapshot("回答正确").lines if line.text not in ("A. 对", "B. 错")
        ))
        self.assertTrue(question_still_visible(before, hidden))
        self.assertEqual(feedback_result(before, hidden)[0], "correct")

    def test_question_fingerprint_changes_when_prompt_changes(self) -> None:
        """同样 A/B 选项的下一题也必须拒绝沿用旧模型答案。"""

        first = sample_snapshot()
        changed = list(first.lines)
        changed[1] = OcrLine("【判断题】这是一道新题", changed[1].box)
        self.assertNotEqual(question_fingerprint(first), question_fingerprint(QuizSnapshot(b"png", tuple(changed))))

    def test_quiz_calibration_round_trip(self) -> None:
        """弹窗位置可独立保存并再次读取。"""

        config = QuizCalibration("Chrome_WidgetWin_1", (0, 0, 1752, 1162), (1752, 1162), (326, 117, 1586, 1077))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quiz_config.json"
            config.save(path)
            self.assertEqual(QuizCalibration.load(path), config)

    def test_multiple_choice_requires_visible_distinct_options(self) -> None:
        """多选题仅接受页面实际存在且不重复的字母。"""

        lines = list(sample_snapshot().lines)
        lines[1] = OcrLine("【多选题】选择正确说法", lines[1].box)
        lines.extend((
            OcrLine("C. 丙", (575, 790, 660, 830)),
            OcrLine("D. 丁", (575, 840, 660, 880)),
        ))
        view = parse_quiz_view(QuizSnapshot(b"png", tuple(lines)))
        self.assertEqual(
            validate_answer({"question_type": "multiple", "answers": ["B", "D"], "uncertain": False}, view),
            QuizAnswer("multiple", ("B", "D")),
        )
        with self.assertRaises(ValueError):
            validate_answer({"question_type": "multiple", "answers": ["B", "B"], "uncertain": False}, view)

    def test_mimo_request_uses_cropped_image_and_json_contract(self) -> None:
        """请求使用本机 JSON 的 URL 与 Key，只发送弹窗 PNG 和约定参数。"""

        sent = []

        # 【新增】模拟 HTTPS 响应，不使用真实 API Key 或课程网络请求。
        def fake_urlopen(outgoing, timeout):
            sent.append((outgoing, timeout))
            response = {"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"question_type": "judgement", "answers": ["B"], "uncertain": False})
            }}]}
            return io.BytesIO(json.dumps(response).encode("utf-8"))

        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "mimo_config.json"
            config_path.write_text(json.dumps({
                "api_url": "https://api.xiaomimimo.com/v1/chat/completions", "api_key": "test-key"
            }), encoding="utf-8")
            with patch("course_switcher.quiz.DEFAULT_MIMO_CONFIG", config_path), patch(
                "course_switcher.quiz.request.urlopen", side_effect=fake_urlopen
            ):
                answer = MiMoSolver().solve(sample_snapshot(), parse_quiz_view(sample_snapshot()))
        self.assertEqual(answer.answers, ("B",))
        outgoing, timeout = sent[0]
        payload = json.loads(outgoing.data)
        self.assertEqual(outgoing.full_url, "https://api.xiaomimimo.com/v1/chat/completions")
        self.assertEqual(outgoing.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(timeout, 30)
        self.assertEqual(payload["model"], "mimo-v2.6-flash")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertTrue(payload["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_mimo_config_rejects_blank_key_and_non_https_url(self) -> None:
        """无效本机配置应在任何网络请求或鼠标操作前停止。"""

        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "mimo_config.json"
            for api_url, api_key in (
                ("https://api.xiaomimimo.com/v1/chat/completions", ""),
                ("http://api.xiaomimimo.com/v1/chat/completions", "test-key"),
            ):
                config_path.write_text(json.dumps({"api_url": api_url, "api_key": api_key}), encoding="utf-8")
                with patch("course_switcher.quiz.DEFAULT_MIMO_CONFIG", config_path), self.assertRaises(ValueError):
                    MiMoSolver()


class QuizFlowTests(unittest.TestCase):
    """【新增】用模拟桌面和 MiMo 检查点击顺序与试运行行为。"""

    def setUp(self) -> None:
        """构造无需真实浏览器的固定窗口。"""

        self.video = Calibration(
            "Chrome_WidgetWin_1", (0, 0, 1752, 1162), (1752, 1162),
            (20, 1000, 130, 1040), (100, 1000), (200, 1000), (300, 1000),
        )
        self.quiz = QuizCalibration("Chrome_WidgetWin_1", (0, 0, 1752, 1162), (1752, 1162), (326, 117, 1586, 1077))

    def test_incomplete_first_ocr_retries_without_click(self) -> None:
        """弹窗刚出现只识别 A 时，重读到 B 后才交给模型。"""

        lines = list(sample_snapshot().lines)
        incomplete = QuizSnapshot(b"png", tuple(line for line in lines if line.text != "B. 错"))

        class FakeDesktop:
            def read_quiz(self, _config):
                return sample_snapshot()

        class FakeStop:
            def wait(self, _seconds):
                return False

        with patch("main.check_window"), patch("main._quiz_click") as click:
            snapshot, view = _ready_quiz_view(FakeDesktop(), self.quiz, incomplete, self.video, 1, FakeStop())
        self.assertEqual(sorted(view.options), ["A", "B"])
        self.assertEqual(snapshot.text, sample_snapshot().text)
        click.assert_not_called()

    # 【新增】用真实错误截图构造前后状态，验证页面答案优先、格式变化和关闭顺序。
    def test_page_answer_recovers_wrong_selection_without_second_model_call(self) -> None:
        wrong = screenshot_fixture("quiz_wrong")
        before = QuizSnapshot(b"png", tuple(line for line in wrong.lines
            if line.text not in ("错误", "正确答案：B")))
        current = QuizSnapshot(b"png", tuple(OcrLine("Ａ、对", line.box) if line.text == "A. 对" else line
                                             for line in wrong.lines))
        correct = QuizSnapshot(b"png", tuple(OcrLine("正确", line.box) if line.text == "错误" else line
                                             for line in wrong.lines))
        for restarting in (False, True):
            with self.subTest(restarting=restarting):
                desktop, solver, stop = Mock(), Mock(), Mock()
                desktop.read_quiz.side_effect = ([current, correct, sample_snapshot(visible=False)] if restarting
                    else [before, wrong, current, correct, sample_snapshot(visible=False)])
                desktop.read_timer.return_value = (TimerReading(12, 100), "0:12/1:40")
                solver.solve.return_value = QuizAnswer("judgement", ("A",))
                stop.wait.return_value = False
                with patch("main.check_window"), patch("main._quiz_click") as click:
                    _handle_quiz(desktop, self.video, self.quiz, wrong if restarting else before,
                                 solver, 1, stop, False)
                self.assertEqual([call.args[-1] for call in click.call_args_list],
                    (["选项 B", "关闭", "恢复播放"] if restarting else ["选项 A", "选项 B", "关闭", "恢复播放"]))
                self.assertEqual(solver.solve.call_count, 0 if restarting else 1)

    # 【新增】已答错时试运行只报告页面答案，不能点击或请求模型。
    def test_wrong_popup_dry_run_does_not_click_or_call_model(self) -> None:
        solver = Mock()
        with patch("main._quiz_click") as click:
            _handle_quiz(None, self.video, self.quiz, screenshot_fixture("quiz_wrong"), solver, 1, None, True)
        solver.solve.assert_not_called()
        click.assert_not_called()

    # 【新增】真实内容变化依旧阻断，不能因为放宽格式匹配而点击其他题目选项。
    def test_changed_option_content_stops_before_click(self) -> None:
        before = sample_snapshot()
        changed = QuizSnapshot(b"png", tuple(OcrLine("A. 新内容", line.box) if line.text == "A. 对" else line
                                             for line in before.lines))
        desktop, solver = Mock(), Mock()
        desktop.read_quiz.return_value = changed
        solver.solve.return_value = QuizAnswer("judgement", ("A",))
        with patch("main.check_window"), patch("main._quiz_click") as click:
            with self.assertRaisesRegex(RuntimeError, "题目选项已变化"):
                _handle_quiz(desktop, self.video, self.quiz, before, solver, 1, Mock(), False)
        click.assert_not_called()

    # 【新增】即使页面答案可读，改选仍错也只尝试一次，不关闭、不恢复播放。
    def test_page_answer_correction_has_single_retry_limit(self) -> None:
        wrong = screenshot_fixture("quiz_wrong")
        desktop, solver = Mock(), Mock()
        desktop.read_quiz.return_value = wrong
        with patch("main.check_window"), patch("main._quiz_click") as click, patch(
                "main._wait_quiz_feedback", return_value=("wrong", "错误", wrong)):
            with self.assertRaisesRegex(RuntimeError, "改选一次后仍显示错误"):
                _handle_quiz(desktop, self.video, self.quiz, wrong, solver, 1, Mock(), False)
        self.assertEqual([call.args[-1] for call in click.call_args_list], ["选项 B"])
        solver.solve.assert_not_called()

    def test_feedback_wait_accepts_correct_result_when_options_hidden(self) -> None:
        """正确反馈先于选项解析返回，避免遮挡时错误停止。"""

        before = sample_snapshot()
        after = QuizSnapshot(b"png", tuple(
            line for line in sample_snapshot("回答正确").lines if line.text not in ("A. 对", "B. 错")
        ))

        class FakeDesktop:
            def read_quiz(self, _config):
                return after

        class FakeStop:
            def wait(self, _seconds):
                return False

        with patch("main.check_window"):
            outcome, _feedback, result = _wait_quiz_feedback(
                FakeDesktop(), self.video, self.quiz, before, 1, FakeStop()
            )
        self.assertEqual(outcome, "correct")
        self.assertEqual(result, after)

    def test_wrong_answer_retries_once_then_closes_and_plays(self) -> None:
        """第一次错后只改选一次，答对后只关一次并播放一次。"""

        first = sample_snapshot()
        wrong = sample_snapshot("回答错误")
        correct = sample_snapshot("回答正确")

        class FakeDesktop:
            # 【新增】按真实流程顺序返回初始、错误及关闭后的页面状态。
            def __init__(self) -> None:
                self.shots = iter((first, wrong, sample_snapshot(visible=False)))

            def read_quiz(self, _config):
                return next(self.shots)

            def read_timer(self, _config):
                return TimerReading(12, 100), "0:12/1:40"

        class FakeSolver:
            # 【新增】模拟模型先答错 A，再依据页面反馈改答 B。
            def __init__(self) -> None:
                self.answers = iter((QuizAnswer("judgement", ("A",)), QuizAnswer("judgement", ("B",))))

            def solve(self, *_args):
                return next(self.answers)

        class FakeStop:
            def is_set(self):
                return False

            def wait(self, _seconds):
                return False

        clicks: list[str] = []
        with patch("main.check_window"), patch("main._quiz_click", side_effect=lambda *args: clicks.append(args[-1])), patch(
            "main._wait_quiz_feedback", side_effect=(("wrong", "回答错误", wrong), ("correct", "回答正确", correct))
        ):
            reading = _handle_quiz(FakeDesktop(), self.video, self.quiz, first, FakeSolver(), 1, FakeStop(), False)
        self.assertEqual(reading, TimerReading(12, 100))
        self.assertEqual(clicks, ["选项 A", "选项 B", "关闭", "恢复播放"])

    def test_dry_run_calls_solver_but_does_not_click(self) -> None:
        """试运行只报告拟选答案，不触发任何本机鼠标事件。"""

        class FakeSolver:
            def solve(self, *_args):
                return QuizAnswer("judgement", ("B",))

        with patch("main._quiz_click") as click:
            result = _handle_quiz(None, self.video, self.quiz, sample_snapshot(), FakeSolver(), 1, None, True)
        self.assertIsNone(result)
        click.assert_not_called()

    # 【新增】完整调用实际反馈识别，不模拟其结果，验证真实截图触发单次关闭。
    def test_actual_screenshot_closes_once_after_selection(self) -> None:
        before = screenshot_fixture("quiz_before")
        after = screenshot_fixture("quiz_correct")
        desktop = Mock()
        desktop.read_quiz.side_effect = [before, after, sample_snapshot(visible=False)]
        desktop.read_timer.return_value = (TimerReading(12, 100), "0:12/1:40")
        solver = Mock()
        solver.solve.return_value = QuizAnswer("judgement", ("B",))
        stop = Mock()
        stop.wait.return_value = False
        clicks = []
        with patch("main.check_window"), patch("main._quiz_click", side_effect=lambda *args: clicks.append((args[-1], args[3]))):
            result = _handle_quiz(desktop, self.video, self.quiz, before, solver, 1, stop, False)
        self.assertEqual(result, TimerReading(12, 100))
        self.assertEqual([label for label, _point in clicks], ["选项 B", "关闭", "恢复播放"])
        self.assertEqual(clicks[1][1], (637, 908))
        solver.solve.assert_called_once()

    # 【新增】重启时遇到已答对页面无需再次请求模型；试运行也不能点击关闭。
    def test_already_correct_popup_closes_without_answering_again(self) -> None:
        after = screenshot_fixture("quiz_correct")
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                desktop = Mock()
                desktop.read_quiz.return_value = sample_snapshot(visible=False)
                desktop.read_timer.return_value = (TimerReading(12, 100), "0:12/1:40")
                solver = Mock()
                stop = Mock()
                stop.wait.return_value = False
                with patch("main.check_window"), patch("main._quiz_click") as click:
                    _handle_quiz(desktop, self.video, self.quiz, after, solver, 1, stop, dry_run)
                self.assertEqual([call.args[-1] for call in click.call_args_list], [] if dry_run else ["关闭", "恢复播放"])
                solver.solve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
