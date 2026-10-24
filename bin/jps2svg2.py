#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""番茄简谱脚本 v2.0 解析器（当前只解析、校验和 dump，不渲染 SVG）。

设计目标：

* 以 ``v2/01-规范正文.md`` 为准建立独立的 v2 语法前端；
* 解析结果是一棵可序列化的 AST，而不是先翻译成 v1 文本；
* 诊断遵循规范 11.3 的格式：``file:line:col: error: message``；
* 严格模式默认开启，``--lenient`` 可把部分可恢复错误降级为警告；
* 命令行默认把 AST 以 JSON 输出，便于检查/测试。

用法::

    bin/jps2svg2.py v2/examples/01-排排坐.jps
    bin/jps2svg2.py --summary v2/examples/02-综合示例.jps
    bin/jps2svg2.py --lenient -o /tmp/ast.json demo/v2/009-排排坐.v2.jps

退出码：0 成功；1 存在 error；2 命令行用法错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field, fields, is_dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


# ---------------------------------------------------------------------------
# 通用数据
# ---------------------------------------------------------------------------
@dataclass
class SourcePos:
    line: int
    column: int = 1

    def to_dict(self) -> dict[str, int]:
        return {"line": self.line, "column": self.column}


@dataclass
class Diagnostic:
    severity: str                 # error / warning / info
    message: str
    pos: SourcePos
    code: str = ""
    spec: str = ""

    def format(self, path: str) -> str:
        code = " [%s]" % self.code if self.code else ""
        spec = " (%s)" % self.spec if self.spec else ""
        return "%s:%d:%d: %s: %s%s%s" % (
            path, self.pos.line, self.pos.column,
            self.severity, self.message, code, spec)


@dataclass
class Version:
    major: int
    minor: int
    raw: str


@dataclass
class KeySignature:
    degree: int
    pitch: str                    # A-G
    accidental: str = ""          # "", "#", "b"
    raw: str = ""

    def identity(self) -> tuple[int, str, str]:
        return (self.degree, self.pitch, self.accidental)


@dataclass
class TimeSignature:
    numerator: int
    denominator: int
    raw: str = ""

    @property
    def measure_length(self) -> Fraction:
        return Fraction(self.numerator * 4, self.denominator)

    def default_beats(self) -> list[int]:
        n, d = self.numerator, self.denominator
        table = {
            (2, 4): [1, 1], (3, 4): [1, 1, 1], (4, 4): [1, 1, 1, 1],
            (5, 4): [1, 1, 1, 1, 1],
            (2, 2): [2, 2], (3, 2): [2, 2, 2], (4, 2): [2, 2, 2, 2],
            (6, 8): [3, 3], (9, 8): [3, 3, 3],
            (12, 8): [3, 3, 3, 3], (3, 8): [3],
            (2, 8): [1, 1], (4, 8): [2, 2], (5, 8): [3, 2],
            (7, 8): [2, 2, 3], (8, 8): [3, 3, 2],
        }
        return table.get((n, d), [1] * n)


@dataclass
class Tempo:
    beat: int | None = None
    bpm: int | None = None
    text: str | None = None
    raw: str = ""


@dataclass
class VoiceDecl:
    voice_id: int
    name: str | None = None
    role: str | None = None
    bracket: str = "auto"
    lyrics: bool = True
    pos: SourcePos | None = None


@dataclass
class Arg:
    raw: str
    kind: str                     # quoted / number / ratio / beats / key / tempo / bare / named
    value: Any = None
    name: str | None = None
    pos: SourcePos | None = None


@dataclass
class Command:
    name: str
    args: list[Arg]
    raw: str
    pos: SourcePos

    def arg(self, index: int = 0) -> Arg | None:
        return self.args[index] if 0 <= index < len(self.args) else None

    def named(self, name: str) -> Arg | None:
        for a in self.args:
            if a.name == name:
                return a
        return None


@dataclass
class TextAnnotation:
    text: str
    below: bool = False
    pos: SourcePos | None = None


@dataclass
class Bar:
    kind: str = "single"          # single/double/final/repeat_start/repeat_end/repeat_both
    repeat_start: bool = False
    repeat_end: bool = False
    repeat_count: int | None = None
    hide: str | None = None       # hidebar / phantombar
    ending: list[int] | None = None
    commands: list[Command] = field(default_factory=list)
    marks: list[Command] = field(default_factory=list)
    texts: list[TextAnnotation] = field(default_factory=list)
    pos: SourcePos | None = None


@dataclass
class GraceNote:
    pitch: int
    octave: int = 0
    accidental: str | None = None
    dots: int = 0
    slashes: int = 0
    written_duration: Fraction = Fraction(1, 2)
    pos: SourcePos | None = None


@dataclass
class NoteEvent:
    pitch: int | str              # 0..7 或 "x"
    octave: int = 0
    accidental: str | None = None
    dots: int = 0
    slashes: int = 0
    written_duration: Fraction = Fraction(1)
    extensions: int = 0
    duration: Fraction = Fraction(1)
    tie: bool = False
    tie_from_prev: bool = False
    hidden: bool = False
    measure_rest: bool = False
    measure_rest_count: int = 1
    grace_before: list[GraceNote] = field(default_factory=list)
    grace_after: list[GraceNote] = field(default_factory=list)
    marks: list[Command] = field(default_factory=list)
    texts: list[TextAnnotation] = field(default_factory=list)
    chord: str | None = None
    nobeam: bool = False
    pos: SourcePos | None = None


@dataclass
class TupletEvent:
    numerator: int
    denominator: int
    events: list[Any] = field(default_factory=list)
    duration: Fraction = Fraction(0)
    pos: SourcePos | None = None
    hide_number: bool = False


@dataclass
class BeamEvent:
    events: list[Any] = field(default_factory=list)
    duration: Fraction = Fraction(0)
    pos: SourcePos | None = None


@dataclass
class GroupStartEvent:
    group_id: int
    group_kind: str
    options: dict[str, Any] = field(default_factory=dict)
    pos: SourcePos | None = None


@dataclass
class GroupEndEvent:
    group_id: int
    pos: SourcePos | None = None


@dataclass
class SlurStartEvent:
    pos: SourcePos | None = None


@dataclass
class SlurEndEvent:
    pos: SourcePos | None = None


@dataclass
class CrescendoStartEvent:
    direction: str                # cresc / dim
    pos: SourcePos | None = None


@dataclass
class CrescendoEndEvent:
    pos: SourcePos | None = None


@dataclass
class VoiceBlock:
    voice_id: int
    options: dict[str, Any]
    measures: list[Measure]
    pos: SourcePos | None = None


@dataclass
class Measure:
    index: int
    start_bar: Bar | None = None
    end_bar: Bar | None = None
    events: list[Any] = field(default_factory=list)
    voice_blocks: list[VoiceBlock] = field(default_factory=list)
    measure_rest_count: int = 1
    duration: Fraction = Fraction(0)
    expected_duration: Fraction | None = None
    free: bool = False
    pickup: bool = False
    pos: SourcePos | None = None


@dataclass
class VoiceLine:
    voice_id: int
    measures: list[Measure]
    break_after: bool = False
    pagebreak_after: bool = False
    pos: SourcePos | None = None


@dataclass
class LyricSyllable:
    text: str
    annotation: str | None = None
    empty: bool = False
    hyphen_after: bool = False
    space_before: bool = False
    pos: SourcePos | None = None


@dataclass
class LyricLine:
    voice_id: int
    label: str | None
    syllables: list[LyricSyllable]
    raw: str
    pos: SourcePos | None = None


@dataclass
class System:
    index: int
    voices: list[VoiceLine] = field(default_factory=list)
    lyrics: list[LyricLine] = field(default_factory=list)
    page_break_before: bool = False
    pos: SourcePos | None = None


@dataclass
class Slur:
    voice_id: int
    start: SourcePos
    end: SourcePos | None = None


@dataclass
class Crescendo:
    voice_id: int
    direction: str
    start: SourcePos
    end: SourcePos | None = None


@dataclass
class Document:
    path: str
    version: Version | None = None
    metadata: dict[str, list[str]] = field(default_factory=dict)
    key: KeySignature | None = None
    time: TimeSignature | None = None
    beats: list[int] | None = None
    pickup: Fraction | None = None
    tempo: Tempo | None = None
    accidentals: str = "measure"
    voices: dict[int, VoiceDecl] = field(default_factory=dict)
    symbols: dict[str, str] = field(default_factory=dict)
    layout: dict[str, str] = field(default_factory=dict)
    systems: list[System] = field(default_factory=list)
    slurs: list[Slur] = field(default_factory=list)
    crescendos: list[Crescendo] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


# ---------------------------------------------------------------------------
# JSON 序列化
# ---------------------------------------------------------------------------
def jsonable(value: Any) -> Any:
    if isinstance(value, Fraction):
        return str(value)
    if isinstance(value, SourcePos):
        return value.to_dict()
    if is_dataclass(value):
        out: dict[str, Any] = {"kind": value.__class__.__name__}
        for f in fields(value):
            if f.name.startswith("_"):
                continue
            out[f.name] = jsonable(getattr(value, f.name))
        return out
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# 词法/预处理
# ---------------------------------------------------------------------------
@dataclass
class LogicalLine:
    text: str
    line: int
    column: int = 1
    is_comment: bool = False


FULLWIDTH_MAP = {
    "＃": "#", "％": "%", "｜": "|", "：": ":", "／": "/",
    "（": "(", "）": ")", "［": "[", "］": "]", "｛": "{",
    "｝": "}", "～": "~", "＠": "@",
}
FULLWIDTH_CHARS = "".join(FULLWIDTH_MAP)


def _strip_comments_line(line: str, in_block: bool) -> tuple[str, bool]:
    """删除一行中的注释，返回 (清理后的文本, 行末是否仍在块注释中)。"""
    out: list[str] = []
    quote = False
    escaped = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_block:
            if line.startswith("%}", i):
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            i += 1
            continue
        if ch == '"':
            quote = not quote
            out.append(ch)
            i += 1
            continue
        if not quote and ch == "%":
            if line.startswith("%{", i):
                in_block = True
                i += 2
                continue
            break
        out.append(ch)
        i += 1
    return "".join(out), in_block


def _normalize_fragment(fragment: str, lenient: bool,
                        diagnostics: list[Diagnostic],
                        pos: SourcePos) -> str:
    """只处理一段语法文本中的全角符号，不递归判断行类型。"""
    out: list[str] = []
    quote = False
    escaped = False
    col = pos.column
    for ch in fragment:
        if escaped:
            out.append(ch)
            escaped = False
            col += 1
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            col += 1
            continue
        if ch == '"':
            quote = not quote
            out.append(ch)
            col += 1
            continue
        if not quote and ch in FULLWIDTH_CHARS:
            replacement = FULLWIDTH_MAP[ch]
            if lenient:
                diagnostics.append(Diagnostic(
                    "warning",
                    "全角语法字符 %s 已按宽松模式转换为 %s" % (ch, replacement),
                    SourcePos(pos.line, col), "fullwidth", "3.6"))
                out.append(replacement)
            else:
                diagnostics.append(Diagnostic(
                    "error",
                    "全角语法字符 %s，请改用 ASCII %s" % (ch, replacement),
                    SourcePos(pos.line, col), "fullwidth", "3.6"))
                out.append(ch)
        else:
            out.append(ch)
        col += 1
    return "".join(out)


def _normalize_syntax(line: str, lenient: bool,
                      diagnostics: list[Diagnostic],
                      pos: SourcePos) -> str:
    """严格模式检查全角语法字符；宽松模式在字符串外转换并告警。"""
    # 歌词正文、标题/文本值中的全角标点合法，因此只处理语法前缀/声部主体。
    stripped = line.lstrip()
    if re.match(r"^L(?:\(\d+\))?\s*:", stripped):
        prefix, sep, body = line.partition(":")
        return _normalize_fragment(prefix, lenient, diagnostics, pos) + sep + body
    if stripped.startswith("@"):
        # 描述头的键必须 ASCII；值（标题、作者等）允许全角。
        m = re.match(r"^(\s*@[^\s]*)(.*)$", line, re.S)
        if m:
            return (_normalize_fragment(m.group(1), lenient, diagnostics, pos)
                    + m.group(2))
        return line
    return _normalize_fragment(line, lenient, diagnostics, pos)


def preprocess(text: str, path: str,
               diagnostics: list[Diagnostic]) -> list[LogicalLine]:
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\x00" in text:
        # 位置只能粗略给出第一处
        before = text.split("\x00", 1)[0]
        line = before.count("\n") + 1
        col = len(before.rsplit("\n", 1)[-1]) + 1
        diagnostics.append(Diagnostic(
            "error", "文件包含 NUL 字符", SourcePos(line, col), "nul", "3.1"))
        text = text.replace("\x00", "")

    physical = text.splitlines()
    logical: list[LogicalLine] = []
    in_block = False
    i = 0
    while i < len(physical):
        start_line = i + 1
        raw = physical[i]
        was_in_block = in_block
        cleaned, in_block = _strip_comments_line(raw, in_block)
        comment_only = (was_in_block or in_block) and not cleaned.strip()
        if not cleaned.strip() and raw.strip():
            comment_only = True
        i += 1
        # 续行：行尾单独的 \（前面可有空白）。
        while True:
            stripped = cleaned.rstrip()
            trailing = len(stripped) - len(stripped.rstrip("\\"))
            if trailing % 2 == 1 and not in_block:
                cleaned = stripped[:-1] + " "
                if i >= len(physical):
                    diagnostics.append(Diagnostic(
                        "error", "续行符后没有下一行",
                        SourcePos(start_line, 1), "continuation", "3.4"))
                    break
                nxt, in_block = _strip_comments_line(physical[i], in_block)
                i += 1
                cleaned += nxt.lstrip()
                continue
            break
        logical.append(LogicalLine(cleaned, start_line, 1, comment_only))

    if in_block:
        diagnostics.append(Diagnostic(
            "error", "块注释 %{ 没有对应的 %}",
            SourcePos(len(physical), 1), "block-comment", "3.2"))
    return logical


# ---------------------------------------------------------------------------
# 参数/命令辅助
# ---------------------------------------------------------------------------
def _split_top_level(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    quote = False
    escaped = False
    depth = 0
    for ch in text:
        if escaped:
            cur.append(ch)
            escaped = False
            continue
        if ch == "\\":
            cur.append(ch)
            escaped = True
            continue
        if ch == '"':
            quote = not quote
            cur.append(ch)
            continue
        if quote:
            cur.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur or parts:
        parts.append("".join(cur).strip())
    return parts


def _unescape_quoted(raw: str, pos: SourcePos | None = None,
                     diagnostics: list[Diagnostic] | None = None) -> str:
    """解析带引号字符串的转义。raw 不含外层引号。"""
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            if diagnostics:
                diagnostics.append(Diagnostic(
                    "warning", "字符串末尾有多余反斜杠", pos or SourcePos(1, 1),
                    "escape", "4.1"))
            out.append("\\")
            break
        esc = raw[i + 1]
        if esc == '"':
            out.append('"')
        elif esc == "\\":
            out.append("\\")
        elif esc == "n":
            out.append("\n")
        elif esc == "t":
            out.append("\t")
        elif esc == "u":
            hexpart = raw[i + 2:i + 6]
            if len(hexpart) == 4 and re.fullmatch(r"[0-9A-Fa-f]{4}", hexpart):
                out.append(chr(int(hexpart, 16)))
                i += 6
                continue
            if diagnostics:
                diagnostics.append(Diagnostic(
                    "error", "非法的 \\uXXXX 转义", pos or SourcePos(1, 1),
                    "escape", "4.1"))
            out.append("\\u")
        else:
            # 规范只定义上述转义；其他反斜杠保留字面值并告警。
            if diagnostics:
                diagnostics.append(Diagnostic(
                    "warning", "未知字符串转义 \\%s" % esc,
                    pos or SourcePos(1, 1), "escape", "4.1"))
            out.append("\\")
            out.append(esc)
        i += 2
    return "".join(out)


def parse_quoted(raw: str, pos: SourcePos,
                 diagnostics: list[Diagnostic]) -> tuple[str, int]:
    """从 raw[0]=='"' 开始解析一个带引号字符串，返回 (值, 结束下标)。"""
    assert raw.startswith('"')
    i = 1
    escaped = False
    while i < len(raw):
        ch = raw[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if ch == '"':
            return _unescape_quoted(raw[1:i], pos, diagnostics), i + 1
        i += 1
    diagnostics.append(Diagnostic(
        "error", "字符串缺少结束引号", pos, "unterminated-string", "4.1"))
    return _unescape_quoted(raw[1:], pos, diagnostics), len(raw)


def parse_arg_value(raw: str, pos: SourcePos,
                    diagnostics: list[Diagnostic]) -> Arg:
    raw = raw.strip()
    if raw.startswith('"'):
        value, end = parse_quoted(raw, pos, diagnostics)
        if raw[end:].strip():
            diagnostics.append(Diagnostic(
                "warning", "引号参数后还有多余内容，已忽略",
                pos, "arg-trailing", "13"))
        return Arg(raw, "quoted", value, None, pos)
    if re.fullmatch(r"-?\d+", raw):
        return Arg(raw, "number", int(raw), None, pos)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return Arg(raw, "number", float(raw), None, pos)
    if re.fullmatch(r"\d+\s*/\s*\d+", raw):
        a, b = re.split(r"\s*/\s*", raw)
        if int(b) == 0:
            diagnostics.append(Diagnostic(
                "error", "比例/分数的分母不能为 0", pos, "ratio", "4.4"))
            return Arg(raw, "bare", raw, None, pos)
        return Arg(raw, "ratio", Fraction(int(a), int(b)), None, pos)
    if re.fullmatch(r"\d+\s*:\s*\d+", raw):
        a, b = re.split(r"\s*:\s*", raw)
        if int(b) == 0:
            diagnostics.append(Diagnostic(
                "error", "比例/分数的分母不能为 0", pos, "ratio", "6.6"))
            return Arg(raw, "bare", raw, None, pos)
        return Arg(raw, "ratio", Fraction(int(a), int(b)), None, pos)
    if re.fullmatch(r"\d+(?:\s*\+\s*\d+)+", raw):
        return Arg(raw, "beats", [int(x) for x in re.split(r"\s*\+\s*", raw)],
                   None, pos)
    if re.fullmatch(r"[1-7]\s*=\s*[#b]?[A-G]", raw):
        return Arg(raw, "key", raw.replace(" ", ""), None, pos)
    if re.fullmatch(r"\d+\s*=\s*\d+", raw):
        return Arg(raw, "tempo", raw.replace(" ", ""), None, pos)
    if re.fullmatch(r"[a-z][a-z0-9-]*", raw):
        return Arg(raw, "bare", raw, None, pos)
    return Arg(raw, "bare", raw, None, pos)


def parse_args(raw: str, pos: SourcePos,
               diagnostics: list[Diagnostic]) -> list[Arg]:
    args: list[Arg] = []
    for part in _split_top_level(raw):
        if part == "":
            continue
        # name=value（等号不能在引号内）
        m = re.match(r"^([a-z][a-z0-9-]*)\s*=\s*(.*)$", part, re.S)
        if m and not part.startswith('"'):
            name, value_raw = m.group(1), m.group(2).strip()
            if value_raw.startswith('"'):
                value, _ = parse_quoted(value_raw, pos, diagnostics)
                args.append(Arg(part, "named", value, name, pos))
            else:
                args.append(Arg(part, "named", value_raw, name, pos))
            continue
        args.append(parse_arg_value(part, pos, diagnostics))
    return args


def _arg_text(arg: Arg | None) -> str | None:
    if arg is None:
        return None
    # ratio/key/tempo/beats 的 Fraction/列表值已经被规范化，校验时需要原始写法。
    if arg.kind in ("ratio", "key", "tempo", "beats"):
        return arg.raw.strip()
    if isinstance(arg.value, str):
        return arg.value
    return str(arg.value)


# ---------------------------------------------------------------------------
# 主体解析器
# ---------------------------------------------------------------------------
class BodyParser:
    def __init__(self, parser: "Parser", text: str, pos: SourcePos,
                 voice_id: int, *,
                 allow_bars: bool = True,
                 allow_voice: bool = True,
                 allow_global: bool = True,
                 in_voice_block: bool = False):
        self.parser = parser
        self.text = text
        self.base_pos = pos
        self.voice_id = voice_id
        self.allow_bars = allow_bars
        self.allow_voice = allow_voice
        self.allow_global = allow_global
        self.in_voice_block = in_voice_block
        self.i = 0
        self.n = len(text)

    # -- 位置 ---------------------------------------------------------------
    def pos(self, offset: int | None = None) -> SourcePos:
        off = self.i if offset is None else offset
        return SourcePos(self.base_pos.line, self.base_pos.column + off)

    def error(self, msg: str, pos: SourcePos | None = None,
              code: str = "", spec: str = "", lenient_ok: bool = False) -> None:
        self.parser.error(msg, pos or self.pos(), code=code, spec=spec,
                          lenient_ok=lenient_ok)

    def warning(self, msg: str, pos: SourcePos | None = None,
                code: str = "", spec: str = "") -> None:
        self.parser.warning(msg, pos or self.pos(), code=code, spec=spec)

    # -- 基本扫描 -----------------------------------------------------------
    def peek(self, offset: int = 0) -> str:
        j = self.i + offset
        return self.text[j] if 0 <= j < self.n else ""

    def skip_ws(self) -> None:
        while self.i < self.n and self.text[self.i] in " \t":
            self.i += 1

    def read_balanced(self, opening: str, closing: str) -> str:
        assert self.peek() == opening
        start = self.i
        depth = 0
        quote = False
        escaped = False
        while self.i < self.n:
            ch = self.text[self.i]
            if escaped:
                escaped = False
                self.i += 1
                continue
            if ch == "\\":
                escaped = True
                self.i += 1
                continue
            if ch == '"':
                quote = not quote
                self.i += 1
                continue
            if quote:
                self.i += 1
                continue
            if ch == opening:
                depth += 1
            elif ch == closing:
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return self.text[start + 1:self.i - 1]
            self.i += 1
        self.error("缺少配对的 %s" % closing, self.pos(start),
                   "unbalanced", "13")
        return self.text[start + 1:]

    # -- 音符 ---------------------------------------------------------------
    def parse_note(self, *, grace: bool = False) -> NoteEvent | GraceNote:
        start = self.i
        pitch_ch = self.peek()
        if pitch_ch not in "01234567x":
            self.error("非法音高 %r" % pitch_ch, self.pos(), "note", "6.1")
            self.i += 1
            pitch_ch = "0"
        self.i += 1
        octave = 0
        accidental: str | None = None
        dots = 0
        slashes = 0
        while self.i < self.n:
            ch = self.peek()
            if ch == "'":
                octave += 1
                self.i += 1
            elif ch == ",":
                octave -= 1
                self.i += 1
            elif ch in "#bn":
                if accidental is not None:
                    self.error("一个音符最多一个变化音", self.pos(),
                               "accidental", "6.3")
                else:
                    accidental = ch
                self.i += 1
            elif ch == ".":
                dots += 1
                self.i += 1
            elif ch == "/":
                slashes += 1
                self.i += 1
            else:
                break
        if abs(octave) > 3:
            self.warning("八度点超过 3 个", self.pos(start), "octave", "6.1")
        if dots > 2:
            self.warning("附点超过 2 个", self.pos(start), "dots", "6.2")
        if slashes > 4:
            self.warning("减时线超过 4 条", self.pos(start), "slashes", "6.2")
        if pitch_ch == "x" and (octave or accidental):
            self.error("节奏音符 x 不得带八度或变化音", self.pos(start),
                       "rhythm-note", "6.4")
            octave = 0
            accidental = None
        if pitch_ch == "0" and accidental:
            self.error("休止符不得带变化音", self.pos(start),
                       "rest-accidental", "6.4")
            accidental = None
        written = Fraction(1, 2 ** slashes) * (2 - Fraction(1, 2 ** dots))
        if grace:
            return GraceNote(
                int(pitch_ch) if pitch_ch.isdigit() else pitch_ch,
                octave, accidental, dots, slashes, written, self.pos(start))
        note = NoteEvent(
            pitch=int(pitch_ch) if pitch_ch.isdigit() else pitch_ch,
            octave=octave,
            accidental=accidental,
            dots=dots,
            slashes=slashes,
            written_duration=written,
            duration=written,
            pos=self.pos(start),
        )
        return note

    # -- 小节线 -------------------------------------------------------------
    def parse_bar(self) -> Bar:
        start = self.i
        bar = Bar(pos=self.pos(start))
        if self.peek() == ":":
            self.i += 1
            if self.peek() != "|":
                self.error("反复记号必须以 :| 或 :|: 出现", self.pos(start),
                           "bar", "7.1")
            else:
                self.i += 1
                if self.peek() == ":":
                    self.i += 1
                    bar.kind = "repeat_both"
                    bar.repeat_end = True
                    bar.repeat_start = True
                else:
                    bar.kind = "repeat_end"
                    bar.repeat_end = True
        else:
            assert self.peek() == "|"
            self.i += 1
            if self.peek() == "|":
                self.i += 1
                bar.kind = "double"
            elif self.peek() == "]":
                self.i += 1
                bar.kind = "final"
            elif self.peek() == ":":
                self.i += 1
                bar.kind = "repeat_start"
                bar.repeat_start = True
        # 跳房子标签：digits { "," digits } "."
        m = re.match(r"(\d+(?:,\d+)*)\.", self.text[self.i:])
        if m:
            bar.ending = [int(x) for x in m.group(1).split(",")]
            self.i += len(m.group(0))
        return bar

    # -- 命令 ---------------------------------------------------------------
    def parse_command(self) -> tuple[Command, str | None]:
        start = self.i
        assert self.peek() == "\\"
        self.i += 1
        if self.peek() in "<>!":
            name = self.peek()
            self.i += 1
        else:
            m = re.match(r"[A-Za-z][A-Za-z0-9-]*", self.text[self.i:])
            if not m:
                self.error("反斜杠后缺少命令名", self.pos(start),
                           "command", "13")
                return Command("", [], "\\", self.pos(start)), None
            name = m.group(0)
            self.i += len(name)
            if name != name.lower():
                self.error("命令名必须小写：\\%s" % name, self.pos(start),
                           "command-case", "3.5")
        args: list[Arg] = []
        if self.peek() == "(":
            arg_pos = self.pos()
            raw = self.read_balanced("(", ")")
            args = parse_args(raw, arg_pos, self.parser.diagnostics)
        block: str | None = None
        if self.peek() == "{":
            block = self.read_balanced("{", "}")
        raw = self.text[start:self.i]
        return Command(name, args, raw, self.pos(start)), block

    # -- 多连音/倚音 ---------------------------------------------------------
    def parse_grace_notes(self, closing: str) -> list[GraceNote]:
        notes: list[GraceNote] = []
        while self.i < self.n:
            self.skip_ws()
            if self.peek() == closing:
                self.i += 1
                break
            ch = self.peek()
            if ch in "01234567x":
                note = self.parse_note(grace=True)
                assert isinstance(note, GraceNote)
                notes.append(note)
                continue
            if ch == "\\":
                cmd, _ = self.parse_command()
                self.warning("倚音内忽略命令 \\%s" % cmd.name, cmd.pos,
                             "grace-command", "6.7")
                continue
            self.error("倚音内只能包含音符", self.pos(), "grace", "6.7")
            self.i += 1
        else:
            if closing:
                self.error("倚音缺少结束的 %s" % closing, self.pos(),
                           "grace", "6.7")
        return notes

    def parse_tuplet(self, ratio: tuple[int, int] | None = None,
                     pos: SourcePos | None = None) -> TupletEvent:
        start_pos = pos or self.pos()
        if ratio is None:
            m = re.match(r"(\d+)\s*:\s*(\d+)\s*\{", self.text[self.i:])
            if not m:
                self.error("多连音语法错误", start_pos, "tuplet", "6.6")
                self.i += 1
                return TupletEvent(1, 1, [], Fraction(0), start_pos)
            num, den = int(m.group(1)), int(m.group(2))
            self.i += m.end() - 1          # 停在 "{"
        else:
            num, den = ratio
            if self.peek() != "{":
                self.error("多连音缺少 {", start_pos, "tuplet", "6.6")
                return TupletEvent(num, den, [], Fraction(0), start_pos)
        if num <= 0 or den <= 0 or num == den:
            self.error("多连音比例必须为正且 N != M", start_pos,
                       "tuplet-ratio", "6.6")
        body = self.read_balanced("{", "}")
        sub = BodyParser(self.parser, body, self.pos(self.i - len(body) - 1),
                         self.voice_id, allow_bars=False,
                         allow_voice=False, allow_global=False,
                         in_voice_block=self.in_voice_block)
        events = sub.parse_body()
        if any(isinstance(e, Bar) for e in events):
            self.error("多连音内不得包含小节线", start_pos, "tuplet-bar", "6.6")
        ev = TupletEvent(num, den, events, Fraction(0), start_pos)
        return ev

    # -- 主体 ---------------------------------------------------------------
    def parse_body(self, stop: str | None = None) -> list[Any]:
        items: list[Any] = []
        last_item: Any = None
        last_bar: Bar | None = None
        last_note: NoteEvent | None = None
        pending_grace: list[GraceNote] = []
        pending_nobeam = False

        def append(item: Any) -> None:
            nonlocal last_item
            items.append(item)
            last_item = item

        def attach_mark(cmd: Command) -> bool:
            if isinstance(last_note, NoteEvent):
                last_note.marks.append(cmd)
                return True
            if isinstance(last_item, Bar):
                last_item.marks.append(cmd)
                return True
            return False

        def attach_text(cmd: Command) -> bool:
            text_arg = cmd.arg(0)
            if text_arg is None:
                self.error("\\text 需要文字参数", cmd.pos, "text", "6.8")
                return True
            text = _arg_text(text_arg) or ""
            below = False
            if len(cmd.args) > 1:
                position = _arg_text(cmd.args[1])
                if position == "below":
                    below = True
                elif position != "above":
                    self.warning("\\text 未知位置参数 %s" % position,
                                 cmd.pos, "text", "6.8")
            if len(cmd.args) > 2:
                self.warning("\\text 有多余参数", cmd.pos, "text", "6.8")
            ann = TextAnnotation(text, below, cmd.pos)
            if isinstance(last_item, Bar):
                last_item.texts.append(ann)
                return True
            if isinstance(last_note, NoteEvent):
                last_note.texts.append(ann)
                return True
            return False

        while self.i < self.n:
            self.skip_ws()
            if self.i >= self.n:
                break
            if stop is not None and self.peek() == stop:
                self.i += 1
                break
            ch = self.peek()

            if ch == "|" or (ch == ":" and self.peek(1) == "|"):
                if not self.allow_bars:
                    self.error("此处不允许小节线", self.pos(), "bar", "7.1")
                bar = self.parse_bar()
                append(bar)
                last_bar = bar
                last_note = None
                continue

            if ch == "\\":
                cmd, block = self.parse_command()
                name = cmd.name
                if (not self.allow_global and name in (
                        "time", "key", "beats", "tempo", "partial", "free",
                        "house", "repeat", "segno", "coda", "tocoda",
                        "fine", "dc", "ds")):
                    self.error("\\%s 不能写在组、多连音或临时声部块内"
                               % name, cmd.pos, "global-command", "8.3")
                    continue
                if name in ("voice", "group", "beam", "grace", "tuplet",
                            "after"):
                    if name == "voice":
                        if not self.allow_voice or self.in_voice_block:
                            self.error("此处不得嵌套 \\voice 块", cmd.pos,
                                       "voice-nested", "8.3")
                            continue
                        if block is None:
                            self.error("\\voice 缺少 {…} 块", cmd.pos,
                                       "voice-block", "8.3")
                            continue
                        vid = 2
                        if cmd.args and cmd.args[0].value is not None:
                            try:
                                vid = int(cmd.args[0].value)
                            except (TypeError, ValueError):
                                self.error("\\voice 的第一个参数必须是声部 ID",
                                           cmd.pos, "voice-id", "8.3")
                        options = self.parse_voice_options(cmd)
                        sub = BodyParser(
                            self.parser, block, cmd.pos, vid,
                            allow_bars=True, allow_voice=False,
                            allow_global=False, in_voice_block=True)
                        sub_items = sub.parse_body()
                        measures = self.parser.build_measures(
                            sub_items, vid, cmd.pos)
                        append(VoiceBlock(vid, options, measures, cmd.pos))
                        # 块不改变主声部当前位置；last_note 保持不变。
                        # 后续 \time 等仍应附着在块之前的小节线上。
                        if last_bar is not None:
                            last_item = last_bar
                        continue

                    if name == "group":
                        self.parser._group_counter += 1
                        gid = self.parser._group_counter
                        gkind, gopts = self.parse_group_options(cmd)
                        if block is None:
                            self.error("\\group 缺少 {…} 块", cmd.pos,
                                       "group", "6.10")
                            continue
                        sub = BodyParser(
                            self.parser, block, cmd.pos, self.voice_id,
                            allow_bars=True, allow_voice=False,
                            allow_global=False,
                            in_voice_block=self.in_voice_block)
                        inner = sub.parse_body()
                        append(GroupStartEvent(gid, gkind, gopts, cmd.pos))
                        items.extend(inner)
                        append(GroupEndEvent(gid, cmd.pos))
                        last_note = self.last_note_in(inner) or last_note
                        continue

                    if name == "beam":
                        if block is None:
                            self.error("\\beam 缺少 {…} 块", cmd.pos,
                                       "beam", "6.9")
                            continue
                        sub = BodyParser(
                            self.parser, block, cmd.pos, self.voice_id,
                            allow_bars=False, allow_voice=False,
                            allow_global=False,
                            in_voice_block=self.in_voice_block)
                        inner = sub.parse_body()
                        ev = BeamEvent(inner, Fraction(0), cmd.pos)
                        append(ev)
                        last_note = self.last_note_in(inner) or last_note
                        continue

                    if name == "grace":
                        if block is None:
                            self.error("\\grace 缺少 {…} 块", cmd.pos,
                                       "grace", "6.7")
                            continue
                        notes = self.parse_grace_notes_from_text(block, cmd.pos)
                        slash = False
                        for a in cmd.args:
                            val = _arg_text(a)
                            if val == "slash":
                                slash = True
                            elif val not in (None, ""):
                                self.warning("\\grace 未知选项 %s" % val,
                                             cmd.pos, "grace-option", "6.7")
                        if slash:
                            for note in notes:
                                note.slashes += 1
                                note.written_duration = (
                                    Fraction(1, 2 ** note.slashes)
                                    * (2 - Fraction(1, 2 ** note.dots)))
                        pending_grace.extend(notes)
                        continue

                    if name == "tuplet":
                        ratio = None
                        if cmd.args:
                            a = cmd.args[0]
                            if isinstance(a.value, Fraction):
                                # 保留写出的 N:M（例如 6:4 不应被约分成 3:2），
                                # 渲染器按写出的 N 显示连音数字。
                                m = re.fullmatch(r"(\d+)\s*[:/]\s*(\d+)",
                                                 a.raw.strip())
                                if m:
                                    ratio = (int(m.group(1)), int(m.group(2)))
                                else:
                                    ratio = (a.value.numerator,
                                             a.value.denominator)
                            elif isinstance(a.value, str) and re.fullmatch(
                                    r"\d+:\d+", a.value):
                                x, y = a.value.split(":")
                                ratio = (int(x), int(y))
                        if ratio is None:
                            self.error("\\tuplet 缺少 N:M 比例", cmd.pos,
                                       "tuplet-ratio", "6.6")
                            ratio = (1, 1)
                        elif (ratio[0] <= 0 or ratio[1] <= 0
                              or ratio[0] == ratio[1]):
                            self.error("多连音比例必须为正且 N != M", cmd.pos,
                                       "tuplet-ratio", "6.6")
                        if block is None:
                            self.error("\\tuplet 缺少 {…} 块", cmd.pos,
                                       "tuplet", "6.6")
                            continue
                        # 把块内容交给 parse_tuplet 的统一实现。
                        save = self.i
                        # 直接构造一个临时文本更简单
                        sub = BodyParser(
                            self.parser, block, cmd.pos, self.voice_id,
                            allow_bars=False, allow_voice=False,
                            allow_global=False,
                            in_voice_block=self.in_voice_block)
                        inner = sub.parse_body()
                        hide_number = False
                        for a in cmd.args[1:]:
                            val = _arg_text(a)
                            if val == "none":
                                hide_number = True
                            elif val not in (None, ""):
                                self.warning("\\tuplet 未知选项 %s" % val,
                                             cmd.pos, "tuplet-option", "6.6")
                        ev = TupletEvent(ratio[0], ratio[1], inner,
                                         Fraction(0), cmd.pos, hide_number)
                        append(ev)
                        last_note = self.last_note_in(inner) or last_note
                        self.i = save
                        continue

                    if name == "after":
                        if block is None:
                            self.error("\\after 缺少 {…} 块", cmd.pos,
                                       "after", "6.7")
                            continue
                        if not isinstance(last_note, NoteEvent):
                            self.error("\\after 必须紧跟在一个主音符之后",
                                       cmd.pos, "after", "6.7")
                            continue
                        last_note.grace_after.extend(
                            self.parse_grace_notes_from_text(block, cmd.pos))
                        continue

                # 非块命令
                if name in ("break", "pagebreak"):
                    if not self.allow_global or self.in_voice_block:
                        self.error("\\%s 不能写在临时声部块内" % name,
                                   cmd.pos, "break-position", "8.3")
                        continue
                    self.parser.mark_break(self.voice_id, name)
                    # line 模式下是行级命令，应位于行尾；auto 模式可在任意小节线后。
                    rest = self.text[self.i:].strip()
                    if rest and self.parser.doc.layout.get("layout.systems") != "auto":
                        self.error("\\%s 必须写在行尾" % name, cmd.pos,
                                   "break-position", "8.2")
                    continue

                if name in ("time", "key", "beats", "tempo", "partial",
                            "free", "house", "repeat", "segno", "coda",
                            "tocoda", "fine", "dc", "ds", "mark",
                            "hidebar", "phantombar"):
                    if not isinstance(last_item, Bar):
                        self.error("\\%s 必须附着在小节线上" % name, cmd.pos,
                                   "bar-command", "7.5")
                        continue
                    if name == "hidebar":
                        last_item.hide = "hidebar"
                    elif name == "phantombar":
                        last_item.hide = "phantombar"
                    elif name == "repeat":
                        a = cmd.arg(0)
                        if a and isinstance(a.value, int) and a.value > 0:
                            last_item.repeat_count = a.value
                        else:
                            self.error("\\repeat(n) 需要正整数", cmd.pos,
                                       "repeat", "7.2")
                    else:
                        last_item.commands.append(cmd)
                    continue

                if name in ("hidden", "measure"):
                    if not isinstance(last_note, NoteEvent):
                        self.error("\\%s 必须紧跟在音符之后" % name, cmd.pos,
                                   "note-command", "6.4")
                        continue
                    if last_note.pitch != 0:
                        self.error("\\%s 只能用于休止符" % name, cmd.pos,
                                   "rest-command", "6.4")
                        continue
                    if name == "hidden":
                        last_note.hidden = True
                    else:
                        last_note.measure_rest = True
                        if cmd.args:
                            if isinstance(cmd.arg(0).value, int):
                                count = cmd.arg(0).value
                                if count <= 0:
                                    self.error("\\measure(n) 的 n 必须是正整数",
                                               cmd.pos, "measure-rest", "6.4")
                                else:
                                    last_note.measure_rest_count = count
                            else:
                                self.error("\\measure(n) 的 n 必须是正整数",
                                           cmd.pos, "measure-rest", "6.4")
                    continue

                if name == "nobeam":
                    pending_nobeam = True
                    continue

                if name in ("<", ">", "!"):
                    if name == "!":
                        append(CrescendoEndEvent(cmd.pos))
                    else:
                        append(CrescendoStartEvent(
                            "cresc" if name == "<" else "dim", cmd.pos))
                    continue

                if name == "text":
                    if not attach_text(cmd):
                        self.error("\\text 前没有可附着的音符或小节",
                                   cmd.pos, "text", "6.8")
                    continue

                if name in ("pp", "p", "mp", "mf", "f", "ff", "fff",
                            "sf", "sfz", "fp", "sfp",
                            "stacc", "ten", "acc", "marc", "fermata",
                            "trill", "mordent", "turn", "gliss", "breath",
                            "caesura", "arpeggio", "sym"):
                    if name == "sym":
                        sym_name = _arg_text(cmd.arg(0))
                        if sym_name and sym_name not in self.parser.doc.symbols:
                            self.warning("未声明的自定义符号 %s" % sym_name,
                                         cmd.pos, "symbol", "4.8")
                    if not attach_mark(cmd):
                        self.error("记号 \\%s 前没有可附着的音符或小节" % name,
                                   cmd.pos, "mark", "6.8")
                    continue

                if name == "chord":
                    if isinstance(last_note, NoteEvent):
                        last_note.chord = _arg_text(cmd.arg(0))
                        self.warning("\\chord 是可选扩展，已保存但不参与校验",
                                     cmd.pos, "chord", "6.8")
                    else:
                        self.error("\\chord 前没有音符", cmd.pos,
                                   "chord", "6.8")
                    continue

                if name == "offset":
                    self.warning("\\offset 只影响排版，解析器已忽略",
                                 cmd.pos, "offset", "6.8")
                    continue

                if name.startswith("x-"):
                    self.warning("忽略未实现的扩展命令 \\%s" % name,
                                 cmd.pos, "extension", "11.1")
                    continue

                self.error("未知的标准命令 \\%s" % name, cmd.pos,
                           "unknown-command", "11.2", lenient_ok=True)
                continue

            if ch == "(":
                append(SlurStartEvent(self.pos()))
                self.i += 1
                continue
            if ch == ")":
                append(SlurEndEvent(self.pos()))
                self.i += 1
                continue
            if ch == "~":
                if isinstance(last_note, NoteEvent):
                    if last_note.tie:
                        self.error("重复的延音线记号 ~", self.pos(),
                                   "tie", "6.5")
                    last_note.tie = True
                else:
                    self.error("延音线 ~ 前没有音符", self.pos(),
                               "tie", "6.5")
                self.i += 1
                continue
            if ch == "-":
                if isinstance(last_note, NoteEvent):
                    if last_note.pitch == 0:
                        self.error("休止符后不得使用增时线 -", self.pos(),
                                   "extension-rest", "5.5")
                    elif last_note.measure_rest:
                        self.error("整小节休止后不得使用增时线 -", self.pos(),
                                   "extension-measure", "5.5")
                    else:
                        last_note.extensions += 1
                        last_note.duration += 1
                else:
                    self.error("增时线 - 前没有音符", self.pos(),
                               "extension", "5.5")
                self.i += 1
                continue
            if ch == "[":
                self.i += 1
                pending_grace.extend(self.parse_grace_notes("]"))
                continue

            m = re.match(r"(\d+)\s*:\s*(\d+)\s*\{", self.text[self.i:])
            if m:
                self.i += m.end() - 1          # 停在 "{"
                ev = self.parse_tuplet((int(m.group(1)), int(m.group(2))),
                                       self.pos())
                append(ev)
                last_note = self.last_note_in(ev.events) or last_note
                continue

            if ch in "01234567x":
                note = self.parse_note()
                assert isinstance(note, NoteEvent)
                if pending_grace:
                    note.grace_before = pending_grace
                    pending_grace = []
                if pending_nobeam:
                    note.nobeam = True
                    pending_nobeam = False
                append(note)
                last_note = note
                continue

            self.error("无法识别的字符 %r" % ch, self.pos(),
                       "syntax", "3.2")
            self.i += 1

        if pending_grace:
            self.error("前倚音后没有主音符", pending_grace[0].pos or self.pos(),
                       "grace", "6.7")
        return items

    def parse_grace_notes_from_text(self, text: str,
                                    pos: SourcePos) -> list[GraceNote]:
        sub = BodyParser(self.parser, text, pos, self.voice_id,
                         allow_bars=False, allow_voice=False,
                         allow_global=False, in_voice_block=self.in_voice_block)
        return sub.parse_grace_notes("")

    def last_note_in(self, items: Iterable[Any]) -> NoteEvent | None:
        last: NoteEvent | None = None
        for item in items:
            if isinstance(item, NoteEvent):
                last = item
            elif isinstance(item, TupletEvent):
                last = self.last_note_in(item.events) or last
            elif isinstance(item, BeamEvent):
                last = self.last_note_in(item.events) or last
        return last

    # -- 选项 ---------------------------------------------------------------
    def parse_voice_options(self, cmd: Command) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "position": "above", "bracket": "brace", "small": False,
            "join": False, "name": None,
        }
        positional = 0
        for a in cmd.args[1:]:
            if a.name:
                if a.name == "name":
                    opts["name"] = a.value
                else:
                    self.warning("\\voice 未知选项 %s" % a.name, cmd.pos,
                                 "voice-option", "8.3")
                continue
            val = _arg_text(a) or ""
            if positional == 0 and val in ("above", "below"):
                opts["position"] = val
            elif positional <= 1 and val in ("brace", "bracket", "line", "none"):
                opts["bracket"] = val
            elif val in ("small", "join"):
                opts[val] = True
            else:
                self.warning("\\voice 未知选项 %s" % val, cmd.pos,
                             "voice-option", "8.3")
            positional += 1
        return opts

    def parse_group_options(self, cmd: Command) -> tuple[str, dict[str, Any]]:
        kind = "paren"
        opts: dict[str, Any] = {"position": "above", "join": False,
                                "label": None}
        for idx, a in enumerate(cmd.args):
            if a.name:
                if a.name == "label":
                    opts["label"] = a.value
                else:
                    self.warning("\\group 未知选项 %s" % a.name, cmd.pos,
                                 "group-option", "6.10")
                continue
            val = _arg_text(a) or ""
            if idx == 0 and val in ("paren", "bracket", "brace", "line"):
                kind = val
            elif val in ("above", "below"):
                opts["position"] = val
            elif val == "join":
                opts["join"] = True
            else:
                self.warning("\\group 未知选项 %s" % val, cmd.pos,
                             "group-option", "6.10")
        return kind, opts


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------
REPEATABLE_KEYS = {
    "subtitle", "lyricist", "composer", "arranger", "translator",
    "artist", "credit", "source", "notes", "voice", "symbol",
}
KNOWN_KEYS = {
    "version", "encoding", "title", "subtitle", "lyricist", "composer",
    "arranger", "translator", "artist", "credit", "copyright", "source",
    "notes", "dedication", "number", "genre", "language", "date", "key",
    "time", "beats", "pickup", "tempo", "accidentals", "voice", "symbol",
}
LAYOUT_KEYS = {
    "layout.page", "layout.margins", "layout.systems",
    "layout.measure-numbers", "layout.page-numbers", "layout.scale",
    "layout.font.note", "layout.font.lyric", "layout.font.text",
    "layout.font.title", "layout.lyrics-bracket", "layout.lyrics-hyphen",
    "layout.spacing", "layout.title-align", "layout.credit-align",
}
STANDARD_MARKS = {
    "pp", "p", "mp", "mf", "f", "ff", "fff", "sf", "sfz", "fp", "sfp",
    "stacc", "ten", "acc", "marc", "fermata", "trill", "mordent",
    "turn", "gliss", "breath", "caesura", "arpeggio", "sym", "text",
    "chord",
}


class Parser:
    def __init__(self, path: str, text: str, *, lenient: bool = False,
                 strict_warnings: bool = False):
        self.path = path
        self.text = text
        self.lenient = lenient
        self.strict_warnings = strict_warnings
        self.diagnostics: list[Diagnostic] = []
        self.doc = Document(path=path)
        self.lines: list[LogicalLine] = []
        self._seen_content = False
        self._body_started = False
        self._pending_break: str | None = None
        self._next_page_break = False
        self._current_system: System | None = None
        self._current_voice: VoiceLine | None = None
        self._system_break_requested = False
        self._pagebreak_requested = False
        self._group_counter = 0

    # -- 诊断 ---------------------------------------------------------------
    def add(self, severity: str, message: str, pos: SourcePos,
            code: str = "", spec: str = "") -> None:
        if severity == "warning" and self.strict_warnings:
            severity = "error"
        self.diagnostics.append(Diagnostic(severity, message, pos, code, spec))

    def error(self, message: str, pos: SourcePos,
              code: str = "", spec: str = "", lenient_ok: bool = False) -> None:
        if lenient_ok and self.lenient:
            self.add("warning", message, pos, code, spec)
        else:
            self.add("error", message, pos, code, spec)

    def warning(self, message: str, pos: SourcePos,
                code: str = "", spec: str = "") -> None:
        self.add("warning", message, pos, code, spec)

    def info(self, message: str, pos: SourcePos,
             code: str = "", spec: str = "") -> None:
        self.add("info", message, pos, code, spec)

    # -- 顶层 ---------------------------------------------------------------
    def parse(self) -> Document:
        self.lines = preprocess(self.text, self.path, self.diagnostics)
        for ll in self.lines:
            ll.text = _normalize_syntax(
                ll.text, self.lenient, self.diagnostics,
                SourcePos(ll.line, ll.column))
            self.parse_line(ll)
        self.finish_system()
        self.finalize_document()
        # 诊断按位置稳定排序
        self.doc.diagnostics = sorted(
            self.diagnostics,
            key=lambda d: (d.pos.line, d.pos.column, d.severity, d.message))
        return self.doc

    def parse_line(self, ll: LogicalLine) -> None:
        raw = ll.text
        stripped = raw.strip()
        if ll.is_comment:
            # 注释行不结束系统，也不是空行。
            return
        if not stripped:
            # 空行在 line 排版模式下结束当前系统；auto 模式下忽略。
            if self.doc.layout.get("layout.systems") != "auto":
                self.finish_system()
            return
        if stripped.startswith("@"):
            self.parse_directive(ll)
            return
        # 规范允许行首空白；分类时先去掉缩进，但正文列号要保留缩进偏移。
        work = raw.lstrip()
        indent = len(raw) - len(work)
        m = re.match(r"^V(\d*)\s*:\s*(.*)$", work)
        if m:
            self._seen_content = True
            self._body_started = True
            vid = int(m.group(1)) if m.group(1) else 1
            body = m.group(2)
            body_col = indent + m.start(2) + 1
            self.start_or_continue_system(vid)
            line = self.parse_voice_line(vid, body,
                                         SourcePos(ll.line, body_col))
            if self._current_system is None:
                self._current_system = System(
                    index=len(self.doc.systems),
                    page_break_before=self._next_page_break,
                    pos=SourcePos(ll.line, 1))
                self._next_page_break = False
            self._current_system.voices.append(line)
            self._current_voice = line
            if line.break_after:
                self._system_break_requested = True
            if line.pagebreak_after:
                self._pagebreak_requested = True
            return
        m = re.match(r"^L(?:\((\d+)\))?\s*:\s*(.*)$", work)
        if m:
            self._seen_content = True
            self._body_started = True
            vid = int(m.group(1)) if m.group(1) else (
                self._current_voice.voice_id if self._current_voice else 1)
            body = m.group(2)
            body_col = indent + m.start(2) + 1
            self.parse_lyric_line(vid, body, SourcePos(ll.line, body_col))
            return
        self._seen_content = True
        self.error("无法识别的行类型", SourcePos(ll.line, 1),
                   "line", "3.2")

    # -- 系统 ---------------------------------------------------------------
    def start_or_continue_system(self, vid: int) -> None:
        if self._current_system is None:
            return
        if self._pagebreak_requested:
            self.finish_system(page_break=True)
            return
        if self._system_break_requested:
            self.finish_system()
            return
        if vid in {v.voice_id for v in self._current_system.voices}:
            self.finish_system()
            return
        # layout.systems=auto 时空白不结束系统；重复声部 ID 仍开始新系统。

    def finish_system(self, page_break: bool = False) -> None:
        if self._current_system is not None:
            self.doc.systems.append(self._current_system)
            self._current_system = None
            self._current_voice = None
        if page_break or self._pagebreak_requested:
            self._next_page_break = True
        self._pagebreak_requested = False
        self._system_break_requested = False

    def mark_break(self, voice_id: int, name: str) -> None:
        # 记录在刚解析的声部行上；如果声部行尚未加入系统，先暂存。
        if name == "pagebreak":
            self._pagebreak_requested = True
        else:
            self._system_break_requested = True

    # -- 描述头 -------------------------------------------------------------
    def parse_directive(self, ll: LogicalLine) -> None:
        raw = ll.text
        m = re.match(r"^@([A-Za-z0-9.-]*)(?:\s+(.*))?$", raw.strip(), re.S)
        if not m:
            self.error("描述头语法错误", SourcePos(ll.line, 1),
                       "directive", "4.1")
            return
        key, value_raw = m.group(1), (m.group(2) or "").strip()
        if self._body_started:
            self.error("描述头必须出现在第一个 V/L 行之前",
                       SourcePos(ll.line, 1), "directive-order", "4.1")
            return
        if not re.fullmatch(r"[a-z][a-z0-9.-]*", key):
            self.error("描述头键名必须是小写字母/数字/./-，且以字母开头",
                       SourcePos(ll.line, 1), "directive-key", "4.1")
            return
        value, _ = self.parse_directive_value(value_raw, ll)

        if not self._seen_content:
            self._seen_content = True
            if key != "version":
                self.error("@version 必须是第一个非空、非注释行",
                           SourcePos(ll.line, 1), "version-first", "4.1")

        if key.startswith("x-"):
            self.warning("忽略未实现的扩展描述头 @%s" % key,
                         SourcePos(ll.line, 1), "extension", "11.1")
            self.doc.metadata.setdefault(key, []).append(value)
            return

        if key in LAYOUT_KEYS:
            if key in self.doc.layout:
                self.error("排版键 @%s 重复" % key, SourcePos(ll.line, 1),
                           "duplicate", "4.7")
            self.validate_layout(key, value, SourcePos(ll.line, 1))
            self.doc.layout[key] = value
            self.doc.metadata.setdefault(key, []).append(value)
            return

        if key not in KNOWN_KEYS:
            if key.startswith("layout."):
                self.warning("忽略未实现的排版键 @%s" % key,
                             SourcePos(ll.line, 1), "layout", "4.7")
            else:
                self.error("未知的描述头 @%s" % key, SourcePos(ll.line, 1),
                           "unknown-key", "4.2", lenient_ok=True)
            self.doc.metadata.setdefault(key, []).append(value)
            return

        if key in self.doc.metadata and key not in REPEATABLE_KEYS:
            self.error("描述头 @%s 不可重复" % key, SourcePos(ll.line, 1),
                       "duplicate", "4.2")
        self.doc.metadata.setdefault(key, []).append(value)

        try:
            if key == "version":
                self.parse_version(value, ll)
            elif key == "encoding":
                if value.lower().replace("-", "") != "utf8":
                    self.error("只允许 @encoding utf-8", SourcePos(ll.line, 1),
                               "encoding", "4.2")
            elif key == "key":
                self.doc.key = self.parse_key(value, SourcePos(ll.line, 1))
            elif key == "time":
                self.doc.time = self.parse_time(value, SourcePos(ll.line, 1))
            elif key == "beats":
                self.doc.beats = self.parse_beats(value, SourcePos(ll.line, 1))
            elif key == "pickup":
                # @pickup 以全音符为 1；内部时值统一用四分音符为 1。
                whole = self.parse_fraction(
                    value, SourcePos(ll.line, 1), "pickup")
                self.doc.pickup = whole * 4
            elif key == "tempo":
                self.doc.tempo = self.parse_tempo(value, SourcePos(ll.line, 1))
            elif key == "accidentals":
                if value not in ("measure", "none"):
                    self.error("@accidentals 只能是 measure 或 none",
                               SourcePos(ll.line, 1), "accidentals", "4.2")
                else:
                    self.doc.accidentals = value
            elif key == "voice":
                self.parse_voice_decl(value, SourcePos(ll.line, 1))
            elif key == "symbol":
                self.parse_symbol_decl(value, SourcePos(ll.line, 1))
        except ValueError as exc:
            self.error(str(exc), SourcePos(ll.line, 1), key, "4.2")

    def parse_directive_value(self, raw: str, ll: LogicalLine) -> tuple[str, int]:
        if raw.startswith('"'):
            value, end = parse_quoted(raw, SourcePos(ll.line, 1),
                                      self.diagnostics)
            return value, end
        # 未加引号：\% 表示字面 %
        return raw.replace("\\%", "%"), len(raw)

    def parse_version(self, value: str, ll: LogicalLine) -> None:
        m = re.fullmatch(r"(\d+)\.(\d+)", value.strip())
        if not m:
            self.error("@version 必须是 major.minor", SourcePos(ll.line, 1),
                       "version", "4.1")
            return
        major, minor = int(m.group(1)), int(m.group(2))
        self.doc.version = Version(major, minor, value.strip())
        if major != 2:
            self.error("不支持的规范主版本 %d" % major, SourcePos(ll.line, 1),
                       "version", "11.4")
        elif minor > 0:
            self.warning("规范小版本 %d 高于本解析器已知的 2.0，按已知语法解析"
                         % minor, SourcePos(ll.line, 1), "version", "11.4")

    def parse_key(self, value: str, pos: SourcePos) -> KeySignature:
        m = re.fullmatch(r"([1-7])\s*=\s*([#b]?)([A-G])", value.strip())
        if not m:
            raise ValueError("@key 语法必须是 <1-7>=<[#b]A-G>")
        return KeySignature(int(m.group(1)), m.group(3), m.group(2),
                            value.strip())

    def parse_time(self, value: str, pos: SourcePos) -> TimeSignature:
        m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", value.strip())
        if not m:
            raise ValueError("@time 语法必须是 N/D")
        n, d = int(m.group(1)), int(m.group(2))
        if n <= 0 or d not in (1, 2, 4, 8, 16, 32, 64):
            raise ValueError("@time 的分母必须是 1/2/4/8/16/32/64，分子为正")
        return TimeSignature(n, d, value.strip())

    def parse_beats(self, value: str, pos: SourcePos) -> list[int]:
        m = re.fullmatch(r"\d+(?:\s*\+\s*\d+)*", value.strip())
        if not m:
            raise ValueError("@beats 语法必须是 n+n+…")
        beats = [int(x) for x in re.split(r"\s*\+\s*", value.strip())]
        if any(b <= 0 for b in beats):
            raise ValueError("@beats 的每一项必须为正整数")
        return beats

    def parse_fraction(self, value: str, pos: SourcePos,
                       name: str) -> Fraction:
        m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", value.strip())
        if not m or int(m.group(2)) == 0:
            raise ValueError("@%s 语法必须是 n/d" % name)
        return Fraction(int(m.group(1)), int(m.group(2)))

    def parse_tempo(self, value: str, pos: SourcePos) -> Tempo:
        raw = value.strip()
        m = re.fullmatch(r'(\d+)\s*=\s*(\d+)\s*(?:"(.*)")?', raw)
        if m:
            beat, bpm = int(m.group(1)), int(m.group(2))
            if beat not in (1, 2, 4, 8, 16):
                raise ValueError("@tempo 的拍值必须是 1/2/4/8/16")
            return Tempo(beat, bpm, m.group(3), raw)
        if raw.startswith('"'):
            text, _ = parse_quoted(raw, pos, self.diagnostics)
            return Tempo(None, None, text, raw)
        return Tempo(None, None, raw, raw)

    def parse_voice_decl(self, value: str, pos: SourcePos) -> None:
        m = re.match(r"(\d+)\s*(.*)$", value.strip())
        if not m:
            raise ValueError("@voice 的第一个参数必须是正整数 ID")
        vid = int(m.group(1))
        if vid <= 0:
            raise ValueError("声部 ID 必须是正整数")
        if vid in self.doc.voices:
            self.error("声部 %d 重复声明" % vid, pos, "voice-duplicate", "4.6")
            return
        rest = m.group(2).strip()
        opts = self.parse_option_string(rest, pos)
        for opt in opts:
            if opt not in ("name", "role", "bracket", "lyrics"):
                self.warning("未知的 @voice 选项 %s" % opt, pos,
                             "voice-option", "4.6")
        role = opts.get("role")
        if role is not None and role not in (
                "melody", "accompaniment", "cue", "percussion", "other"):
            self.error("未知的声部 role=%s" % role, pos, "voice-role", "4.6")
        bracket = opts.get("bracket", "auto")
        if bracket not in ("none", "brace", "bracket", "line", "auto"):
            self.error("未知的声部 bracket=%s" % bracket, pos,
                       "voice-bracket", "4.6")
        lyrics = True
        if "lyrics" in opts:
            if opts["lyrics"] not in ("yes", "no"):
                self.error("声部 lyrics 只能是 yes/no", pos,
                           "voice-lyrics", "4.6")
            else:
                lyrics = opts["lyrics"] == "yes"
        self.doc.voices[vid] = VoiceDecl(
            vid, opts.get("name"), role, bracket, lyrics, pos)

    def parse_symbol_decl(self, value: str, pos: SourcePos) -> None:
        m = re.match(r"([a-z][a-z0-9-]*)\s*(.*)$", value.strip(), re.S)
        if not m:
            raise ValueError("@symbol 语法是 @symbol name \"text\"")
        name = m.group(1)
        if name in STANDARD_MARKS:
            raise ValueError("自定义符号不能覆盖标准记号名 %s" % name)
        if name in self.doc.symbols:
            self.error("自定义符号 %s 重复声明" % name, pos,
                       "symbol-duplicate", "4.8")
            return
        raw = m.group(2).strip()
        if raw.startswith('"'):
            text, _ = parse_quoted(raw, pos, self.diagnostics)
        else:
            text = raw
        self.doc.symbols[name] = text

    def parse_option_string(self, text: str, pos: SourcePos) -> dict[str, str]:
        """解析 @voice 的 ``name="..." role=...`` 选项。"""
        opts: dict[str, str] = {}
        i = 0
        n = len(text)
        while i < n:
            while i < n and text[i] in " \t":
                i += 1
            if i >= n:
                break
            m = re.match(r"([a-z][a-z0-9-]*)\s*=", text[i:])
            if not m:
                # 裸词，忽略并告警
                m2 = re.match(r"[a-z][a-z0-9-]*", text[i:])
                if m2:
                    self.warning("忽略 @voice 的裸选项 %s" % m2.group(0),
                                 pos, "voice-option", "4.6")
                    i += len(m2.group(0))
                else:
                    i += 1
                continue
            key = m.group(1)
            i += m.end()
            while i < n and text[i] in " \t":
                i += 1
            if i < n and text[i] == '"':
                val, end = parse_quoted(text[i:], pos, self.diagnostics)
                i += end
            else:
                m3 = re.match(r"[^\s]+", text[i:])
                val = m3.group(0) if m3 else ""
                i += len(val)
            opts[key] = val
        return opts

    def validate_layout(self, key: str, value: str, pos: SourcePos) -> None:
        v = value.strip()
        if key == "layout.page":
            if not (v in ("A4", "A3", "B5", "Letter") or
                    re.fullmatch(r"\d+(?:\.\d+)?x\d+(?:\.\d+)?", v)):
                self.error("layout.page 取值非法：%s" % v, pos,
                           "layout", "4.7")
        elif key == "layout.margins":
            if len(re.findall(r"-?\d+(?:\.\d+)?", v)) != 4:
                self.error("layout.margins 需要四个长度", pos,
                           "layout", "4.7")
        elif key == "layout.systems":
            if v not in ("line", "auto"):
                self.error("layout.systems 只能是 line/auto", pos,
                           "layout", "4.7")
        elif key == "layout.measure-numbers":
            if not (v in ("off", "system", "all") or
                    re.fullmatch(r"every:\d+", v)):
                self.error("layout.measure-numbers 取值非法", pos,
                           "layout", "4.7")
        elif key == "layout.page-numbers":
            if v not in ("on", "off"):
                self.error("layout.page-numbers 只能是 on/off", pos,
                           "layout", "4.7")
        elif key == "layout.scale":
            try:
                if float(v) <= 0:
                    raise ValueError
            except ValueError:
                self.error("layout.scale 必须是正数", pos, "layout", "4.7")
        elif key in ("layout.font.note", "layout.font.lyric",
                     "layout.font.text", "layout.font.title"):
            if not v:
                self.error("%s 不能为空" % key, pos, "layout", "4.7")
        elif key == "layout.lyrics-bracket":
            if v not in ("auto", "none"):
                self.error("layout.lyrics-bracket 只能是 auto/none", pos,
                           "layout", "4.7")
        elif key == "layout.lyrics-hyphen":
            if v not in ("auto", "on", "off"):
                self.error("layout.lyrics-hyphen 只能是 auto/on/off", pos,
                           "layout", "4.7")
        elif key == "layout.spacing":
            if v not in ("tight", "normal", "loose"):
                self.error("layout.spacing 只能是 tight/normal/loose", pos,
                           "layout", "4.7")
        elif key in ("layout.title-align", "layout.credit-align"):
            allowed = ("center", "left") if key.endswith("title-align") else (
                "right", "left")
            if v not in allowed:
                self.error("%s 取值非法" % key, pos, "layout", "4.7")

    # -- 声部/歌词 -----------------------------------------------------------
    def parse_voice_line(self, vid: int, body: str, pos: SourcePos) -> VoiceLine:
        parser = BodyParser(self, body, pos, vid)
        items = parser.parse_body()
        measures = self.build_measures(items, vid, pos)
        line = VoiceLine(vid, measures, pos=pos)
        line.break_after = self._system_break_requested
        line.pagebreak_after = self._pagebreak_requested
        # 这两个标志由 BodyParser 写入 parser 的全局状态；清掉以免下一行重复。
        self._system_break_requested = False
        self._pagebreak_requested = False
        self._group_counter = 0
        return line

    def parse_lyric_line(self, vid: int, body: str, pos: SourcePos) -> None:
        if self._current_system is None:
            self.error("歌词行必须出现在当前系统内", pos, "lyric-system", "9.1")
            return
        if not self.voice_exists_in_system(vid):
            self.error("歌词指定的声部 %d 不在当前系统内" % vid, pos,
                       "lyric-voice", "9.1")
            return
        if not body.strip():
            self.warning("空歌词行已忽略", pos, "empty-lyric", "9.1")
            return
        lyric = self.parse_lyrics(vid, body, pos)
        self._current_system.lyrics.append(lyric)

    def voice_exists_in_system(self, vid: int) -> bool:
        if self._current_system is None:
            return False
        for v in self._current_system.voices:
            if v.voice_id == vid:
                return True
            for m in v.measures:
                for b in m.voice_blocks:
                    if b.voice_id == vid:
                        return True
        return False

    def parse_lyrics(self, vid: int, body: str, pos: SourcePos) -> LyricLine:
        i = 0
        n = len(body)
        while i < n and body[i] in " \t":
            i += 1
        label: str | None = None
        if i < n and body[i] == '"':
            label, end = parse_quoted(body[i:], SourcePos(pos.line, pos.column + i),
                                      self.diagnostics)
            i += end
        syllables: list[LyricSyllable] = []
        pending_annotation: str | None = None
        pending_prefix = ""
        pending_space = False
        merge_next = False
        hyphen_after = False

        def add_syllable(text: str, empty: bool = False,
                         at: int | None = None) -> None:
            nonlocal pending_annotation, pending_prefix, pending_space, merge_next, hyphen_after
            sp = SourcePos(pos.line, pos.column + (at if at is not None else i))
            if pending_prefix:
                text = pending_prefix + text
                pending_prefix = ""
            if merge_next and syllables:
                syllables[-1].text += text
                if pending_annotation:
                    syllables[-1].annotation = pending_annotation
                merge_next = False
            else:
                syllables.append(LyricSyllable(
                    text, pending_annotation, empty,
                    hyphen_after, pending_space, sp))
                pending_annotation = None
                pending_space = False
                hyphen_after = False

        while i < n:
            ch = body[i]
            if ch in " \t":
                pending_space = True
                i += 1
                continue
            if ch == "\\":
                if body.startswith("\\text", i):
                    j = i + 5
                    if j < n and body[j] == "(":
                        # 解析括号内参数
                        depth = 0
                        quote = False
                        escaped = False
                        k = j
                        while k < n:
                            c = body[k]
                            if escaped:
                                escaped = False
                            elif c == "\\":
                                escaped = True
                            elif c == '"':
                                quote = not quote
                            elif not quote and c == "(":
                                depth += 1
                            elif not quote and c == ")":
                                depth -= 1
                                if depth == 0:
                                    k += 1
                                    break
                            k += 1
                        raw_args = body[j + 1:k - 1]
                        args = parse_args(raw_args,
                                          SourcePos(pos.line, pos.column + j),
                                          self.diagnostics)
                        pending_annotation = _arg_text(args[0]) if args else ""
                        i = k
                        continue
                # 普通转义
                if i + 1 < n:
                    add_syllable(body[i + 1], at=i)
                    i += 2
                else:
                    self.warning("歌词末尾有多余反斜杠", pos, "escape", "9.3")
                    i += 1
                continue
            if ch == "_":
                add_syllable("", empty=True, at=i)
                i += 1
                continue
            if ch == "~":
                merge_next = True
                i += 1
                continue
            if ch == "-":
                # 连字符属于前一个音节；下一个音节不应继承。
                if syllables:
                    syllables[-1].hyphen_after = True
                i += 1
                continue
            # 汉字、拉丁词、其他字素簇
            if ch.isascii() and (ch.isalnum() or ch == "'"):
                j = i
                while j < n and body[j].isascii() and (
                        body[j].isalnum() or body[j] == "'"):
                    j += 1
                add_syllable(body[i:j], at=i)
                i = j
                continue
            if ch in "，。！？；：、,.!?;:" or unicodedata.category(ch).startswith("P"):
                # 标点并入前一个原子；没有前一个则等下一个。
                if syllables:
                    syllables[-1].text += ch
                    # 标点前的空格只是排版；标点已经并入前一个原子。
                    pending_space = False
                else:
                    pending_prefix += ch
                i += 1
                continue
            add_syllable(ch, at=i)
            i += 1

        if merge_next:
            self.warning("歌词末尾的 ~ 没有可合并的下一个原子", pos,
                         "lyric-merge", "9.3")
        return LyricLine(vid, label, syllables, body, pos)

    # -- 小节构建 -----------------------------------------------------------
    def build_measures(self, items: list[Any], vid: int,
                       pos: SourcePos) -> list[Measure]:
        measures: list[Measure] = []
        current_start: Bar | None = None
        current_events: list[Any] = []
        current_blocks: list[VoiceBlock] = []
        last_pos = pos
        had_marker = False

        def is_zero_width(ev: Any) -> bool:
            return isinstance(ev, (GroupStartEvent, GroupEndEvent,
                                   SlurStartEvent, SlurEndEvent,
                                   CrescendoStartEvent, CrescendoEndEvent))

        def is_end_marker(ev: Any) -> bool:
            return isinstance(ev, (GroupEndEvent, SlurEndEvent,
                                   CrescendoEndEvent))

        def has_music(events: list[Any]) -> bool:
            return any(not is_zero_width(e) for e in events)

        def finish(end_bar: Bar | None) -> None:
            nonlocal current_start, current_events, current_blocks, had_marker
            m = Measure(
                index=len(measures),
                start_bar=current_start,
                end_bar=end_bar,
                events=current_events,
                voice_blocks=current_blocks,
                pos=current_start.pos if current_start else last_pos,
            )
            # measure_rest_count 由事件决定
            for ev in current_events:
                if isinstance(ev, NoteEvent) and ev.measure_rest:
                    m.measure_rest_count = max(1, ev.measure_rest_count)
                    break
            measures.append(m)
            current_start = None
            current_events = []
            current_blocks = []
            had_marker = False

        for item in items:
            last_pos = getattr(item, "pos", last_pos) or last_pos
            if is_zero_width(item):
                # 行尾/小节线后的 ) 或 \! 属于前一个小节，不能因此制造空小节。
                had_marker = True
                if (is_end_marker(item) and not current_events
                        and measures):
                    measures[-1].events.append(item)
                else:
                    current_events.append(item)
                continue
            if isinstance(item, Bar):
                # 只有结束类记号、没有音符时，把它们归到上一个小节。
                if (current_events and not has_music(current_events)
                        and measures and all(is_end_marker(e)
                                             for e in current_events)):
                    measures[-1].events.extend(current_events)
                    current_events = []
                music = has_music(current_events)
                if (current_start is not None and not music
                        and not current_blocks):
                    # 相邻小节线：:| |2. 这类写法应在同一个边界合并，
                    # 而不是制造一个空小节。普通 | | 仍报空小节。
                    if (not current_events and not had_marker
                            and not (current_start.repeat_start
                                     or current_start.repeat_end
                                     or current_start.ending or current_start.hide
                                     or current_start.kind != "single"
                                     or item.repeat_start or item.repeat_end
                                     or item.ending or item.hide
                                     or item.kind != "single")):
                        self.error("空小节", item.pos or pos,
                                   "empty-measure", "7.1")
                        had_marker = True
                    self.merge_bars(current_start, item)
                    continue
                if (current_start is None and not music
                        and not current_blocks and not measures):
                    current_start = item
                    continue
                finish(item)
                current_start = item
                continue
            if isinstance(item, VoiceBlock):
                if has_music(current_events):
                    self.error("\\voice 块必须放在小节线之后或行首",
                               item.pos or pos, "voice-position", "8.3")
                current_blocks.append(item)
                continue
            current_events.append(item)
        if has_music(current_events) or current_blocks:
            finish(None)
        elif current_events:
            # 只有零宽度记号：归到上一个小节，避免空小节。
            if measures:
                measures[-1].events.extend(current_events)
            else:
                finish(None)
        elif current_start is not None and not measures:
            # 只有一个小节线，构成空小节。
            finish(None)
        return measures

    def merge_bars(self, a: Bar, b: Bar) -> None:
        """把同一小节边界上的两个小节线合并。"""
        a.repeat_start = a.repeat_start or b.repeat_start
        a.repeat_end = a.repeat_end or b.repeat_end
        if a.repeat_start and a.repeat_end:
            a.kind = "repeat_both"
        elif a.repeat_end:
            a.kind = "repeat_end"
        elif a.repeat_start:
            a.kind = "repeat_start"
        elif b.kind != "single":
            a.kind = b.kind
        if b.ending is not None:
            a.ending = b.ending
        if b.hide is not None:
            a.hide = b.hide
        if b.repeat_count is not None:
            a.repeat_count = b.repeat_count
        a.commands.extend(b.commands)
        a.marks.extend(b.marks)
        a.texts.extend(b.texts)

    # -- 文档收尾/校验 -------------------------------------------------------
    def finalize_document(self) -> None:
        if not self._seen_content:
            self.error("缺少 @version", SourcePos(1, 1), "version", "4.1")
        elif self.doc.version is None:
            self.error("缺少 @version 2.0", SourcePos(1, 1),
                       "version", "4.1")
        if self.doc.time is None:
            self.error("缺少 @time", SourcePos(1, 1), "time", "4.2")
        if self.doc.key is None:
            self.error("缺少 @key", SourcePos(1, 1), "key", "4.2")
        if self.doc.beats is not None and self.doc.time is not None:
            if sum(self.doc.beats) != self.doc.time.numerator:
                self.error("@beats 总和必须等于拍号分子",
                           SourcePos(1, 1), "beats", "4.4")
        if self.doc.pickup is not None and self.doc.time is not None:
            if self.doc.pickup <= 0 or self.doc.pickup > self.doc.time.measure_length:
                self.error("@pickup 必须大于 0 且不超过一个小节",
                           SourcePos(1, 1), "pickup", "4.4")

        for idx, system in enumerate(self.doc.systems):
            self.validate_system(system, idx)
        self.validate_spans()
        self.validate_ties()
        self.validate_repeats_global()
        self.validate_jumps_global()
        self.validate_lyrics()

    def validate_system(self, system: System, system_index: int) -> None:
        if not system.voices:
            self.error("系统内没有声部行", system.pos or SourcePos(1, 1),
                       "empty-system", "8.2")
            return
        # 0\measure(n) 在谱面上占 n 个实际小节；声部对齐按有效小节数计算。
        counts = [self.effective_measure_count(v) for v in system.voices]
        max_effective = max(counts) if counts else 0
        for v, count in zip(system.voices, counts):
            if count != max_effective:
                self.error(
                    "系统 %d 的声部 %d 有 %d 个小节，其他声部有 %d 个" % (
                        system_index + 1, v.voice_id, count, max_effective),
                    v.pos or SourcePos(1, 1), "voice-alignment", "8.5")
        # 变拍/变调命令仍按实际小节索引扫描；多小节休止后的命令位置是已知扩展限制。
        max_count = max((len(v.measures) for v in system.voices), default=0)
        for v in system.voices:
            if v.voice_id not in self.doc.voices:
                self.warning("声部 %d 未用 @voice 声明" % v.voice_id,
                             v.pos or SourcePos(1, 1), "voice-undeclared", "4.6")
            for i, m in enumerate(v.measures):
                if (m.end_bar is not None and m.end_bar.kind == "final"
                        and i != len(v.measures) - 1):
                    self.error("|] 只能出现在全曲或独立段落的结尾",
                               m.end_bar.pos or m.pos or SourcePos(1, 1),
                               "final-bar", "7.1")

        # 以所有声部的小节线命令建立共同的变拍/变调时间线。
        prev_system = (self.doc.systems[system_index - 1]
                       if system_index > 0 else None)
        carry_time = (self.trailing_bar_command(prev_system, "time")
                      if prev_system is not None else None)
        carry_key = (self.trailing_bar_command(prev_system, "key")
                     if prev_system is not None else None)
        carry_beats = (self.trailing_bar_command(prev_system, "beats")
                       if prev_system is not None else None)
        carry_partial = (self.trailing_bar_command(prev_system, "partial")
                         if prev_system is not None else None)
        carry_free = (self.trailing_bar_command(prev_system, "free")
                      if prev_system is not None else None)
        current_time = self.doc.time or TimeSignature(4, 4)
        current_key = self.doc.key
        current_beats = self.doc.beats
        if carry_time is not None:
            raw = _arg_text(carry_time.arg(0)) if carry_time.arg(0) else None
            if not raw:
                self.error("\\time 需要 N/D 参数", carry_time.pos,
                           "time", "7.5")
            else:
                try:
                    current_time = self.parse_time(raw, carry_time.pos)
                except ValueError as exc:
                    self.error(str(exc), carry_time.pos, "time", "7.5")
        if carry_key is not None:
            raw = _arg_text(carry_key.arg(0)) if carry_key.arg(0) else None
            if not raw:
                self.error("\\key 需要 1=C 参数", carry_key.pos,
                           "key", "7.5")
            else:
                try:
                    current_key = self.parse_key(raw, carry_key.pos)
                except ValueError as exc:
                    self.error(str(exc), carry_key.pos, "key", "7.5")
        if carry_beats is not None:
            raw = _arg_text(carry_beats.arg(0)) if carry_beats.arg(0) else None
            if not raw:
                self.error("\\beats 需要 n+n 参数", carry_beats.pos,
                           "beats", "7.5")
            else:
                try:
                    current_beats = self.parse_beats(raw, carry_beats.pos)
                    if sum(current_beats) != current_time.numerator:
                        self.error("\\beats 总和必须等于当前拍号分子",
                                   carry_beats.pos, "beats", "7.5")
                except ValueError as exc:
                    self.error(str(exc), carry_beats.pos, "beats", "7.5")
        expected_by_idx: list[Fraction] = []
        pickup_by_idx: list[Fraction | None] = []
        free_by_idx: list[bool] = []
        for m_idx in range(max_count):
            time_cmd = self.first_bar_command(system, m_idx, "time")
            key_cmd = self.first_bar_command(system, m_idx, "key")
            beats_cmd = self.first_bar_command(system, m_idx, "beats")
            partial_cmd = self.first_bar_command(system, m_idx, "partial")
            free_cmd = self.first_bar_command(system, m_idx, "free")
            if m_idx == 0:
                partial_cmd = partial_cmd or carry_partial
                free_cmd = free_cmd or carry_free
            if time_cmd is not None:
                a = time_cmd.arg(0)
                raw = _arg_text(a) if a else None
                if not raw:
                    self.error("\\time 需要 N/D 参数", time_cmd.pos,
                               "time", "7.5")
                else:
                    try:
                        current_time = self.parse_time(raw, time_cmd.pos)
                    except ValueError as exc:
                        self.error(str(exc), time_cmd.pos, "time", "7.5")
            if key_cmd is not None:
                a = key_cmd.arg(0)
                raw = _arg_text(a) if a else None
                if not raw:
                    self.error("\\key 需要 1=C 参数", key_cmd.pos,
                               "key", "7.5")
                else:
                    try:
                        current_key = self.parse_key(raw, key_cmd.pos)
                    except ValueError as exc:
                        self.error(str(exc), key_cmd.pos, "key", "7.5")
            if beats_cmd is not None:
                a = beats_cmd.arg(0)
                raw = _arg_text(a) if a else None
                if not raw:
                    self.error("\\beats 需要 n+n 参数", beats_cmd.pos,
                               "beats", "7.5")
                else:
                    try:
                        current_beats = self.parse_beats(raw, beats_cmd.pos)
                        if sum(current_beats) != current_time.numerator:
                            self.error("\\beats 总和必须等于当前拍号分子",
                                       beats_cmd.pos, "beats", "7.5")
                    except ValueError as exc:
                        self.error(str(exc), beats_cmd.pos, "beats", "7.5")

            expected = current_time.measure_length
            pickup: Fraction | None = None
            if system_index == 0 and m_idx == 0 and self.doc.pickup is not None:
                pickup = self.doc.pickup
            if partial_cmd is not None:
                a = partial_cmd.arg(0)
                raw = _arg_text(a) if a else None
                if not raw:
                    self.error("\\partial 需要 n/d 参数", partial_cmd.pos,
                               "partial", "7.5")
                else:
                    try:
                        # \partial(n/d) 同样以全音符为 1。
                        pickup = self.parse_fraction(raw, partial_cmd.pos,
                                                    "partial") * 4
                    except ValueError as exc:
                        self.error(str(exc), partial_cmd.pos, "partial", "7.5")
            free = free_cmd is not None
            expected_by_idx.append(expected)
            pickup_by_idx.append(pickup)
            free_by_idx.append(free)

            for v in system.voices:
                if m_idx >= len(v.measures):
                    continue
                m = v.measures[m_idx]
                m.expected_duration = expected * m.measure_rest_count
                m.free = free
                m.pickup = pickup is not None
                if pickup is not None:
                    m.expected_duration = pickup * m.measure_rest_count
                self.validate_measure(m, v.voice_id, system_index)

        # 临时声部块按锚点小节对齐
        for v in system.voices:
            for anchor, m in enumerate(v.measures):
                for block in m.voice_blocks:
                    remaining = max_count - anchor
                    if len(block.measures) > remaining:
                        self.error(
                            "临时声部 %d 的块有 %d 个小节，超出主声部剩余 %d 个" % (
                                block.voice_id, len(block.measures), remaining),
                            block.pos or SourcePos(1, 1),
                            "voice-block-range", "8.3")
                    for bm_idx, bm in enumerate(block.measures):
                        idx = anchor + bm_idx
                        if idx < len(expected_by_idx):
                            expected = expected_by_idx[idx]
                            pickup = pickup_by_idx[idx]
                            bm.expected_duration = expected * bm.measure_rest_count
                            bm.free = free_by_idx[idx]
                            bm.pickup = pickup is not None
                            if pickup is not None:
                                bm.expected_duration = pickup * bm.measure_rest_count
                        else:
                            bm.expected_duration = (expected_by_idx[-1]
                                                    if expected_by_idx else None)
                        self.validate_measure(bm, block.voice_id,
                                              system_index)

        # 反复/跳房子/跳转在文档级按声部校验，允许跨系统。

    @staticmethod
    def effective_measure_count(v: VoiceLine) -> int:
        return sum(max(1, m.measure_rest_count) for m in v.measures)

    def first_bar_command(self, system: System, m_idx: int,
                          name: str, *, warn: bool = True) -> Command | None:
        found: Command | None = None
        for v in system.voices:
            if m_idx >= len(v.measures):
                continue
            bar = v.measures[m_idx].start_bar
            if bar is None:
                continue
            for cmd in bar.commands:
                if cmd.name == name:
                    if found is None:
                        found = cmd
                    elif warn and cmd.raw != found.raw:
                        self.warning(
                            "同一小节边界上的 \\%s 命令不一致，使用第一个" % name,
                            cmd.pos, "command-conflict", "8.5")
        return found

    def trailing_bar_command(self, system: System, name: str,
                             *, warn: bool = True) -> Command | None:
        """取系统最后一个公共小节边界上的行内命令。

        行尾的小节线命令属于“下一个小节”；当下一小节在下一个系统时，
        需要把它带到下一个系统的第一小节。
        """
        if not system.voices:
            return None
        max_count = max(len(v.measures) for v in system.voices)
        found: Command | None = None
        for v in system.voices:
            if len(v.measures) != max_count or not v.measures:
                continue
            bar = v.measures[-1].end_bar
            if bar is None:
                continue
            for cmd in bar.commands:
                if cmd.name == name:
                    if found is None:
                        found = cmd
                    elif warn and cmd.raw != found.raw:
                        self.warning(
                            "同一小节边界上的 \\%s 命令不一致，使用第一个" % name,
                            cmd.pos, "command-conflict", "8.5")
        return found

    def validate_measure(self, m: Measure, vid: int,
                         system_index: int) -> None:
        if not m.events and not m.voice_blocks:
            self.error("空小节", m.pos or SourcePos(1, 1),
                       "empty-measure", "7.1")
            m.duration = Fraction(0)
            return
        measure_rests = [
            e for e in m.events
            if isinstance(e, NoteEvent) and e.measure_rest
        ]
        if measure_rests:
            other = [e for e in m.events if not isinstance(e, (
                GroupStartEvent, GroupEndEvent, SlurStartEvent, SlurEndEvent,
                CrescendoStartEvent, CrescendoEndEvent))]
            if len(other) != 1:
                self.error("0\\measure 必须独占一个小节",
                           measure_rests[0].pos or m.pos or SourcePos(1, 1),
                           "measure-rest", "6.4")
            m.duration = m.expected_duration or Fraction(0)
            return
        total = sum((self.event_duration(e) for e in m.events), Fraction(0))
        m.duration = total
        if m.free:
            return
        expected = m.expected_duration
        if expected is None:
            return
        if total != expected:
            self.error(
                "第 %d 系统声部 %d 第 %d 小节有 %s 拍，应为 %s 拍" % (
                    system_index + 1, vid, m.index + 1, total, expected),
                m.pos or SourcePos(1, 1), "measure-duration", "5.6",
                lenient_ok=True)

    def event_duration(self, ev: Any) -> Fraction:
        if isinstance(ev, NoteEvent):
            return ev.duration
        if isinstance(ev, TupletEvent):
            inner = sum((self.event_duration(e) for e in ev.events),
                        Fraction(0))
            if ev.numerator > 0 and ev.denominator > 0:
                ev.duration = inner * Fraction(ev.denominator, ev.numerator)
            else:
                # 比例非法时已经报错；避免这里再次除零而中断恢复。
                ev.duration = inner
            return ev.duration
        if isinstance(ev, BeamEvent):
            inner = sum((self.event_duration(e) for e in ev.events),
                        Fraction(0))
            ev.duration = inner
            return inner
        return Fraction(0)

    # -- 反复/跳房子 ---------------------------------------------------------
    def validate_jumps_global(self) -> None:
        segnos: list[Command] = []
        codas: list[Command] = []
        tocodas: list[Command] = []
        fines: list[Command] = []
        jumps: list[Command] = []
        seen_bars: set[int] = set()
        for system in self.doc.systems:
            for v in system.voices:
                for m in v.measures:
                    for bar in (m.start_bar, m.end_bar):
                        if bar is None or id(bar) in seen_bars:
                            continue
                        seen_bars.add(id(bar))
                        for cmd in bar.commands:
                            if cmd.name == "segno":
                                segnos.append(cmd)
                            elif cmd.name == "coda":
                                codas.append(cmd)
                            elif cmd.name == "tocoda":
                                tocodas.append(cmd)
                            elif cmd.name == "fine":
                                fines.append(cmd)
                            elif cmd.name in ("dc", "ds"):
                                jumps.append(cmd)
        for cmd in jumps:
            to = cmd.named("to")
            target = _arg_text(to) if to is not None else "fine"
            if target == "coda":
                if not tocodas or not codas:
                    self.error("\\%s(to=\"coda\") 缺少 To Coda 或 Coda 目标"
                               % cmd.name, cmd.pos, "jump-target", "7.4")
            elif target not in (None, "fine"):
                self.error("\\%s 的 to 只能是 fine 或 coda" % cmd.name,
                           cmd.pos, "jump-target", "7.4")
            if cmd.name == "ds" and not segnos:
                self.error("\\ds 缺少 Segno 目标", cmd.pos,
                           "jump-target", "7.4")
        if len(segnos) > 1:
            self.warning("存在多个 Segno，播放器使用最近的一个",
                         segnos[1].pos, "jump-target", "7.4")
        if len(codas) > 1:
            self.warning("存在多个 Coda，播放器使用最近的一个",
                         codas[1].pos, "jump-target", "7.4")
        if tocodas and not codas:
            self.error("\\tocoda 缺少 Coda 目标", tocodas[0].pos,
                       "jump-target", "7.4")

    def validate_repeats_global(self) -> None:
        timelines: dict[int, list[Measure]] = {}
        for system in self.doc.systems:
            for v in system.voices:
                timelines.setdefault(v.voice_id, []).extend(v.measures)
        for vid, measures in timelines.items():
            pos = measures[0].pos if measures else SourcePos(1, 1)
            self.validate_repeats(measures, pos or SourcePos(1, 1))

    def validate_repeats(self, measures: Iterable[Measure],
                         pos: SourcePos) -> None:
        # 小节线对象可能同时是前一行的 end_bar 和下一小节的 start_bar；
        # 按出现顺序去重，避免同一个边界被校验两次。
        ordered: list[Bar] = []
        seen: set[int] = set()
        for m in measures:
            for bar in (m.start_bar, m.end_bar):
                if bar is None:
                    continue
                if id(bar) in seen:
                    continue
                seen.add(id(bar))
                ordered.append(bar)
        stack: list[Bar] = []
        house_labels: list[int] = []
        repeat_count = 2
        for bar in ordered:
            # |: \house(...) 的跳房子从同一个小节线开始，因此先压栈。
            if bar.repeat_start and not bar.repeat_end:
                stack.append(bar)
                if bar.repeat_count:
                    repeat_count = bar.repeat_count
            if bar.ending:
                # :| |2. 会在同一个边界上结束反复并开始第 2 跳房子；
                # 此时栈尚未弹出，仍然是合法的跳房子位置。
                if not stack and not bar.repeat_end:
                    self.error("跳房子必须位于反复内",
                               bar.pos or SourcePos(1, 1),
                               "house", "7.3")
                house_labels.extend(bar.ending)
            for cmd in bar.commands:
                if cmd.name == "house":
                    if not stack and not bar.repeat_end:
                        self.error("跳房子必须位于反复内", cmd.pos,
                                   "house", "7.3")
                elif cmd.name == "repeat":
                    if cmd.arg(0) and isinstance(cmd.arg(0).value, int):
                        repeat_count = cmd.arg(0).value
            if bar.repeat_end:
                if not stack:
                    self.error("反复结束 :| 没有对应的反复开始",
                               bar.pos or SourcePos(1, 1),
                               "repeat", "7.2")
                else:
                    stack.pop()
            if bar.repeat_start and bar.repeat_end:
                stack.append(bar)
                if bar.repeat_count:
                    repeat_count = bar.repeat_count
        if stack:
            self.error("反复开始 |: 没有对应的结束 :|",
                       stack[-1].pos or SourcePos(1, 1), "repeat", "7.2")
        if house_labels and repeat_count and max(house_labels) != repeat_count:
            self.warning(
                "跳房子数量 %d 与反复次数 %d 不一致" % (
                    max(house_labels), repeat_count),
                pos, "house-count", "7.3")

    # -- 连线/渐强 -----------------------------------------------------------
    def iter_events(self, events: Iterable[Any]) -> Iterator[Any]:
        for ev in events:
            if isinstance(ev, (TupletEvent, BeamEvent)):
                yield from self.iter_events(ev.events)
            else:
                yield ev

    def validate_spans(self) -> None:
        # 连音线和渐强/渐弱允许跨系统/跨页；按声部把各系统的边界串起来。
        timelines: dict[int, list[Any]] = {}
        for system in self.doc.systems:
            for v in system.voices:
                events = timelines.setdefault(v.voice_id, [])
                for m in v.measures:
                    events.extend(self.iter_events(m.events))
        for vid, events in timelines.items():
            slur_stack: list[SlurStartEvent] = []
            cresc_stack: list[CrescendoStartEvent] = []
            for ev in events:
                if isinstance(ev, SlurStartEvent):
                    slur_stack.append(ev)
                elif isinstance(ev, SlurEndEvent):
                    if not slur_stack:
                        self.error("连音线 ) 没有对应的 (",
                                   ev.pos or SourcePos(1, 1),
                                   "slur", "6.5")
                    else:
                        start_ev = slur_stack.pop()
                        self.doc.slurs.append(Slur(
                            vid, start_ev.pos or SourcePos(1, 1), ev.pos))
                elif isinstance(ev, CrescendoStartEvent):
                    cresc_stack.append(ev)
                elif isinstance(ev, CrescendoEndEvent):
                    if not cresc_stack:
                        self.error("渐强/渐弱结束 \\! 没有起点",
                                   ev.pos or SourcePos(1, 1),
                                   "crescendo", "6.8")
                    else:
                        start_ev = cresc_stack.pop()
                        self.doc.crescendos.append(Crescendo(
                            vid, start_ev.direction,
                            start_ev.pos or SourcePos(1, 1), ev.pos))
            if slur_stack:
                self.error("连音线 ( 没有闭合",
                           slur_stack[-1].pos or SourcePos(1, 1),
                           "slur", "6.5")
            if cresc_stack:
                self.warning("渐强/渐弱没有 \\! 结束",
                             cresc_stack[-1].pos or SourcePos(1, 1),
                             "crescendo", "6.8")

    # -- 延音线 -------------------------------------------------------------
    def iter_notes(self, events: Iterable[Any]) -> Iterator[NoteEvent]:
        for ev in self.iter_events(events):
            if isinstance(ev, NoteEvent):
                yield ev

    def validate_ties(self) -> None:
        # 按声部 ID 汇总跨系统的音符时间线，并按小节计算实际变化音。
        # 先建立每个系统/小节的调号时间线（行内 \key 从该小节起生效）。
        key_map: dict[tuple[int, int], KeySignature | None] = {}
        prev_system: System | None = None
        for system in self.doc.systems:
            current_key = self.doc.key
            if prev_system is not None:
                carry_key = self.trailing_bar_command(
                    prev_system, "key", warn=False)
                if carry_key is not None:
                    raw = _arg_text(carry_key.arg(0)) if carry_key.arg(0) else None
                    if raw:
                        try:
                            current_key = self.parse_key(raw, carry_key.pos)
                        except ValueError:
                            pass
            max_count = max((len(v.measures) for v in system.voices),
                            default=0)
            for m_idx in range(max_count):
                cmd = self.first_bar_command(system, m_idx, "key", warn=False)
                if cmd is not None:
                    raw = _arg_text(cmd.arg(0)) if cmd.arg(0) else None
                    if raw:
                        try:
                            current_key = self.parse_key(raw, cmd.pos)
                        except ValueError:
                            pass
                key_map[(system.index, m_idx)] = current_key
            prev_system = system

        timelines: dict[int, list[tuple[NoteEvent, tuple | None, str | None]]] = {}
        for system in self.doc.systems:
            for v in system.voices:
                timeline = timelines.setdefault(v.voice_id, [])
                for m in v.measures:
                    state: dict[tuple[int, int], str | None] = {}
                    key = key_map.get((system.index, m.index), self.doc.key)
                    key_id = key.identity() if key is not None else None
                    for note in self.iter_notes(m.events):
                        if note.pitch in (0, "x"):
                            # 休止/x 仍然进入时间线，便于诊断“延音线接到休止符”。
                            timeline.append((note, key_id, None))
                            continue
                        eff: str | None
                        if note.accidental == "n":
                            eff = None
                        elif note.accidental in ("#", "b"):
                            eff = note.accidental
                        elif self.doc.accidentals == "measure":
                            eff = state.get((int(note.pitch), note.octave))
                        else:
                            eff = None
                        if self.doc.accidentals == "measure" and note.accidental:
                            state[(int(note.pitch), note.octave)] = eff
                        timeline.append((note, key_id, eff))

        for vid, timeline in timelines.items():
            for idx, (note, key_id, eff) in enumerate(timeline):
                if not note.tie:
                    continue
                if note.pitch == 0:
                    self.error("延音线不能连接到休止符",
                               note.pos or SourcePos(1, 1), "tie", "6.5")
                    continue
                if idx + 1 >= len(timeline):
                    self.error("延音线没有后续音符",
                               note.pos or SourcePos(1, 1), "tie", "6.5")
                    continue
                nxt, next_key, next_eff = timeline[idx + 1]
                if note.pitch == "x" or nxt.pitch == "x":
                    # 规范 6.4 明确 x 可以带延音线；但 x 不能和休止/有音高音符相连。
                    if note.pitch != "x" or nxt.pitch != "x":
                        self.error("延音线不能连接节奏音符 x 与休止符或有音高音符",
                                   note.pos or SourcePos(1, 1), "tie", "6.5")
                    nxt.tie_from_prev = True
                    continue
                if nxt.pitch == 0:
                    self.error("延音线不能连接到休止符",
                               note.pos or SourcePos(1, 1), "tie", "6.5")
                    continue
                if ((note.pitch, note.octave, eff, key_id)
                        != (nxt.pitch, nxt.octave, next_eff, next_key)):
                    self.error(
                        "延音线两端实际音高不同：%s%s vs %s%s" % (
                            note.pitch, eff or "", nxt.pitch, next_eff or ""),
                        note.pos or SourcePos(1, 1), "tie-pitch", "6.5")
                nxt.tie_from_prev = True

    # -- 歌词 ---------------------------------------------------------------
    def validate_lyrics(self) -> None:
        for system in self.doc.systems:
            anchors: dict[int, int] = {}
            for v in system.voices:
                anchors[v.voice_id] = self.count_anchors(v)
            # 临时声部块与主声部可能使用同一个 ID；L(id) 优先依附主声部，
            # 只有当前系统没有该主声部时才使用临时块。
            for v in system.voices:
                for m in v.measures:
                    for b in m.voice_blocks:
                        if b.voice_id not in anchors:
                            anchors[b.voice_id] = self.count_anchors_in_measures(
                                b.measures)
            for lyric in system.lyrics:
                total = anchors.get(lyric.voice_id)
                if total is None:
                    self.error("歌词声部 %d 没有对应的音符行" % lyric.voice_id,
                               lyric.pos or SourcePos(1, 1),
                               "lyric-voice", "9.1")
                    continue
                if len(lyric.syllables) > total:
                    extra = lyric.syllables[total]
                    self.error(
                        "歌词音节数 %d 多于音符锚点 %d；第 %d 个音节无对应音符" % (
                            len(lyric.syllables), total, total + 1),
                        extra.pos or lyric.pos or SourcePos(1, 1),
                        "lyric-count", "9.2")

    def count_anchors(self, v: VoiceLine) -> int:
        return self.count_anchors_in_measures(v.measures)

    def count_anchors_in_measures(self, measures: Iterable[Measure]) -> int:
        count = 0
        for m in measures:
            for ev in self.iter_events(m.events):
                if not isinstance(ev, NoteEvent):
                    continue
                if ev.pitch in (0, "x") and ev.pitch != "x":
                    continue
                if ev.tie_from_prev:
                    continue
                count += 1
        return count


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def dump_document(doc: Document, *, summary: bool = False) -> str:
    if summary:
        lines = []
        title = doc.metadata.get("title", [""])[0]
        lines.append("file: %s" % doc.path)
        lines.append("version: %s" % (doc.version.raw if doc.version else "-"))
        lines.append("title: %s" % title)
        lines.append("key: %s" % (doc.key.raw if doc.key else "-"))
        lines.append("time: %s" % (doc.time.raw if doc.time else "-"))
        lines.append("systems: %d" % len(doc.systems))
        for s in doc.systems:
            lines.append("  system %d: voices=%s lyrics=%d measures=%s" % (
                s.index + 1,
                ",".join(str(v.voice_id) for v in s.voices),
                len(s.lyrics),
                ",".join(str(len(v.measures)) for v in s.voices)))
        errors = sum(1 for d in doc.diagnostics if d.severity == "error")
        warnings = sum(1 for d in doc.diagnostics if d.severity == "warning")
        lines.append("diagnostics: %d error, %d warning" % (errors, warnings))
        return "\n".join(lines) + "\n"
    return json.dumps(jsonable(doc), ensure_ascii=False, indent=2) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="解析番茄简谱脚本 v2.0，输出 AST JSON（暂不渲染 SVG）。")
    ap.add_argument("files", nargs="+", metavar="FILE",
                    help="一个或多个 .jps 文件")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="把 JSON 写入文件；多个输入时必须是目录")
    ap.add_argument("--format", choices=["json", "summary"], default="json",
                    help="输出格式（默认 json）")
    ap.add_argument("--summary", action="store_true",
                    help="等价于 --format summary")
    ap.add_argument("--lenient", action="store_true",
                    help="宽松模式：全角符号自动转换，部分错误降级为警告")
    ap.add_argument("--strict", action="store_true",
                    help="把 warning 升级为 error（语法始终严格；与 --lenient 互斥）")
    ap.add_argument("--indent", type=int, default=2,
                    help="JSON 缩进，默认 2")
    args = ap.parse_args(argv)

    if args.lenient and args.strict:
        ap.error("--lenient 与 --strict 不能同时使用")
    if args.summary:
        args.format = "summary"

    multiple = len(args.files) > 1
    out_path = Path(args.output) if args.output else None
    if multiple and out_path is not None and not out_path.is_dir():
        ap.error("多个输入文件时 -o 必须是已存在的目录")

    exit_code = 0
    results: list[Document] = []
    for file_name in args.files:
        path = Path(file_name)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            print("%s:1:1: error: 文件不是有效的 UTF-8：%s" % (path, exc),
                  file=sys.stderr)
            exit_code = 1
            continue
        except OSError as exc:
            print("%s:1:1: error: 无法读取文件：%s" % (path, exc),
                  file=sys.stderr)
            exit_code = 1
            continue

        parser = Parser(str(path), text, lenient=args.lenient,
                        strict_warnings=args.strict)
        doc = parser.parse()
        results.append(doc)
        for diag in doc.diagnostics:
            print(diag.format(str(path)), file=sys.stderr)
        if any(d.severity == "error" for d in doc.diagnostics):
            exit_code = 1

        if args.format == "summary":
            rendered = dump_document(doc, summary=True)
        else:
            rendered = json.dumps(jsonable(doc), ensure_ascii=False,
                                  indent=args.indent) + "\n"

        if out_path is not None:
            target = (out_path / (path.stem + ".json")) if multiple else out_path
            try:
                target.write_text(rendered, encoding="utf-8")
            except OSError as exc:
                print("%s:1:1: error: 无法写入 %s：%s" % (path, target, exc),
                      file=sys.stderr)
                exit_code = 1
        elif not multiple:
            sys.stdout.write(rendered)

    if multiple and out_path is None:
        if args.format == "summary":
            for doc in results:
                sys.stdout.write(dump_document(doc, summary=True))
        else:
            data = [jsonable(d) for d in results]
            sys.stdout.write(json.dumps(data, ensure_ascii=False,
                                        indent=args.indent) + "\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
