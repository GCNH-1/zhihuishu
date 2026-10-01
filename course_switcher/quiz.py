"""【新增】解析课程弹题截图，并通过 MiMo 获取可校验的选项答案。"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import Path
import re
import unicodedata
from urllib import error, request
from urllib.parse import urlsplit


# 【新增】只将弹窗截图发送给模型，减少视频画面及页面其他信息的传输。
MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.6-flash"
QUIZ_MARKER = "AI助教小智给你出题啦"
# 【新增】密钥文件固定在项目目录，运行命令的位置改变也能读到同一份本机配置。
DEFAULT_MIMO_CONFIG = Path(__file__).resolve().parent.parent / "mimo_config.json"


@dataclass(frozen=True)
class QuizCalibration:
    """【新增】保存弹窗屏幕区域及其所属浏览器窗口，供下次运行复用。"""

    window_class: str
    window_rect: tuple[int, int, int, int]
    screen_size: tuple[int, int]
    dialog_rect: tuple[int, int, int, int]

    # 【新增】将答题标定与视频标定分开，避免弹窗遮住播放器计时。
    def save(self, path: Path) -> None:
        """写入可复用的答题弹窗位置。"""

        path.write_text(json.dumps({"version": 1, **self.__dict__}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 【新增】严格检查持久化坐标，防止配置损坏后误点。
    @classmethod
    def load(cls, path: Path) -> QuizCalibration:
        """读取答题标定。"""

        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError("答题标定文件版本不受支持，请重新运行 calibrate-quiz")
        try:
            config = cls(
                window_class=str(data["window_class"]),
                window_rect=_integer_tuple(data["window_rect"], 4),
                screen_size=_integer_tuple(data["screen_size"], 2),
                dialog_rect=_integer_tuple(data["dialog_rect"], 4),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("答题标定文件不完整，请重新运行 calibrate-quiz") from exc
        config.validate()
        return config

    # 【新增】限制弹窗选区在主屏幕内，并确保与视频标定属于同一个固定窗口。
    def validate(self) -> None:
        """校验屏幕边界和窗口元数据。"""

        width, height = self.screen_size
        left, top, right, bottom = self.dialog_rect
        wl, wt, wr, wb = self.window_rect
        if not self.window_class or width <= 0 or height <= 0 or wl >= wr or wt >= wb:
            raise ValueError("答题标定窗口信息无效")
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise ValueError("答题弹窗区域无效，请重新标定")
        if right - left < 200 or bottom - top < 150:
            raise ValueError("答题弹窗区域太小，请框选整个弹窗")


# 【新增】单独限制 JSON 数组类型，防止字符串和布尔值被误作屏幕坐标。
def _integer_tuple(value: object, count: int) -> tuple[int, ...]:
    """解析固定长度的整数坐标。"""

    if not isinstance(value, list) or len(value) != count or any(type(item) is not int for item in value):
        raise ValueError("答题标定坐标格式无效")
    return tuple(value)


@dataclass(frozen=True)
class OcrLine:
    """【新增】保留 OCR 文字及绝对屏幕坐标，供本机鼠标选项定位。"""

    text: str
    box: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[int, int]:
        """返回文字框的中心点击点。"""

        left, top, right, bottom = self.box
        return ((left + right) // 2, (top + bottom) // 2)


@dataclass(frozen=True)
class QuizSnapshot:
    """【新增】一次弹窗截图及相应 OCR 结果。"""

    png: bytes
    lines: tuple[OcrLine, ...]

    @property
    def text(self) -> str:
        """合并弹窗 OCR 文字，作为模型的辅助输入。"""

        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True)
class QuizView:
    """【新增】可安全点击的可见选项、按钮及题型。"""

    options: dict[str, OcrLine]
    close: OcrLine | None
    submit: OcrLine | None
    question_type: str | None
    multi_page: bool


# 【新增】用稳定标题片段识别已标定弹窗，降低完整长标题 OCR 误差的影响。
def is_quiz_visible(snapshot: QuizSnapshot) -> bool:
    """判断截图是否仍为 AI 助教弹题。"""

    # 【修改】框选时可能带少量页面边缘，扫描完整弹窗文字避免标题落在前六行之外。
    title = "".join(line.text for line in snapshot.lines).replace(" ", "")
    return "AI助教小智" in title and "出题" in title


# 【新增】只从选项开头的字母识别点击位置，支持 A/B 判断题及 A–D 单多选。
# 参数: value (str) 单行 OCR 文字。
# 返回: str | None 识别出的 A–D 字母；不会把英文单词开头直接当作选项。
def _option_letter(value: str) -> str | None:
    """兼容全角字母、不同标点、空格及紧邻汉字的选项标签。"""

    normalized = unicodedata.normalize("NFKC", value).strip()
    match = re.match(r"^\(?([A-D])\)?(?:[.、:：\-]\s*|\s+|(?=[\u3400-\u9fff])|$)", normalized, re.IGNORECASE)
    return match.group(1).upper() if match else None


# 【新增】选中后 OCR 可能改变标签标点、全半角和空格，仅忽略这些展示差异。
# 参数: value (str) 选项 OCR 原文；返回: str 不含标签的规范化选项内容。
def option_text_identity(value: str) -> str:
    """保留选项实际文字，避免空格变化误触发换题保护。"""

    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(
        r"^\(?[A-D]\)?(?:[.、:：\-]\s*|\s+|(?=[\u3400-\u9fff])|$)",
        "", normalized, count=1, flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", "", normalized)


# 【新增】截图中的独立“正确/错误”位于题型左侧；用位置排除选项和答案说明。
# 参数: snapshot (QuizSnapshot) 弹窗 OCR；返回: tuple[OcrLine, ...] 题型旁的结果标记。
def _feedback_badge_lines(snapshot: QuizSnapshot) -> tuple[OcrLine, ...]:
    """仅识别与题型同行、位于其左侧的独立结果文字。"""

    headings = [line for line in snapshot.lines if any(
        marker in line.text for marker in ("判断题", "单选题", "多选题")
    )]
    if len(headings) != 1:
        return ()
    heading = headings[0]
    return tuple(line for line in snapshot.lines if
                 re.sub(r"\s+", "", line.text) in ("正确", "错误")
                 and line.box[2] <= heading.box[0]
                 and min(line.box[3], heading.box[3]) - max(line.box[1], heading.box[1])
                 >= min(line.box[3] - line.box[1], heading.box[3] - heading.box[1]) * 0.5)


# 【新增】供答后确认及重启接管使用，不能仅凭“正确答案”字样认为用户已答对。
# 参数: snapshot (QuizSnapshot) 当前截图；返回: str | None 明确的 correct/wrong 状态。
def quiz_feedback_badge(snapshot: QuizSnapshot) -> str | None:
    """读取题干左侧唯一的对错标记。"""

    badges = _feedback_badge_lines(snapshot)
    if len(badges) != 1:
        return None
    return "correct" if re.sub(r"\s+", "", badges[0].text) == "正确" else "wrong"


# 【修改】先以题干和底部按钮确定选项所在的垂直区域，再识别文字格式，减少漏检和误检。
def parse_quiz_view(snapshot: QuizSnapshot) -> QuizView:
    """从 OCR 框提取当前题目的交互元素。"""

    if not is_quiz_visible(snapshot):
        raise ValueError("没有识别到已标定的 AI 助教弹题")
    options: dict[str, OcrLine] = {}
    close_lines: list[OcrLine] = []
    submit_lines: list[OcrLine] = []
    question_type: str | None = None
    question_bottom: int | None = None
    multi_page = False
    for line in snapshot.lines:
        value = line.text.strip()
        if value == "关闭":
            close_lines.append(line)
        if value in ("提交", "提交答案", "确定", "确认"):
            submit_lines.append(line)
        if "判断题" in value:
            question_type = "judgement"
            question_bottom = line.box[3]
        elif "多选题" in value:
            question_type = "multiple"
            question_bottom = line.box[3]
        elif "单选题" in value:
            question_type = "single"
            question_bottom = line.box[3]
        # 【新增】只有页面明确显示多题数量或下一题文字才停止，不把装饰箭头误判为多题。
        if re.search(r"(?:共\s*[2-9]\d*\s*题|第\s*\d+\s*/\s*[2-9]\d*\s*题|下一题)", value):
            multi_page = True
    # 【修改】题型识别失败时停止，避免模型误判单选与多选而多次点击。
    if question_type is None or question_bottom is None:
        raise ValueError("未识别到判断题、单选题或多选题标识，请手动处理")
    if len(close_lines) != 1 or len(submit_lines) > 1:
        raise ValueError("无法唯一定位弹窗的关闭或提交按钮，请手动处理")
    # 【修改】只在题干下方、关闭按钮上方寻找标签；判断题可用独立“对/错”文字补齐 A/B。
    # 【修改】答后“正确答案：”下面可能单独识别出 B，必须将答案说明区域排除。
    option_bottom = min([close_lines[0].box[1] - 12] + [
        line.box[1] - 1 for line in snapshot.lines
        if re.match(r"^(?:正确答案|参考答案|答案解析)", re.sub(r"\s+", "", line.text))
    ])
    candidates = [line for line in snapshot.lines if
                  line.box[1] >= question_bottom + 12 and line.box[3] <= option_bottom]
    for line in candidates:
        letter = _option_letter(line.text)
        if letter is None:
            continue
        if letter in options:
            raise ValueError(f"选项 {letter} 被识别了多次，请重新标定弹窗")
        options[letter] = line
    if question_type == "judgement":
        for line in candidates:
            value = unicodedata.normalize("NFKC", line.text).strip().replace(" ", "")
            letter = "A" if value in ("对", "正确") else "B" if value in ("错", "错误") else None
            if letter is not None and letter not in options:
                options[letter] = line
    expected = [chr(ord("A") + index) for index in range(len(options))]
    if (len(options) < 2 or len(options) > 4 or sorted(options) != expected
            or question_type == "judgement" and sorted(options) != ["A", "B"]):
        # 【新增】只列出选项区域的 OCR 候选，方便定位漏检，避免将整页内容写入错误日志。
        preview = "、".join(repr(line.text) for line in candidates[:8]) or "无"
        label = "判断题 A/B" if question_type == "judgement" else "连续的 A–D"
        raise ValueError(f"无法可靠定位{label}选项；OCR 候选：{preview}")
    # 【修改】提交按钮只能位于选项之后，避免把题目文字误识别成可点击按钮。
    if submit_lines and submit_lines[0].box[1] <= max(line.box[3] for line in options.values()):
        raise ValueError("提交按钮位置异常，请手动处理")
    return QuizView(options, close_lines[0], submit_lines[0] if submit_lines else None, question_type, multi_page)


# 【新增】模型等待期间题目可能更新；题干指纹用于阻止旧答案点到新题。
def question_fingerprint(snapshot: QuizSnapshot) -> str:
    """抽取题型与题干的稳定文字，不包含反馈和选项。"""

    # 【修改】与动态选项解析共用真实选项区域，OCR 漏掉标点时也能正确截取题干。
    view = parse_quiz_view(snapshot)
    first_option_top = min(line.box[1] for line in view.options.values())
    ordered = sorted(snapshot.lines, key=lambda line: (line.box[1], line.box[0]))
    start = next((index for index, line in enumerate(ordered) if any(
        marker in line.text for marker in ("判断题", "单选题", "多选题")
    )), None)
    if start is None:
        raise ValueError("无法定位题干，请手动处理")
    if ordered[start].box[1] >= first_option_top:
        raise ValueError("无法确认题干与选项边界，请手动处理")
    # 【修改】结果标记与题干首行高度接近，OCR 排序可能把它插入题干中。
    badges = _feedback_badge_lines(snapshot)
    return re.sub(r"\s+", "", "".join(
        line.text for line in ordered[start:] if line.box[1] < first_option_top and line not in badges
    ))


# 【新增】反馈可能遮住选项；只要求原题干仍可见，不再强制反馈画面完整识别选项。
# 参数: before (QuizSnapshot) 点击前画面；after (QuizSnapshot) 点击后画面。
# 返回: bool 题干文字仍存在时为真。
def question_still_visible(before: QuizSnapshot, after: QuizSnapshot) -> bool:
    """在反馈画面中确认原题目没有被换掉。"""

    original = question_fingerprint(before)
    ordered = sorted(after.lines, key=lambda line: (line.box[1], line.box[0]))
    # 【修改】过滤独立结果标记，避免答对后被误判成另一道题。
    badges = _feedback_badge_lines(after)
    current = re.sub(r"\s+", "", "".join(line.text for line in ordered if line not in badges))
    return original in current


# 【新增】只比较点击后新出现的文字，避免题干中的“正确”“错误”影响判定。
def feedback_result(before: QuizSnapshot, after: QuizSnapshot) -> tuple[str | None, str]:
    """返回 correct、wrong 或无法确认，以及新增反馈文字。"""

    old = {re.sub(r"\s+", "", line.text) for line in before.lines}
    fresh = " ".join(line.text for line in after.lines if re.sub(r"\s+", "", line.text) not in old)
    normalized = re.sub(r"\s+", "", fresh)
    # 【新增】截图中的结果只有“正确/错误”两个字；要求它位于题型旁且状态确实变化。
    badge = quiz_feedback_badge(after)
    if badge is not None and badge != quiz_feedback_badge(before):
        return badge, fresh
    if re.search(r"回答错误|答错|答题错误|回答不正确|回答错了|答案错误|很遗憾|错误答案", normalized):
        return "wrong", fresh
    if re.search(r"回答正确|答对|答题正确|回答对了|恭喜答对|恭喜你答对|作答正确|答案正确", normalized):
        return "correct", fresh
    return None, fresh


@dataclass(frozen=True)
class QuizAnswer:
    """【新增】经过本地规则验证的模型答案。"""

    question_type: str
    answers: tuple[str, ...]


# 【新增】JSON 模式仍可能输出不合规字段，所以点击前强制校验题型和选项。
def validate_answer(data: object, view: QuizView, previous: tuple[str, ...] = ()) -> QuizAnswer:
    """拒绝无效、重复或不确定的模型选择。"""

    if not isinstance(data, dict) or data.get("uncertain") is not False:
        raise ValueError("MiMo 未给出确定的答题结果")
    question_type = data.get("question_type")
    answers = data.get("answers")
    if question_type not in ("judgement", "single", "multiple") or not isinstance(answers, list):
        raise ValueError("MiMo 答案格式无效")
    if view.question_type is not None and question_type != view.question_type:
        raise ValueError("MiMo 判断的题型与弹窗文字不一致")
    if any(type(item) is not str or item not in view.options for item in answers):
        raise ValueError("MiMo 返回了页面上不存在的选项")
    selected = tuple(sorted(set(answers)))
    if len(selected) != len(answers) or len(selected) == 0:
        raise ValueError("MiMo 返回了重复或空选项")
    if question_type in ("judgement", "single") and len(selected) != 1:
        raise ValueError("单选或判断题只能选择一个答案")
    if question_type == "multiple" and len(selected) < 2:
        raise ValueError("多选题至少需要两个选项")
    if question_type == "judgement" and sorted(view.options) != ["A", "B"]:
        raise ValueError("判断题选项不是 A/B")
    if selected == previous:
        raise ValueError("重试答案与首次相同，停止重复点击")
    return QuizAnswer(question_type, selected)


# 【新增】网页已给出正确答案时直接读取，不再要求模型重新推测。
# 参数: snapshot (QuizSnapshot) 答后画面；view (QuizView) 当前实际选项。
# 返回: QuizAnswer | None 经题型与选项验证的页面答案；没有答案标签时返回 None。
def revealed_quiz_answer(snapshot: QuizSnapshot, view: QuizView) -> QuizAnswer | None:
    """读取选项下方的正确答案，兼容标签和字母分成多个 OCR 框。"""

    labels = [line for line in snapshot.lines if re.match(
        r"^正确答案\s*[:：]", unicodedata.normalize("NFKC", line.text).strip()
    )]
    if not labels:
        return None
    if len(labels) != 1:
        raise ValueError("页面出现多个正确答案区域，无法安全改选")
    label = labels[0]
    # 【新增】只接受选项下方、关闭按钮上方的答案说明，避免读取题干中的示例答案。
    if (label.box[1] <= max(line.box[3] for line in view.options.values())
            or view.close is None or label.box[3] >= view.close.box[1]):
        raise ValueError("正确答案区域位置异常，停止改选")
    value = re.sub(r"^正确答案\s*[:：]\s*", "", unicodedata.normalize("NFKC", label.text).strip())
    # 【新增】例如“正确答案：”和蓝色 B 分框时，只合并右侧且同行的文字。
    adjacent = sorted((line for line in snapshot.lines if line is not label
        and line.box[0] >= label.box[2] - 4
        and min(line.box[3], label.box[3]) - max(line.box[1], label.box[1])
        >= min(line.box[3] - line.box[1], label.box[3] - label.box[1]) * 0.5), key=lambda line: line.box[0])
    for line in adjacent:
        value += " " + unicodedata.normalize("NFKC", line.text).strip()
    value = value.strip().upper()
    if not re.fullmatch(r"[A-D](?:[\s,，、;/／]*[A-D])*", value):
        raise ValueError("无法读取页面正确答案中的完整选项字母，停止改选")
    letters = re.findall(r"[A-D]", value)
    if len(set(letters)) != len(letters) or any(letter not in view.options for letter in letters):
        raise ValueError("页面正确答案包含重复或不存在的选项，停止改选")
    if view.question_type in ("single", "judgement") and len(letters) != 1:
        raise ValueError("页面给出的单选或判断题答案不唯一，停止改选")
    if view.question_type == "multiple" and len(letters) < 2:
        raise ValueError("页面多选题答案不完整，停止改选")
    return QuizAnswer(view.question_type, tuple(sorted(letters)))


class MiMoSolver:
    """【修改】从本机 JSON 读取接口地址和密钥，再访问 MiMo 兼容接口。"""

    # 【修改】从被 Git 忽略的 JSON 读取接口地址与密钥，启动时拒绝空白或不安全配置。
    def __init__(self) -> None:
        try:
            data = json.loads(DEFAULT_MIMO_CONFIG.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("找不到 mimo_config.json，请按 mimo_config.example.json 创建并填写") from exc
        except json.JSONDecodeError as exc:
            raise ValueError("mimo_config.json 不是有效 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("mimo_config.json 必须是包含 api_url 和 api_key 的对象")
        api_url = data.get("api_url")
        api_key = data.get("api_key")
        if not isinstance(api_url, str) or not api_url.strip():
            raise ValueError("请在 mimo_config.json 中填写 api_url")
        if not isinstance(api_key, str) or not api_key.strip() or api_key.strip().startswith("<"):
            raise ValueError("请在 mimo_config.json 中填写真实的 api_key")
        # 【新增】支持文档中的 v1 基础 URL 和完整聊天接口 URL；只接受 HTTPS，避免明文传输密钥。
        api_url = api_url.strip().rstrip("/")
        if api_url == MIMO_BASE_URL or urlsplit(api_url).path.rstrip("/").endswith("/v1"):
            api_url += "/chat/completions"
        parsed = urlsplit(api_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not parsed.path.endswith("/chat/completions")):
            raise ValueError("api_url 必须是 HTTPS 的 /chat/completions 接口地址")
        self.url = api_url
        self.key = api_key.strip()

    # 【新增】失败重试时带上上次答案和反馈，但模型始终只能返回可见选项字母。
    def solve(
        self,
        snapshot: QuizSnapshot,
        view: QuizView,
        previous: tuple[str, ...] = (),
        feedback: str = "",
    ) -> QuizAnswer:
        """请求 MiMo 识别题目并校验其 JSON 答案。"""

        prompt = (
            "根据图片中的当前课程练习题作答，以图片为准，OCR 仅供辅助。"
            "只返回 JSON 对象，字段严格为 question_type、answers、uncertain。"
            "question_type 只能是 judgement、single、multiple；answers 是 A 到 D 的字母数组；"
            "无法确定时 uncertain 为 true 且 answers 为空数组，否则为 false。"
            f"\nOCR：{snapshot.text}\n可见选项：{','.join(sorted(view.options))}"
        )
        if previous:
            prompt += f"\n上次选择：{','.join(previous)}；页面反馈：{feedback}。请重新判断，不要重复同一答案。"
        encoded = base64.b64encode(snapshot.png).decode("ascii")
        payload = {
            "model": MIMO_MODEL,
            "messages": [
                {"role": "system", "content": "你负责识别课程练习题。只输出符合用户指定字段的 JSON，不输出其他内容。"},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                    {"type": "text", "text": prompt},
                ]},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 1024,
        }
        # 【修改】只向 JSON 中校验过的 HTTPS 端点发送一次请求，不在日志中输出 API Key。
        outgoing = request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(outgoing, timeout=30) as incoming:
                response = json.load(incoming)
        except error.HTTPError as exc:
            raise RuntimeError(f"MiMo 请求失败，HTTP {exc.code}") from exc
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"MiMo 网络请求失败：{exc.reason if isinstance(exc, error.URLError) else '超时'}") from exc
        except json.JSONDecodeError as exc:
            # 【新增】接口返回非 JSON 时给出可操作错误，不向终端输出可能含敏感信息的原始响应。
            raise ValueError("MiMo 响应不是有效 JSON，请稍后重试") from exc
        try:
            choice = response["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("MiMo 输出未完整结束")
            content = choice["message"]["content"]
            data = json.loads(content or "")
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("MiMo 返回的 JSON 无法解析") from exc
        return validate_answer(data, view, previous)
