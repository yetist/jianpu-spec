#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jps1to2 —— 把番茄简谱脚本 v1.0 转换为 v2.0（草案）格式。

用法::

    bin/jps1to2.py demo/009-排排坐.jps
    bin/jps1to2.py -o out/ demo/*.jps

输入 ``foo.jps`` 默认输出 ``foo.v2.jps``，并在旁边生成
``foo.v2.report.txt``。转换器遵循 ``v2/02-v1迁移指南.md``：能确定的写法
直接映射；有歧义或需要人工确认的地方不会静默丢弃，而是写入 ``% TODO:``
注释和报告文件。

注意：本工具只负责“尽量正确”的迁移，v1 的 ``()``、``&xx``、歌词 ``@``
等本身存在歧义，转换后仍需人工复核。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Sequence

TOOL_VERSION = "0.1"

DYNAMICS = {"pp", "p", "mp", "mf", "f", "ff", "fff", "sf", "sfz", "fp", "sfp"}
SKIP_WORDS = {"sbf"}

# v1 符号表中的常见记号（见 v1/03-曲部分.md 的记号表）。
# `+` 是 v1 的长记号写法，如 &sby+、&xby+、&cy+。
MARK_ORNAMENTS = {
    "yc": "\\fermata",
    "bc": "\\ten",
    "zy": "\\acc",
    "dy": "\\stacc",
    "hx": "\\breath",
    "shy": "\\gliss(up)",
    "xhy": "\\gliss(down)",
    "sby": "\\mordent",
    "sby+": "\\mordent",
    "xby": "\\mordent(lower)",
    "xby+": "\\mordent(lower)",
    "cy": "\\trill",
    "cy+": "\\trill",
}


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
class Report:
    """收集待人工确认的问题。"""

    def __init__(self) -> None:
        self.entries: list[tuple[int, str]] = []

    def add(self, line: int, message: str) -> None:
        message = " ".join(str(message).split())
        item = (line, message)
        if item not in self.entries:
            self.entries.append(item)

    def since(self, index: int) -> list[tuple[int, str]]:
        return self.entries[index:]

    def __len__(self) -> int:
        return len(self.entries)


def render_mark_ornament(name: str, report: Report, line: int) -> str | None:
    """把 v1 的常见记号名映射为 v2 命令；未知记号返回 None。"""
    command = MARK_ORNAMENTS.get(name)
    if command is None:
        return None
    if name in ("shy", "xhy"):
        direction = "上" if name == "shy" else "下"
        report.add(
            line,
            f"v1 的 &{name} 是{direction}滑音；已保留为 {command}，"
            "请确认渲染器支持方向参数",
        )
    if name.endswith("+"):
        report.add(
            line,
            f"v1 的 &{name} 带 `+`（长记号），已按 {command} 输出，"
            "请确认是否需要延长或自定义符号",
        )
    return command


# ---------------------------------------------------------------------------
# 抽象语法树
# ---------------------------------------------------------------------------
@dataclass
class Slot:
    """v1 歌词可以占用的一个音符位。"""

    anchor: bool
    pitch: str
    note: str = ""


@dataclass
class NoteAtom:
    pitch: str
    acc: str = ""
    octave: int = 0
    dots: int = 0
    slashes: int = 0
    front_graces: list[str] = field(default_factory=list)
    back_graces: list[str] = field(default_factory=list)
    commands: list[tuple] = field(default_factory=list)
    join: int = 0          # v1 的 ~（并入同一拍）
    split: int = 0         # v1 的 ^（强制切开）
    tie_continuation: bool = False  # 混合组内被前一个同音音符用 ~ 连住


@dataclass
class ExtensionAtom:
    pass


@dataclass
class OpenAtom:
    tuplet: bool = False


@dataclass
class CloseAtom:
    pass


@dataclass
class CommandAtom:
    command: tuple


@dataclass
class BlockAtom:
    kind: str
    body: str


@dataclass
class BarAtom:
    raw: str
    lines: int = 0
    left_rep: bool = False
    right_rep: bool = False
    terminal: bool = False
    hide: bool = False
    phantombar: bool = False
    house_open: bool = False
    house_close: bool = False
    house_unclosed: bool = False
    house_label: str = ""
    meter: str = ""
    text: str = ""
    tags: list[str] = field(default_factory=list)
    opens: int = 0
    closes: int = 0


@dataclass
class GroupAtom:
    tuplet: bool = False
    closed: bool = False
    children: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------
def is_punct(ch: str) -> bool:
    return unicodedata.category(ch).startswith("P")


def _is_house_start(s: str, i: int) -> bool:
    j = i + 1
    n = len(s)
    while j < n and s[j] in "+/":
        j += 1
    return j < n and s[j] == '"'


def _scan_bar(s: str, i: int) -> tuple[str, int]:
    n = len(s)
    j = i
    buf = ""
    # 跳房子标签前缀 `[++/"..."]`：在读到结束引号前，`+`、`/` 都是标签的一部分。
    house_prefix = False
    while j < n:
        c = s[j]
        if c in "|:":
            buf += c
            j += 1
            continue
        if c == "/":
            if house_prefix or (buf and buf[-1] in "|]"):
                buf += "/"
                j += 1
                continue
            break
        if c in "*+":
            buf += c
            j += 1
            continue
        if c == "&":
            k = j + 1
            while k < n and s[k].isalpha():
                k += 1
            buf += s[j:k]
            j = k
            continue
        if c == "]":
            buf += c
            j += 1
            continue
        if c == "[":
            if _is_house_start(s, j):
                buf += c
                house_prefix = True
                j += 1
                continue
            break
        if c == '"':
            k = s.find('"', j + 1)
            if k < 0:
                k = n - 1
            buf += s[j:k + 1]
            house_prefix = False
            j = k + 1
            continue
        if c == "(" and not buf:
            buf += c
            j += 1
            continue
        break
    return buf, j


def tokenize(s: str) -> list[tuple[str, str]]:
    """把一行 v1 曲谱切成 (kind, text)，kind 为 note/bar/block。"""
    tokens: list[tuple[str, str]] = []
    cur = ""
    cur_has = False
    i = 0
    n = len(s)
    quote = False
    grace = 0

    def flush() -> None:
        nonlocal cur, cur_has
        if cur:
            tokens.append(("note", cur))
            cur = ""
            cur_has = False

    while i < n:
        c = s[i]
        if quote:
            cur += c
            if c == '"':
                quote = False
            i += 1
            continue
        if grace > 0:
            cur += c
            if c == "[":
                grace += 1
            elif c == "]":
                grace -= 1
            i += 1
            continue
        if c in " \t":
            flush()
            i += 1
            continue
        if c in "|:":
            flush()
            tok, i = _scan_bar(s, i)
            tokens.append(("bar", tok))
            continue
        if c == "[" and _is_house_start(s, i):
            flush()
            tok, i = _scan_bar(s, i)
            tokens.append(("bar", tok))
            continue
        if c == "{":
            flush()
            j = s.find("}", i)
            if j < 0:
                j = n - 1
            tokens.append(("block", s[i:j + 1]))
            i = j + 1
            continue
        if c == "}":
            i += 1
            continue
        if c == '"':
            quote = True
            cur += c
            i += 1
            continue
        if c == "[":
            grace = 1
            cur += c
            i += 1
            continue
        if c.isdigit() or c == "-":
            if cur_has:
                flush()
            cur_has = True
            cur += c
            i += 1
            continue
        cur += c
        i += 1
    flush()
    return tokens


# ---------------------------------------------------------------------------
# 语法
# ---------------------------------------------------------------------------
def parse_note_token(tok: str, line: int, report: Report) -> list:
    """把一个 note token 拆成 NoteAtom / OpenAtom / CloseAtom 等事件。"""
    events: list = []
    last: NoteAtom | None = None
    pending_front: list[str] = []
    pending_cmds: list[tuple] = []
    n = len(tok)
    i = 0

    def new_note(pitch: str) -> NoteAtom:
        nonlocal pending_front, pending_cmds
        note = NoteAtom(pitch=pitch)
        note.front_graces.extend(pending_front)
        note.commands.extend(pending_cmds)
        pending_front = []
        pending_cmds = []
        events.append(note)
        return note

    while i < n:
        c = tok[i]
        if c == "(":
            tuplet = i + 1 < n and tok[i + 1] == "y"
            events.append(OpenAtom(tuplet=tuplet))
            if tuplet:
                i += 1
        elif c == ")":
            events.append(CloseAtom())
        elif c == "[":
            j = tok.find("]", i)
            if j < 0:
                report.add(line, f"倚音括号未闭合：{tok!r}")
                j = n - 1
            grp = tok[i:j + 1]
            body = grp[1:-1]
            if body.startswith("h"):
                body = body[1:]
                if last is not None:
                    last.back_graces.append(convert_grace_body(body))
                else:
                    report.add(line, "后倚音前没有主音符，已按前倚音保留")
                    pending_front.append(convert_grace_body(body))
            else:
                if last is not None:
                    last.front_graces.append(convert_grace_body(body))
                else:
                    pending_front.append(convert_grace_body(body))
            i = j
        elif c == '"':
            j = tok.find('"', i + 1)
            if j < 0:
                j = n
            content = tok[i + 1:j]
            cmd = ("text", content.replace("_", " "))
            if last is not None:
                last.commands.append(cmd)
            else:
                pending_cmds.append(cmd)
            i = j
        elif c == "&":
            k = i + 1
            while k < n and tok[k].isalnum():
                k += 1
            if k < n and tok[k] == "+":
                k += 1
            name = tok[i + 1:k]
            cmd = ("mark", name)
            if last is not None:
                last.commands.append(cmd)
            else:
                pending_cmds.append(cmd)
            i = k - 1
        elif c in "<>":
            k = i + 1
            while k < n and tok[k] == "+":
                k += 1
            cmd = ("cresc", c, k - (i + 1))
            if last is not None:
                last.commands.append(cmd)
            else:
                pending_cmds.append(cmd)
            i = k - 1
        elif c == "!":
            cmd = ("cresc", "end", 0)
            if last is not None:
                last.commands.append(cmd)
            else:
                pending_cmds.append(cmd)
        elif c in "/.":
            if last is not None:
                if c == "/":
                    last.slashes += 1
                else:
                    last.dots += 1
            # `(/`、`(.` 是 v1 在跨行/切分连音线处的写法：括号已单独处理，
            # 这里的 `/`、`.` 没有附着音符，直接忽略。
        elif c in ",'":
            if last is not None:
                last.octave += 1 if c == "'" else -1
            else:
                report.add(line, f"八度符号前没有音符：{tok!r}")
        elif c in "#$=":
            if last is not None:
                last.acc = c
            else:
                report.add(line, f"变化音前没有音符：{tok!r}")
        elif c in "~^":
            if last is not None:
                if c == "~":
                    last.join += 1
                else:
                    last.split += 1
            else:
                report.add(line, f"{c} 前没有音符，已忽略")
        elif c == "-":
            events.append(ExtensionAtom())
            last = None
        elif c.isdigit():
            last = new_note(c)
        elif c == "X":
            last = new_note("9")
        else:
            report.add(line, f"v1 音符中有无法识别的字符 {c!r}（{tok!r}）")
        i += 1
    # 行内单独出现的 `&dc` 等记号没有附着音符：不要静默丢弃。
    for cmd in pending_cmds:
        events.append(CommandAtom(cmd))
    return events


def convert_grace_body(body: str) -> str:
    """倚音内部只保留音高/时值；v1 的 $、= 改为 v2 的 b、n。"""
    body = body.replace("$", "b").replace("=", "n")
    body = body.replace("&", "")
    return " ".join(body.split())


def parse_block(tok: str) -> BlockAtom:
    body = tok[1:-1] if tok.endswith("}") else tok[1:]
    body = body.strip()
    kind = ""
    for prefix in ("dsb", "bz"):
        if body.startswith(prefix):
            kind = prefix
            body = body[len(prefix):].lstrip()
            break
    return BlockAtom(kind=kind, body=body)


def parse_bar(tok: str, line: int, report: Report) -> BarAtom:
    bar = BarAtom(raw=tok)
    bar.lines = tok.count("|")
    bar.left_rep = "|:" in tok
    bar.right_rep = ":|" in tok
    # v1 只有单个 `|` 后的 `/` 才是隐藏小节线；`||/` 仍是双小节线。
    bar.hide = bool(re.search(r"\|/", tok)) and tok.count("|") == 1
    bar.phantombar = "|*" in tok
    bar.opens = tok.count("(")
    bar.closes = tok.count(")")

    # `|]` 只有在行末（可能跟 &dc 等记号）才是终止线；`:|][+"2"` 里的 `]`
    # 是跳房子的右边界，不是终止线。
    terminal_spans = [
        m.span()
        for m in re.finditer(r"\|](?:\s*/)?(?:\s*&[A-Za-z]+\+?)*\s*$", tok)
    ]
    bar.terminal = bool(terminal_spans)

    # 跳房子 [ ... ]：只在方括号后紧跟引号时才算标签
    for idx, ch in enumerate(tok):
        if ch == "]":
            in_terminal = any(start <= idx < end for start, end in terminal_spans)
            if not in_terminal:
                bar.house_close = True
        if ch == "[":
            j = idx + 1
            while j < len(tok) and tok[j] in "+/":
                if tok[j] == "/":
                    bar.house_unclosed = True
                j += 1
            bar.house_open = True
            if j < len(tok) and tok[j] == '"':
                k = tok.find('"', j + 1)
                if k < 0:
                    k = len(tok)
                label = tok[j + 1:k]
                if label.startswith("p:"):
                    bar.meter = label[2:]
                else:
                    bar.house_label = label
    if bar.house_open and not bar.house_label:
        # `[+]` 之类没有标签的写法：保留一个空标签，下面会报告
        report.add(line, f"跳房子没有文字标签：{tok!r}")
    if "[/" in tok:
        bar.house_unclosed = True

    # 其余引号：临时拍号或小节文字
    bracket_span = None
    m = re.search(r'\[\s*[+/]*"', tok)
    if m:
        start = m.start() + m.group(0).rfind('"')
        end = tok.find('"', start + 1)
        if end >= 0:
            bracket_span = (start, end)
    for qm in re.finditer(r'"([^"]*)"', tok):
        if bracket_span and bracket_span[0] == qm.start():
            continue
        content = qm.group(1)
        if content.startswith("p:"):
            bar.meter = content[2:]
        elif content:
            bar.text = content.replace("_", " ")

    bar.tags = [m.group(1) for m in re.finditer(r"&([A-Za-z]+\+?)", tok)]
    if bar.opens or bar.closes:
        report.add(line, "小节线上的跨行连音线括号已移除（v2 自动续接）")
    if bar.house_unclosed:
        report.add(line, "v1 的跳房子开口标记 `[/` 已移除（v2 自动闭合）")
    return bar


def build_tree(events: Sequence, line: int, report: Report, carry_open: int = 0) -> list:
    """把线性事件组装成组树。

    ``carry_open`` 是同一 v1 声部上一行留下的未闭合连音线数量。v1 的跨行
    连音线会在下一行行首写一个 ``)``；只有确实有上一行遗留的 ``(`` 时才
    保留这个右括号，否则按多余括号删除。
    """
    root: list = []
    stack: list[tuple[GroupAtom | None, list]] = [(None, root)]
    for event in events:
        if isinstance(event, OpenAtom):
            group = GroupAtom(tuplet=event.tuplet)
            stack[-1][1].append(group)
            stack.append((group, group.children))
        elif isinstance(event, CloseAtom):
            if len(stack) > 1:
                group = stack.pop()[0]
                if group is not None:
                    group.closed = True
            elif carry_open > 0:
                # 上一行留下的连音线：保留右括号来闭合它。
                stack[0][1].append(CommandAtom(("raw", ")")))
                carry_open -= 1
                report.add(line, "行首的右括号已按上一行未闭合的连音线保留")
            else:
                report.add(line, "多余的右括号 `)` 已删除（上一行没有未闭合的连音线）")
        else:
            stack[-1][1].append(event)
    if len(stack) > 1:
        report.add(line, "连音线/多连音括号未在本行闭合，已保留到行尾；请人工检查跨行情况")
    return root


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------
def quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def need_quote(value: str) -> bool:
    return (not value) or value != value.strip() or "%" in value


def header_value(value: str) -> str:
    return quote(value) if need_quote(value) else value


def is_bar_piece(piece: str) -> bool:
    return piece.startswith(("|", ":|"))


def normalize_house_label(label: str) -> str:
    label = label.strip()
    if re.fullmatch(r"\d+(?:\s*,\s*\d+)*\.?", label):
        label = label.rstrip(".")
        return label.replace(" ", "") + "."
    return ""


def escape_lyric_atom(text: str) -> str:
    """让歌词 token 在 v2 中按字面显示。"""
    if text == "":
        return ""
    if text in ("_", "~", "-", "\\", "%"):
        return "\\" + text
    if text.startswith("\\"):
        return "\\" + text
    return text


# ---------------------------------------------------------------------------
# 单行转换
# ---------------------------------------------------------------------------
class LineContext:
    def __init__(self, report: Report, line: int, collect_slots: bool = True) -> None:
        self.report = report
        self.line = line
        self.collect_slots = collect_slots
        self.slots: list[Slot] = []
        self.pending_bar: list[tuple] = []
        self.group_depth = 0
        self.join_count = 0
        self.split_count = 0
        self.hidden_count = 0
        self.hidebar_count = 0
        self.phantom_count = 0
        self.paren_balance = 0

    def add_pending(self, command: tuple) -> None:
        self.pending_bar.append(command)

    def flush_pending_after(self, pieces: list[str]) -> None:
        for command in self.pending_bar:
            pieces.append(self.render_bar_command(command))
        self.pending_bar.clear()

    def finish(self, pieces: list[str]) -> None:
        if self.pending_bar:
            rendered = [self.render_bar_command(c) for c in self.pending_bar]
            idx = -1
            for i, p in enumerate(pieces):
                if is_bar_piece(p):
                    idx = i
            if idx >= 0:
                pieces[idx + 1:idx + 1] = rendered
            else:
                pieces.extend(rendered)
            self.report.add(self.line, "跳转/结束记号原本不在小节线上，已移到最近的小节线；请人工确认")
            self.pending_bar.clear()
        if self.group_depth:
            closer = "}" * self.group_depth
            # 如果行末是终止线，把自动补的括弧放在终止线之前，
            # 避免生成 `|] }` 这种终止线后还有结构命令的写法。
            idx = -1
            for i, p in enumerate(pieces):
                if p.strip().startswith("|]"):
                    idx = i
            if idx >= 0:
                pieces[idx:idx] = [closer]
            else:
                pieces.append(closer)
            self.report.add(self.line, "v1 的 &zkh 圆括弧未闭合，已在行尾补上 `}`")
            self.group_depth = 0
        if self.join_count:
            self.report.add(self.line, f"删除了 {self.join_count} 个 v1 的 `~`（并入同一拍），请检查减时线分组")
        if self.split_count:
            self.report.add(self.line, f"删除了 {self.split_count} 个 v1 的 `^`（强制切开），请检查减时线分组")
        if self.hidden_count or self.hidebar_count or self.phantom_count:
            self.report.add(
                self.line,
                f"保留了 v1 的 {self.hidden_count} 个隐藏休止 `8`、"
                f"{self.hidebar_count} 个隐藏小节线 `|/` 和 {self.phantom_count} 个占位小节线 `|*`"
                "（已转为 0\\hidden / |\\hidebar / |\\phantombar）；"
                "如果它们只是声部对齐填充，请人工删除",
            )

    def render_bar_command(self, command: tuple) -> str:
        kind = command[0]
        if kind == "mark":
            name = command[1]
            if name in ("dc", "ds", "fine"):
                return "\\" + name
            if name == "ty":
                return "\\tocoda"
            if name == "hs":
                return "\\dc"
            if name == "sf":
                self.report.add(self.line, "v1 的 &sf 映射为 \\sfz（v2 没有 \\sf），请确认")
                return "\\sfz"
            if name in DYNAMICS:
                return "\\" + name
            if name in ("cresc", "dim"):
                return "\\<" if name == "cresc" else "\\>"
            mapped = render_mark_ornament(name, self.report, self.line)
            if mapped is not None:
                return mapped
            self.report.add(self.line, f"未知的 v1 记号 `&{name}`，已保留为 \\x-v1-{name.lower()}，请确认含义")
            return "\\x-v1-" + name.lower()
        if kind == "text":
            return f"\\text({quote(command[1])})"
        if kind == "cresc":
            if command[1] == "end":
                return "\\!"
            return "\\<" if command[1] == "<" else "\\>"
        return ""


def note_base(note: NoteAtom) -> tuple[str, bool]:
    """返回 (v2 音符文本, 是否是可发声锚点)。"""
    pitch = note.pitch
    hidden = False
    if pitch == "8":
        pitch = "0"
        hidden = True
    elif pitch == "9":
        pitch = "x"

    acc = {"#": "#", "$": "b", "=": "n"}.get(note.acc, "")
    octave = "'" * max(note.octave, 0) if note.octave >= 0 else "," * (-note.octave)
    text = pitch + acc + octave + "." * note.dots + "/" * note.slashes
    if hidden:
        text += "\\hidden"
    anchor = pitch in "1234567x"
    return text, anchor


def render_note(note: NoteAtom, ctx: LineContext, tie: bool = False) -> str:
    if note.pitch == "8":
        ctx.hidden_count += 1
    if note.pitch in ("0", "8", "9") and (note.acc or note.octave):
        ctx.report.add(
            ctx.line,
            "休止符/节奏音符上的变化音或八度已删除（v2 不允许），请确认原谱意图",
        )
        note.acc = ""
        note.octave = 0
    base, anchor = note_base(note)
    prefix = "".join(f"[{g}]" for g in note.front_graces if g)
    suffix = "".join(f"\\after{{{g}}}" for g in note.back_graces if g)
    out = prefix + base + suffix
    if tie:
        out += "~"

    if ctx.collect_slots:
        ctx.slots.append(
            Slot(
                anchor=anchor and not note.tie_continuation,
                pitch=note.pitch,
                note=out,
            )
        )

    if note.join:
        ctx.join_count += note.join
    if note.split:
        ctx.split_count += note.split

    for command in note.commands:
        out += render_note_command(command, ctx)

    return out


def render_note_command(command: tuple, ctx: LineContext) -> str:
    kind = command[0]
    if kind == "mark":
        name = command[1]
        if name in SKIP_WORDS:
            return ""
        if name == "sf":
            ctx.report.add(ctx.line, "v1 的 &sf 映射为 \\sfz（v2 没有 \\sf），请确认")
            return "\\sfz"
        if name in DYNAMICS:
            return "\\" + name
        if name in ("dc", "ds", "fine", "ty", "hs"):
            if name == "ty":
                ctx.report.add(ctx.line, "v1 的 &ty 已按 \\tocoda 处理，请确认它是否其实是 To Coda 或其他跳转")
            elif name == "hs":
                ctx.report.add(ctx.line, "v1 的 &hs 含义不明确，已按 \\dc 处理，请确认")
            ctx.add_pending(command)
            return ""
        if name == "cresc":
            return "\\<"
        if name == "dim":
            return "\\>"
        mapped = render_mark_ornament(name, ctx.report, ctx.line)
        if mapped is not None:
            return mapped
        if name == "zkh":
            ctx.group_depth += 1
            return "\\group(paren){"
        if name == "ykh":
            if ctx.group_depth <= 0:
                ctx.report.add(ctx.line, "&ykh 没有对应的 &zkh，已忽略")
                return ""
            ctx.group_depth -= 1
            return "}"
        ctx.report.add(ctx.line, f"未知的 v1 记号 `&{name}`，已保留为 \\x-v1-{name.lower()}，请确认含义")
        return "\\x-v1-" + name.lower()
    if kind == "text":
        return f"\\text({quote(command[1])})"
    if kind == "cresc":
        if command[1] == "end":
            return "\\!"
        return "\\<" if command[1] == "<" else "\\>"
    if kind == "raw":
        if command[1] == ")":
            ctx.paren_balance -= 1
        elif command[1] == "(":
            ctx.paren_balance += 1
        return command[1]
    return ""


def render_bar(bar: BarAtom, pieces: list[str], ctx: LineContext) -> None:
    if bar.hide and not bar.terminal:
        ctx.hidebar_count += 1
    if bar.phantombar and not bar.terminal:
        ctx.phantom_count += 1
    # 基础小节线
    if bar.terminal:
        base = "|]"
    elif bar.hide:
        base = "|\\hidebar"
    elif bar.phantombar:
        base = "|\\phantombar"
    elif bar.left_rep and bar.right_rep:
        base = ":|:"
    elif bar.left_rep:
        base = "|:"
    elif bar.right_rep:
        base = ":|"
    elif bar.lines >= 2:
        # v1 的 `||` 在参考实现里画成“细线 + 粗线”，是终止线；
        # v2 对应的写法是 `|]`（v1 没有通用的双小节线）。
        base = "|]"
    elif bar.lines == 1:
        base = "|"
    else:
        base = ""

    commands: list[tuple] = []
    for tag in bar.tags:
        if tag == "sbf":
            continue
        if tag == "ty":
            ctx.report.add(ctx.line, "v1 的 &ty 已按 \\tocoda 处理，请确认它是否其实是 To Coda 或其他跳转")
        elif tag == "hs":
            ctx.report.add(ctx.line, "v1 的 &hs 含义不明确，已按 \\dc 处理，请确认")
        commands.append(("mark", tag))
    if bar.meter:
        commands.append(("meter", bar.meter))
    if bar.text:
        commands.append(("bar_text", bar.text))

    label = normalize_house_label(bar.house_label)
    if bar.house_open and bar.house_label and not label:
        commands.append(("house", bar.house_label))
        ctx.report.add(ctx.line, f"跳房子标签 `{bar.house_label}` 不是数字，已使用 \\house(\"...\")")
    if bar.terminal and bar.house_open:
        ctx.report.add(ctx.line, "终止线后又出现跳房子起点，请人工确认结构")

    if base:
        piece = base
        if bar.house_open:
            if label:
                if base == "|":
                    piece += label
                else:
                    piece += " |" + label
            elif not bar.house_label:
                piece += " \\house(\"\")"
        pieces.append(piece)
    elif bar.house_open:
        if label:
            if pieces and is_bar_piece(pieces[-1]):
                if pieces[-1] == "|":
                    pieces[-1] += label
                else:
                    pieces.append("|" + label)
            else:
                pieces.append("|" + label)
        elif bar.house_label:
            target = "" + f"\\house({quote(bar.house_label)})"
            if pieces and is_bar_piece(pieces[-1]):
                pieces[-1] += " " + target
            else:
                pieces.append("| " + target)

    for command in commands:
        kind = command[0]
        if kind == "meter":
            pieces.append(f"\\time({command[1]})")
        elif kind == "bar_text":
            pieces.append(f"\\text({quote(command[1])})")
        elif kind == "house":
            pieces.append(f"\\house({quote(command[1])})")
        else:
            pieces.append(ctx.render_bar_command(command))

    if ctx.pending_bar:
        ctx.flush_pending_after(pieces)


def note_core(note: NoteAtom) -> tuple[str, str, int]:
    return (note.pitch, note.acc, note.octave)


def nodes_duration(nodes: Sequence) -> Fraction:
    total = Fraction(0)
    for node in nodes:
        if isinstance(node, NoteAtom):
            dotted = Fraction(2 ** (node.dots + 1) - 1, 2 ** node.dots)
            total += dotted / (2 ** node.slashes)
        elif isinstance(node, ExtensionAtom):
            total += 1
        elif isinstance(node, GroupAtom):
            total += nodes_duration(node.children)
    return total


def only_notes(nodes: Sequence) -> list[NoteAtom]:
    result: list[NoteAtom] = []
    for node in nodes:
        if isinstance(node, NoteAtom):
            result.append(node)
        elif isinstance(node, (ExtensionAtom, BarAtom)):
            continue
        else:
            return []
    return result


def same_pitch_notes(notes: Sequence[NoteAtom]) -> bool:
    if len(notes) < 2:
        return False
    first = notes[0]
    if first.pitch in ("0", "8"):
        return False
    acc = first.acc
    for note in notes[1:]:
        if note.pitch != first.pitch or note.octave != first.octave:
            return False
        # v1 中同小节的第二个同音音符可以省略变化音
        if note.acc and note.acc != acc:
            return False
    return True


def same_pitch_pair(a: NoteAtom, b: NoteAtom) -> bool:
    if a.pitch in ("0", "8") or b.pitch in ("0", "8"):
        return False
    if a.pitch != b.pitch or a.octave != b.octave:
        return False
    return not (b.acc and b.acc != a.acc)


def convert_nodes(nodes: Sequence, ctx: LineContext) -> list[str]:
    pieces: list[str] = []
    for node in nodes:
        if isinstance(node, NoteAtom):
            pieces.append(render_note(node, ctx))
        elif isinstance(node, ExtensionAtom):
            pieces.append("-")
        elif isinstance(node, BarAtom):
            render_bar(node, pieces, ctx)
        elif isinstance(node, CommandAtom):
            text = render_note_command(node.command, ctx)
            if text:
                pieces.append(text)
        elif isinstance(node, BlockAtom):
            pieces.append(convert_block(node, ctx))
        elif isinstance(node, GroupAtom):
            pieces.extend(convert_group(node, ctx))
    return pieces


def render_children(nodes: Sequence, ctx: LineContext) -> str:
    pieces = convert_nodes(nodes, ctx)
    return " ".join(p for p in pieces if p)


def convert_group(group: GroupAtom, ctx: LineContext) -> list[str]:
    if group.tuplet:
        return convert_tuplet(group, ctx)

    notes = only_notes(group.children)
    if group.children and isinstance(group.children[0], ExtensionAtom):
        ctx.report.add(
            ctx.line,
            "v1 的 `(- ...)` 组以增时线开头（跨小节延音/切分连音），已去掉括号并保留时值；请确认延音线位置",
        )
        return convert_nodes(group.children, ctx)
    if same_pitch_notes(notes):
        # 同音相连：v1 的 () 转成 v2 的 ~
        ctx.report.add(ctx.line, "同音 () 已按延音线转换为 `~`，请确认它确实不是同音连音线")
        pieces: list[str] = []
        note_index = 0
        slots_before = len(ctx.slots)
        for child in group.children:
            if isinstance(child, NoteAtom):
                rendered = render_note(child, ctx, tie=note_index < len(notes) - 1)
                pieces.append(rendered)
                note_index += 1
            elif isinstance(child, ExtensionAtom):
                ctx.report.add(ctx.line, "延音线组内的增时线 `-` 已删除，请核对时值")
            elif isinstance(child, BarAtom):
                render_bar(child, pieces, ctx)
            else:
                pieces.extend(convert_nodes([child], ctx))
        # 延音线的后续音符不是歌词锚点
        for slot in ctx.slots[slots_before + 1:]:
            slot.anchor = False
        return pieces

    # 普通连音线。若组内有相邻同音音符，按迁移指南拆成内部 `~`。
    tie_after: set[int] = set()
    mixed_ties = 0
    for i, child in enumerate(group.children[:-1]):
        nxt = group.children[i + 1]
        if (
            isinstance(child, NoteAtom)
            and isinstance(nxt, NoteAtom)
            and same_pitch_pair(child, nxt)
        ):
            nxt.tie_continuation = True
            tie_after.add(id(child))
            mixed_ties += 1
    if mixed_ties:
        ctx.report.add(
            ctx.line,
            f"混合连音线组内发现 {mixed_ties} 处相邻同音，已用 `~` 连接，请确认它们是延音线还是分句连音线",
        )

    ctx.paren_balance += 1
    if not group.closed:
        # 不补右括号：v2 的连音线可以跨行，下一行会继续
        pieces = ["("]
        for child in group.children:
            if isinstance(child, NoteAtom):
                pieces.append(render_note(child, ctx, tie=id(child) in tie_after))
            else:
                pieces.extend(convert_nodes([child], ctx))
        return pieces
    pieces = ["("]
    for child in group.children:
        if isinstance(child, NoteAtom):
            pieces.append(render_note(child, ctx, tie=id(child) in tie_after))
        else:
            pieces.extend(convert_nodes([child], ctx))
    pieces.append(")")
    ctx.paren_balance -= 1
    return pieces


def convert_tuplet(group: GroupAtom, ctx: LineContext) -> list[str]:
    duration = nodes_duration(group.children)
    if duration <= 0:
        ctx.report.add(ctx.line, "多连音组时值为 0，未生成比例")
        return convert_nodes(group.children, ctx)
    inner = render_children(group.children, ctx)
    if any(isinstance(n, BarAtom) for n in group.children):
        ctx.report.add(
            ctx.line,
            "v2 的多连音组不能包含小节线，已去掉 (y) 包裹；请人工拆分并补回比例",
        )
        return [inner]

    target = Fraction(1)
    while target * 2 <= duration:
        target *= 2
    ratio = duration / target
    if ratio == 1:
        ctx.report.add(
            ctx.line,
            f"v1 多连音 (y ...) 的写成总时值为 {duration}，没有缩放，已去掉 (y) 包裹",
        )
        return [inner]
    n, d = ratio.numerator, ratio.denominator
    ctx.report.add(
        ctx.line,
        f"多连音比例 {n}:{d} 由 v1 自动算法推导（写成总时值 {duration}），请确认",
    )
    return [f"{n}:{d}{{" + inner + "}"]


def convert_block(block: BlockAtom, ctx: LineContext) -> str:
    tokens = tokenize(block.body)
    events: list = []
    for kind, tok in tokens:
        if kind == "bar":
            events.append(parse_bar(tok, ctx.line, ctx.report))
        elif kind == "block":
            ctx.report.add(ctx.line, "临时声部块内又出现块，已忽略内层块")
        else:
            events.extend(parse_note_token(tok, ctx.line, ctx.report))
    for idx, event in enumerate(events):
        if isinstance(event, BarAtom) and event.terminal:
            if any(not isinstance(x, CommandAtom) for x in events[idx + 1 :]):
                event.terminal = False
                ctx.report.add(
                    ctx.line,
                    "行中的终止线 `|]` 已按普通小节线处理；"
                    "请确认它是否只是跳房子/分段结束",
                )
    tree = build_tree(events, ctx.line, ctx.report)
    inner_ctx = LineContext(ctx.report, ctx.line, collect_slots=False)
    inner_pieces = convert_nodes(tree, inner_ctx)
    inner_ctx.finish(inner_pieces)
    if inner_ctx.paren_balance > 0:
        # 临时声部块内部不应跨块延续连音线；补闭合，避免影响主声部。
        inner_pieces.append(")" * inner_ctx.paren_balance)
        ctx.report.add(ctx.line, "临时声部块内有未闭合的连音线，已在块尾补上 `)`")
        inner_ctx.paren_balance = 0
    inner = " ".join(p for p in inner_pieces if p)

    if block.kind == "bz":
        options = "2, above, brace, small"
    elif block.kind == "dsb" or not block.kind:
        options = "2, above, brace"
    else:
        ctx.report.add(ctx.line, "未知的临时声部块类型，按 dsb 处理")
        options = "2, above, brace"
    if inner:
        return f"\\voice({options}){{ {inner} }}"
    return f"\\voice({options}){{}}"


def convert_voice_body(
    body: str, line: int, report: Report, carry_open: int = 0
) -> tuple[str, list[Slot], int]:
    events: list = []
    for kind, tok in tokenize(body):
        if kind == "bar":
            events.append(parse_bar(tok, line, report))
        elif kind == "block":
            events.append(parse_block(tok))
        else:
            events.extend(parse_note_token(tok, line, report))
    # `|]` 只有在行末（后面没有音符/小节/括号）才是终止线；
    # v1 也把 `|]` 当作跳房子闭合，所以行中的 `|]` 按普通小节线处理。
    for idx, event in enumerate(events):
        if isinstance(event, BarAtom) and event.terminal:
            if any(not isinstance(x, CommandAtom) for x in events[idx + 1 :]):
                event.terminal = False
                report.add(
                    line,
                    "行中的终止线 `|]` 已按普通小节线处理；"
                    "请确认它是否只是跳房子/分段结束",
                )
    tree = build_tree(events, line, report, carry_open=carry_open)
    ctx = LineContext(report, line, collect_slots=True)
    pieces = convert_nodes(tree, ctx)
    ctx.finish(pieces)
    return " ".join(p for p in pieces if p), ctx.slots, carry_open + ctx.paren_balance


# ---------------------------------------------------------------------------
# 歌词
# ---------------------------------------------------------------------------
def parse_v1_lyrics(text: str) -> list[str | None]:
    """按 v1 规则把歌词切成 token；None 表示 `@`。"""
    tokens: list[str | None] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t":
            i += 1
        elif c == "@":
            tokens.append(None)
            i += 1
        elif c in "{}":
            i += 1
        elif c in "-/":
            i += 1
        elif c == "~":
            i += 1
            if i < n:
                if tokens and tokens[-1] is not None:
                    # v1 的 ~ 是合字，v2 保留 ~ 让两个字仍然只占一个锚点
                    tokens[-1] += "~" + text[i]
                else:
                    tokens.append(text[i])
                i += 1
        elif c == '"':
            # 歌词中间的引号段在 v1 中也是注释，不占槽位。
            j = text.find('"', i + 1)
            i = (j + 1) if j >= 0 else n
        elif c.isascii() and c.isalpha():
            j = i
            while j < n and text[j].isascii() and (text[j].isalpha() or text[j] in "'’"):
                j += 1
            tokens.append(text[i:j])
            i = j
        elif is_punct(c):
            if tokens and tokens[-1] is not None:
                tokens[-1] += c
            else:
                tokens.append(c)
            i += 1
        else:
            tokens.append(c)
            i += 1
    return tokens


def convert_lyrics(text: str, slots: Sequence[Slot], line: int, report: Report) -> str:
    original = text
    i = 0
    n = len(text)
    tokens: list[str | None] = []
    while i < n and text[i] in " \t@":
        if text[i] == "@":
            tokens.append(None)
        i += 1

    annotation = ""
    if i < n and text[i] == '"':
        j = text.find('"', i + 1)
        if j < 0:
            j = n
        annotation = text[i + 1:j].replace("_", " ")
        i = j + 1
    tokens.extend(parse_v1_lyrics(text[i:]))

    if annotation:
        result = [f"\\text({quote(annotation)})"]
    else:
        result = []

    if not slots:
        report.add(line, "歌词前没有可对应的曲谱行，`@` 已按 `_` 保留，请人工检查")
        for token in tokens:
            if token is None:
                result.append("_")
            else:
                result.append(escape_lyric_atom(token))
        return " ".join(result)

    if len(tokens) > len(slots):
        report.add(
            line,
            f"歌词音节数（{len(tokens)}）多于 v1 音符槽位（{len(slots)}），多余音节请人工处理",
        )

    for index, token in enumerate(tokens):
        slot = slots[index] if index < len(slots) else None
        if token is None:
            if slot is not None and slot.anchor:
                result.append("_")
            # 休止、隐藏休止、延音线后续音符：v2 自动跳过，不需要占位
        else:
            if slot is not None and not slot.anchor:
                report.add(
                    line,
                    f"第 {index + 1} 个歌词音节落在休止/延音线后续/隐藏音符上，"
                    "已删除该音节以保持后续对齐；请人工检查",
                )
                continue
            result.append(escape_lyric_atom(token))

    if "/" in original and re.search(r"[A-Za-z]/[A-Za-z]", original):
        report.add(line, "英文歌词中的 `/` 已转成空格，请确认是否需要改成 `-` 作音节分隔")
    return " ".join(result)


# ---------------------------------------------------------------------------
# 描述头
# ---------------------------------------------------------------------------
@dataclass
class HeaderState:
    titles: list[str] = field(default_factory=list)
    voices: dict[int, str] = field(default_factory=dict)
    key: str = ""
    key_letter: str = ""
    time: str = ""
    tempo_num: str = ""
    time_denominator: int = 0
    aux_time: str = ""
    has_version: bool = False


def split_author(value: str) -> list[tuple[str, str]]:
    """把 v1 的 Z 行拆成 (v2 键, 值)。"""
    text = value.strip()
    if not text:
        return [("credit", value)]
    m = re.match(r"^(词曲)\s*[:：]?\s*(.*)$", text)
    if m:
        return [("lyricist", m.group(2)), ("composer", m.group(2))]
    if "词曲" in text:
        name = text.replace("词曲", "").strip(" :：")
        return [("lyricist", name), ("composer", name)]
    m = re.match(r"^(作词|词)\s*[:：]?\s*(.*)$", text)
    if m and m.group(2).strip():
        return [("lyricist", m.group(2).strip())]
    m = re.search(r"\s+词$", text)
    if m:
        return [("lyricist", text[:m.start()].strip())]
    m = re.match(r"^(作曲|曲)\s*[:：]?\s*(.*)$", text)
    if m and m.group(2).strip():
        return [("composer", m.group(2).strip())]
    m = re.search(r"\s+曲$", text)
    if m:
        return [("composer", text[:m.start()].strip())]
    m = re.match(r"^(编曲|改编|编合唱)\s*[:：]?\s*(.*)$", text)
    if m and m.group(2).strip():
        return [("arranger", m.group(2).strip())]
    m = re.search(r"\s+(编曲|改编|编合唱)$", text)
    if m:
        return [("arranger", text[:m.start()].strip())]
    m = re.match(r"^(演唱|演奏)\s*[:：]?\s*(.*)$", text)
    if m and m.group(2).strip():
        return [("artist", m.group(2).strip())]
    m = re.search(r"\s+(演唱|演奏)$", text)
    if m:
        return [("artist", text[:m.start()].strip())]
    return [("credit", text)]


def convert_key(value: str, report: Report, line: int, state: HeaderState) -> list[str]:
    val = value.strip()
    m = re.fullmatch(r"([#$b]?)([A-Ga-g])([#$b]?)", val)
    if not m:
        report.add(line, f"无法解析 v1 调号 `{value}`，请人工填写 @key")
        return []
    acc_before, letter, acc_after = m.groups()
    acc = acc_before or acc_after
    v2acc = "#" if acc == "#" else "b" if acc in ("$", "b") else ""
    state.key_letter = letter.upper()
    state.key = f"1={v2acc}{letter.upper()}"
    report.add(
        line,
        f"v1 不区分大小调；已按大调输出 @key {state.key}，若为小调请改为 @key 6={v2acc}{letter.upper()}",
    )
    return [f"@key {state.key}"]


def convert_time(value: str, report: Report, line: int, state: HeaderState) -> list[str]:
    val = value.strip()
    m = re.match(r"^(\d+\s*/\s*\d+)\s*(.*)$", val)
    if not m:
        report.add(line, f"无法解析 v1 拍号 `{value}`，请人工填写 @time")
        return []
    main = m.group(1).replace(" ", "")
    rest = m.group(2).strip()
    state.time = main
    try:
        state.time_denominator = int(main.split("/")[1])
    except (IndexError, ValueError):
        state.time_denominator = 0
    if rest:
        aux = rest.strip().strip("()").strip()
        state.aux_time = aux
        report.add(
            line,
            f"v1 只列出了辅助拍号 `{aux}` 而没有位置；转成 @time {main} 后，请用 \\time(...) 放到正确的小节线",
        )
    return [f"@time {main}"]


def convert_tempo(value: str, report: Report, line: int, state: HeaderState) -> list[str]:
    val = value.strip()
    m = re.match(r"^(\d+)\s*(.*)$", val)
    if m:
        bpm, rest = m.group(1), m.group(2).strip()
        state.tempo_num = bpm
        beat = 4
        result = f"@tempo {beat}={bpm}"
        if rest:
            result += " " + quote(rest)
        if state.time_denominator == 8:
            report.add(line, f"v1 的 J: 没有说明拍值单位；已按 4={bpm} 输出，6/8 曲目可能需要 8={bpm}")
        return [result]
    if val:
        return [f"@tempo {quote(val)}"]
    report.add(line, "J: 为空，已忽略")
    return []


def convert_header_line(key: str, value: str, state: HeaderState, report: Report, line: int) -> list[str]:
    value = value.strip()
    if key == "V":
        state.has_version = True
        return []
    if key == "B":
        state.titles.append(value)
        if len(state.titles) == 1:
            return [f"@title {header_value(value)}"]
        return [f"@subtitle {header_value(value)}"]
    if key == "Z":
        return [f"@{kind} {header_value(val)}" for kind, val in split_author(value)]
    if key == "D":
        return convert_key(value, report, line, state)
    if key == "P":
        return convert_time(value, report, line, state)
    if key == "J":
        return convert_tempo(value, report, line, state)
    if key == "X":
        return [f"@number {header_value(value)}"]
    if key == "H":
        return [f"@layout.page {value}"]
    if key == "S":
        return [f"@source {header_value(value)}"]
    report.add(line, f"未知的描述头键 {key}:，已忽略")
    return []


# ---------------------------------------------------------------------------
# 文件转换
# ---------------------------------------------------------------------------
Q_RE = re.compile(r'^\s*Q(\d*)\s*(?:"([^"]*)")?\s*:\s*(.*)$')
C_RE = re.compile(r"^\s*C(\d*)\s*:\s*(.*)$")
H_RE = re.compile(r"^\s*([VBZDPJXH])\s*:\s*(.*)$")
S_RE = re.compile(r"^\s*S\s*:\s*(.*)$")


def detect_version(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "%")):
            continue
        if stripped.startswith("@version"):
            parts = stripped.split(None, 1)
            return parts[1].strip() if len(parts) > 1 else ""
        if stripped.startswith("@"):
            # 已经是 v2 的描述头；没有 @version 时也按 v2 处理，避免破坏文件。
            return "2.0"
        m = re.match(r"^V\s*:\s*(\S+)", stripped)
        if m:
            return m.group(1)
        if re.match(r"^[QC]\d*\"?", stripped):
            return "1.0"
        return ""
    return ""


def convert_text(text: str, source_name: str = "<stdin>") -> tuple[str, Report]:
    report = Report()
    state = HeaderState()
    lines = text.splitlines()

    header_lines: list[str] = []
    body_lines: list[str] = []
    in_header = True
    header_done = False

    # 第一遍：描述头
    body_start = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not in_header:
            body_start = index
            break
        if not stripped:
            if header_done:
                in_header = False
                body_start = index
                break
            header_lines.append("")
            continue
        if stripped.startswith("#"):
            header_lines.append("%" + line.strip()[1:])
            header_done = True
            continue
        m = H_RE.match(line)
        if m:
            header_lines.extend(convert_header_line(m.group(1), m.group(2), state, report, index + 1))
            header_done = True
            continue
        m = S_RE.match(line)
        if m:
            header_lines.extend(convert_header_line("S", m.group(1), state, report, index + 1))
            header_done = True
            continue
        in_header = False
        body_start = index
        break

    # 第二遍：正文
    header_report_count = len(report)
    system_ids: list[int] = []
    q_infos: dict[int, tuple[int, list[Slot]]] = {}
    last_q: tuple[int, list[Slot]] | None = None
    output_body: list[str] = []
    voice_carry: dict[int, int] = {}
    q_indices = [
        i for i in range(body_start, len(lines)) if Q_RE.match(lines[i])
    ]

    def add_line_todos(before: int) -> None:
        for _, message in report.since(before):
            output_body.append(f"% TODO: {message}")

    for index in range(body_start, len(lines)):
        line = lines[index]
        line_no = index + 1
        stripped = line.strip()

        if not stripped:
            output_body.append("")
            system_ids = []
            q_infos = {}
            last_q = None
            continue

        if stripped.startswith("#"):
            output_body.append("%" + line.strip()[1:])
            continue

        if stripped == "[fenye]":
            # 分页是行级命令，必须挂在系统最后一个声部行末尾；不能挂到歌词行。
            target = -1
            for i in range(len(output_body) - 1, -1, -1):
                if output_body[i].startswith("V"):
                    target = i
                    break
            if target >= 0:
                output_body[target] = output_body[target].rstrip() + " \\pagebreak"
            else:
                report.add(line_no, "v1 的 [fenye] 前没有声部行，已单独输出 \\pagebreak，请人工放置")
                output_body.append("\\pagebreak")
            continue

        m = Q_RE.match(line)
        if m:
            qid = int(m.group(1) or "1")
            name = m.group(2)
            body = m.group(3).strip()
            if qid in system_ids:
                system_ids = []
                q_infos = {}
                last_q = None
            if name and qid not in state.voices:
                state.voices[qid] = name
            before = len(report)
            carry_before = voice_carry.get(qid, 0)
            converted, slots, carry_after = convert_voice_body(
                body, line_no, report, carry_open=carry_before
            )
            voice_carry[qid] = carry_after
            output_body.append(f"V{qid}: {converted}".rstrip())
            if q_indices and index != q_indices[-1] and re.search(
                r"\|]\s*(?:\\[A-Za-z][^\s]*\s*)*$", converted
            ):
                report.add(
                    line_no,
                    "行末的终止线 `|]` 后面还有声部行，请确认它是否只是跳房子/分段结束",
                )
            add_line_todos(before)
            info = (qid, slots)
            system_ids.append(qid)
            q_infos[qid] = info
            last_q = info
            continue

        m = C_RE.match(line)
        if m:
            before = len(report)
            explicit = m.group(1)
            body = m.group(2).strip()
            target = None
            attach_id = 1
            if explicit:
                attach_id = int(explicit)
                target = q_infos.get(attach_id)
                if target is None and last_q is not None:
                    # 有些 v1 曲谱用 C1/C2/C3... 表示同一曲调的多段歌词，
                    # 并不一定有对应的 Q2/Q3。此时退回当前系统最后一个声部。
                    attach_id, _ = last_q
                    target = last_q
                    report.add(
                        line_no,
                        f"v1 的 C{explicit} 没有对应的 Q{explicit}，"
                        f"已按当前声部 {attach_id} 的多行歌词处理；请人工检查",
                    )
            elif last_q is not None:
                # v1 的 C 行依附于“上一行 Q”，也就是当前系统最近出现的声部。
                attach_id, _ = last_q
                target = last_q
            slots = target[1] if target else []
            converted = convert_lyrics(body, slots, line_no, report)
            if explicit or len(system_ids) > 1:
                output_body.append(f"L({attach_id}): {converted}".rstrip())
            else:
                output_body.append(f"L: {converted}".rstrip())
            add_line_todos(before)
            continue

        # 其他行：不认识的 v1 内容不静默丢弃
        report.add(line_no, f"无法识别的行，已保留为注释：{stripped}")
        output_body.append("% " + line)

    # 组装输出。每个声部独立收尾：如果最后仍有未闭合的连音线，补 `)`。
    for qid, carry in sorted(voice_carry.items()):
        if carry <= 0:
            continue
        last_voice = -1
        prefix = f"V{qid}:"
        for i, out_line in enumerate(output_body):
            if out_line.startswith(prefix):
                last_voice = i
        message = (
            f"声部 {qid} 在文件结尾仍有 {carry} 个未闭合的连音线，"
            "已在最后一行补上 `)`；请人工确认"
        )
        report.add(len(lines), message)
        if last_voice >= 0:
            line = output_body[last_voice].rstrip()
            closer = ")" * carry
            # 终止线必须留在行末；需要补的连音线右括号放在它前面。
            pos = line.rfind("|]")
            if pos >= 0 and re.fullmatch(
                r"\s*(?:\\[A-Za-z][^\s]*\s*)*", line[pos + 2 :]
            ):
                line = line[:pos] + closer + " " + line[pos:]
            else:
                line += " " + closer
            output_body[last_voice] = line
            output_body.insert(last_voice + 1, f"% TODO: {message}")
        else:
            output_body.append(f"% TODO: {message}")

    header_text = "\n".join(header_lines).strip("\n")
    body_text = "\n".join(output_body).strip("\n")

    result: list[str] = ["@version 2.0"]
    result.append("% 由 bin/jps1to2.py 从 v1.0 自动转换；TODO 表示需要人工确认。")
    if header_text:
        result.append(header_text)
    if state.voices:
        for vid in sorted(state.voices):
            name = state.voices[vid]
            result.append(f'@voice {vid} name={quote(name)}')
    # 描述头阶段的 TODO 放在正文前
    header_todos = [m for _, m in report.entries[:header_report_count]]
    for message in header_todos:
        result.append(f"% TODO: {message}")
    if body_text:
        result.append("")
        result.append(body_text)
    return "\n".join(result).rstrip() + "\n", report


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def report_text(source: Path | str, output: Path | str | None, report: Report) -> str:
    lines = [
        "jps1to2 conversion report",
        f"source: {source}",
        f"output: {output if output is not None else '<stdout>'}",
        f"warnings: {len(report)}",
        "",
    ]
    for line, message in report.entries:
        lines.append(f"line {line}: {message}")
    if not report.entries:
        lines.append("(no warnings)")
    return "\n".join(lines) + "\n"


def resolve_outputs(
    inputs: list[Path],
    out: Path | None,
    report_path: Path | None,
    out_is_dir: bool = False,
) -> list[tuple[Path, Path, Path | None]]:
    if out is not None:
        if len(inputs) > 1 or out_is_dir:
            if out.exists() and not out.is_dir():
                raise SystemExit(f"输出目录路径已存在且不是目录：{out}")
            out.mkdir(parents=True, exist_ok=True)
            pairs = [(src, out / (src.stem + ".v2.jps")) for src in inputs]
        else:
            if out.exists() and out.is_dir():
                pairs = [(inputs[0], out / (inputs[0].stem + ".v2.jps"))]
            else:
                pairs = [(inputs[0], out)]
    else:
        pairs = [(src, src.with_name(src.stem + ".v2.jps")) for src in inputs]

    result: list[tuple[Path, Path, Path | None]] = []
    for src, dst in pairs:
        if report_path is not None:
            if len(inputs) > 1:
                raise SystemExit("--report 只能和单个输入一起使用")
            rpt: Path | None = report_path
        else:
            rpt = dst.with_name(dst.name[: -len(dst.suffix)] + ".report.txt") if dst.suffix else dst.with_name(dst.name + ".report.txt")
        result.append((src, dst, rpt))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jps1to2",
        description="把 v1.0 番茄简谱脚本 (.jps) 转换为 v2.0 草案格式",
    )
    parser.add_argument("inputs", nargs="+", metavar="FILE", help="v1 .jps 文件或目录")
    parser.add_argument("-o", "--output", metavar="PATH", help="输出文件或目录（默认：<name>.v2.jps）")
    parser.add_argument("--report", metavar="FILE", help="报告文件（仅单个输入）")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件，只在终端显示摘要")
    parser.add_argument("--stdout", action="store_true", help="把转换结果写到标准输出（不写文件）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    parser.add_argument("-q", "--quiet", action="store_true", help="不显示转换摘要")
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    args = parser.parse_args(argv)

    paths: list[Path] = []
    for raw in args.inputs:
        path = Path(raw)
        if path.is_dir():
            paths.extend(
                sorted(
                    p
                    for p in path.iterdir()
                    if p.is_file()
                    and p.suffix == ".jps"
                    and not p.name.endswith(".v2.jps")
                )
            )
        else:
            paths.append(path)
    if not paths:
        print("没有找到 .jps 文件", file=sys.stderr)
        return 2

    if args.stdout and len(paths) > 1:
        print("--stdout 只能和单个输入一起使用", file=sys.stderr)
        return 2

    out_arg = args.output
    out_is_dir = bool(out_arg and out_arg.endswith(("/", "\\")))
    targets = (
        []
        if args.stdout
        else resolve_outputs(
            paths,
            Path(out_arg) if out_arg else None,
            Path(args.report) if args.report else None,
            out_is_dir=out_is_dir,
        )
    )
    if not args.stdout:
        for src, dst, rpt in targets:
            if dst.exists() and not args.force:
                print(f"输出文件已存在（使用 --force 覆盖）：{dst}", file=sys.stderr)
                return 1

    exit_code = 0
    for pos, src in enumerate(paths):
        try:
            text = src.read_text(encoding="utf-8-sig")
        except OSError as exc:
            print(f"读取失败 {src}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        version = detect_version(text)
        if version.startswith("2"):
            print(f"跳过已经是 v2 的文件：{src}", file=sys.stderr)
            exit_code = 1
            continue

        converted, report = convert_text(text, str(src))
        if args.stdout:
            sys.stdout.write(converted)
            if not args.quiet:
                print(report_text(src, None, report), file=sys.stderr)
            continue

        _, dst, rpt = targets[pos]
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(converted, encoding="utf-8")
            if not args.no_report and rpt is not None:
                rpt.write_text(report_text(src, dst, report), encoding="utf-8")
        except OSError as exc:
            print(f"写入失败 {dst}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        if not args.quiet:
            extra = f"，报告：{rpt}" if (not args.no_report and rpt is not None) else ""
            print(f"{src} -> {dst}（{len(report)} 条待确认{extra}）")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
