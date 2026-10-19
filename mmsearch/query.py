"""查询里的过滤条件：时间、文件类型、文件夹。解析后从查询文字里去掉，剩下的才去编码和匹配关键词。

    上周的截图        修改时间在上周，查询「截图」
    报销 ext:pdf      只看 PDF（ext:pdf,docx 多个；也可以直接写 .pdf）
    in:项目A 报价     路径的文件夹部分包含「项目A」
    after:2026-10-01 before:2026-10-15

时间词（今天、昨天、前天、本周/这周、上周、本月/这个月、上个月、今年、去年、最近N天/近N天）要单独成词，
或者放在开头并跟着「的」：「去年在海边拍的照片」里的「去年」说的是拍摄时间，不当作修改时间过滤。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from . import config

_ALL_EXT = config.IMAGE_EXT | config.AUDIO_EXT | config.VIDEO_EXT | config.TEXT_EXT | config.DOC_EXT
_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_RECENT = re.compile(r"^(?:最近|近)(\d+|[一两二三四五六七八九十])(天|周|个月)$")
_WORDS = ("今天", "昨天", "前天", "本周", "这周", "上周", "本月", "这个月", "上个月", "今年", "去年")
_LEAD = re.compile(r"^(" + "|".join(_WORDS) + r"|(?:最近|近)(?:\d+|[一两二三四五六七八九十])(?:天|周|个月))的")


@dataclass
class Filters:
    exts: set[str] = field(default_factory=set)  # 带点的小写扩展名
    dirs: list[str] = field(default_factory=list)  # 小写，文件夹路径需包含
    after: float | None = None  # 修改时间下限（含），epoch 秒
    before: float | None = None  # 上限（不含）
    labels: list[str] = field(default_factory=list)  # 给界面显示的说明

    def __bool__(self):
        return bool(self.exts or self.dirs or self.after is not None or self.before is not None)


def _day(d: dt.date) -> float:
    return dt.datetime.combine(d, dt.time()).timestamp()


def _month_start(d: dt.date, back: int = 0) -> dt.date:
    y, m = d.year, d.month - back
    while m < 1:
        y, m = y - 1, m + 12
    return dt.date(y, m, 1)


def time_range(word: str, today: dt.date) -> tuple[float, float] | None:
    """时间词 → [开始, 结束)。周从周一开始。"""
    one = dt.timedelta(days=1)
    monday = today - dt.timedelta(days=today.weekday())
    ranges = {
        "今天": (today, today + one), "昨天": (today - one, today), "前天": (today - 2 * one, today - one),
        "本周": (monday, today + one), "这周": (monday, today + one), "上周": (monday - 7 * one, monday),
        "本月": (_month_start(today), today + one), "这个月": (_month_start(today), today + one),
        "上个月": (_month_start(today, 1), _month_start(today)),
        "今年": (dt.date(today.year, 1, 1), today + one), "去年": (dt.date(today.year - 1, 1, 1), dt.date(today.year, 1, 1)),
    }
    if word in ranges:
        a, b = ranges[word]
        return _day(a), _day(b)
    m = _RECENT.match(word)
    if m:
        n = int(m[1]) if m[1].isdigit() else _NUM[m[1]]
        days = n * {"天": 1, "周": 7, "个月": 30}[m[2]]
        return _day(today - dt.timedelta(days=days - 1)), _day(today + one)
    return None


def _date(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(s.replace("/", "-").replace(".", "-"))
    except ValueError:
        return None


def parse(text: str, today: dt.date | None = None) -> tuple[str, Filters]:
    """返回 (去掉过滤条件后的查询, 过滤条件)。认不出的写法原样留在查询里。"""
    today = today or dt.date.today()
    f = Filters()

    def set_time(rng, label):
        a, b = rng
        f.after = a if f.after is None else max(f.after, a)
        f.before = b if f.before is None else min(f.before, b)
        f.labels.append(label)

    text = text.strip()
    m = _LEAD.match(text)
    if m and time_range(m[1], today):
        set_time(time_range(m[1], today), m[1])
        text = text[m.end():]
    rest = []
    for tok in text.split():
        low = tok.lower()
        key, _, val = low.partition(":")
        if key == "ext" and val:
            exts = {"." + e.lstrip(".") for e in val.split(",") if e.strip(".")}
            f.exts |= exts
            f.labels.append("类型 " + "、".join(sorted(e[1:] for e in exts)))
        elif key == "in" and val:
            f.dirs.append(val)
            f.labels.append(f"文件夹 {tok.split(':', 1)[1]}")
        elif key in ("after", "before") and _date(val):
            t = _day(_date(val) + dt.timedelta(days=1 if key == "before" else 0))  # before 包含当天
            set_time((t, float("inf")) if key == "after" else (float("-inf"), t), f"{'晚于' if key == 'after' else '早于'} {val}")
        elif low.startswith(".") and low in _ALL_EXT:
            f.exts.add(low)
            f.labels.append(f"类型 {low[1:]}")
        elif time_range(tok, today):
            set_time(time_range(tok, today), tok)
        else:
            rest.append(tok)
    if f.after == float("-inf"):
        f.after = None
    if f.before == float("inf"):
        f.before = None
    return " ".join(rest), f
