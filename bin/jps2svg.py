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
HOUSE_PAD = 30                      # 跳房子横线距音符行的高度 (参考实现实测)
HOUSE_STEP = 10                     # "[+" 每多一个 "+", 横线再抬高这么多
HOUSE_CLEAR = 16                    # 横线与音符上方内容(连音线等)的最小间隙
HOUSE_RESERVE = 12                  # 有跳房子时行首额外让出的高度
HOUSE_CLAMP = 25                    # 横线距上一行内容底部的最小距离
HOUSE_TICK = 10                     # 跳房子两端竖线下垂高度
HOUSE_GAP = 4                       # 相邻跳房子共用一个端点时, 两根竖线间的间隙

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
FUDIAN_R = 2.4                      # 附点半径 (参考实现约 5px 直径, 比八度点大)
FUDIAN_Y = 0.5                      # 附点与音符行的垂直偏移 (数字竖直中心)
FUDIAN_UNIT = 0.5                   # 附点音符后面的额外间距系数 (让开附点)
ACC_UNIT = 0.2                      # 升降号左右各占的间距系数
GRACE_UNIT = 0.28                   # 每个倚音占用的间距系数

VOICE_GAP = 78                      # 同一组内相邻声部的基线间距
LYRIC_OFF = 44                      # 最后一声部基线 -> 首行歌词基线

# 音符字体风格: 番茄简谱用 config.js 的 shuzi_font 选择 b/c 两种音符字形,
# 两者字身高度不同, 所以纵向行距与歌词位置也不同 (脚本本身未记录该设置)。
# 实测两套参考 SVG 的参数:
#   b: 声部行距 78, 首行歌词占 28, 歌词基线 = 音符行 + 44
#   c: 声部行距 68, 首行歌词占 25, 歌词基线 = 音符行 + 41
NOTE_STYLES = {
    "b": {"vgap": 78.0, "lead": 28.0, "off": 44.0},
    "c": {"vgap": 68.0, "lead": 25.0, "off": 41.0},
}
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
UNIT_PAI = 1.0                      # 临时拍号占用的小节内宽度
TEMP_DY = 28                        # 临时多声部 {dsb}: 主声部下移 / 临时声部上移
TEMP_DY_BZ = 40                     # 临时伴奏 {bz}: 临时声部在主声部上方的高度
TEMP_SCALE_BZ = 0.78                # 临时伴奏 {bz} 的数字缩小比例 (参考实现小号数字)
TEMP_PAD = 20                       # 临时多声部: 花括号两侧的固定留白
SBF_PAD = 20                        # &sbf 行内多声部: 小节线后再让出的空间

# 跨行连音线: 两端各画半段弧线, 行末那半再用平线连到右边界
TIE_HALF_W = 11.7                   # 半段弧线的水平宽度
TIE_HALF_TOP = 24.9                 # 弧顶离音符行的高度

# 倚音 (参考实现用预组合的 qy 字形, 实测参数):
#   最右侧倚音数字中心 = 主音符中心 - 12; 组内相邻数字间距 7
#   数字基线 = 音符基线 - 14, 字号约 12.5
#   减时线 = 1 + 该组最多的 '/' 数, 位于基线下方 10.5/8.5/6.5
GRACE_DX = 12.0                     # 倚音数字中心到主音符中心的距离
GRACE_STEP = 7.0                    # 组内相邻倚音的间距
GRACE_SIZE = 12.5                   # 倚音数字字号
GRACE_BASE = 14.0                   # 倚音数字基线抬高量
GRACE_UNDER = 3.5                   # 减时线相对数字中心的半宽


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
        self.tup = 1.0             # 连音(多连音)重新分配时值的系数
        self.tup_grp = None        # 所属连音组 (相邻同组音符之间排紧)
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
        self.temps = []            # 临时伴奏/多声部块 TempBlock

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


class TempBlock(object):
    """{bz ...} / {dsb ...} 临时伴奏 / 临时多声部块。

    块内的音符与主旋律同时进行, 从主旋律的第 m0 个小节开始, 依次占用
    后续小节 (小节数 = 块内的小节数)。参考实现把这些小节展开成上下两行:
    临时声部在上, 主声部在下, 整段左右各画一个花括号。
    """

    def __init__(self, kind="", m0=0, elements=None):
        self.kind = kind
        self.m0 = m0
        self.elements = elements or []

    @property
    def measures(self):
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
        # 块首/块尾的空片段不算小节 (如 `{ | 6, - }`)
        while result and not result[0][0]:
            result.pop(0)
        while result and not result[-1][0]:
            result.pop()
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
                el.grace_back.append(parse_graces(grp[2:-1]))
            else:
                el.grace_front.append(parse_graces(grp[1:-1]))
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
    """解析 {bz ...} / {dsb ...} 临时伴奏/多声部块, 返回 (类型, 元素列表)。"""
    body = tok[1:-1] if tok.endswith("}") else tok[1:]
    body = body.strip()
    kind = ""
    for prefix in ("bz", "dsb"):
        if body.startswith(prefix):
            kind = prefix
            body = body[len(prefix):]
            break
    els = []
    for k, t in tokenize(body):
        if k == "bar":
            els.append(parse_bar(t))
        elif k == "note":
            els.append(parse_note(t))
    return kind, merge_elements(els)


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


def _fit_tuplet(seg):
    """把一段连音音符的时值缩到 "不超过原总时值的 2 的幂"。

    参考实现自动计算连音, 3 个八分连音占 1 拍 (每个 1/3 拍), 而不是 1.5 拍。
    """
    total = sum(duration(n) for n in seg)
    if total <= 0:
        return
    target = 1.0
    while target > total + 1e-9:
        target /= 2.0
    while target * 2 <= total + 1e-9:
        target *= 2.0
    if target >= total - 1e-9:
        return
    f = target / total
    for n in seg:
        n.tup *= f
        n.tup_grp = seg


def apply_tuplets(elements):
    """按行处理连音/多连音: 括号内的音符重新分配时值。

    行末还没有右括弧的连音按参考实现的做法收口到本行最后一个音符。
    """
    stack = []
    notes = []
    for el in elements:
        if not isinstance(el, NoteEl):
            continue
        idx = len(notes)
        notes.append(el)
        for _ in range(el.opens):
            stack.append((idx, el))
        for _ in range(el.closes):
            if stack:
                i, a = stack.pop()
                if a.multi and idx > i:
                    _fit_tuplet(notes[i:idx + 1])
    while stack:
        i, a = stack.pop()
        if a.multi and len(notes) - 1 > i:
            _fit_tuplet(notes[i:])


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
    group_numbered = False          # 当前 system 的声部行是否用的是编号写法

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
            numbered = bool(m.group(2))
            voice = int(m.group(2)) if numbered else 1
            name = m.group(4) or ""
            body = m.group(5)
            if kind == "Q":
                # 单声部行 ("Q:") 自成一行; 多声部行 ("Q1:","Q2:") 才合并成
                # 一个 system。已编号的声部行只有在前一行是歌词行、且编号回退
                # 时, 才开启新的 system (声部编号重新从 1 开始)。
                if qgroup:
                    if not numbered or not group_numbered:
                        flush_group()
                    elif (prev_kind == "C"
                          and voice <= max(v.voice for v in qgroup)):
                        flush_group()
                q = QLine(voice, name)
                raw = []
                for kind2, tok in tokenize(body):
                    if kind2 == "bar":
                        raw.append(parse_bar(tok))
                    elif kind2 == "block":
                        bkind, bels = parse_block(tok)
                        # 块从当前小节开始 (之前已结束的小节数)
                        m0 = len([e for e in raw if isinstance(e, BarEl)])
                        q.temps.append(TempBlock(bkind, m0, bels))
                    else:
                        raw.append(parse_note(tok))
                q.elements = merge_elements(raw)
                apply_tuplets(q.elements)
                for tb in q.temps:
                    apply_tuplets(tb.elements)
                qgroup.append(q)
                order.append(("Q", q))
                group_numbered = numbered
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
    return d * getattr(el, "tup", 1.0)


def text_width(s, size):
    """估算一段文字的宽度 (CJK 按全角, 其余按 0.55 倍字号)。"""
    w = 0.0
    for ch in s:
        w += size if ord(ch) > 0x2E7F else size * 0.55
    return w


def parse_graces(inner):
    """把 "[..]" 里的倚音串拆成 [(数字, 八度, 减时线数), ...]。"""
    out = []
    i, n = 0, len(inner)
    while i < n:
        c = inner[i]
        if not c.isdigit():
            i += 1
            continue
        octv = 0
        slashes = 0
        j = i + 1
        while j < n and inner[j] in ",'":
            octv += 1 if inner[j] == "'" else -1
            j += 1
        while j < n and inner[j] == "/":
            slashes += 1
            j += 1
        while j < n and inner[j] in ",'":
            octv += 1 if inner[j] == "'" else -1
            j += 1
        out.append((c, octv, slashes))
        i = j
    return out


def coef(a, b, onset_a=None, onset_b=None):
    """两个相邻元素之间的时值系数 (相对单位宽度)。

    参考实现的间距是 2 个基准单位(同一拍内)/ 3 个单位(跨拍) / 2.8 个单位
    (小节线两侧); 本模块的系数取它的一半, 即 1 / 1.5 / 1.4。
    附点在数字右侧额外占 1 个单位(0.5), 升降号左右各占 0.4 个单位(0.2)。
    """
    if isinstance(a, BarEl):
        return UNIT_BAR + ACC_UNIT * bool(getattr(b, "acc", ""))
    if isinstance(b, BarEl):
        return UNIT_BAR + ACC_UNIT * bool(getattr(a, "acc", ""))
    # 附点本身占一点宽度: 后面的音符要让开, 否则附点会压到下一个数字上
    # (同一拍里的附点八分 + 十六分本来只有 1 个单位宽, 附点必然重叠)。
    extra = FUDIAN_UNIT if getattr(a, "dots", 0) else 0.0
    # 升降号画在数字左侧, 但它占的宽度平均分到前后两个间距里
    extra += ACC_UNIT * (bool(getattr(a, "acc", ""))
                         + bool(getattr(b, "acc", "")))
    same_beat = (onset_a is not None and onset_b is not None
                 and int(onset_a) == int(onset_b))
    # 连音(多连音)内部: 音符挤在同一拍里, 和同一拍的减时线音符一样紧挨着
    ga = getattr(a, "tup_grp", None)
    in_tuplet = ga is not None and ga is getattr(b, "tup_grp", None)
    if in_tuplet or (same_beat and a.slashes > 0 and b.slashes > 0):
        return UNIT_SAME_BEAM + extra
    # 倚音需要额外空间: 参考实现每个倚音占 0.56 个基准单位(0.28)
    n_grace = sum(len(g) for g in b.grace_front)
    return UNIT_SHORT + extra + GRACE_UNIT * n_grace


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
class Renderer(object):
    def __init__(self, header, note_font="b"):
        self.header = header
        st = NOTE_STYLES.get(note_font, NOTE_STYLES["b"])
        self.vgap = st["vgap"] * 1.0
        self.style_lead = st["lead"]
        self.style_tight = (note_font == "c")
        self.lyric_off = st["off"]
        self.page_w, self.page_h = PAGE_SIZES.get(
            header.page, PAGE_SIZES["A4"])
        self.k = self.page_w / REF_W        # 相对 A4 的缩放系数
        self.pages = []
        self.page_index = -1
        self._last_bottom = 0.0
        self._outfile = "out.svg"
        self.y = 0.0
        self._first_base = 0.0
        self._pai_dx = 0.0              # 临时拍号相对小节线的水平偏移
        self._prev_u = None             # 上一行用过的单位宽度
        self._tie_carry = {}            # id(qline) -> [行首跨行弧线, 行末跨行弧线]

    # ---- 底层绘制辅助 -------------------------------------------------
    @staticmethod
    def _svg_text(dwg, s, x, y, size, anchor="middle", family=FONT_NOTE,
                  fill="#1b1b1b", weight="normal", style="normal"):
        dwg.add(svgwrite.text.Text(
            s, insert=(x, y), font_size="%.2fpx" % size, font_family=family,
            text_anchor=anchor, fill=fill, font_weight=weight,
            font_style=style))

    def text(self, s, x, y, size, anchor="middle", family=FONT_NOTE,
             fill="#1b1b1b", weight="normal", style="normal"):
        self._svg_text(self.dwg, s, x, y, size, anchor, family, fill,
                       weight, style)

    def line(self, x1, y1, x2, y2, width=1.5):
        self.dwg.add(svgwrite.shapes.Line(
            start=(x1, y1), end=(x2, y2), stroke="#1b1b1b",
            stroke_width="%.2fpx" % width, stroke_linecap="round"))

    def dot(self, x, y, r=None):
        if r is None:
            r = DOT_R * self.k
        self.dwg.add(svgwrite.shapes.Circle(
            center=(x, y), r="%.2fpx" % r, fill="#1b1b1b"))

    # ---- 主入口 -------------------------------------------------------
    def render(self, items, outfile):
        self._outfile = outfile
        self._new_page()
        bottom = self._render_header()
        self._page_top = MARGIN * self.k
        self._first_base = bottom + FIRST_GAP * self.k
        self.y = self._first_base
        self._last_bottom = 0.0
        # 横向疏密状态 (见 _render_system): None 表示用初始单位宽度。
        self._unit_state = None
        self._tie_carry = self._tie_carries(items)
        last_sys = None
        for i, it in enumerate(items):
            if it[0] != "pagebreak":
                last_sys = i
        for i, item in enumerate(items):
            if item[0] == "pagebreak":
                self._new_page()
                self.y = self._page_top
            else:
                self._render_system(item[1], item[2], item[3],
                                    last=(i == last_sys))
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
        # 参考实现 (bin/jianpu.py) 不会因为描述头里多出空行/空 B:/Z: 行而
        # 上提整块描述头: 空行只是让作者块/标题块多占一行, 谱行起点不变。
        dy = 0.0
        if h.titles:
            self.text(h.titles[0], self.page_w / 2.0, (TITLE_Y + dy) * k,
                      TITLE_SIZE * k, family=FONT_SANS, weight="bold")
            for i, t in enumerate(h.titles[1:], 1):
                self.text(t, self.page_w / 2.0,
                          (SUB_Y + dy + (i - 1) * 26) * k, SUB_SIZE * k,
                          family=FONT_SANS)
        has_j = bool(h.tempo)
        bottom = ((AUTH_BOTTOM_J if has_j else AUTH_BOTTOM) + dy) * k
        na = len(h.authors)
        for i, a in enumerate(h.authors):
            y = bottom - (AUTH_GAP * (na - 1 - i) + 3) * k
            self.text(a, MUSIC_R * k, y, AUTH_SIZE * k, anchor="end",
                      family=FONT_SANS)
        # 调号
        if h.key:
            acc = {"#": "\u266f", "$": "\u266d"}.get(h.key_acc, "")
            self.text("1=", MARGIN * k, (KEY_Y + dy) * k, KEY_SIZE * k,
                      anchor="start", family=FONT_NOTE, weight="bold")
            x = MARGIN * k + 36 * k
            if acc:
                self.text(acc, MARGIN * k + 30 * k, (KEY_Y - 10 + dy) * k,
                          KEY_SIZE * k * 0.62, anchor="start", family=FONT_NOTE)
            self.text(h.key, x, (KEY_Y + dy) * k, KEY_SIZE * k,
                      anchor="start", family=FONT_NOTE, weight="bold")
        # 拍号
        if h.pai and "/" in h.pai:
            num, den = h.pai.split("/", 1)
            px = MARGIN * k + 64 * k
            self.text(num, px, (KEY_Y - 17 + dy) * k, PAI_SIZE * k,
                      family=FONT_NOTE, weight="bold")
            self.text(den, px, (KEY_Y + 8 + dy) * k, PAI_SIZE * k,
                      family=FONT_NOTE, weight="bold")
            self.line(px - 8 * k, (KEY_Y - 11 + dy) * k, px + 8 * k,
                      (KEY_Y - 11 + dy) * k, 1.0 * k)
            if h.pai_aux:
                self.text(h.pai_aux, px + 13 * k, (KEY_Y + dy) * k,
                          SMALL * k, anchor="start", family=FONT_SANS)
        # 速度
        if h.tempo:
            if h.tempo_num:
                self.text("\u2669= %s" % h.tempo_num, MARGIN * k,
                          (TEMPO_Y + dy) * k, TEMPO_SIZE * k, anchor="start",
                          family=FONT_SANS)
            else:
                self.text(h.tempo, MARGIN * k, (TEMPO_Y + dy) * k,
                          TEMPO_SIZE * k,
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
        if notes and getattr(notes[-1], "acc", ""):
            tail += ACC_UNIT
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

    def _render_system(self, qlines, lyrics, order=None, last=False):
        k = self.k
        voices = [q.measures for q in qlines]
        nmeas = max(len(v) for v in voices) if voices else 0
        if nmeas == 0:
            return
        nv = len(qlines)

        # 临时伴奏 / 临时多声部 ({bz ...} / {dsb ...}): 与主旋律同时进行,
        # 从主旋律的第 m0 个小节开始占用若干小节。参考实现把这些小节展开
        # 成上下两行 (临时声部在上, 主声部在下), 整段左右各加花括号。
        temps = []                      # [{vi, m0, n, meas, coefs}, ...]
        for vi, q in enumerate(qlines):
            for tb in getattr(q, "temps", ()):
                tm = tb.measures
                if not tm or tb.m0 >= nmeas:
                    continue
                tm = tm[:nmeas - tb.m0]
                temps.append({"vi": vi, "m0": tb.m0, "n": len(tm), "meas": tm,
                              "block": tb,
                              "coefs": [self._measure_coefs(n, b, False)
                                        for (n, b) in tm]})

        has_dyn = any(isinstance(el, NoteEl) and el.dynamics
                      for q in qlines for el in q.elements)
        house_plus = 0
        has_house = False
        for q in qlines:
            for el in q.elements:
                if isinstance(el, BarEl) and el.house_open:
                    has_house = True
                    house_plus = max(house_plus, el.plus)
        at_top = (abs(self.y - self._first_base) < 0.1
                  or abs(self.y - self._page_top) < 0.1)

        # 跳房子横线高度 = 音符行上方 max(HOUSE_PAD + HOUSE_STEP*"+" 的个数,
        # 本行上方内容(连音线弧顶/八度点/力度/记号)的最高点 + HOUSE_CLEAR)。
        # 排版时内容整体下移, 让横线正好落在上一行内容底部之下 HOUSE_CLAMP
        # 处, 而不是把横线压下来, 于是横线与连音线之间始终留有间隙
        # (参考 006 末行: 横线 584、音符行 624, 连音线弧顶上方约 16px)。
        base_off = HOUSE_PAD + HOUSE_STEP * house_plus
        content_off = 0.0
        if has_house:
            for q in qlines[:1]:
                content_off = max(content_off,
                                  self._row_headroom(q) + HOUSE_CLEAR)
        house_off = max(base_off, content_off)

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
        # 只有歌词与声部交错 (多声部谱) 时, 首行歌词才"贴"进声部行距里;
        # 单声部下方连着几行歌词时, 首行与其余行同样按 LYRIC_LINE 递进
        # (参考实现实测 015/016: 4 行/2 行歌词的簇高都是 78+28*n)。
        tight = interleaved
        if self.style_tight:
            ly_lead = self.style_lead
        else:
            ly_lead = LYRIC_LEAD if tight else LYRIC_LINE
        sys_h = sum((self.vgap if c[0] is not None else 0.0)
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
                cur += self.vgap * k
            if clys:
                b = max(b, nb_rel + (self.lyric_off + (len(clys) - 1) * LYRIC_LINE
                                     + LYRIC_SIZE * 0.5) * k)
                cur += (ly_lead + LYRIC_LINE * (len(clys) - 1)) * k
            sys_bottom = max(sys_bottom, b - self.y)

        pad = (HOUSE_RESERVE * k) if has_house \
            else ((DYN_PAD * k) if (has_dyn and at_top) else 0.0)
        if has_house and not at_top:
            # 有跳房子: 内容整体下移, 让横线正好落在上一行内容底部之下
            # HOUSE_CLAMP 处, 空出 house_off 的高度给横线和上方内容。
            pad = max(pad, (self._last_bottom
                            + (HOUSE_CLAMP + house_off) * k) - self.y)
        # 参考实现只在源码里的分页符处换页, 不按版心高度自动折行; 这里仅当
        # 内容会超出物理页面时兜底另起一页。
        if self.y + pad + sys_bottom > self.page_h - 20 * k:
            self._new_page()
            self.y = self._page_top
            self._last_bottom = 0.0
            at_top = True
            pad = (HOUSE_RESERVE * k) if has_house else 0.0
        self.y += pad

        # 跳房子横线: 高度已在上面按本行上方内容算好 (house_off); 若受版面
        # 所限仍可能压到上一行, 再兜底压到上一行内容底部之下。
        house_y = None
        if has_house:
            house_y = self.y - house_off * k
            if not at_top:
                house_y = max(house_y, self._last_bottom + HOUSE_CLAMP * k)

        left = (MUSIC_L_MV if nv > 1 else MUSIC_L) * k
        # 声部名称占左侧空间: 参考实现把整行 (含声部括弧) 右移名称宽度
        if nv > 1:
            for q in qlines:
                if q.name:
                    left += text_width(q.name, LYRIC_SIZE) * k
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
            if any(m < len(voices[vi]) and voices[vi][m][0]
                   for vi in range(nv)):
                return True
            for t in temps:
                j = m - t["m0"]
                if 0 <= j < t["n"] and t["meas"][j][0]:
                    return True
            return False

        def pai_at(m):
            """第 m 小节起始小节线上挂着的临时拍号 (写在上一小节收尾小节线上)。"""
            if m <= 0:
                return None
            for vi in range(nv):
                vv = voices[vi]
                if m - 1 < len(vv) and vv[m - 1][1] is not None \
                        and vv[m - 1][1].pai:
                    return vv[m - 1][1].pai
            return None

        # 行首小节线: 谱行以 "|"(或 "|:" 等) 开头时, 第一个音符同样要让出
        # 小节线到音符的间距; 行首直接是音符时则紧贴左边距。
        lead_bar0 = None
        if voices and voices[0] and voices[0][0][1] is not None \
                and not has_notes(0):
            lead_bar0 = voices[0][0][1]

        def first_of(m):
            """第 m 小节的第一个音符 (跨声部查找)。"""
            for vi in range(nv):
                vv = voices[vi]
                if m < len(vv) and vv[m][0]:
                    return vv[m][0][0]
            for t in temps:
                j = m - t["m0"]
                if 0 <= j < t["n"] and t["meas"][j][0]:
                    return t["meas"][j][0][0]
            return None

        def lead_coef(m):
            # 小节线 -> 本小节第一个音符之间的系数。
            if m == 0:
                c = 0.0
            elif not has_notes(m - 1):
                # 上一小节没有音符: 只有行首小节线后面才让出间距
                c = (UNIT_BAR if (m - 1 == 0 and lead_bar0 is not None
                                  and not lead_bar0.hide) else 0.0)
            else:
                c = UNIT_BAR
            # 升降号画在数字左侧, 占的宽度分一半到前面的间距里
            first = first_of(m)
            if first is not None and getattr(first, "acc", ""):
                c += ACC_UNIT
            # 临时拍号紧贴小节线, 要让出写拍号的宽度, 后面的音符相应右移
            if pai_at(m):
                c += UNIT_PAI
            return c

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
            srcs = [vcoefs[vi][m] for vi in range(nv) if m < len(vcoefs[vi])]
            for t in temps:
                j = m - t["m0"]
                if 0 <= j < t["n"]:
                    srcs.append(t["coefs"][j])
            tl = sorted({o for (_cfs, ons) in srcs for o in ons})
            seq = []
            acc = 0.0
            for j, t in enumerate(tl):
                nxt = tl[j + 1] if j + 1 < len(tl) else None
                c = None
                for cfs, ons in srcs:
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
        # &sbf (行内才出现第二声部): 该小节线后面要画声部括线, 参考实现在
        # 小节线之后多留 SBF_PAD px, 否则括线会压在小节线上。
        sbf_m = None
        for vi in range(nv):
            for m in range(nmeas - 1):
                bar = voices[vi][m][1] if m < len(voices[vi]) else None
                if bar is not None and getattr(bar, "sbf", False):
                    if sbf_m is None or m + 1 < sbf_m:
                        sbf_m = m + 1
        sbf_pad = SBF_PAD * k if sbf_m is not None else 0.0
        # 临时多声部 {dsb..}: 区间两端各画一个花括号, 花括号所在的小节两侧
        # 各留 TEMP_PAD px 固定空白 (参考实现实测: 花括号不会紧贴音符)。
        # 临时伴奏 {bz..} 没有花括号, 也不留空白。
        pad_l = [0.0] * nmeas
        pad_r = [0.0] * nmeas
        for t in temps:
            if t["block"].kind != "dsb":
                continue
            m0, n = t["m0"], t["n"]
            end = m0 + n
            if end > nmeas:
                continue
            pad_l[m0] = TEMP_PAD * k
            if end < nmeas:
                pad_r[end - 1] = TEMP_PAD * k
        temp_pad = sum(pad_l) + sum(pad_r)
        available = right - left - sbf_pad - temp_pad
        fit_u = available / total
        # 参考实现的横向疏密: 有一个"当前单位宽度", 初始为 12.5px (本模块的
        # 系数是它的一半, 故为 25px)。谱行在当前单位下已经占到整行 63% 以上
        # 时, 把它撑满 (或压缩) 整行; 短行则沿用当前单位左对齐, 不拉伸。
        # 行末小节收尾的系数比小节线大, 判断是否拉伸时按小节线算 (与参考
        # 实现的计算方式一致)。
        model = total - (UNIT_END - UNIT_BAR if last else 0.0)
        u_default = 25.0 * k
        cur_u = getattr(self, "_unit_state", None) or u_default
        natural = model * cur_u + sbf_pad + temp_pad
        if natural >= 0.63 * available:
            u = fit_u
        else:
            u = min(cur_u, fit_u)
        self._unit_state = u
        # 临时拍号画在小节线右侧 UNIT_PAI 个单位宽处 (参考实现实测)
        self._pai_dx = UNIT_PAI * u

        # 小节起点 (= 上一个小节末尾的小节线位置); sbf_m 之后整体右移
        mstart = []
        cpad = []                       # 小节内容相对小节线的固定偏移
        x = left
        for m in range(nmeas):
            mstart.append(x + (sbf_pad if sbf_m is not None and m > sbf_m
                               else 0.0))
            cpad.append(pad_l[m])
            x += grid[m] * u + pad_l[m] + pad_r[m]
        mend = mstart + [x + sbf_pad]

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
                    px = (mstart[m] + cpad[m]
                          + (lead + cum_lookup(gcum[m], o)) * u)
                    if sbf_m is not None and m == sbf_m:
                        px += sbf_pad
                    lst.append((px, el))
                pv.append(lst)
                bv.append((mend[m + 1], bar))
            pos.append(pv)
            barx.append(bv)

        # 临时声部的音符位置: 与主声部共用同一小节起点和时间网格
        tpos = []                       # tpos[ti] = [(x, el, m), ...]
        for t in temps:
            lst = []
            for j, (notes, bar) in enumerate(t["meas"]):
                m = t["m0"] + j
                onsets = t["coefs"][j][1]
                lead = lead_coef(m)
                for i, el in enumerate(notes):
                    o = onsets[i] if i < len(onsets) else 0.0
                    lst.append((mstart[m] + cpad[m]
                                + (lead + cum_lookup(gcum[m], o)) * u,
                                el, m))
            tpos.append(lst)

        def span_dy(vi, m):
            """该小节里主声部要下移的距离 (0 表示不下移)。

            临时多声部 {dsb..} 是把两行并排写成一组: 临时声部在上方
            (基线 - TEMP_DY), 主声部压到下方 (基线 + TEMP_DY)。临时伴奏
            {bz..} 只是在上方多一行, 主声部仍在基线上 (参考实现实测)。
            """
            for t in temps:
                if (t["vi"] == vi and t["block"].kind == "dsb"
                        and t["m0"] <= m < t["m0"] + t["n"]):
                    return TEMP_DY * k
            return 0.0

        def span_any(vi, m):
            """该小节是否在某个临时声部区间内 (不论类型)。"""
            for t in temps:
                if t["vi"] == vi and t["m0"] <= m < t["m0"] + t["n"]:
                    return True
            return False

        def temp_up(vi, m=None):
            """临时声部那一行相对主声部基线的上移量 (0 表示没有)。

            m 为 None 时取该声部所有临时块的最大值。
            """
            up = 0.0
            for t in temps:
                if t["vi"] != vi:
                    continue
                if m is not None and not (t["m0"] <= m < t["m0"] + t["n"]):
                    continue
                up = max(up, (TEMP_DY if t["block"].kind == "dsb"
                              else TEMP_DY_BZ) * k)
            return up

        def span_end(vi, m):
            for t in temps:
                if t["vi"] == vi and m == t["m0"] + t["n"] - 1:
                    return True
            return False

        # --- 3. 按"簇"自上而下定位声部行与歌词行 ---
        #
        # 每个簇占 声部行 + 首行歌词(LYRIC_LEAD/LYRIC_LINE) + 其余歌词行的
        # 高度; 歌词行基线 = 所属声部基线 + LYRIC_OFF + j*LYRIC_LINE。
        vy = {}                         # id(qline) -> 音符行基线 y
        lyric_y = {}                    # id(lyric) -> 歌词行基线 y
        lyric_anchor = {}               # id(lyric) -> 所属声部序号
        cursor = self.y
        for q, lys in clusters:
            up = 0.0                    # 本簇上方要留出临时声部那一行的高度
            down = False                # 主声部整簇下移 (只有 {dsb} 会)
            if q is not None:
                # 带临时伴奏/多声部的声部: 临时声部在基线上方一行, 簇先让出
                # 那一行的高度; {dsb} 的主声部还要压到下方一行。
                vi0 = qindex.get(id(q))
                up = temp_up(vi0)
                down = any(t["vi"] == vi0 and t["block"].kind == "dsb"
                           for t in temps)
                if up:
                    cursor += up
                vy[id(q)] = cursor
                cursor += self.vgap * k
                if down and not lys:
                    # 没有歌词时, 主声部下方那一行也要占簇高
                    cursor += TEMP_DY * k
            nb = vy.get(id(q)) if q is not None else self.y
            for j, ly in enumerate(lys):
                lyric_y[id(ly)] = nb + (self.lyric_off + j * LYRIC_LINE
                                        + (TEMP_DY if down else 0)) * k
                lyric_anchor[id(ly)] = qindex.get(id(q), 0)
            if lys:
                cursor += (ly_lead + LYRIC_LINE * (len(lys) - 1)) * k
                if down:                    # 歌词已随声部下移, 簇也相应加高
                    cursor += TEMP_DY * k

        # --- 4. 绘制各声部 ---
        slots = []                      # slots[vi] = [(x, y_nb, is_lyric)]
        idpos = {}                      # id(el) -> (x, y_nb)
        vtop = None
        vbot = None
        for vi, q in enumerate(qlines):
            nb = vy.get(id(q), self.y + vi * self.vgap * k)
            vtop = nb if vtop is None else min(vtop, nb)
            vbot = nb if vbot is None else max(vbot, nb)
            if nv > 1 and q.name:
                self.text(q.name, left - 35 * k, nb + 6 * k,
                          LYRIC_SIZE * k, anchor="end", family=FONT_SANS)
            sl = []
            for m in range(nmeas):
                dy = span_dy(vi, m)
                for (px, el) in pos[vi][m]:
                    self._draw_note(el, px, nb + dy)
                    idpos[id(el)] = (px, nb + dy)
                    if el.is_lyric_slot:
                        sl.append((px, nb + dy))
                self._draw_underlines(pos[vi][m], nb + dy,
                                      vcoefs[vi][m][1] if m < len(vcoefs[vi])
                                      else [])
            for m in range(nmeas):
                bx, bar = barx[vi][m]
                if bar is None:
                    continue
                self._draw_bar(bar, bx, nb + span_dy(vi, m))
                # 临时声部区间内部的小节线上下两行都要画; 区间收尾的小节线
                # 只画在主声部 (参考实现里临时声部那一行是空白的)。
                if span_any(vi, m) and not span_end(vi, m):
                    self._draw_bar(bar, bx, nb - temp_up(vi, m))
            slots.append(sl)
            self._draw_hairpins(q, idpos, nb)

        # --- 4b. 临时伴奏 / 临时多声部声部 ---
        for t, lst in zip(temps, tpos):
            tq = qlines[t["vi"]]
            up = (TEMP_DY if t["block"].kind == "dsb" else TEMP_DY_BZ) * k
            tnb = vy.get(id(tq), self.y) - up
            for (px, el, m) in lst:
                self._draw_note(el, px, tnb,
                                TEMP_SCALE_BZ if t["block"].kind == "bz" else 1.0)
                idpos[id(el)] = (px, tnb)
            for j, (notes, bar) in enumerate(t["meas"]):
                m = t["m0"] + j
                self._draw_underlines([(px, el) for (px, el, mm) in lst
                                       if mm == m], tnb, t["coefs"][j][1])
            self._draw_ties(t["block"].elements, idpos, k)

        # --- 5. 声部括弧 ---
        # 整行多声部: 括弧在行首; 行内才出现第二声部 (&sbf) 时, 括弧落在
        # 该音符处 (参考实现放在音符左 25.5px, 与行首括弧同一形状)。
        if nv > 1:
            sbf_bar = None
            for vi in range(nv):
                for m in range(nmeas):
                    bx, bar = barx[vi][m]
                    if bar is not None and getattr(bar, "sbf", False):
                        if sbf_bar is None or bx < sbf_bar:
                            sbf_bar = bx
            if sbf_bar is None:
                self._draw_voice_bracket(left, vtop, vbot, k)
            else:
                # 括弧落在第二个声部真正进入的那个音符左侧 25.5px
                xs = [px for vi in range(nv) for m in range(nmeas)
                      for (px, el) in pos[vi][m] if px > sbf_bar + 1]
                self._draw_voice_bracket(min(xs) if xs else sbf_bar,
                                         vtop, vbot, k)

        # --- 5b. 临时多声部的花括号 ---
        # 参考实现: 区间起点 (哪怕就在行首) 总画左花括号, 区间收尾正好落在
        # 行末时不画右花括号。花括号距区间两端的小节线 20px。
        for t in temps:
            if t["block"].kind != "dsb":
                continue
            nb = vy.get(id(qlines[t["vi"]]), self.y)
            m0, n = t["m0"], t["n"]
            end = m0 + n
            if end > nmeas:
                continue
            self._draw_curly(mstart[m0] + TEMP_PAD * k, nb, k, 1)
            if mend[end] < right - 0.5:
                self._draw_curly(mend[end] - TEMP_PAD * k, nb, k, -1)

        # --- 6. 连音线 ---
        for vi, q in enumerate(qlines):
            cin, cout = self._tie_carry.get(id(q), (0, 0))
            self._draw_ties(q.elements, idpos, k, cin, cout, right + 1 * k)

        # --- 7. 跳房子 ---
        self._draw_houses(qlines, mstart, mend, nmeas,
                          house_y if house_y is not None else self.y)

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

        self._last_bottom = max(self._last_bottom, self.y + sys_bottom)
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
    def _draw_note(self, el, cx, nb, scale=1.0):
        if scale != 1.0:
            # 缩小绘制 (临时伴奏 {bz..} 用的是参考实现的小号数字): 以音符
            # 锚点为中心整体缩放, 数字/八度点/附点/倚音一起缩小。
            g = self.dwg.g(transform=("translate(%.3f %.3f) scale(%.4f) "
                                      "translate(%.3f %.3f)"
                                      % (cx, nb, scale, -cx, -nb)))
            self.dwg.add(g)
            old = self.dwg
            self.dwg = g
            try:
                self._draw_note(el, cx, nb, 1.0)
            finally:
                self.dwg = old
            return
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
        # 附点: 与数字竖直中心对齐, 半径比八度点大 (参考实现实测量得)
        for i in range(el.dots):
            self.dot(cx + (FUDIAN_DX + i * 7) * k, nb + FUDIAN_Y * k,
                     FUDIAN_R * k)
        # 倚音 (每组最右侧音符中心距主音符 GRACE_DX, 多个组依次再让开)
        for i, g in enumerate(el.grace_front):
            self._draw_grace(g, cx - (GRACE_DX + i * 9) * k, nb, back=False)
        for i, g in enumerate(el.grace_back):
            self._draw_grace(g, cx + (GRACE_DX + i * 9) * k, nb, back=True)
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
            if a == "yc":               # 延长记号: 圆点 + 上方细弧
                self._yanchang(cx, nb)
            elif a == "bc":             # 保持音记号: 音符上方短横
                self.line(cx - 5 * k, nb - 14.1 * k,
                          cx + 5 * k, nb - 14.1 * k, 1.2 * k)
            elif a == "dy":
                self.dot(cx, y, 1.8 * k)
            elif a == "zy":
                self.text(">", cx, y, fs * 0.6, family=FONT_NOTE)
            elif a == "hx":
                self.text(",", cx, y, fs * 0.6, family=FONT_NOTE)
            elif a == "sby":            # 上波音记号: 音符上方的小波纹
                self._boyin(cx, nb)
            elif a == "zkh":            # 伴奏左括弧
                self._paren(cx - 14 * k, nb, -1)
            elif a == "ykh":            # 伴奏右括弧
                self._paren(cx + 14 * k, nb, 1)
            else:
                self.text(a, cx, y, SMALL * k,
                          family="'Times New Roman',serif", style="italic")

    def _yanchang(self, cx, nb):
        """延长记号 (&yc)。

        参考实现的 yanchang 字形: 一个小圆点, 上方一条由两段弧组成的细弧
        (形状像小叶子)。整体挂在音符行上方, 横向跨音符中心左右各 6.1px。
        """
        k = self.k
        x0 = cx - 6.097 * k
        x1 = cx + 6.097 * k
        y0 = nb - 14.0 * k              # 弧的两个端点
        d = ("M %.2f %.2f C %.2f %.2f %.2f %.2f %.2f %.2f "
             "C %.2f %.2f %.2f %.2f %.2f %.2f Z") % (
            x0, y0,
            cx - 2.439 * k, nb - 21.467 * k,
            cx + 2.439 * k, nb - 21.467 * k,
            x1, y0,
            cx + 2.439 * k, nb - 20.72 * k,
            cx - 2.439 * k, nb - 20.72 * k,
            x0, y0)
        self.dwg.add(self.dwg.path(d=d, fill="#1b1b1b", stroke="#1b1b1b",
                                   stroke_width="%.2fpx" % (1.0 * k)))
        self.dot(cx, nb - 15.2 * k, 1.2 * k)

    def _boyin(self, cx, nb):
        """上波音记号 (&sby)。

        参考实现的 boyinfu_shang1 字形: 一条宽约 15px、高约 7.5px 的细波纹
        (两个波峰), 横向以音符中心居中, 挂在音符行上方约 14~22px 处。
        """
        k = self.k
        pts = [(-7.5, 17.5), (-4.4, 20.3), (-0.7, 16.6),
               (1.9, 20.3), (5.5, 16.6), (7.5, 19.0)]
        d = "M %.2f %.2f " % (cx + pts[0][0] * k, nb - pts[0][1] * k) + \
            " ".join("L %.2f %.2f" % (cx + dx * k, nb - dy * k)
                     for dx, dy in pts[1:])
        self.dwg.add(self.dwg.path(d=d, fill="none", stroke="#1b1b1b",
                                   stroke_width="%.2fpx" % (2.7 * k),
                                   stroke_linecap="round",
                                   stroke_linejoin="round"))

    def _paren(self, x, nb, direc):
        """绘制伴奏大括弧 (&zkh / &ykh)。

        参考实现里括弧高约 18px (上下各约 9px), 孤形很浅; 这里按同样尺寸
        绘制, 使高度与行内音符一致。
        """
        k = self.k
        h = 9.1 * k                     # 半高 (总高 ≈ 18.2k)
        bulge = 3.2 * k * direc         # 弧度
        d = "M %.2f %.2f Q %.2f %.2f %.2f %.2f" % (
            x, nb - h, x + bulge, nb, x, nb + h)
        self.dwg.add(self.dwg.path(d=d, fill="none", stroke="#1b1b1b",
                                   stroke_width="%.2fpx" % (1.6 * k)))

    def _draw_grace(self, notes, cx, nb, back=False):
        """画一组倚音: 小号数字 + 减时线 + 八度点 + 指向主音符的小钩。"""
        if not notes:
            return
        k = self.k
        n = len(notes)
        step = GRACE_STEP * k
        # cx 是组内最靠近主音符的那个数字的中心
        if back:
            xs = [cx + i * step for i in range(n)]
        else:
            xs = sorted(cx - i * step for i in range(n))
        base = nb - GRACE_BASE * k
        size = GRACE_SIZE * k
        # 减时线: 1 + 组内最多 '/' 数, 自下而上排列
        lines = 1 + max(sl for _, _, sl in notes)
        left = xs[0] - GRACE_UNDER * k
        right = xs[-1] + GRACE_UNDER * k
        for j in range(lines):
            y = nb - (10.5 - 2 * j) * k
            self.line(left, y, right, y, 1.0 * k)
        for x, (digit, octv, _sl) in zip(xs, notes):
            self.text(digit, x, base, size, family=FONT_NOTE)
            for i in range(octv):
                self.dot(x, base - (10.5 + i * 6) * k, DOT_R * k * 0.85)
            for i in range(-octv):
                self.dot(x, base + (6.0 + i * 6) * k, DOT_R * k * 0.85)
        # 倚音小钩 (参考实现 yiyinxian_qian): 数字下方弯向主音符的小笔画
        gx = xs[-1] if not back else xs[0]
        sgn = 1.0 if not back else -1.0
        d = "M %.2f %.2f C %.2f %.2f %.2f %.2f %.2f %.2f" % (
            gx - 0.5 * k, nb - 9.0 * k,
            gx - 0.5 * k, nb - 9.0 * k,
            gx + sgn * 1.2 * k, nb - 5.0 * k,
            gx + sgn * 4.4 * k, nb - 4.4 * k)
        self.dwg.add(self.dwg.path(d=d, fill="none", stroke="#1b1b1b",
                                   stroke_width="%.2fpx" % (1.0 * k),
                                   stroke_linecap="round"))

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
            # 临时拍号: 与曲谱同高 (分数线落在音符行上), 画在小节线右侧,
            # 后面音符已因 lead_coef 让出的空间而右移。
            num, den = bar.pai.split("/", 1)
            fs = PAI_SIZE * k * 1.1
            px = cx + self._pai_dx
            self.text(num, px, nb - 10 * k + fs * 0.35, fs,
                      family=FONT_NOTE, weight="bold")
            self.text(den, px, nb + 10 * k + fs * 0.35, fs,
                      family=FONT_NOTE, weight="bold")
            self.line(px - 6.9 * k, nb, px + 6.9 * k, nb, 1.0 * k)
        _ = dots

    # ---- 声部括弧 -----------------------------------------------------
    def _draw_curly(self, x, y, k, face=1):
        """临时伴奏/多声部区间的花括号 (对应参考实现 dakuohu_zuo_2/_you_2)。

        face=1 为左花括号 "{" (尖端朝左), face=-1 为右花括号 "}"。上下各
        伸出 39.6px, 覆盖上下两行音符。
        """
        pts = [(5.4, -38.8), (1.5, -34.0), (0.6, -24.0), (0.6, -16.0),
               (0.6, -8.0), (-0.3, -3.0), (-3.5, -0.9), (-3.5, 0.9),
               (-0.3, 3.0), (0.6, 8.0), (0.6, 16.0), (0.6, 24.0),
               (1.5, 34.0), (5.4, 38.8)]
        p = [(x + px * face * k, y + py * k) for (px, py) in pts]
        d = ("M %.2f %.2f C %.2f %.2f %.2f %.2f %.2f %.2f "
             "C %.2f %.2f %.2f %.2f %.2f %.2f "
             "L %.2f %.2f "
             "C %.2f %.2f %.2f %.2f %.2f %.2f "
             "C %.2f %.2f %.2f %.2f %.2f %.2f") % tuple(
                 v for pt in p for v in pt)
        self.dwg.add(self.dwg.path(d=d, fill="none", stroke="#1b1b1b",
                                   stroke_width="%.2fpx" % (2 * k),
                                   stroke_linecap="round",
                                   stroke_linejoin="round"))

    def _draw_voice_bracket(self, left, top, bottom, k):
        """多声部左侧括线: 粗 4px + 细 2px 两根竖线, 两端再用一条斜线把
        两根竖线连起来 (参考实现的 shengbufu_shang / shengbufu_xia 字形)。

        粗线在音符左 25.5px、上下各超出 6.5px; 细线在左 21px、上下各超出
        8px。斜线起于细线右上方, 向左下方落到粗线顶端, 下端镜像。"""
        for dx, over, w in ((25.5, 6.5, 4.0), (21.0, 8.0, 2.0)):
            x = left - dx * k
            self.line(x, top - over * k, x, bottom + over * k, w * k)
        spec = ((left - 17.9 * k, top - 13.0 * k, 5.5, 7.4),
                (left - 17.75 * k, bottom + 13.4 * k, -5.72, -7.7))
        for x0, y0, cy, ey in spec:
            d = "M %.2f %.2f C %.2f %.2f %.2f %.2f %.2f %.2f" % (
                x0, y0, x0, y0, x0 - k, y0 + cy * k, x0 - 9.4 * k,
                y0 + ey * k)
            self.dwg.add(self.dwg.path(d=d, fill="none", stroke="#1b1b1b",
                                       stroke_width="%.2fpx" % (2 * k)))

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
        """渐强 (<) / 渐弱 (>) 楔形。

        参考实现用两条独立的开放式斜线 (从尖端出发), 右侧不封口; 范围从
        起音音符左边 7px 到收音音符右边 7px, 尖端在音符行上方 30px 处,
        开口半高 5px。
        """
        if a is None or b is None:
            return
        d, plus = cresc
        x1 = a[0] - 7 * k               # 起音音符左缘
        x2 = b[0] + 7 * k               # 收音音符右缘
        y = nb - (30 + plus * 7) * k
        sp = 5.0 * k
        if d == "<":
            ax, bx = x1, x2             # 尖端在左, 向右张开
        else:
            ax, bx = x2, x1             # 尖端在右, 向左张开
        self.line(ax, y, bx, y - sp, 1.0 * k)
        self.line(ax, y, bx, y + sp, 1.0 * k)

    # ---- 连音线 -------------------------------------------------------
    @staticmethod
    def _tie_lift(level, marked, hi):
        """连音线端点离音符行的高度 (参考系 px)。"""
        return 16 + 5 * (1 if hi else 0) + 7 * (1 if marked else 0) + 7 * level

    @staticmethod
    def _tie_top(level, marked, hi, multi):
        """连音线 (含弧顶, 必要时含线上数字) 在音符行上方的高度。"""
        h = Renderer._tie_lift(level, marked, hi) + 8      # 弧顶比端点高 8px
        if multi:
            h += 5 + SMALL * 0.78       # 连音线上标注的数字
        return h

    def _row_headroom(self, qline):
        """一个声部行自身内容在音符行上方能达到的最高高度 (参考系 px)。

        跳房子横线必须落在这些内容之上, 不能压到连音线/八度点/力度/渐强线。
        只统计确实会高过 HOUSE_PAD 的东西 (歌词、减时线都在行下方, 各种
        小记号最高约 22px, 参考实现也不会为它们抬高跳房子)。
        """
        top = 0.0
        for el in qline.elements:
            if not isinstance(el, NoteEl):
                continue
            if el.dynamics:
                top = max(top, 26 + SMALL * 0.78)      # 力度记号 (基线 -26)
            if el.octave > 0:                          # 高八度点
                top = max(top, OCT_HIGH + (el.octave - 1) * OCT_STEP)
            if el.cresc:                               # 渐强/渐弱线的上开口
                top = max(top, 30 + el.cresc[1] * 7 + 5)
        for a, b, level, multi, marked, hi, _n in self._tie_specs(
                qline.elements):
            top = max(top, self._tie_top(level, marked, hi, multi))
        return top

    @staticmethod
    def _tie_spec(a, b, level, count):
        # 音符上方若还有记号 (延长/保持/重音/顿音) 或高八度点, 连音线要让开;
        # 包住别的连音线时 (内层已收束) 也要更高
        marked = any(m in ("yc", "bc", "zy", "dy")
                     for n in (a, b) for m in n.articulations)
        hi = a.octave > 0 or b.octave > 0
        return (a, b, level, a.multi, marked, hi, count)

    def _tie_specs(self, elements):
        """按开/闭顺序把音符配成连音线。

        返回 [(起点元素, 终点元素, 嵌套层数, 是否标数字, 上方有记号,
        高八度, 连音包含的音符数)]。绘制和纵向占位计算共用这套配对逻辑。
        """
        specs = []
        stack = []                      # [序号, 起始元素, 内部已收束的连音线条数]
        notes = []
        for el in elements:
            if not isinstance(el, NoteEl):
                continue
            start = len(notes)
            notes.append(el)
            for _ in range(el.opens):
                stack.append([start, el, 0])
            for _ in range(el.closes):
                if stack:
                    i, a, inner = stack.pop()
                    if i == start:
                        # 只包住一个音符的连音线 (如 (3)) 参考实现不画,
                        # 也不算进外层连音线的嵌套层数
                        continue
                    specs.append(self._tie_spec(a, el, inner,
                                                len(notes) - i))
                    for s in stack:
                        s[2] += 1
        # 连音(多连音)到行末还没遇到右括弧时, 参考实现在本行最后一个音符处
        # 收口, 数字就是本行里连起来的音符数 (跨行的部分在下一行另画弧线,
        # 不再重复写数字)。
        while stack:
            i, a, inner = stack.pop()
            if a.multi and notes:
                specs.append(self._tie_spec(a, notes[-1], inner,
                                            len(notes) - i))
        return specs

    @staticmethod
    def _tie_scan(elements, depth=0):
        """统计一行的连音线开闭情况 (用于跨行连音线)。

        返回 (行末仍未收口的条数, 行首第一个音符就收口的条数)。``depth`` 是
        从上一行带过来的未收口条数, 这些连音线只能被行首的右括弧收掉, 因为
        新的左括弧会压在上面 (后开先闭)。
        """
        lead = None
        for el in elements:
            if not isinstance(el, NoteEl):
                continue
            if lead is None:
                lead = min(depth, el.closes)
            depth += el.opens - el.closes
            if depth < 0:
                depth = 0
        return depth, (lead or 0)

    def _tie_carries(self, items):
        """按声部把各行的连音线开闭串起来, 得出跨行连音线的两端。

        参考实现遇到跨行的连音线时, 在行末画一段上扬的弧线, 在下一行行首
        画一段落到音符上的弧线 (连音/多连音同样处理)。只有真正在下一行行首
        收口的连音线才画, 源码里漏写右括弧的不画。
        """
        info = {}
        per_voice = {}
        for it in items:
            if it[0] != "system":
                continue
            for vi, q in enumerate(it[1]):
                per_voice.setdefault(vi, []).append(q)
        for qs in per_voice.values():
            prev_q = None
            prev_depth = 0
            for q in qs:
                depth, lead = self._tie_scan(q.elements, prev_depth)
                info[id(q)] = [min(prev_depth, lead), 0]
                if prev_q is not None:
                    info[id(prev_q)][1] = min(prev_depth, lead)
                prev_q, prev_depth = q, depth
        return info

    def _draw_tie_half(self, x, y, k, start, right=None):
        """跨行连音线的半段: start=True 画行首那半 (从弧顶落到音符),
        否则画行末那半 (从音符上扬到弧顶, 再拉一条平线到行尾)。"""
        if start:
            ax, ay = x - TIE_HALF_W * k, y - TIE_HALF_TOP * k
            d = ("M %.2f %.2f C %.2f %.2f %.2f %.2f %.2f %.2f"
                 % (ax, ay, ax, ay, ax + 7.6 * k, ay + 0.6 * k,
                    x, y - 16 * k))
        else:
            ax, ay = x + TIE_HALF_W * k, y - TIE_HALF_TOP * k
            d = ("M %.2f %.2f C %.2f %.2f %.2f %.2f %.2f %.2f"
                 % (ax, ay, ax, ay, ax - 7.6 * k, ay + 0.6 * k,
                    x, y - 16 * k))
            if right is not None and right > ax:
                self.line(ax, ay, right, ay, 1.2 * k)
        self.dwg.add(self.dwg.path(d=d, fill="none", stroke="#1b1b1b",
                                   stroke_width="%.2fpx" % (1.2 * k)))

    def _draw_ties(self, elements, idpos, k, carry_in=0, carry_out=0,
                   right=None):
        for a, b, level, multi, marked, hi, n in self._tie_specs(elements):
            if id(a) in idpos and id(b) in idpos:
                self._tie(idpos[id(a)], idpos[id(b)], level,
                          multi, k, marked, hi, n)
        notes = [el for el in elements
                 if isinstance(el, NoteEl) and id(el) in idpos]
        if not notes:
            return
        for _ in range(carry_in):
            x, y = idpos[id(notes[0])]
            self._draw_tie_half(x, y, k, True)
        for _ in range(carry_out):
            x, y = idpos[id(notes[-1])]
            self._draw_tie_half(x, y, k, False, right)

    def _tie(self, a, b, level, multi, k, marked=False, hi=False, count=2):
        x1, y1 = a
        x2, y2 = b
        # 端点高度基准 16px (参考实现实测 16/24): 高八度点 +5, 上方有记号
        # +7, 每嵌套一层 +7; 弧顶再比端点高 8px。
        lift = self._tie_lift(level, marked, hi)
        y = min(y1, y2) - lift * k
        mid = (x1 + x2) / 2.0
        h = 16 * k
        d = "M %.2f %.2f Q %.2f %.2f %.2f %.2f" % (x1, y, mid, y - h, x2, y)
        self.dwg.add(self.dwg.path(d=d, fill="none", stroke="#1b1b1b",
                                   stroke_width="%.2fpx" % (1.2 * k)))
        if multi:
            # 连音数字: 写在本行音符数上, 位于弧顶 (参考实现用一个带白底的
            # 小号数字把弧线遮出一个缺口), 字号比八度点略大的小数字。
            ny = y - 7 * k
            half = 5 * k
            self.dwg.add(self.dwg.rect(insert=(mid - half, ny - half),
                                       size=(2 * half, 2 * half),
                                       fill="#ffffff"))
            self.text(str(count), mid, ny + 3.5 * k, 10 * k,
                      family=FONT_NOTE, weight="bold")

    # ---- 跳房子 -------------------------------------------------------
    def _draw_houses(self, qlines, mstart, mend, nmeas, y):
        """绘制跳房子 (反复跳跃记号)。

        横线高度由调用方按"音符行上方内容 + HOUSE_CLEAR"算好 (house_off),
        两端竖线下垂 HOUSE_TICK, 房号写在横线下方、左端稍右处。

        相邻两个跳房子共用一个端点 (前一个的终点小节线就是后一个的起点)
        时, 两根竖线各向自己一侧让出 HOUSE_GAP/2, 避免叠在一条线上。
        """
        k = self.k
        half = HOUSE_GAP * k / 2.0      # 相邻跳房子各让出的间隙
        open_bar = None
        xa = 0.0
        last_end = None                 # 上一个跳房子终点所用的小节线位置
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
                # 本条小节线上紧接着还要开启下一个跳房子 -> 终点左移
                xe = xb - (half if bar.house_open else 0.0)
                self.line(xa, y, xe, y, 1.2 * k)
                self.line(xa, y, xa, y + HOUSE_TICK * k, 1.2 * k)
                if not (bar.house_unclosed or open_bar.house_unclosed):
                    self.line(xe, y, xe, y + HOUSE_TICK * k, 1.2 * k)
                if open_bar.house_text:
                    self.text(open_bar.house_text, xa + 3 * k,
                              y + (HOUSE_TICK + 6) * k, SMALL * k,
                              anchor="start", family=FONT_SANS)
                open_bar = None
                last_end = xb
            if bar.house_open:
                open_bar = bar
                x0 = mend[m + 1]
                # 上一个跳房子刚在同一个位置结束 -> 起点右移, 两根竖线分开
                if last_end is not None and abs(x0 - last_end) < 1.0:
                    x0 += half
                xa = x0


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def convert(infile, outfile=None, fmt="svg", note_font="b"):
    text = open(infile, encoding="utf-8").read()
    header, items = parse_jps(text)
    if outfile:
        base = os.path.splitext(outfile)[0]
    else:
        base = os.path.splitext(infile)[0]
    svg_out = base + ".svg"
    renderer = Renderer(header, note_font)
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
    parser.add_argument("--note-font", metavar="b|c", default="b",
                        choices=["b", "c"],
                        help="音符字体风格 (影响纵向行距, 默认 b)")
    parser.add_argument("file", metavar="FILE", help="输入的 .jps 文件")
    args = parser.parse_args(argv)
    convert(args.file, args.output, args.format, args.note_font)
    return 0


if __name__ == "__main__":
    sys.exit(main())
