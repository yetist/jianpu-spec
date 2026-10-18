#! /usr/bin/env python3
# -*- coding: utf-8 -*-
# FileName: jps2svg.py
#
# 将「番茄简谱脚本」(.jps) 渲染为 SVG 简谱图片。
#
# 这是一个基于 svgwrite 的纯本地实现，不依赖任何 web 接口。
# 用法:
#     jps2svg.py song.jps            # 生成 song.svg
#     jps2svg.py -o out.svg song.jps
#     jps2svg.py -f png song.jps     # 通过 rsvg-convert 转成 png
#
# 本程序是自由的：您可以按照 GNU General Public License 的条款重新分发
# 和/或修改它。

"""
简谱脚本(.jps) -> SVG 渲染器。

实现了《番茄简谱脚本 v1.0 说明手册》中的绝大部分记谱语法：

* 描述头: V / B / Z / D / P / J / X / H
* 曲: Q[编号]["声部名"]:，词: C[编号]:，注释行(#)
* 基本音符 1-7、休止符 0、隐藏休止符 8、节奏音符 9、增时线 -
* 高低音 (,)(')、减时线(时值线) /、附点 .、升降还原 # $ =
* 小节线 |  ||  |:  :|  :|:  ||/  隐藏小节线 |/  |*
* 连音线 () 、多连音 (y ... ) 、渐强渐弱 < > ! 、渐强渐弱位置调整 +
* 装饰音(倚音) [..] 前倚音 / [h..] 后倚音
* 跳房子 [..] (含 [/ 不封闭、[+ 位置调整)、临时拍号 |"p:x/y"
* 伴奏括弧 &zkh &ykh、常用符号 &xx(力度/记号等)、音符注释 "..."
* 多声部 Q1/Q2...，声部名、&sbf、临时多声部 {dsb..} / 临时伴奏 {bz..}
* 歌词: 一字一音、@ 跳过、~ 连字、标点自动、歌词注释 ".."、英文 / 分词
* 分页 [fenye]

为了保持实现可读，少量高级效果做了近似处理（例如声部间的节拍对齐按
小节对齐、跨行连音线不自动跨页等），这些在代码中以 NOTE 标注。
"""

import argparse
import os
import re
import sys
import unicodedata

try:
    import svgwrite
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 svgwrite 模块: pip install svgwrite\n")
    raise


# --------------------------------------------------------------------------
# 基本常量 (单位: px, 依据 A4 = 210x297mm, 96dpi)
# --------------------------------------------------------------------------
MM = 96.0 / 25.4
PAGE_SIZES = {
    "A4": (210 * MM, 297 * MM),      # 793.7 x 1122.5
    "A3": (297 * MM, 420 * MM),
    "B5": (176 * MM, 250 * MM),
    "Letter": (8.5 * 96, 11 * 96),
}

FONT_SANS = "'Microsoft YaHei','Noto Sans CJK SC','PingFang SC',sans-serif"
FONT_NOTE = "Georgia,'Times New Roman','Songti SC',serif"

NOTE_SIZE = 28          # 音符数字字号
CELL = 36               # 单个音符的参考宽度
GS = 0.36               # 倚音占基本音符宽度的比例
BAR_RATIO = 0.5         # 小节线占基本音符宽度的比例
LYRIC_SIZE = 18         # 歌词字号
LYRIC_LINE_H = 24       # 歌词行间距
SMALL = 15              # 倚音/注释等小字号
DOT_R = 2.3             # 八度点半径
UL_GAP = 8              # 减时线行间距
MIN_CELL = 14           # 单个音符最小宽度


def is_punct(ch):
    return unicodedata.category(ch).startswith("P")


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
class NoteEl(object):
    """一个音符元素 (含休止符 / 增时线 / 倚音 / 各类记号)。"""

    def __init__(self):
        self.pitch = None          # '0'-'9' 或 '-' (增时线)
        self.octave = 0            # >0 高音, <0 低音
        self.dots = 0              # 附点个数
        self.slashes = 0           # 减时线个数
        self.acc = None            # '#','$','='
        self.marks = []            # &xxx 记号
        self.grace_front = []      # 前倚音字符串列表
        self.grace_back = []       # 后倚音字符串列表
        self.text = ""             # 音符注释
        self.cresc = None          # (方向, +号个数)
        self.cresc_end = False
        self.opens = 0             # 连音线开始数
        self.closes = 0            # 连音线结束数
        self.multi = False         # 多连音
        self.beat_up = False       # ^ 强制切分
        self.beat_join = False     # ~ 并入同一拍

    @property
    def is_rest(self):
        return self.pitch in ("0", "8")

    @property
    def is_hold(self):
        return self.pitch == "-"

    @property
    def is_lyric_slot(self):
        # 增时线不占歌词位, 其余音符/休止符占一个歌词位
        return self.pitch is not None and not self.is_hold

    @property
    def dynamics(self):
        return [m for m in self.marks if m in ("pp", "p", "mp", "mf",
                                               "f", "ff", "fff", "sf",
                                               "fp", "sfp")]

    @property
    def articulations(self):
        return [m for m in self.marks if m not in self.dynamics]


class BarEl(object):
    """小节线 / 跳房子 / 反复 / 临时拍号 等结构符号。"""

    def __init__(self):
        self.lines = 0             # 竖线数量
        self.left_rep = False      # 左反复 |:
        self.right_rep = False     # 右反复 :|
        self.hide = False          # 隐藏小节线 (|/ 或 |*)
        self.hide_space = False    # 隐藏但占位 (|*)
        self.pai = None            # 临时拍号 "p:x/y"
        self.tags = []             # &dc &ds &fine &ty &hs
        self.house_open = False    # 跳房子起点
        self.house_close = False   # 跳房子终点
        self.house_unclosed = False  # 跳房子右侧不封闭
        self.house_text = ""       # 跳房子文字
        self.plus = 0              # 跳房子位置调整 +
        self.sbf = False           # &sbf 声部括弧起始
        self.opens = 0             # 小节线处的连音线开始
        self.closes = 0            # 小节线处的连音线结束

    @property
    def width(self):
        return BAR_RATIO


class QLine(object):
    def __init__(self, voice=1, name=""):
        self.voice = voice
        self.name = name
        self.elements = []         # NoteEl / BarEl 交替

    @property
    def measures(self):
        """按小节切分, 返回 [(notes, bar), ...]。"""
        result = []
        notes = []
        for el in self.elements:
            if isinstance(el, BarEl):
                result.append((notes, el))
                notes = []
            else:
                notes.append(el)
        if notes:
            result.append((notes, None))
        return result


class Header(object):
    def __init__(self):
        self.version = ""
        self.titles = []
        self.authors = []
        self.key = ""
        self.key_acc = ""
        self.pai = ""
        self.pai_aux = ""
        self.tempo = ""
        self.tempo_num = ""
        self.number = ""
        self.page = "A4"


class LyricLine(object):
    """一行歌词, 解析为与音符槽位一一对应的文字列表。"""

    def __init__(self, voice, text):
        self.voice = voice
        self.raw = text
        self.notes = []
        self.annotation = ""
        self.parse()

    def parse(self):
        text = self.raw
        out = []
        i = 0
        n = len(text)
        m = re.match(r'^\s*"([^"]*)"', text)
        if m:
            self.annotation = m.group(1).replace("_", " ")
            i = m.end()
        while i < n:
            c = text[i]
            if c in " \t":
                i += 1
                continue
            if c == '"':
                j = text.find('"', i + 1)
                i = (j + 1) if j >= 0 else n
                continue
            if c == "@":
                out.append("")
                i += 1
                continue
            if c in "{}":
                i += 1
                continue
            if c == "-":
                i += 1
                continue
            if c == "~":
                if out and i + 1 < n:
                    out[-1] += text[i + 1]
                    i += 2
                else:
                    i += 1
                continue
            if c == "/":
                i += 1
                continue
            if c.isascii() and c.isalpha():
                j = i
                while j < n and text[j].isascii() and text[j].isalpha():
                    j += 1
                out.append(text[i:j])
                i = j
                continue
            if is_punct(c):
                if out:
                    out[-1] += c
                else:
                    out.append(c)
                i += 1
                continue
            out.append(c)
            i += 1
        self.notes = out


# --------------------------------------------------------------------------
# 词法分析: 把 Q 行切成 token
# --------------------------------------------------------------------------
def _is_house_start(s, i):
    j = i + 1
    n = len(s)
    while j < n and s[j] in "+/":
        j += 1
    return j < n and s[j] == '"'


def _is_bar_paren(s, i):
    j = i
    n = len(s)
    while j < n and (s[j] == "(" or s[j] == " "):
        j += 1
    return j < n and s[j] in "|:"


def _scan_bar(s, i):
    n = len(s)
    j = i
    buf = ""
    while j < n:
        c = s[j]
        if c in "|:":
            buf += c
            j += 1
            continue
        if c == "/":
            if buf and buf[-1] in "|]":
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
                j += 1
                continue
            break
        if c == '"':
            k = s.find('"', j + 1)
            if k < 0:
                k = n - 1
            buf += s[j:k + 1]
            j = k + 1
            continue
        if c == "(" and not buf:
            buf += "("
            j += 1
            continue
        break
    return buf, j


def tokenize(s):
    """返回 [(kind, text), ...], kind 为 'note' / 'bar' / 'block'。"""
    tokens = []
    cur = ""
    cur_has = False
    i = 0
    n = len(s)
    quote = False
    grace = 0

    def flush():
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
        if c == "(" and _is_bar_paren(s, i):
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
            if cur_has and c != "~":
                flush()
            cur_has = True
            cur += c
            i += 1
            continue
        cur += c
        i += 1
    flush()
    return tokens


# --------------------------------------------------------------------------
# 语法分析
# --------------------------------------------------------------------------
def parse_note(tok):
    el = NoteEl()
    i = 0
    n = len(tok)
    while i < n:
        c = tok[i]
        if c == "(":
            el.opens += 1
            if i + 1 < n and tok[i + 1] == "y":
                el.multi = True
                i += 1
        elif c == ")":
            el.closes += 1
        elif c == "[":
            j = tok.find("]", i)
            if j < 0:
                j = n - 1
            grp = tok[i:j + 1]
            if grp[1:2] == "h":
                el.grace_back.append(grp)
            else:
                el.grace_front.append(grp)
            i = j
        elif c == '"':
            j = tok.find('"', i + 1)
            if j < 0:
                j = n
            el.text = tok[i + 1:j]
            i = j
        elif c == "&":
            k = i + 1
            while k < n and tok[k].isalnum():
                k += 1
            el.marks.append(tok[i + 1:k])
            i = k - 1
        elif c in "<>":
            k = i + 1
            while k < n and tok[k] == "+":
                k += 1
            el.cresc = (c, k - (i + 1))
            i = k - 1
        elif c == "!":
            el.cresc_end = True
        elif c == "/":
            el.slashes += 1
        elif c == ".":
            el.dots += 1
        elif c == ",":
            el.octave -= 1
        elif c == "'":
            el.octave += 1
        elif c in "#$=":
            el.acc = c
        elif c == "~":
            el.beat_join = True
        elif c == "^":
            el.beat_up = True
        elif c.isdigit() or c == "-":
            if el.pitch is None:
                el.pitch = c
        i += 1
    return el


def parse_bar(tok):
    el = BarEl()
    i = 0
    n = len(tok)
    last_bracket = False
    while i < n:
        c = tok[i]
        if c == "(":
            el.opens += 1
        elif c == ")":
            el.closes += 1
        elif c == "|":
            el.lines += 1
        elif c == ":":
            if el.lines > 0:
                el.right_rep = True
            else:
                el.left_rep = True
        elif c == "/":
            if last_bracket:
                el.house_unclosed = True
            elif el.lines == 1:
                el.hide = True
        elif c == "*":
            el.hide = True
            el.hide_space = True
        elif c == "&":
            k = i + 1
            while k < n and tok[k].isalpha():
                k += 1
            tag = tok[i + 1:k]
            if tag == "sbf":
                el.sbf = True
            elif tag:
                el.tags.append(tag)
            i = k - 1
        elif c == "[":
            el.house_open = True
        elif c == "]":
            el.house_close = True
            last_bracket = True
        elif c == "+":
            el.plus += 1
        elif c == '"':
            k = tok.find('"', i + 1)
            if k < 0:
                k = n
            content = tok[i + 1:k]
            if content.startswith("p:"):
                el.pai = content[2:]
            else:
                el.house_text = content
            i = k
        i += 1
    return el


def parse_key(val):
    acc = ""
    letter = ""
    for ch in val:
        if ch in "#$":
            acc = ch
        elif ch.isalpha():
            letter = ch
    return letter.upper(), acc


def parse_pai(val):
    val = val.strip()
    m = re.match(r"^(\d+\s*/\s*\d+)\s*(.*)$", val)
    if not m:
        return val, ""
    main = m.group(1).replace(" ", "")
    rest = m.group(2).strip().strip("()").strip()
    return main, rest


def parse_block(tok):
    """解析 {bz ...} / {dsb ...} 临时伴奏/多声部块。"""
    body = tok[1:-1] if tok.endswith("}") else tok[1:]
    body = body.strip()
    for prefix in ("bz", "dsb"):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    els = []
    for kind, t in tokenize(body):
        if kind == "bar":
            els.append(parse_bar(t))
        elif kind == "note":
            el = parse_note(t)
            if el.pitch is not None:
                els.append(el)
    return els


def parse_jps(text):
    """解析脚本, 返回 (Header, items)。

    items 为列表, 每项是 ('system', [QLine...], [LyricLine...]) 或 ('pagebreak',)。
    """
    header = Header()
    items = []
    qgroup = []
    lyrics = []

    def flush_group():
        nonlocal qgroup, lyrics
        if qgroup:
            items.append(("system", qgroup, lyrics))
        qgroup = []
        lyrics = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[fenye]":
            flush_group()
            items.append(("pagebreak",))
            continue
        m = re.match(r'^(Q|C)(\d*)("([^"]*)")?\s*:(.*)$', line)
        if m:
            kind = m.group(1)
            voice = int(m.group(2)) if m.group(2) else 1
            name = m.group(4) or ""
            body = m.group(5)
            if kind == "Q":
                if lyrics:            # 上一组已经带歌词, 开始新的 system
                    flush_group()
                if qgroup:            # 声部编号回退 => 新的一组
                    maxv = max(v.voice for v in qgroup)
                    if voice <= maxv:
                        flush_group()
                q = QLine(voice, name)
                for kind2, tok in tokenize(body):
                    if kind2 == "bar":
                        q.elements.append(parse_bar(tok))
                    elif kind2 == "block":
                        q.elements.extend(parse_block(tok))
                    else:
                        el = parse_note(tok)
                        if el.pitch is None and not (
                                el.opens or el.closes):
                            continue
                        q.elements.append(el)
                qgroup.append(q)
            else:
                lyrics.append(LyricLine(voice, body))
            continue
        m2 = re.match(r"^([VBZDPJXH])\s*:\s*(.*)$", line)
        if m2:
            key, val = m2.group(1), m2.group(2).strip()
            if key == "V":
                header.version = val
            elif key == "B":
                header.titles.append(val)
            elif key == "Z":
                header.authors.append(val)
            elif key == "D":
                header.key, header.key_acc = parse_key(val)
            elif key == "P":
                header.pai, header.pai_aux = parse_pai(val)
            elif key == "J":
                header.tempo = val
                if re.match(r"^\d+$", val):
                    header.tempo_num = val
            elif key == "X":
                header.number = val
            elif key == "H":
                header.page = val
    flush_group()
    return header, items


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
class Renderer(object):
    def __init__(self, header):
        self.header = header
        self.page_w, self.page_h = PAGE_SIZES.get(
            header.page, PAGE_SIZES["A4"])
        self.margin = 80
        self.content_w = self.page_w - 2 * self.margin
        self.pages = []
        self.page_index = -1
        self._outfile = "out.svg"

    # ---- 底层绘制辅助 -------------------------------------------------
    @staticmethod
    def text(dwg, s, x, y, size=NOTE_SIZE, anchor="middle",
             family=FONT_NOTE, fill="#101010", weight="normal",
             style="normal"):
        dwg.add(dwg.text(s, insert=(x, y), font_size="%.1fpx" % size,
                         font_family=family, text_anchor=anchor,
                         fill=fill, font_weight=weight, font_style=style))

    @staticmethod
    def line(dwg, x1, y1, x2, y2, width=1.4):
        dwg.add(dwg.line(start=(x1, y1), end=(x2, y2),
                         stroke="#101010", stroke_width=width,
                         stroke_linecap="round"))

    @staticmethod
    def dot(dwg, x, y, r=DOT_R):
        dwg.add(dwg.circle(center=(x, y), r=r, fill="#101010"))

    # ---- 主入口 -------------------------------------------------------
    def render(self, items, outfile):
        self._outfile = outfile
        self._new_page()
        self.y = self.margin
        self._render_header()
        for item in items:
            if item[0] == "pagebreak":
                self._new_page()
                self.y = self.margin
            else:
                self._render_system(item[1], item[2])
        return self.pages

    def _new_page(self):
        self.page_index += 1
        dwg = svgwrite.Drawing(size=("%.2fpx" % self.page_w,
                                     "%.2fpx" % self.page_h))
        dwg.add(dwg.rect(insert=(0, 0), size=("100%", "100%"),
                         fill="#ffffff"))
        self.pages.append(dwg)
        self.dwg = dwg
        return dwg

    def _save_page(self, index):
        base, ext = os.path.splitext(self._outfile)
        if index == 0:
            name = self._outfile
        else:
            name = "%s-%d%s" % (base, index, ext)
        self.pages[index].saveas(name)
        return name

    # ---- 描述头 -------------------------------------------------------
    def _render_header(self):
        dwg = self.dwg
        margin = self.margin
        y = self.y + 24
        for i, title in enumerate(self.header.titles):
            size = 36 if i == 0 else 20
            self.text(dwg, title, self.page_w / 2, y, size=size,
                      family=FONT_SANS, weight="bold")
            y += 40 if i == 0 else 28
        ay = self.y + 34
        for author in self.header.authors:
            self.text(dwg, author, self.page_w - margin, ay, size=16,
                      anchor="end", family=FONT_SANS)
            ay += 22
        y += 8
        if self.header.key:
            acc = {"#": "\u266f", "$": "\u266d"}.get(self.header.key_acc, "")
            self.text(dwg, "1=%s%s" % (acc, self.header.key), margin, y + 18,
                      size=20, anchor="start", family=FONT_NOTE, weight="bold")
        if self.header.pai:
            parts = self.header.pai.split("/")
            if len(parts) == 2:
                px = margin + 104
                self.text(dwg, parts[0], px, y + 10, size=17,
                          family=FONT_NOTE, weight="bold")
                self.text(dwg, parts[1], px, y + 30, size=17,
                          family=FONT_NOTE, weight="bold")
                self._line_under_pai(px, y + 20)
                self.text(dwg, self.header.tempo, px + 30, y + 22, size=15,
                          anchor="start", family=FONT_SANS)
        self.y = y + 46

    def _line_under_pai(self, px, y):
        # 拍号中间的分数线
        self.line(self.dwg, px - 1, y - 9.5, px + 15, y - 9.5, 1.0)

    # ---- 行块 (system) 排布 ------------------------------------------
    def _slot_units(self, el):
        if isinstance(el, NoteEl):
            if el.pitch is None:       # 纯连音线标记, 不占宽度
                return 0.0
            return 1.0 + GS * (len(el.grace_front) + len(el.grace_back))
        return 0.0

    def _slot_width(self, el, cell):
        if isinstance(el, NoteEl):
            if el.pitch is None:
                return 0.0
            return cell * (1.0 + GS * (len(el.grace_front) + len(el.grace_back)))
        return cell

    def _render_system(self, qlines, lyrics):
        voices = [q.measures for q in qlines]
        nmeasures = max(len(v) for v in voices) if voices else 0
        if nmeasures == 0:
            return
        # 每个小节的音符单位数 (跨声部取最大值), 单位 = 一个基本音符宽度
        units = []
        for m in range(nmeasures):
            best = 0.0
            for v in voices:
                if m < len(v):
                    notes, _bar = v[m]
                    best = max(best, sum(self._slot_units(el) for el in notes))
            units.append(best)
        sumu = sum(units) or 1.0
        # 求解使整行铺满页面宽度的音符宽度
        cell = self.content_w / (sumu + BAR_RATIO * nmeasures)
        cell = max(MIN_CELL, min(cell, 42.0))
        mw = [u * cell + BAR_RATIO * cell for u in units]

        nvoices = len(qlines)
        nlyric = len(lyrics)

        # 计算减时线/低音点所需的最大垂直深度, 保证歌词不与减时线重叠
        depth = 11
        for q in qlines:
            for el in q.elements:
                if isinstance(el, NoteEl) and el.slashes > 0:
                    depth = max(depth, 11 + 9 * max(0, -el.octave)
                                + 8 * (el.slashes - 1))
        vstep = 46 + depth + 22

        # 按小节折行 (正常情况下 cell 已使整行容纳, 仅极长行才会多于一行)
        rows = []
        cur = []
        cur_w = 0.0
        for m in range(nmeasures):
            if cur and cur_w + mw[m] > self.content_w + 0.5:
                rows.append(cur)
                cur = []
                cur_w = 0.0
            cur.append(m)
            cur_w += mw[m]
        if cur:
            rows.append(cur)

        for row in rows:
            row_h = nvoices * vstep + nlyric * LYRIC_LINE_H + 22
            if self.y + row_h > self.page_h - self.margin:
                self._new_page()
                self.y = self.margin
            self._render_row(qlines, voices, mw, row, lyrics, cell, vstep)

    def _render_row(self, qlines, voices, mw, row, lyrics, cell, vstep):
        dwg = self.dwg
        top = self.y
        nvoices = len(qlines)
        nlyric = len(lyrics)
        row_h = nvoices * vstep + nlyric * LYRIC_LINE_H + 22

        measure_x = {}
        x0 = self.margin
        for m in row:
            measure_x[m] = x0
            x0 += mw[m]

        if nvoices > 1:
            by1 = top + 16
            by2 = top + (nvoices - 1) * vstep + 58
            self.line(dwg, self.margin - 18, by1, self.margin - 18, by2, 1.6)
            for yy in (by1, by2):
                self.line(dwg, self.margin - 18, yy, self.margin - 9, yy, 1.6)

        all_slots = []
        for vi, q in enumerate(qlines):
            y_nb = top + vi * vstep + 46
            slots = []
            slot_no = 0
            for m in row:
                if m >= len(voices[vi]):
                    continue
                notes, bar = voices[vi][m]
                placed = []
                x = measure_x[m]
                for el in notes:
                    w = self._slot_width(el, cell)
                    self._draw_note(el, x, y_nb, cell)
                    placed.append((x, w, el))
                    if el.is_lyric_slot:
                        slots.append((slot_no, x + w / 2.0, y_nb))
                        slot_no += 1
                    x += w
                self._draw_underlines(placed, y_nb)
                if bar is not None:
                    self._draw_bar(bar, measure_x[m] + mw[m] - BAR_RATIO * cell / 2.0,
                                   y_nb)
            self._draw_hairpins(qlines[vi], slots, y_nb)
            all_slots.append(slots)

        for vi, q in enumerate(qlines):
            self._draw_ties(q.elements, all_slots[vi], cell)

        self._draw_houses(voices, measure_x, mw, row, top)

        ly = top + nvoices * vstep + 4
        lsize = max(11.0, min(LYRIC_SIZE, cell * 0.95))
        for li, lyric in enumerate(lyrics):
            vi = lyric.voice - 1
            if vi < 0 or vi >= nvoices:
                vi = 0
            slots = all_slots[vi]
            if lyric.annotation:
                self.text(dwg, lyric.annotation,
                          self.margin - 18, ly + li * LYRIC_LINE_H + 4,
                          size=lsize * 0.8, anchor="end", family=FONT_SANS)
            for idx, (slot_no, cx, y_nb) in enumerate(slots):
                if idx < len(lyric.notes) and lyric.notes[idx]:
                    self.text(dwg, lyric.notes[idx], cx,
                              ly + li * LYRIC_LINE_H, size=lsize,
                              family=FONT_SANS)
        self.y = top + row_h

    # ---- 减时线(时值线) ----------------------------------------------
    def _draw_underlines(self, placed, y_nb):
        i = 0
        n = len(placed)
        while i < n:
            el = placed[i][2]
            if not (isinstance(el, NoteEl) and el.slashes > 0):
                i += 1
                continue
            j = i
            maxslash = 0
            maxlow = 0
            while j < n:
                e2 = placed[j][2]
                if not (isinstance(e2, NoteEl) and e2.slashes > 0):
                    break
                maxslash = max(maxslash, e2.slashes)
                maxlow = max(maxlow, max(0, -e2.octave))
                j += 1
            base = y_nb + 11 + 9 * maxlow
            for k in range(1, maxslash + 1):
                yy = base + (k - 1) * UL_GAP
                s = i
                while s < j:
                    if placed[s][2].slashes < k:
                        s += 1
                        continue
                    t = s
                    while t < j and placed[t][2].slashes >= k:
                        t += 1
                    xa = placed[s][0]
                    xb = placed[t - 1][0] + placed[t - 1][1]
                    self.line(self.dwg, xa + 1, yy, xb - 1, yy, 1.6)
                    s = t
            i = j

    # ---- 绘制一个音符 -------------------------------------------------
    def _draw_note(self, el, x_left, y_nb, cell):
        if el.pitch is None:          # 仅连音线标记, 无音符可画
            return
        dwg = self.dwg
        fs = max(13.0, min(NOTE_SIZE, cell * 0.8))
        gw = cell * GS
        gx = x_left
        for g in el.grace_front:
            self._draw_grace(g, gx + gw / 2.0, y_nb, fs * 0.6)
            gx += gw
        cx = x_left + gw * len(el.grace_front) + cell / 2.0

        if el.is_hold:
            self.line(dwg, cx - cell * 0.28, y_nb - fs * 0.28,
                      cx + cell * 0.28, y_nb - fs * 0.28, 2.0)
        elif el.pitch == "8":
            pass                                   # 隐藏休止符: 不绘制
        elif el.pitch == "0":
            self.text(dwg, "0", cx, y_nb, size=fs, family=FONT_NOTE,
                      weight="bold")
        elif el.pitch == "9":
            self.text(dwg, "X", cx, y_nb, size=fs, family=FONT_NOTE,
                      weight="bold")
        else:
            self.text(dwg, el.pitch, cx, y_nb, size=fs, family=FONT_NOTE,
                      weight="bold")

        if el.pitch != "8" and not el.is_hold:
            if el.acc:
                sym = {"#": "\u266f", "$": "\u266d",
                       "=": "\u266e"}.get(el.acc, el.acc)
                self.text(dwg, sym, cx - fs * 0.42, y_nb - fs * 0.25,
                          size=fs * 0.58, family=FONT_NOTE)
            for k in range(el.octave):
                self.dot(dwg, cx, y_nb - fs * 0.47 - fs * 0.34 * k)
            for k in range(-el.octave):
                self.dot(dwg, cx, y_nb + fs * 0.37 + fs * 0.34 * k)
            for k in range(el.dots):
                self.dot(dwg, cx + fs * 0.45 + fs * 0.22 * k,
                         y_nb - fs * 0.2)
            if el.text:
                self.text(dwg, el.text, cx, y_nb - fs * 1.05,
                          size=SMALL, family=FONT_SANS)
            self._draw_articulation(el, cx, y_nb, fs)

        bx = x_left + gw * len(el.grace_front) + cell
        for g in el.grace_back:
            self._draw_grace(g, bx + gw / 2.0, y_nb, fs * 0.6)
            bx += gw

    def _draw_articulation(self, el, cx, y_nb, fs):
        for a in el.articulations:
            y = y_nb - fs * 1.02
            if a in ("yc", "bc"):
                self.line(self.dwg, cx - fs * 0.32, y, cx + fs * 0.32, y, 1.2)
            elif a == "dy":
                self.dot(self.dwg, cx, y, 2.2)
            elif a == "zy":
                self.text(self.dwg, ">", cx, y, size=fs * 0.55, family=FONT_NOTE)
            elif a == "hx":
                self.text(self.dwg, ",", cx, y, size=fs * 0.55, family=FONT_NOTE)
            elif a == "zkh":        # 伴奏左括弧
                self.text(self.dwg, "(", cx + fs * 0.32, y_nb + fs * 0.16,
                          size=fs * 1.7, family=FONT_NOTE)
            elif a == "ykh":        # 伴奏右括弧
                self.text(self.dwg, ")", cx - fs * 0.32, y_nb + fs * 0.16,
                          size=fs * 1.7, family=FONT_NOTE)
            else:
                self.text(self.dwg, a, cx, y, size=SMALL,
                          family="'Times New Roman',serif", style="italic")

    def _draw_grace(self, grp, cx, y_nb, size):
        inner = grp.strip("[]")
        if inner.startswith("h"):
            inner = inner[1:]
        nums = [c for c in inner if c.isdigit()]
        if not nums:
            return
        self.text(self.dwg, nums[-1], cx, y_nb - size * 0.28,
                  size=max(size, 11), family=FONT_NOTE)

    # ---- 绘制小节线 ---------------------------------------------------
    def _draw_bar(self, bar, cx, y_nb):
        if bar.hide:
            return
        dwg = self.dwg
        y1 = y_nb - 26
        y2 = y_nb + 18
        draw = not (bar.house_open and not bar.lines)
        if draw:
            if bar.lines >= 2:
                self.line(dwg, cx - 4, y1, cx - 4, y2, 1.4)
                self.line(dwg, cx + 3, y1, cx + 3, y2, 3.0)
            elif bar.left_rep and bar.right_rep:
                self.line(dwg, cx - 9, y1, cx - 9, y2, 1.4)
                self.line(dwg, cx - 3, y1, cx - 3, y2, 3.0)
                self.line(dwg, cx + 7, y1, cx + 7, y2, 3.0)
                self.line(dwg, cx + 13, y1, cx + 13, y2, 1.4)
                self.dot(dwg, cx + 3, y_nb - 6)
                self.dot(dwg, cx + 3, y_nb + 4)
            elif bar.right_rep:
                self.dot(dwg, cx - 8, y_nb - 6)
                self.dot(dwg, cx - 8, y_nb + 4)
                self.line(dwg, cx - 2, y1, cx - 2, y2, 1.4)
                self.line(dwg, cx + 4, y1, cx + 4, y2, 3.0)
            elif bar.left_rep:
                self.line(dwg, cx - 4, y1, cx - 4, y2, 3.0)
                self.line(dwg, cx + 3, y1, cx + 3, y2, 1.4)
                self.dot(dwg, cx + 8, y_nb - 6)
                self.dot(dwg, cx + 8, y_nb + 4)
            else:
                self.line(dwg, cx, y1, cx, y2, 1.4)
        tagmap = {"dc": "D.C.", "ds": "D.S.", "fine": "Fine",
                  "ty": "\u2192\u2192", "hs": "\u20de"}
        for t in bar.tags:
            self.text(dwg, tagmap.get(t, t), cx, y_nb - 34, size=SMALL,
                      family=FONT_SANS)
        if bar.pai:
            p = bar.pai.split("/")
            if len(p) == 2:
                self.text(dwg, p[0], cx, y_nb - 30, size=14, family=FONT_NOTE,
                          weight="bold")
                self.text(dwg, p[1], cx, y_nb - 16, size=14, family=FONT_NOTE,
                          weight="bold")

    # ---- 渐强 / 渐弱 --------------------------------------------------
    def _draw_hairpins(self, qline, slots, y_nb):
        xs = {}
        idx = 0
        for el in qline.elements:
            if isinstance(el, NoteEl):
                if idx < len(slots):
                    xs[id(el)] = slots[idx][1]
                if el.is_lyric_slot:
                    idx += 1
        start = None
        for el in qline.elements:
            if not isinstance(el, NoteEl):
                continue
            if el.cresc and start is None:
                start = el
            if el.cresc_end and start is not None:
                self._hairpin(start.cresc, xs.get(id(start)),
                              xs.get(id(el)), y_nb)
                start = None

    def _hairpin(self, cresc, x1, x2, y_nb):
        if x1 is None or x2 is None:
            return
        dwg = self.dwg
        d, plus = cresc
        y = y_nb - 34 - plus * 7
        spread = 7
        if d == "<":
            pts = [(x1, y), (x2, y - spread), (x2, y + spread), (x1, y)]
        else:
            pts = [(x1, y - spread), (x2, y), (x1, y + spread)]
        dwg.add(dwg.polyline(points=pts, fill="none", stroke="#101010",
                             stroke_width=1.1))

    # ---- 连音线 -------------------------------------------------------
    def _draw_ties(self, elements, slots, cell):
        dwg = self.dwg
        pos = {}
        idx = 0
        for el in elements:
            if isinstance(el, NoteEl) and el.is_lyric_slot:
                if idx < len(slots):
                    pos[id(el)] = slots[idx]
                idx += 1
        stack = []
        for el in elements:
            if not isinstance(el, NoteEl):
                continue
            for _ in range(el.opens):
                stack.append(el)
            for _ in range(el.closes):
                if stack:
                    a = stack.pop()
                    if id(a) in pos and id(el) in pos:
                        self._tie(pos[id(a)], pos[id(el)], len(stack),
                                  a.multi, cell)

    def _tie(self, a, b, level, multi, cell):
        dwg = self.dwg
        x1, y1 = a[1], a[2]
        x2, y2 = b[1], b[2]
        y = min(y1, y2) - 24 - level * 8
        mid = (x1 + x2) / 2.0
        h = 7
        d = "M %.1f %.1f Q %.1f %.1f %.1f %.1f" % (x1, y, mid, y - h, x2, y)
        dwg.add(dwg.path(d=d, fill="none", stroke="#101010",
                         stroke_width=1.2))
        if multi:
            n = int(abs(x2 - x1) / (cell or 1)) + 1
            self.text(dwg, str(n), mid, y - 6, size=13, family=FONT_NOTE)

    # ---- 跳房子 -------------------------------------------------------
    def _draw_houses(self, voices, measure_x, mw, row, top):
        dwg = self.dwg
        open_bar = None
        xa = 0.0
        for m in row:
            for v in voices:
                if m >= len(v):
                    continue
                _notes, bar = v[m]
                if bar is None:
                    continue
                if bar.house_open:
                    open_bar = bar
                    xa = measure_x[m]
                if bar.house_close and open_bar is not None:
                    xb = measure_x[m] + mw[m] - 4
                    y = top + 6 - open_bar.plus * 6
                    self.line(dwg, xa, y, xb, y, 1.2)
                    self.line(dwg, xa, y, xa, y + 16, 1.2)
                    if not (bar.house_unclosed or open_bar.house_unclosed):
                        self.line(dwg, xb, y, xb, y + 16, 1.2)
                    if open_bar.house_text:
                        self.text(dwg, open_bar.house_text, (xa + xb) / 2,
                                  y - 4, size=SMALL, family=FONT_SANS)
                    open_bar = None


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def convert(infile, outfile=None, fmt="svg"):
    text = open(infile, encoding="utf-8").read()
    header, items = parse_jps(text)
    if outfile:
        base = os.path.splitext(outfile)[0]
    else:
        base = os.path.splitext(infile)[0]
    svg_out = base + ".svg"
    renderer = Renderer(header)
    renderer.render(items, svg_out)
    for i in range(len(renderer.pages)):
        renderer._save_page(i)
    if fmt != "svg":
        for i in range(len(renderer.pages)):
            svg = svg_out if i == 0 else "%s-%d.svg" % (base, i)
            out = ("%s.%s" % (base, fmt)) if i == 0 else \
                  ("%s-%d.%s" % (base, i, fmt))
            os.system("rsvg-convert -f %s -o %s %s" % (fmt, out, svg))
    return renderer.pages


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="将番茄简谱脚本(.jps)渲染为 SVG 简谱。")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="输出文件名 (默认与输入同名, 扩展名 .svg)")
    parser.add_argument("-f", "--format", metavar="FORMAT",
                        choices=["svg", "png", "pdf", "jpg"],
                        default="svg", help="输出格式, 默认 svg")
    parser.add_argument("file", metavar="FILE", help="输入的 .jps 文件")
    args = parser.parse_args(argv)
    convert(args.file, args.output, args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
