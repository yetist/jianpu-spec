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

版面尽可能贴近在线转换器(http://zhipu.lezhi99.com/)的输出：
每一行按"时值/节拍"自动撑满页面宽度并两端对齐，同一拍内的减时线音符
相互靠近；多声部共用同一套小节网格。仍有个别高级效果做了近似处理，
已在代码中以 NOTE 标注。
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
# 基本常量
#
# 在线转换器以 1000x1415 作为 A4 画布，本程序沿用同一坐标系，其它纸张
# 按比例缩放。所有尺寸常量都以 A4(宽 1000) 为基准。
# --------------------------------------------------------------------------
REF_W = 1000.0                      # A4 参考宽度
PX_PER_MM = REF_W / 210.0           # 1000 / 210 mm
PAGE_SIZES = {
    "A4": (210 * PX_PER_MM, 297 * PX_PER_MM),
    "A3": (297 * PX_PER_MM, 420 * PX_PER_MM),
    "B5": (176 * PX_PER_MM, 250 * PX_PER_MM),
    "Letter": (8.5 * 25.4 * PX_PER_MM, 11 * 25.4 * PX_PER_MM),
}

FONT_SANS = ("'Microsoft YaHei','Noto Sans CJK SC','PingFang SC',"
             "'Source Han Sans SC','WenQuanYi Zen Hei',sans-serif")
FONT_NOTE = ("'Liberation Serif','Times New Roman','Nimbus Roman',"
             "'FreeSerif',serif")

MARGIN = 80                         # 页边距 (文字用)
MUSIC_L = 83                        # 单声部音乐左边界
MUSIC_L_MV = 103                    # 多声部音乐左边界 (留出声部括弧)
MUSIC_R = 923                       # 音乐右边界

TITLE_Y = 140                       # 主标题基线
SUB_Y = 183                         # 副标题基线
KEY_Y = 187                         # 调号/拍号基线
TEMPO_Y = 222                       # 速度标记基线
AUTH_BOTTOM_J = 226                 # 有速度行时作者块的底边
AUTH_BOTTOM = 196                   # 无速度行时作者块的底边
AUTH_GAP = 21                       # 作者行间距
FIRST_GAP = 40                      # 描述头底部 -> 首行第一声部基线
DYN_PAD = 12                        # 首行上方若有力度记号再让出的空间

TITLE_SIZE = 36
SUB_SIZE = 20
AUTH_SIZE = 16
KEY_SIZE = 27
PAI_SIZE = 16
TEMPO_SIZE = 16
NOTE_SIZE = 26                      # 音符数字字号
LYRIC_SIZE = 18                     # 歌词字号
ANNOT_SIZE = 16                     # 歌词注释(段落号)字号
SMALL = 13                          # 记号/注释小字号
DOT_R = 1.7                         # 八度点半径
FUDIAN_DX = 11                      # 附点相对音符中心的水平偏移

VOICE_GAP = 78                      # 同一组内相邻声部的基线间距
LYRIC_OFF = 44                      # 最后一声部基线 -> 首行歌词基线
LYRIC_LINE = 28                     # 歌词行间距
LYRIC_LEAD = 15                     # 声部行与其"贴身"歌词之间的净行距

BAR_UP = 14                         # 小节线高出音符基线
BAR_DN = 13                         # 小节线低于音符基线
UL_Y = 12                           # 第一条减时线相对基线的偏移
UL_GAP = 5                          # 减时线间距
OCT_HIGH = 13                       # 高八度点相对基线的偏移
OCT_LOW = 13                        # 低八度点相对基线的偏移
OCT_STEP = 9                        # 多个八度点的间距

# 相邻元素之间的"时值系数": 行宽 / 系数和 = 单位宽度
UNIT_SAME_BEAM = 1.0                # 同一拍内的减时线音符
UNIT_SHORT = 1.5                    # 前一音符 ≤ 半拍时的间距
UNIT_LONG = 2.0                     # 前一音符 > 半拍时的间距
UNIT_BAR = 1.4                      # 与小节线相邻
UNIT_END = 2.0                      # 行末最后一小节收尾


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
    def sung(self):
        # 是否是一个可发声的音符 (用于歌词/记号定位)
        return self.pitch is not None

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
    """一行歌词, 解析为一个个"槽位记号"。

    tokens 里每个元素要么是字符串(该槽位要显示的歌词), 要么是 None(表示
    脚本里的 ``@``, 即"跳过一个音符")。汉字一字一槽, 标点自动并入前一个
    槽位, ``~`` 把下一个字并入当前槽位, 英文单词以 ``/`` 分隔。
    """

    def __init__(self, voice, text):
        self.voice = voice
        self.raw = text
        self.tokens = []
        self.annotation = ""
        self.parse()

    def parse(self):
        text = self.raw
        i = 0
        n = len(text)
        # 前导 @ (空槽) 之后可以跟一个引号注释, 例如 @"1、"奇异恩典...
        while i < n and text[i] in " \t@":
            if text[i] == "@":
                self.tokens.append(None)
            i += 1
        if i < n and text[i] == '"':
            j = text.find('"', i + 1)
            if j < 0:
                j = n
            self.annotation = text[i + 1:j].replace("_", " ")
            i = j + 1
        while i < n:
            c = text[i]
            if c in " \t":
                i += 1
            elif c == '"':
                j = text.find('"', i + 1)
                i = (j + 1) if j >= 0 else n
            elif c == "@":
                self.tokens.append(None)
                i += 1
            elif c in "{}":
                i += 1
            elif c in "-/":
                i += 1
            elif c == "~":
                i += 1
                if i < n and self.tokens and self.tokens[-1] is not None:
                    self.tokens[-1] += text[i]
                    i += 1
                elif i < n:
                    self.tokens.append(text[i])
                    i += 1
            elif c.isascii() and c.isalpha():
                j = i
                while j < n and text[j].isascii() and (text[j].isalpha()
                                                       or text[j] in "'’"):
                    j += 1
                self.tokens.append(text[i:j])
                i = j
            elif is_punct(c):
                if self.tokens and self.tokens[-1] is not None:
                    self.tokens[-1] += c
                else:
                    self.tokens.append(c)
                i += 1
            else:
                self.tokens.append(c)
                i += 1


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
            if i > 0 and tok[i - 1] == "|":
                el.left_rep = True
            else:
                el.right_rep = True
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
            els.append(parse_note(t))
    return merge_elements(els)


def merge_elements(raw):
    """整理元素列表：

    * 丢弃纯记号(无音高)token，把其连音线起止/多连音信息并入相邻音符；
    * 例如 `3 (// 4//)` 中的 `(//` 合并到前一个音符 `3` 上。
    """
    merged = []
    pending_opens = 0
    pending_multi = False
    for el in raw:
        if isinstance(el, NoteEl) and el.pitch is None:
            prev = None
            if (merged and isinstance(merged[-1], NoteEl)
                    and merged[-1].pitch is not None):
                prev = merged[-1]
            if prev is not None:
                # 记号 token 的时值/记号信息归属前一个音符,
                # 例如 `5 (.` => 附点四分音符, `3 (//` => 十六分音符。
                prev.dots += el.dots
                prev.slashes += el.slashes
                if el.acc and not prev.acc:
                    prev.acc = el.acc
                prev.grace_front += el.grace_front
                prev.grace_back += el.grace_back
                prev.marks += el.marks
                if el.text and not prev.text:
                    prev.text = el.text
                prev.opens += el.opens
                prev.multi = prev.multi or el.multi
                prev.closes += el.closes
            else:
                if el.opens:
                    pending_opens += el.opens
                    pending_multi = pending_multi or el.multi
                if el.closes and merged and isinstance(merged[-1], NoteEl):
                    merged[-1].closes += el.closes
            continue
        if isinstance(el, NoteEl) and pending_opens:
            el.opens += pending_opens
            el.multi = el.multi or pending_multi
            pending_opens = 0
            pending_multi = False
        merged.append(el)
    return merged


def parse_jps(text):
    """解析脚本, 返回 (Header, items)。

    items 为列表, 每项是 ('system', [QLine...], [LyricLine...], order)
    或 ('pagebreak',)。order 保存声部/歌词在源码中出现的先后次序, 每项为
    ('Q', qline) 或 ('C', lyric), 供渲染时按源顺序纵向排布。
    """
    header = Header()
    items = []
    qgroup = []
    lyrics = []
    order = []

    def flush_group():
        nonlocal qgroup, lyrics, order
        if qgroup:
            items.append(("system", qgroup, lyrics, order))
        qgroup = []
        lyrics = []
        order = []

    prev_kind = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if qgroup:                      # 空行 => 开始新的 system
                flush_group()
            prev_kind = None
            continue
        if line.startswith("#"):
            continue
        if line == "[fenye]":
            flush_group()
            items.append(("pagebreak",))
            prev_kind = None
            continue
        m = re.match(r'^(Q|C)(\d*)("([^"]*)")?\s*:(.*)$', line)
        if m:
            kind = m.group(1)
            voice = int(m.group(2)) if m.group(2) else 1
            name = m.group(4) or ""
            body = m.group(5)
            if kind == "Q":
                # 参考实现: 只有"歌词行之后的声部行"且声部编号回退时,
                # 才开启新的 system (曲谱中声部编号重新从 1 开始时,
                # 若上一行仍是声部行, 则属于同一个 system)。
                if qgroup and prev_kind == "C":
                    maxv = max(v.voice for v in qgroup)
                    if voice <= maxv:
                        flush_group()
                q = QLine(voice, name)
                raw = []
                for kind2, tok in tokenize(body):
                    if kind2 == "bar":
                        raw.append(parse_bar(tok))
                    elif kind2 == "block":
                        raw.extend(parse_block(tok))
                    else:
                        raw.append(parse_note(tok))
                q.elements = merge_elements(raw)
                qgroup.append(q)
                order.append(("Q", q))
            else:
                lyric = LyricLine(voice, body)
                lyrics.append(lyric)
                order.append(("C", lyric))
            prev_kind = kind
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
# 时值 / 系数计算
# --------------------------------------------------------------------------
def duration(el):
    """返回以四分音符为 1 拍的时值。"""
    d = 2.0 ** (-el.slashes)
    if el.dots:
        d *= 2 - 2.0 ** (-el.dots)
    return d


def coef(a, b, onset_a=None, onset_b=None):
    """两个相邻元素之间的时值系数 (相对单位宽度)。

    参考实现里: 同一拍内的相邻减时线音符紧挨着排 (1 个单位); 其余情况按
    前一个音符的时值给间距 -- 短音符(≤ 半拍) 1.5 个单位, 长音符 2 个单位。
    """
    if isinstance(a, BarEl) or isinstance(b, BarEl):
        return UNIT_BAR
    if (a.slashes > 0 and b.slashes > 0 and onset_a is not None
            and onset_b is not None and int(onset_a) == int(onset_b)):
        return UNIT_SAME_BEAM
    # 倚音需要额外空间
    return (UNIT_SHORT if duration(a) <= 0.5 else UNIT_LONG) \
        + 0.6 * len(b.grace_front)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
class Renderer(object):
    def __init__(self, header):
        self.header = header
        self.page_w, self.page_h = PAGE_SIZES.get(
            header.page, PAGE_SIZES["A4"])
        self.k = self.page_w / REF_W        # 相对 A4 的缩放系数
        self.pages = []
        self.page_index = -1
        self._outfile = "out.svg"
        self.y = 0.0
        self._first_base = 0.0

    # ---- 底层绘制辅助 -------------------------------------------------
    @staticmethod
    def _svg_text(dwg, s, x, y, size, anchor="middle", family=FONT_NOTE,
                  fill="#1b1b1b", weight="normal", style="normal"):
        dwg.add(dwg.text(s, insert=(x, y),
                         font_size="%.2fpx" % size, font_family=family,
                         text_anchor=anchor, fill=fill, font_weight=weight,
                         font_style=style))

    def text(self, s, x, y, size, anchor="middle", family=FONT_NOTE,
             fill="#1b1b1b", weight="normal", style="normal"):
        self._svg_text(self.dwg, s, x, y, size, anchor, family, fill,
                       weight, style)

    def line(self, x1, y1, x2, y2, width=1.5):
        self.dwg.add(self.dwg.line(start=(x1, y1), end=(x2, y2),
                                   stroke="#1b1b1b",
                                   stroke_width="%.2fpx" % width,
                                   stroke_linecap="round"))

    def dot(self, x, y, r=None):
        if r is None:
            r = DOT_R * self.k
        self.dwg.add(self.dwg.circle(center=(x, y), r="%.2fpx" % r,
                                     fill="#1b1b1b"))

    # ---- 主入口 -------------------------------------------------------
    def render(self, items, outfile):
        self._outfile = outfile
        self._new_page()
        bottom = self._render_header()
        self._page_top = MARGIN * self.k
        self._first_base = bottom + FIRST_GAP * self.k
        self.y = self._first_base
        for item in items:
            if item[0] == "pagebreak":
                self._new_page()
                self.y = self._page_top
            else:
                self._render_system(item[1], item[2], item[3])
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

    def _save_page(self, index, total=1):
        base, ext = os.path.splitext(self._outfile)
        if total <= 1:
            name = self._outfile
        else:
            name = "%s-%d%s" % (base, index + 1, ext)
        self.pages[index].saveas(name)
        return name

    # ---- 描述头 -------------------------------------------------------
    def _render_header(self):
        k = self.k
        h = self.header
        dwg = self.dwg
        if h.titles:
            self.text(h.titles[0], self.page_w / 2.0, TITLE_Y * k,
                      TITLE_SIZE * k, family=FONT_SANS, weight="bold")
            for i, t in enumerate(h.titles[1:], 1):
                self.text(t, self.page_w / 2.0,
                          (SUB_Y + (i - 1) * 26) * k, SUB_SIZE * k,
                          family=FONT_SANS)
        has_j = bool(h.tempo)
        bottom = (AUTH_BOTTOM_J if has_j else AUTH_BOTTOM) * k
        na = len(h.authors)
        for i, a in enumerate(h.authors):
            y = bottom - (AUTH_GAP * (na - 1 - i) + 3) * k
            self.text(a, MUSIC_R * k, y, AUTH_SIZE * k, anchor="end",
                      family=FONT_SANS)
        # 调号
        if h.key:
            acc = {"#": "\u266f", "$": "\u266d"}.get(h.key_acc, "")
            self.text("1=", MARGIN * k, KEY_Y * k, KEY_SIZE * k,
                      anchor="start", family=FONT_NOTE, weight="bold")
            x = MARGIN * k + 36 * k
            if acc:
                self.text(acc, MARGIN * k + 30 * k, (KEY_Y - 10) * k,
                          KEY_SIZE * k * 0.62, anchor="start", family=FONT_NOTE)
            self.text(h.key, x, KEY_Y * k, KEY_SIZE * k,
                      anchor="start", family=FONT_NOTE, weight="bold")
        # 拍号
        if h.pai and "/" in h.pai:
            num, den = h.pai.split("/", 1)
            px = MARGIN * k + 64 * k
            self.text(num, px, (KEY_Y - 17) * k, PAI_SIZE * k,
                      family=FONT_NOTE, weight="bold")
            self.text(den, px, (KEY_Y + 8) * k, PAI_SIZE * k,
                      family=FONT_NOTE, weight="bold")
            self.line(px - 8 * k, (KEY_Y - 11) * k, px + 8 * k,
                      (KEY_Y - 11) * k, 1.0 * k)
            if h.pai_aux:
                self.text(h.pai_aux, px + 13 * k, KEY_Y * k,
                          SMALL * k, anchor="start", family=FONT_SANS)
        # 速度
        if h.tempo:
            if h.tempo_num:
                self.text("\u2669= %s" % h.tempo_num, MARGIN * k,
                          TEMPO_Y * k, TEMPO_SIZE * k, anchor="start",
                          family=FONT_SANS)
            else:
                self.text(h.tempo, MARGIN * k, TEMPO_Y * k, TEMPO_SIZE * k,
                          anchor="start", family=FONT_SANS)
        return bottom

    # ---- 行块 (system) -------------------------------------------------
    @staticmethod
    def _measure_coefs(notes, bar, last=False):
        """返回一个小节的 (系数列表, 起始拍位置列表)。

        系数列表长度 = len(notes) (若有小节线) 或 len(notes)+1 (虚拟收尾)。
        """
        onsets = []
        t = 0.0
        for el in notes:
            onsets.append(t)
            t += duration(el)
        tail = UNIT_END if last else UNIT_BAR
        coefs = []
        for i in range(len(notes)):
            nxt = notes[i + 1] if i + 1 < len(notes) else bar
            oa = onsets[i]
            ob = onsets[i + 1] if i + 1 < len(notes) else None
            if i + 1 == len(notes):
                coefs.append(tail)          # 收尾(或虚拟收尾)
            elif nxt is None:
                coefs.append(tail)
            else:
                coefs.append(coef(notes[i], nxt, oa, ob))
        if not notes:
            coefs.append(tail)
        return coefs, onsets

    def _render_system(self, qlines, lyrics, order=None):
        k = self.k
        voices = [q.measures for q in qlines]
        nmeas = max(len(v) for v in voices) if voices else 0
        if nmeas == 0:
            return
        nv = len(qlines)
        nl = len(lyrics)

        has_dyn = any(isinstance(el, NoteEl) and el.dynamics
                      for q in qlines for el in q.elements)
        has_house = any(isinstance(el, BarEl) and el.house_open
                        for q in qlines for el in q.elements)
        at_top = (abs(self.y - self._first_base) < 0.1
                  or abs(self.y - self._page_top) < 0.1)

        # --- 纵向: 按源码顺序把声部行与歌词行分为若干"簇" ---
        #
        # 脚本正文里, 一个声部行连同紧跟在它后面的歌词行构成一个"簇": 歌词
        # 紧贴该声部下方 (LYRIC_OFF), 同簇内多行歌词按 LYRIC_LINE 递进。簇的
        # 高度 = 声部行 (VOICE_GAP) + (首行歌词 LYRIC_LEAD) + 其余歌词行。
        # 当歌词与声部交错(后面还跟着别的声部)时, 参考实现把歌词"贴"进声部
        # 行距里, 首行歌词只额外占用 LYRIC_LEAD 的高度。
        seq = order if order else (
            [("Q", q) for q in qlines] + [("C", ly) for ly in lyrics])
        qindex = {id(q): i for i, q in enumerate(qlines)}
        clusters = []                   # [(qline 或 None, [lyric, ...]), ...]
        for lk, lobj in seq:
            if lk == "Q":
                clusters.append([lobj, []])
            elif clusters:
                clusters[-1][1].append(lobj)
            else:
                clusters.append([None, [lobj]])
        interleaved = any(clusters[i][1] for i in range(len(clusters) - 1))
        tight = interleaved or (nv == 1 and nl >= 2)
        ly_lead = LYRIC_LEAD if tight else LYRIC_LINE
        sys_h = sum((VOICE_GAP if c[0] is not None else 0.0)
                    + (ly_lead + LYRIC_LINE * (len(c[1]) - 1) if c[1] else 0.0)
                    for c in clusters) * k
        # 版心底部: 用于判断本行是否还能排进当前页。歌词行只需到最后一行的
        # 字身底部, 不必按整行高度(簇尾的 LYRIC_LEAD)计算。
        cur = self.y
        sys_bottom = 0.0
        for cq, clys in clusters:
            nb_rel = cur
            b = nb_rel + (BAR_DN * k if cq is not None else 0.0)
            if cq is not None:
                cur += VOICE_GAP * k
            if clys:
                b = max(b, nb_rel + (LYRIC_OFF + (len(clys) - 1) * LYRIC_LINE
                                     + LYRIC_SIZE * 0.5) * k)
                cur += (ly_lead + LYRIC_LINE * (len(clys) - 1)) * k
            sys_bottom = max(sys_bottom, b - self.y)

        pad = (DYN_PAD * k) if (has_house or (has_dyn and at_top)) else 0.0
        # 参考实现只在源码里的分页符处换页, 不按版心高度自动折行; 这里仅当
        # 内容会超出物理页面时兜底另起一页。
        if self.y + pad + sys_bottom > self.page_h - 20 * k:
            self._new_page()
            self.y = self._page_top
            pad = 0.0
        self.y += pad

        left = (MUSIC_L_MV if nv > 1 else MUSIC_L) * k
        right = MUSIC_R * k
        # 行首若是隐藏休止符 8, 参考实现会整体右移一段; 行首带伴奏左括弧
        # (&zkh) 时也稍作右移, 给括弧留出位置。
        first_note = None
        for q in qlines:
            for el in q.elements:
                if isinstance(el, NoteEl):
                    first_note = el
                    break
            if first_note is not None:
                break
        if first_note is not None:
            if first_note.pitch == "8":
                left += 38 * k
            elif "zkh" in first_note.marks:
                left += 9 * k

        # --- 1. 每个小节的时值系数, 生成跨声部共享的"时间网格" ---
        #
        # 参考实现里同一时刻(onset)的音符在所有声部中 x 坐标相同: 先把各声部
        # 的 onset 合并, 再用一条公共的系数序列把小节撑满, 每个音符落在自己
        # onset 对应的位置上。
        vcoefs = []                     # vcoefs[vi][m] = (coefs, onsets)
        for v in voices:
            mm = []
            for m in range(nmeas):
                notes, bar = v[m] if m < len(v) else ([], None)
                mm.append(self._measure_coefs(notes, bar, m == nmeas - 1))
            vcoefs.append(mm)

        def has_notes(m):
            return any(m < len(voices[vi]) and voices[vi][m][0]
                       for vi in range(nv))

        def lead_coef(m):
            # 小节线 -> 本小节第一个音符之间的系数。
            # 行首或上一小节为空(只是行首小节线)时不留空隙。
            if m == 0 or not has_notes(m - 1):
                return 0.0
            return UNIT_BAR

        def cum_lookup(seq, onset):
            """seq 为 [(onset, 累计系数), ...], 返回 onset 之前的累计系数。"""
            val = 0.0
            for t, c in seq:
                if t <= onset + 1e-9:
                    val = c
                else:
                    break
            return val

        grid = []                       # grid[m] = 该小节系数总和(含前导)
        gcum = []                       # gcum[m] = [(onset, 累计系数), ...]
        for m in range(nmeas):
            if not has_notes(m):
                grid.append(0.0)
                gcum.append([])
                continue
            tl = sorted({o for vi in range(nv) if m < len(vcoefs[vi])
                         for o in vcoefs[vi][m][1]})
            seq = []
            acc = 0.0
            for j, t in enumerate(tl):
                nxt = tl[j + 1] if j + 1 < len(tl) else None
                c = None
                for vi in range(nv):
                    if m >= len(vcoefs[vi]):
                        continue
                    cfs, ons = vcoefs[vi][m]
                    for i, o in enumerate(ons):
                        if abs(o - t) > 1e-9:
                            continue
                        nxt_o = ons[i + 1] if i + 1 < len(ons) else None
                        if nxt is None:
                            if nxt_o is None:
                                c = cfs[i]
                        elif nxt_o is not None and abs(nxt_o - nxt) < 1e-9:
                            c = cfs[i]
                        if c is not None:
                            break
                    if c is not None:
                        break
                if c is None:
                    c = UNIT_BAR
                if j + 1 < len(tl):
                    seq.append((tl[j + 1], acc + c))
                acc += c
            grid.append(lead_coef(m) + acc)
            gcum.append(seq)
        total = sum(grid) or 1.0
        u = (right - left) / total

        # 小节起点 (= 上一个小节末尾的小节线位置)
        mstart = []
        x = left
        for m in range(nmeas):
            mstart.append(x)
            x += grid[m] * u
        mend = mstart + [right]

        # --- 2. 计算每个声部每个音符的 x 位置 ---
        pos = []                        # pos[vi][m] = [(x, el), ...]
        barx = []                       # barx[vi][m] = (x, bar)
        for vi in range(nv):
            pv = []
            bv = []
            for m in range(nmeas):
                notes, bar = voices[vi][m] if m < len(voices[vi]) else ([], None)
                onsets = vcoefs[vi][m][1] if m < len(voices[vi]) else []
                lead = lead_coef(m)
                lst = []
                for i, el in enumerate(notes):
                    o = onsets[i] if i < len(onsets) else 0.0
                    px = mstart[m] + (lead + cum_lookup(gcum[m], o)) * u
                    lst.append((px, el))
                pv.append(lst)
                bv.append((mend[m + 1], bar))
            pos.append(pv)
            barx.append(bv)

        # --- 3. 按"簇"自上而下定位声部行与歌词行 ---
        #
        # 每个簇占 声部行 + 首行歌词(LYRIC_LEAD/LYRIC_LINE) + 其余歌词行的
        # 高度; 歌词行基线 = 所属声部基线 + LYRIC_OFF + j*LYRIC_LINE。
        vy = {}                         # id(qline) -> 音符行基线 y
        lyric_y = {}                    # id(lyric) -> 歌词行基线 y
        lyric_anchor = {}               # id(lyric) -> 所属声部序号
        cursor = self.y
        for q, lys in clusters:
            if q is not None:
                vy[id(q)] = cursor
                cursor += VOICE_GAP * k
            nb = vy.get(id(q)) if q is not None else self.y
            for j, ly in enumerate(lys):
                lyric_y[id(ly)] = nb + (LYRIC_OFF + j * LYRIC_LINE) * k
                lyric_anchor[id(ly)] = qindex.get(id(q), 0)
            if lys:
                cursor += (ly_lead + LYRIC_LINE * (len(lys) - 1)) * k

        # --- 4. 绘制各声部 ---
        slots = []                      # slots[vi] = [(x, y_nb, is_lyric)]
        idpos = {}                      # id(el) -> (x, y_nb)
        vtop = None
        vbot = None
        for vi, q in enumerate(qlines):
            nb = vy.get(id(q), self.y + vi * VOICE_GAP * k)
            vtop = nb if vtop is None else min(vtop, nb)
            vbot = nb if vbot is None else max(vbot, nb)
            if nv > 1 and q.name:
                self.text(q.name, left - 35 * k, nb + 6 * k,
                          SMALL * k, anchor="end", family=FONT_SANS)
            sl = []
            for m in range(nmeas):
                for (px, el) in pos[vi][m]:
                    self._draw_note(el, px, nb)
                    idpos[id(el)] = (px, nb)
                    if el.is_lyric_slot:
                        sl.append((px, nb))
                self._draw_underlines(pos[vi][m], nb,
                                      vcoefs[vi][m][1] if m < len(vcoefs[vi])
                                      else [])
            for m in range(nmeas):
                bx, bar = barx[vi][m]
                if bar is not None:
                    self._draw_bar(bar, bx, nb)
            slots.append(sl)
            self._draw_hairpins(q, idpos, nb)

        # --- 5. 声部括弧 ---
        if nv > 1:
            self._draw_voice_bracket(left, vtop, vbot, k)

        # --- 6. 连音线 ---
        for vi, q in enumerate(qlines):
            self._draw_ties(q.elements, idpos, k)

        # --- 7. 跳房子 ---
        self._draw_houses(qlines, mstart, mend, nmeas)

        # --- 8. 歌词 ---
        #
        # 一字一音: 依次落到所属声部的音符上 (歌词行上方的那个声部)。连音线的
        # 结束音同样占一个歌词位, 只有源码里的 ``@`` 才会"吃掉"紧跟其后的一个
        # 音符。
        for lyric in lyrics:
            ly = lyric_y.get(id(lyric), self.y)
            vi = lyric_anchor.get(id(lyric), 0)
            if not (0 <= vi < len(slots)):
                vi = 0
            base_slots = slots[vi] if slots else []
            placed = []
            cur = 0
            for tok in lyric.tokens:
                if tok is None:
                    if cur < len(base_slots):
                        cur += 1
                    continue
                if cur >= len(base_slots):
                    break
                placed.append((base_slots[cur][0], tok))
                cur += 1
            if lyric.annotation and placed:
                # 段落号右对齐到首字左缘 (与歌词首字同一 x)
                self.text(lyric.annotation,
                          placed[0][0] - LYRIC_SIZE * k / 2.0, ly,
                          ANNOT_SIZE * k, anchor="end", family=FONT_SANS)
            for sx, txt in placed:
                self.text(txt, sx - LYRIC_SIZE * k / 2.0, ly,
                          LYRIC_SIZE * k, anchor="start", family=FONT_SANS)

        self.y = cursor

    # ---- 减时线(时值线) ----------------------------------------------
    def _draw_underlines(self, placed, nb, onsets=None):
        """绘制减时线。

        连续的减时线音符若跨越了拍子 (四分音符为一拍), 参考实现会按拍把
        下划线断开, 而不是连成一条贯穿的线; 这里按音符起始拍分组后再画。
        """
        k = self.k
        n = len(placed)

        def beat(i):
            if onsets and i < len(onsets):
                return int(onsets[i] + 1e-9)
            return 0

        i = 0
        while i < n:
            el = placed[i][1]
            if not (isinstance(el, NoteEl) and el.slashes > 0):
                i += 1
                continue
            j = i
            while (j < n and isinstance(placed[j][1], NoteEl)
                   and placed[j][1].slashes > 0):
                j += 1
            # 同一拍内的连续音符组成一组, 组间留空隙
            g = i
            while g < j:
                b = beat(g)
                h = g + 1
                while h < j and beat(h) == b:
                    h += 1
                self._draw_beam_group(placed, g, h, nb)
                g = h
            i = j

    def _draw_beam_group(self, placed, a, b, nb):
        """画一组 (同一拍内) 减时线音符的下划线。"""
        k = self.k
        maxslash = max(placed[t][1].slashes for t in range(a, b))
        for lv in range(1, maxslash + 1):
            yy = nb + (UL_Y + (lv - 1) * UL_GAP) * k
            s = a
            while s < b:
                if placed[s][1].slashes < lv:
                    s += 1
                    continue
                t = s
                while t < b and placed[t][1].slashes >= lv:
                    t += 1
                xa = placed[s][0] - 6 * k
                last = placed[t - 1][1]
                # 末音符带附点时, 减时线延长到附点之后
                dx = (FUDIAN_DX + 4) if last.dots else 6
                xb = placed[t - 1][0] + dx * k
                self.line(xa, yy, xb, yy, 1.5 * k)
                s = t

    # ---- 绘制一个音符 -------------------------------------------------
    def _draw_note(self, el, cx, nb):
        if el.pitch is None or el.pitch == "8":
            return
        k = self.k
        fs = NOTE_SIZE * k
        base = nb + fs * 0.36
        if el.is_hold:
            self.line(cx - 7 * k, nb, cx + 7 * k, nb, 1.8 * k)
        elif el.pitch == "9":
            self.text("X", cx, base, fs, family=FONT_NOTE, weight="bold")
        else:
            self.text(el.pitch, cx, base, fs, family=FONT_NOTE,
                      weight="bold")

        if el.pitch == "9":
            pass
        if el.acc:
            sym = {"#": "\u266f", "$": "\u266d",
                   "=": "\u266e"}.get(el.acc, el.acc)
            self.text(sym, cx - fs * 0.62, base - fs * 0.28,
                      fs * 0.6, family=FONT_NOTE)
        # 八度点
        low_y = nb + (UL_Y + 5 + max(0, el.slashes - 1) * UL_GAP
                      if el.slashes else OCT_LOW) * k
        for i in range(el.octave):
            self.dot(cx, nb - (OCT_HIGH + i * OCT_STEP) * k)
        for i in range(-el.octave):
            self.dot(cx, low_y + i * OCT_STEP * k)
        # 附点
        for i in range(el.dots):
            self.dot(cx + (FUDIAN_DX + i * 7) * k, nb + 4 * k, DOT_R * k * 1.1)
        # 倚音
        for i, g in enumerate(el.grace_front):
            self._draw_grace(g, cx - (12 + i * 9) * k, nb, fs * 0.55)
        for i, g in enumerate(el.grace_back):
            self._draw_grace(g, cx + (12 + i * 9) * k, nb, fs * 0.55)
        # 音符注释
        if el.text:
            self.text(el.text, cx, nb - 22 * k, SMALL * k, family=FONT_SANS)
        # 力度/记号
        self._draw_marks(el, cx, nb, fs)

    def _draw_marks(self, el, cx, nb, fs):
        k = self.k
        dyn = el.dynamics
        if dyn:
            self.text(" ".join(dyn), cx, nb - 26 * k, SMALL * k,
                      family="'Times New Roman',serif", style="italic")
        for a in el.articulations:
            y = nb - 22 * k
            if a in ("yc", "bc"):
                self.line(cx - 5 * k, y, cx + 5 * k, y, 1.2 * k)
            elif a == "dy":
                self.dot(cx, y, 1.8 * k)
            elif a == "zy":
                self.text(">", cx, y, fs * 0.6, family=FONT_NOTE)
            elif a == "hx":
                self.text(",", cx, y, fs * 0.6, family=FONT_NOTE)
            elif a == "zkh":            # 伴奏左括弧
                self._paren(cx - 11 * k, nb, -1)
            elif a == "ykh":            # 伴奏右括弧
                self._paren(cx + 9 * k, nb, 1)
            else:
                self.text(a, cx, y, SMALL * k,
                          family="'Times New Roman',serif", style="italic")

    def _paren(self, x, nb, direc):
        """绘制一个细长的大括弧 (伴奏 &zkh / &ykh)。"""
        k = self.k
        top = nb - 40 * k
        bot = nb + 20 * k
        mid = (top + bot) / 2.0
        bulge = 8 * k * direc
        d = "M %.2f %.2f Q %.2f %.2f %.2f %.2f" % (
            x, top, x + bulge, mid, x, bot)
        self.dwg.add(self.dwg.path(d=d, fill="none", stroke="#1b1b1b",
                                   stroke_width="%.2fpx" % (1.3 * k)))

    def _draw_grace(self, grp, cx, nb, size):
        inner = grp.strip("[]")
        if inner.startswith("h"):
            inner = inner[1:]
        nums = [c for c in inner if c.isdigit()]
        if not nums:
            return
        k = self.k
        self.text(nums[-1], cx, nb - 10 * k, max(size, 10 * k),
                  family=FONT_NOTE)

    # ---- 绘制小节线 ---------------------------------------------------
    def _draw_bar(self, bar, cx, nb):
        if bar.hide:
            return
        k = self.k
        y1 = nb - BAR_UP * k
        y2 = nb + BAR_DN * k
        w = 1.5 * k
        dots = bar.left_rep or bar.right_rep
        # 竖线
        if bar.lines >= 2:
            self.line(cx - 1 * k, y1, cx - 1 * k, y2, w)
            self.line(cx + 4 * k, y1, cx + 4 * k, y2, 4.0 * k)
        elif bar.left_rep and bar.right_rep:
            self.line(cx - 3 * k, y1, cx - 3 * k, y2, w)
        elif bar.right_rep:
            self.line(cx + 1 * k, y1, cx + 1 * k, y2, w)
            self.line(cx + 6 * k, y1, cx + 6 * k, y2, 4.0 * k)
        elif bar.left_rep:
            self.line(cx - 6 * k, y1, cx - 6 * k, y2, 4.0 * k)
            self.line(cx - 1 * k, y1, cx - 1 * k, y2, w)
        else:
            self.line(cx, y1, cx, y2, w)
        # 反复记号圆点
        if bar.left_rep:
            for dy in (-6, 4):
                self.dot(cx + 4 * k, nb + dy * k, DOT_R * k)
        if bar.right_rep:
            for dy in (-6, 4):
                self.dot(cx - 5 * k, nb + dy * k, DOT_R * k)
        # 标记与临时拍号
        tagmap = {"dc": "D.C.", "ds": "D.S.", "fine": "Fine",
                  "ty": "\u2192\u2192", "hs": "\u20de"}
        for t in bar.tags:
            self.text(tagmap.get(t, t), cx, y1 - 8 * k, SMALL * k,
                      family=FONT_SANS)
        if bar.pai and "/" in bar.pai:
            num, den = bar.pai.split("/", 1)
            self.text(num, cx, nb - 24 * k, PAI_SIZE * k * 0.9,
                      family=FONT_NOTE, weight="bold")
            self.text(den, cx, nb - 8 * k, PAI_SIZE * k * 0.9,
                      family=FONT_NOTE, weight="bold")
            self.line(cx - 7 * k, nb - 16 * k, cx + 7 * k, nb - 16 * k,
                      1.0 * k)
        _ = dots

    # ---- 声部括弧 -----------------------------------------------------
    def _draw_voice_bracket(self, left, top, bottom, k):
        y1 = top - BAR_UP * k
        y2 = bottom + BAR_DN * k
        x = left - 14 * k
        self.line(x, y1, x, y2, 1.4 * k)
        self.line(x, y1, x + 7 * k, y1, 1.4 * k)
        self.line(x, y2, x + 7 * k, y2, 1.4 * k)

    # ---- 渐强 / 渐弱 --------------------------------------------------
    def _draw_hairpins(self, qline, idpos, nb):
        k = self.k
        start = None
        for el in qline.elements:
            if not isinstance(el, NoteEl):
                continue
            if el.cresc and start is None:
                start = el
            if el.cresc_end and start is not None:
                self._hairpin(start.cresc, idpos.get(id(start)),
                              idpos.get(id(el)), nb, k)
                start = None

    def _hairpin(self, cresc, a, b, nb, k):
        if a is None or b is None:
            return
        d, plus = cresc
        x1, x2 = a[0], b[0]
        y = nb - (30 + plus * 7) * k
        sp = 6 * k
        if d == "<":
            pts = [(x1, y), (x2, y - sp), (x2, y + sp), (x1, y)]
        else:
            pts = [(x1, y - sp), (x2, y), (x1, y + sp)]
        self.dwg.add(self.dwg.polyline(points=pts, fill="none",
                                       stroke="#1b1b1b",
                                       stroke_width="%.2fpx" % (1.1 * k)))

    # ---- 连音线 -------------------------------------------------------
    def _draw_ties(self, elements, idpos, k):
        stack = []
        for el in elements:
            if not isinstance(el, NoteEl):
                continue
            for _ in range(el.opens):
                stack.append(el)
            for _ in range(el.closes):
                if stack:
                    a = stack.pop()
                    if id(a) in idpos and id(el) in idpos:
                        self._tie(idpos[id(a)], idpos[id(el)], len(stack),
                                  a.multi, k)

    def _tie(self, a, b, level, multi, k):
        x1, y1 = a
        x2, y2 = b
        y = min(y1, y2) - (17 + level * 7) * k
        mid = (x1 + x2) / 2.0
        h = 7 * k
        d = "M %.2f %.2f Q %.2f %.2f %.2f %.2f" % (x1, y, mid, y - h, x2, y)
        self.dwg.add(self.dwg.path(d=d, fill="none", stroke="#1b1b1b",
                                   stroke_width="%.2fpx" % (1.2 * k)))
        if multi:
            n = int(abs(x2 - x1) / max(k, 1)) + 1
            self.text(str(n), mid, y - 5 * k, SMALL * k, family=FONT_NOTE)

    # ---- 跳房子 -------------------------------------------------------
    def _draw_houses(self, qlines, mstart, mend, nmeas):
        k = self.k
        open_bar = None
        xa = 0.0
        for m in range(nmeas):
            bar = None
            for q in qlines:
                mm = q.measures
                if m < len(mm) and mm[m][1] is not None:
                    bar = mm[m][1]
                    break
            if bar is None:
                continue
            # 先结束上一个跳房子, 再开启新的 (同一小节线可能既是终点又是起点)
            if bar.house_close and open_bar is not None:
                xb = mend[m + 1]
                y = self.y + (6 - open_bar.plus * 6) * k
                self.line(xa, y, xb, y, 1.2 * k)
                self.line(xa, y, xa, y + 14 * k, 1.2 * k)
                if not (bar.house_unclosed or open_bar.house_unclosed):
                    self.line(xb, y, xb, y + 14 * k, 1.2 * k)
                if open_bar.house_text:
                    self.text(open_bar.house_text + ".", xa + 12 * k,
                              y - 3 * k, SMALL * k, anchor="start",
                              family=FONT_SANS)
                open_bar = None
            if bar.house_open:
                open_bar = bar
                xa = mend[m + 1]


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
    total = len(renderer.pages)
    svgs = []
    for i in range(total):
        svgs.append(renderer._save_page(i, total))
    if fmt != "svg":
        for svg in svgs:
            out = svg[:-4] + "." + fmt
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
