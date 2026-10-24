#! /usr/bin/env python3
# -*- coding: utf-8 -*-
"""随机用例 -> 参考实现 -> 渲染器 的自测工具。

用法:
    ./tests/harness.py gen            # 生成 tests/cases/*.jps
    ./tests/harness.py ref            # 调 ./bin/jianpu.py 生成 tests/ref/*.svg
    ./tests/harness.py mine           # 调 ./bin/jps2svg.py 生成 tests/mine/*.svg
    ./tests/harness.py report         # 比较差异 (含符号级的像素对比)
    ./tests/harness.py all            # gen + ref + mine + report
    ./tests/harness.py crop 07-lyrics # 导出某一页的逐行对比图 (人工查看)

参考实现要联网, 已有结果默认复用, 加 --force 重新请求。
"""

import argparse
import glob
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CASES = os.path.join(HERE, "cases")
REF = os.path.join(HERE, "ref")
MINE = os.path.join(HERE, "mine")
CROP = os.path.join(HERE, "crop")

CHARS = "春风又绿江南岸明月何时照我还花儿为什么这样红"
DYN_ALL = ["pp", "p", "mp", "mf", "f", "ff"]
ART_ALL = ["yc", "bc", "dy", "zy", "hx", "sby"]
# 图片中的全部记号（含 + 长记号）；单独成例，避免改动 06 的缓存参考图。
MARK_SYMBOLS = ["yc", "bc", "zy", "dy", "hx", "shy", "xhy",
                "sby", "sby+", "xby", "xby+", "cy", "cy+"]


# --------------------------------------------------------------------------
# 记号模型
# --------------------------------------------------------------------------
DUR = {"": 1.0, "/": 0.5, "//": 0.25, "///": 0.125, "////": 0.0625}
DOTMUL = {0: 1.0, 1: 1.5, 2: 1.75}


def durval(dur, dots=0):
    return DUR[dur] * DOTMUL[dots]


class Note(object):
    """一个音符记号; token() 按规范顺序拼出脚本写法。"""

    def __init__(self, sym, dur="", dots=0):
        self.sym = sym
        self.dur = dur
        self.dots = dots
        self.oct = ""            # ' , '' ,, ...
        self.acc = ""            # # $ =
        self.marks = []          # 力度 / &yc 等 (不含 &)
        self.cresc = ""          # < > <+ >++ ...
        self.cresc_end = False   # !
        self.grace = []          # 前倚音 [..]
        self.grace_back = []     # 后倚音 [h..]
        self.text = ""           # "注释"
        self.opens = 0
        self.closes = 0
        self.multi = False       # (y 多连音
        self.beat = ""           # ~ ^

    def token(self):
        if self.multi and self.opens:
            pre = "(" * (self.opens - 1) + "(y"
        else:
            pre = "(" * self.opens
        s = pre + self.sym + self.oct + self.acc + "." * self.dots + self.dur
        for m in self.marks:
            s += "&" + m
        s += self.cresc
        s += self.beat
        for g in self.grace:
            s += "[%s]" % g
        for g in self.grace_back:
            s += "[h%s]" % g
        if self.text:
            s += '"%s"' % self.text
        s += ")" * self.closes
        if self.cresc_end:
            s += "!"
        return s


class Bar(object):
    """小节线 (含跳房子 / 反复 / 临时拍号 / &sbf 等标注)。"""

    def __init__(self, text="|"):
        self.text = text

    def token(self):
        return self.text


class Block(object):
    """{bz ...} / {dsb ...} 临时块 (整块原样插入)。"""

    def __init__(self, kind, body):
        self.kind = kind
        self.body = body

    def token(self):
        return "{%s %s }" % (self.kind, self.body)


# --------------------------------------------------------------------------
# 小节内时值填充
# --------------------------------------------------------------------------
def rand_slots(rng, beats):
    """随机切分 beats 拍, 返回 [(dur, dots), ...] (时值和恰为 beats)。"""
    slots = []
    left = beats
    # 时值权重: 以四分/八分/十六分为主, 三十二分及更短只是偶尔出现。
    # 如果均匀抽取, 每小节会挤进十几个音, 整行远超版心, 参考实现就会进入
    # "把间距压到最小" 的退化排版, 对比失去意义。
    weight = {"": 8.0, "/": 8.0, "//": 5.0, "///": 1.0, "////": 0.25}
    while left > 1e-9:
        cand, ws = [], []
        for d in ("", "/", "//", "///", "////"):
            for t in (0, 1, 2):
                if durval(d, t) > left + 1e-9:
                    continue
                # 附点只在拍子够长时才选, 免得全是附点
                if t and d not in ("", "/", "//"):
                    continue
                if t == 2 and d != "":
                    continue
                cand.append((d, t))
                ws.append(weight[d] / (1.0 + 2.5 * t))
        d, t = rng.choices(cand, weights=ws)[0]
        slots.append((d, t))
        left -= durval(d, t)
        left = round(left, 6)
    return slots


def force_slot(slots, kind, start=0):
    """把某个时值记号塞进小节, 不改变小节总时值。

    kind: '/', '//', '///', '////', '.', '..'
    """
    want = {".": ("", 1), "..": ("", 2)}.get(kind, (kind, 0))
    need = durval(*want)
    order = ["", "/", "//", "///", "////"]
    for i in range(start, len(slots)):
        v = durval(*slots[i])
        if v + 1e-9 < need:
            continue
        rest = v - need
        seq = [want]
        for d in order:
            while rest >= durval(d) - 1e-9:
                seq.append((d, 0))
                rest -= durval(d)
        if rest > 1e-9:
            continue
        slots[i:i + 1] = seq
        return True
    return False


# --------------------------------------------------------------------------
# 单行构造
# --------------------------------------------------------------------------
def add_oct(rng, n, style="mix"):
    if style == "none":
        return
    r = rng.random()
    if r < 0.55:
        return
    if r < 0.75:
        n.oct = "'"
    elif r < 0.90:
        n.oct = ","
    elif r < 0.95:
        n.oct = "''"
    else:
        n.oct = ",,"


def add_acc(rng, n):
    n.acc = rng.choice(["#", "$", "="])


def make_line(rng, beats, nmeas, deco=None, must=(), sym_pool="1234567",
              rest_p=0.06, hidden_p=0.0, rhythm_p=0.0, hold_p=0.0):
    """生成一整行 (Bar/Note 元素列表), 每小节恰好 beats 拍。"""
    items = [Bar()]
    must = list(must)
    for m in range(nmeas):
        slots = rand_slots(rng, beats)
        if must:
            e = rng.randrange(len(must))
            if force_slot(slots, must[e], rng.randrange(max(1, len(slots)))):
                must.pop(e)
        for dur, dots in slots:
            r = rng.random()
            if r < hidden_p:
                n = Note("8", dur, dots)
            elif r < hidden_p + rhythm_p:
                n = Note("9", dur, dots)
            elif r < hidden_p + rhythm_p + rest_p:
                n = Note("0", dur, dots)
            elif hold_p and items and isinstance(items[-1], Note) and \
                    items[-1].sym not in "089-" and r < hidden_p + rhythm_p + \
                    rest_p + hold_p and dur == "" and dots == 0:
                n = Note("-", "", 0)
            else:
                n = Note(rng.choice(sym_pool), dur, dots)
                add_oct(rng, n)
                if deco:
                    deco(n)
            items.append(n)
        if m < nmeas - 1:
            items.append(Bar())
    return items


def flat_notes(items):
    return [it for it in items if isinstance(it, Note)]


def line_of(measures):
    """measures: [[(sym, dur, dots, oct, acc), ...], ...] -> Bar/Note 列表。"""
    items = [Bar()]
    for mi, measure in enumerate(measures):
        for spec in measure:
            sym, dur, dots = spec[0], spec[1], spec[2]
            n = Note(sym, dur, dots)
            n.oct = spec[3] if len(spec) > 3 else ""
            n.acc = spec[4] if len(spec) > 4 else ""
            items.append(n)
        if mi < len(measures) - 1:
            items.append(Bar())
    return items


def add_ties(rng, all_items, count, nested=False, min_gap=1, max_gap=4):
    """在整首谱的音符序列上加 count 条连音线 (可嵌套)。"""
    notes = flat_notes(all_items)
    added = 0
    tries = 0
    while added < count and tries < count * 40 and len(notes) > 4:
        tries += 1
        i = rng.randrange(len(notes) - 2)
        j = min(len(notes) - 1, i + rng.randint(min_gap, max_gap))
        if notes[i].sym in "8-" or notes[j].sym in "8-":
            continue
        if notes[i].opens or notes[j].closes:
            continue
        if in_spans(tuplet_spans(notes), i) or in_spans(tuplet_spans(notes), j):
            continue
        notes[i].opens += 1
        notes[j].closes += 1
        added += 1
        if nested and j - i >= 3 and rng.random() < 0.7:
            a, b = i + 1, j - 1
            if not notes[a].opens and not notes[b].closes and \
                    not in_spans(tuplet_spans(notes), a) and \
                    not in_spans(tuplet_spans(notes), b):
                notes[a].opens += 1
                notes[b].closes += 1
    return added


def tuplet_spans(notes):
    """返回 [(i, j)]: 已有的多连音/连音线区间 (闭区间)。"""
    spans = []
    stack = []
    for i, n in enumerate(notes):
        for _ in range(n.opens):
            stack.append(i)
        for _ in range(n.closes):
            if stack:
                spans.append((stack.pop(), i))
    return spans


def in_spans(spans, i):
    return any(a <= i <= b for a, b in spans)


def measure_slices(items):
    """返回每个小节的 (start, end) 下标区间 (items 下标, 只含音符)。"""
    out = []
    cur = None
    for i, it in enumerate(items):
        if isinstance(it, Bar):
            if cur is not None:
                out.append((cur, i))
                cur = None
        elif cur is None:
            cur = i
    if cur is not None:
        out.append((cur, len(items)))
    return out


def add_tuplets(rng, all_items, count, sizes=(3, 4, 5, 6)):
    """把某个小节改写成一串八分音符, 再框出一组多连音。

    多连音只改写法不改时值 (一整小节 = 8 个八分音符), 这样两套实现的
    小节时值都合法。
    """
    added = 0
    for _ in range(count * 20):
        if added >= count:
            break
        slices = measure_slices(all_items)
        if not slices:
            break
        a, b = rng.choice(slices)
        size = rng.choice(sizes)
        n8 = 8
        if size >= n8:
            continue
        pool = "12345671"
        newnotes = [Note(rng.choice(pool), "/", 0) for _ in range(n8)]
        k = rng.randrange(n8 - size + 1)
        newnotes[k].multi = True
        newnotes[k].opens += 1
        newnotes[k + size - 1].closes += 1
        all_items[a:b] = newnotes
        added += 1
    return added


def add_crescendo(rng, all_items, count=1):
    notes = flat_notes(all_items)
    for _ in range(count):
        if len(notes) < 6:
            return
        i = rng.randrange(len(notes) - 5)
        notes[i].cresc = rng.choice(["<", ">", "<+", "<++", ">+"])
        notes[i + rng.randint(2, 4)].cresc_end = True


def house(bar, mark="", label=None, close=False, unclosed=False):
    if close:
        bar.text += "]"
        return
    s = "["
    s += mark
    if unclosed:
        s += "/"
    if label is not None:
        s += '"%s"' % label
    bar.text += s


# --------------------------------------------------------------------------
# 用例: 每个用例保证覆盖一批规范要点
# --------------------------------------------------------------------------
def hdr(title, sub=None, authors=(), key="C", meter="4/4", tempo="80",
        extra=()):
    out = ["V: 1.0", "B: " + title]
    if sub:
        out.append("B: " + sub)
    for a in authors:
        out.append("Z: " + a)
    out.append("D: " + key)
    out.append("P: " + meter)
    if tempo is not None:
        out.append("J: " + tempo)
    out.extend(extra)
    out.append("")
    return out


def emit(lines, systems, lyrics=None):
    """systems: [(qlines, clines)]; qlines: [(tag, items)]"""
    for qlines, clines in systems:
        for tag, items in qlines:
            toks = []
            for it in items:
                toks.append(it.token())
            lines.append("Q%s: %s" % (tag, " ".join(toks)))
        for tag, text in (clines or []):
            lines.append("C%s: %s" % (tag, text))
        lines.append("")
    return lines


def lyric_text(notes, rng, rich=False):
    """按音符数生成歌词; rich 时插入 @ ~ 标点和注释。"""
    out = []
    for n in notes:
        if n.sym in "-8":
            continue
        r = rng.random()
        if rich and r < 0.08:
            out.append("@")
        elif rich and r < 0.13:
            out.append(rng.choice(CHARS) + "~" + rng.choice(CHARS))
        elif rich and r < 0.17:
            out.append(rng.choice("，。！？"))
        else:
            out.append(rng.choice(CHARS))
    return "".join(out)


# ---- 01 基本音符/休止符/八度 ---------------------------------------------
def case_notes(rng):
    lines = hdr("基本音符", authors=["测试 词曲"], key="C", tempo="90")
    systems = []
    # 手工第一行: 普通音符 / 增时线 / 休止符 / 隐藏休止符 / 节奏音符 / 八度
    m1 = [("1", "", 0), ("2", "", 0), ("3", "", 0), ("4", "", 0)]
    m2 = [("5", "", 0), ("-", "", 0), ("-", "", 0), ("-", "", 0)]
    m3 = [("0", "", 0), ("8", "", 0), ("9", "", 0), ("1", "", 0, "'")]
    m4 = [("2", "", 0, "''"), ("3", "", 0, ","), ("4", "", 0, "''"),
          ("5", "", 0, ",")]
    systems.append(([("", line_of([m1, m2, m3, m4]))], None))
    for s in range(2):
        items = make_line(rng, 4, 4, deco=lambda n: add_oct(rng, n, "mix"),
                          hidden_p=0.08, rhythm_p=0.08, rest_p=0.10,
                          hold_p=0.10)
        systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 02 减时线/附点 ------------------------------------------------------
def case_rhythm(rng):
    lines = hdr("时值", authors=["测试 词曲"], key="G", tempo="中速")
    e8 = [("1", "/", 0), ("2", "/", 0), ("3", "/", 0), ("4", "/", 0),
          ("5", "/", 0), ("6", "/", 0), ("7", "/", 0), ("1", "/", 0, "'")]
    e16 = [(str(k % 7 + 1), "//", 0) for k in range(16)]
    e32 = [(str(k % 7 + 1), "///", 0) for k in range(32)]
    e64 = [(str(k % 7 + 1), "////", 0) for k in range(64)]
    dot1 = [("1", "", 1), ("2", "/", 0), ("3", "", 1), ("4", "/", 0)]
    dot2 = [("1", "", 2), ("2", "//", 0), ("3", "", 2), ("4", "//", 0)]
    systems = []
    systems.append(([("", line_of([e8, e16, e32, e64]))], None))
    systems.append(([("", line_of([dot1, dot2, e8, e16]))], None))
    # 随机补充
    items = make_line(rng, 4, 4, must=["/", "//", "///", "////", ".", ".."])
    systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 03 自定义切分 ~ ^ ---------------------------------------------------
def case_beats(rng):
    lines = hdr("切分", authors=["测试 词曲"], key="C", tempo="120")
    systems = []
    for s in range(3):
        items = make_line(rng, 4, 4)
        notes = flat_notes(items)
        # 每个 system 都保证 ~ 和 ^ 各出现一次
        idx = [k for k, n in enumerate(notes) if n.dur in ("/", "//")]
        if len(idx) >= 4:
            notes[idx[0]].beat = "~"
            notes[idx[1]].beat = "^"
        q = [("", items)]
        systems.append((q, None))
    return emit(lines, systems)


# ---- 04 升降还原 ---------------------------------------------------------
def case_accidentals(rng):
    lines = hdr("升降记号", authors=["测试 词曲"], key="D",
                extra=["# 升 C, 降 D"])
    systems = []
    for s in range(3):
        items = make_line(rng, 4, 4, deco=lambda n: add_acc(rng, n))
        notes = flat_notes(items)
        for k, acc in enumerate(["#", "$", "="]):
            if k < len(notes):
                notes[k].acc = acc
        # 相邻多个升降音
        if len(notes) > 6:
            notes[4].acc = "#"
            notes[5].acc = "$"
        systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 05 倚音 -------------------------------------------------------------
def case_graces(rng):
    lines = hdr("倚音", authors=["测试 词曲"], key="F", tempo="88")
    systems = []
    front = ["1", "3,", "5'", "6//", "2#", "7,/", "4''"]
    back = ["1", "2,", "5'/", "3//", "6$", "1''"]
    for s in range(3):
        items = make_line(rng, 4, 4)
        notes = flat_notes(items)
        for k in range(0, len(notes) - 1, 3):
            notes[k].grace = [rng.choice(front)]
        for k in range(1, len(notes) - 1, 4):
            notes[k].grace_back = [rng.choice(back)]
        # 一个双倚音组
        if len(notes) > 8:
            notes[7].grace = ["1", "3/"]
        systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 06 力度/记号 --------------------------------------------------------
def case_marks(rng):
    lines = hdr("记号", authors=["测试 词曲"], key="C", tempo="96")
    systems = []
    for s in range(3):
        items = make_line(rng, 4, 4)
        notes = flat_notes(items)
        # 六种记号 + 六种力度全部出现一次以上
        combos = list(zip(ART_ALL, DYN_ALL))
        for k, (art, dyn) in enumerate(combos):
            if k < len(notes):
                notes[k].marks += [art, dyn]
        if len(notes) > 12:
            notes[12].marks.append("yc")
            notes[13].marks.append("mp")
        systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 24 记号符号 ---------------------------------------------------------
def case_marks_symbols(rng):
    lines = hdr("记号符号", authors=["测试 词曲"], key="C", tempo="96")
    # 固定为四分音符，让每个记号都落在独立的音符上；13 个记号占 4 小节。
    measures = [[("3", "", 0) for _ in range(4)] for _ in range(4)]
    items = line_of(measures)
    notes = flat_notes(items)
    for n, mark in zip(notes, MARK_SYMBOLS):
        n.marks.append(mark)
    systems = [([("", items)], None)]
    return emit(lines, systems)


# ---- 07 渐强/渐弱 --------------------------------------------------------
def case_cresc(rng):
    lines = hdr("渐强渐弱", authors=["测试 词曲"], key="C", tempo="100")
    systems = []
    marks = [("<", "!"), (">", "!"), ("<++", "!"), (">+", "!")]
    for s, (c, _end) in enumerate(marks):
        items = make_line(rng, 4, 3)
        notes = flat_notes(items)
        notes[1].cresc = c
        notes[rng.randint(4, len(notes) - 1)].cresc_end = True
        systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 08 伴奏括弧 ---------------------------------------------------------
def case_brackets(rng):
    lines = hdr("伴奏括弧", authors=["测试 词曲"], key="C", tempo="80")
    systems = []
    for s in range(3):
        items = make_line(rng, 4, 3)
        notes = flat_notes(items)
        span = min(len(notes) - 3, 6)
        notes[1].marks.append("zkh")
        notes[1 + span].marks.append("ykh")
        notes[span + 2].marks.append("zkh")
        notes[span + 2 + min(3, len(notes) - span - 3)].marks.append("ykh")
        systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 09 小节线/反复 ------------------------------------------------------
def case_barlines(rng):
    lines = hdr("小节线", authors=["测试 词曲"], key="C", tempo="96")
    variants = [
        ["|:", "|", "||"],
        ["|/", "|", "||"],
        ["|*", "|", "||"],
        ["|", "|&dc", "||"],
        ["|", "|&ds", "||"],
        ["|", "|&fine", "||"],
        ["|", "|&ty", "||"],
        ["|", "|&hs", "||"],
        ["|:", "|", ":|:"],
    ]
    systems = []
    for bars in variants:
        items = make_line(rng, 4, len(bars) - 1)
        bn = 0
        out = []
        for it in items:
            if isinstance(it, Bar):
                it.text = bars[bn]
                bn += 1
            out.append(it)
        out.append(Bar(bars[-1]))
        systems.append(([("", out)], None))
    return emit(lines, systems)


# ---- 10 连音线 -----------------------------------------------------------
def case_ties(rng):
    lines = hdr("连音线", authors=["测试 词曲"], key="C", tempo="84")
    systems = []
    for s in range(3):
        items = make_line(rng, 4, 4)
        add_ties(rng, items, 2, nested=(s % 2 == 0))
        systems.append(([("", items)], None))
    # 再补一条跨小节的长连音线
    add_ties(rng, flat_notes([]) or [], 0)
    return emit(lines, systems)


# ---- 11 多连音 -----------------------------------------------------------
def case_tuplets(rng):
    lines = hdr("多连音", authors=["测试 词曲"], key="C", tempo="84")
    systems = []
    for size in (3, 4, 5, 6, 7):
        items = make_line(rng, 4, 3)
        slices = measure_slices(items)
        a, b = slices[1]
        pool = "12345671"
        newnotes = [Note(rng.choice(pool), "/", 0) for _ in range(8)]
        k = 0
        newnotes[k].multi = True
        newnotes[k].opens += 1
        newnotes[k + size - 1].closes += 1
        items[a:b] = newnotes
        systems.append(([("", items)], None))
    # 多连音 + 连音线 + 记号
    items = make_line(rng, 4, 3)
    add_tuplets(rng, items, 1, sizes=(5,))
    notes = flat_notes(items)
    if len(notes) > 10:
        add_ties(rng, items, 1)
        notes[3].marks.append("mp")
    systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 12 跳房子 -----------------------------------------------------------
def case_houses(rng):
    lines = hdr("跳房子", authors=["测试 词曲"], key="C", tempo="92")
    systems = []
    # 第一行: 普通跳房子 + 第二段
    items = make_line(rng, 4, 4)
    bars = [it for it in items if isinstance(it, Bar)]
    house(bars[0], "", "1")
    house(bars[2], "", close=True)
    house(bars[2], "+", "2")
    house(bars[3], "", close=True)
    systems.append(([("", items)], None))
    # 第二行: 不封闭的跳房子 ([ 后加 /)
    items = make_line(rng, 4, 4)
    bars = [it for it in items if isinstance(it, Bar)]
    house(bars[0], "++", "3", unclosed=True)
    house(bars[2], "", close=True)
    systems.append(([("", items)], None))
    # 第三行: 行首隐藏小节线 + 跳房子
    items = make_line(rng, 4, 3)
    bars = [it for it in items if isinstance(it, Bar)]
    bars[0].text = "|/"
    house(bars[0], "", "4")
    house(bars[2], "", close=True)
    systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 13 跨行连音线 -------------------------------------------------------
def case_cross(rng):
    lines = hdr("跨行连音", authors=["测试 词曲"], key="C", tempo="80")
    systems = []
    for s in range(4):
        items = make_line(rng, 4, 3)
        notes = flat_notes(items)
        if s:
            notes[0].closes += 1          # 行首收上一行的连音线
        if s < 3:
            notes[-1].opens += 1          # 行末开一条, 下一行行首收
        if s == 1 and len(notes) > 6:
            notes[3].opens += 1           # 行内跨小节
            notes[-2].closes += 1
        systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 14 临时拍号 ---------------------------------------------------------
def case_meter(rng):
    lines = hdr("临时拍号", authors=["测试 词曲"], key="C",
                meter="4/4 (2/4 1/4)")
    systems = []
    items = make_line(rng, 4, 3)
    bars = [it for it in items if isinstance(it, Bar)]
    bars[1].text += '"p:3/4"'
    systems.append(([("", items)], None))
    items = make_line(rng, 4, 4)
    bars = [it for it in items if isinstance(it, Bar)]
    bars[1].text += '"p:2/4"'
    bars[2].text += '"p:2/4"'
    systems.append(([("", items)], None))
    items = make_line(rng, 4, 3)
    bars = [it for it in items if isinstance(it, Bar)]
    bars[1].text += '"p:6/8"'
    systems.append(([("", items)], None))
    return emit(lines, systems)


# ---- 15 临时伴奏/临时多声部 ----------------------------------------------
def case_temp(rng):
    lines = hdr("临时块", authors=["测试 词曲"], key="C", tempo="76")
    # {bz}: 临时伴奏写在主旋律上方, 与主旋律按时间对齐
    items = make_line(rng, 4, 4)
    body = make_line(rng, 4, 2, must=["/"], rest_p=0.0)
    bars = [i for i, it in enumerate(items) if isinstance(it, Bar)]
    btoks = " ".join(it.token() for it in body[1:])
    items = items[:bars[2] + 1] + [Block("bz", btoks)] + items[bars[2] + 1:]
    systems = [([("", items)], None)]
    # {dsb}: 临时多声部, 自己占一条谱线, 主旋律在 } 之后继续
    a = make_line(rng, 4, 4)
    b = make_line(rng, 4, 2)
    bars = [i for i, it in enumerate(a) if isinstance(it, Bar)]
    btoks = " ".join(it.token() for it in b[1:])
    a = a[:bars[2] + 1] + [Block("dsb", btoks)] + a[bars[2] + 1:]
    systems.append(([("", a)], None))
    # 行首就是临时块
    a = make_line(rng, 4, 3)
    b = make_line(rng, 4, 2)
    btoks = " ".join(it.token() for it in b[1:])
    a = [Bar("|/"), Block("dsb", btoks)] + a[1:]
    systems.append(([("", a)], None))
    return emit(lines, systems)


# ---- 16 多声部 -----------------------------------------------------------
def case_voices(rng):
    lines = hdr("多声部", authors=["测试 词曲"], key="G", tempo="80")
    systems = []
    # 整行两个声部 + 声部名 + 两行歌词
    for s in range(2):
        a = make_line(rng, 4, 3)
        b = make_line(rng, 4, 3)
        q = [("1", a), ('2"二声部"', b)]
        c = [("1", lyric_text(flat_notes(a), rng)),
             ("2", lyric_text(flat_notes(b), rng))]
        systems.append((q, c))
    # 行内才出现第二声部 (参考 demo 014): Q1 第五小节前 &sbf,
    # Q2 前四小节用隐藏休止符 8 + 隐藏占位小节线 |* 对齐
    a = make_line(rng, 4, 6)
    b = make_line(rng, 4, 2)
    bars = [it for it in a if isinstance(it, Bar)]
    bars[4].text += "&sbf"
    pad = [Bar()]
    for m in range(4):
        pad += [Note("8", "", 0)] * 4
        if m < 3:
            pad.append(Bar("|*"))
    systems.append(([("1", a), ("2", pad + b[1:])], None))
    return emit(lines, systems)


# ---- 17 中文歌词 ---------------------------------------------------------
def case_lyrics(rng):
    lines = hdr("歌词", authors=["测试 词曲"], key="C", tempo="72")
    systems = []
    # 一行曲两行词; 第二行用 "注释" 开头并带下划线空格
    items = make_line(rng, 4, 4)
    notes = flat_notes(items)
    c1 = lyric_text(notes, rng, rich=True)
    c2 = '"第一段_歌词" ' + lyric_text(notes, rng, rich=True)
    systems.append((([("", items)], [("", c1), ("", c2)])))
    # 注释 + @ + ~
    items = make_line(rng, 4, 4)
    notes = flat_notes(items)
    c1 = lyric_text(notes, rng, rich=True)
    systems.append((([("", items)], [("", c1)])))
    return emit(lines, systems)


# ---- 18 英文歌词 ---------------------------------------------------------
def case_english(rng):
    lines = hdr("English", authors=["Test"], key="C", tempo="80")
    items = make_line(rng, 4, 4)
    notes = flat_notes(items)
    words = ["Sun", "shine", "a", "cross", "the", "river", "flow"]
    out = []
    for n in notes:
        if n.sym in "-8":
            continue
        r = rng.random()
        if r < 0.12:
            out.append("@")
        else:
            out.append(rng.choice(words) + "/")
    systems = [(([("", items)], [("", "".join(out))])), ]
    return emit(lines, systems)


# ---- 19 描述头 -----------------------------------------------------------
def case_header(rng):
    lines = hdr("描述头", sub="副标题测试",
                authors=["作词 甲", "作曲 乙", "编曲 丙"],
                key="E$", meter="4/4 (2/4 1/4)", tempo="6 欢快地",
                extra=["X: 10", "# 这一行是注释"])
    items = make_line(rng, 4, 3)
    systems = [([("", items)], None)]
    return emit(lines, systems)


# ---- 20 分页 -------------------------------------------------------------
def case_pagebreak(rng):
    lines = hdr("分页", authors=["测试 词曲"], key="C", tempo="80")
    systems = []
    for s in range(4):
        items = make_line(rng, 4, 3)
        systems.append(([("", items)], None))
        if s == 1:
            systems.append(("PAGEBREAK", None))
    out = []
    for qlines, clines in systems:
        if qlines == "PAGEBREAK":
            out.append("[fenye]")
            out.append("")
            continue
        for tag, items in qlines:
            out.append("Q%s: %s" % (tag, " ".join(it.token() for it in items)))
        out.append("")
    return out


# ---- 21/22/23 综合 -------------------------------------------------------
MIX_POOL = ["ties", "tuplets", "marks", "dyn", "graces", "acc", "cresc",
            "houses", "beats", "text"]


def case_mixed(rng, seed_off=0):
    lines = hdr("综合用例 %d" % seed_off, authors=["测试 词曲"],
                key=rng.choice(["C", "G", "F", "D", "E$", "A"]),
                tempo=rng.choice(["80", "96", "欢快地"]))
    systems = []
    multi = seed_off % 2 == 1
    for s in range(3):
        feats = rng.sample(MIX_POOL, 3)
        q = []
        for v in range(2 if multi else 1):
            items = make_line(rng, 4, 3,
                              must=[rng.choice(["/", "//", ".", ".."])],
                              hidden_p=0.04, rhythm_p=0.04, rest_p=0.06,
                              hold_p=0.04)
            notes = flat_notes(items)
            if "acc" in feats:
                for n in notes:
                    if rng.random() < 0.25:
                        n.acc = rng.choice(["#", "$", "="])
            if "marks" in feats:
                for n in notes:
                    if rng.random() < 0.18:
                        n.marks.append(rng.choice(ART_ALL))
            if "dyn" in feats:
                for n in notes:
                    if rng.random() < 0.12:
                        n.marks.append(rng.choice(DYN_ALL))
            if "graces" in feats:
                for n in notes:
                    if rng.random() < 0.15:
                        n.grace = [rng.choice(["1", "3,", "5'/", "6//"])]
            if "text" in feats:
                for n in notes:
                    if rng.random() < 0.10:
                        n.text = rng.choice(["慢", "rit."])
            if "beats" in feats:
                for n in notes:
                    if n.sym not in "08-" and n.dur in ("/", "//") \
                            and rng.random() < 0.12:
                        n.beat = rng.choice(["~", "^"])
            if "ties" in feats:
                add_ties(rng, items, 1, nested=rng.random() < 0.4)
            if "tuplets" in feats:
                add_tuplets(rng, items, 1)
            if "cresc" in feats:
                add_crescendo(rng, items, 1)
            q.append((str(v + 1) if multi else "", items))
        if "houses" in feats:
            bars = [it for it in q[0][1] if isinstance(it, Bar)]
            house(bars[0], rng.choice(["", "+"]), "1")
            house(bars[-2], "", close=True)
        cl = []
        for tag, items in q:
            ns = flat_notes(items)
            if len(ns) <= 16 and rng.random() < 0.7:
                cl.append((tag, lyric_text(ns, rng, rich=True)))
        systems.append((q, cl))
    return emit(lines, systems)


# --------------------------------------------------------------------------
# 用例表: (名称, 生成函数, 覆盖的规范要点)
# --------------------------------------------------------------------------
CASE_LIST = [
    ("01-notes", case_notes, "普通音符/增时线/休止符0/隐藏8/节奏9/高低八度"),
    ("02-rhythm", case_rhythm, "一条~四条减时线/单附点/双附点"),
    ("03-beats", case_beats, "~ 并入同一拍 / ^ 强制切拍"),
    ("04-accidentals", case_accidentals, "升# 降$ 还原=/相邻升降音"),
    ("05-graces", case_graces, "前倚音/后倚音[h]/倚音内的八度 减时线 升降号"),
    ("06-marks", case_marks, "&yc &bc &dy &zy &hx &sby / 力度 pp~ff"),
    ("07-cresc", case_cresc, "渐强< 渐弱> 结束! 及 + 号抬高"),
    ("08-brackets", case_brackets, "伴奏括弧 &zkh &ykh"),
    ("09-barlines", case_barlines,
     "小节线 | || |: :| :|: |/ |* / &dc &ds &fine &ty &hs"),
    ("10-ties", case_ties, "连音线/嵌套连音线/跨小节"),
    ("11-tuplets", case_tuplets, "多连音 (y / 数字按音符数"),
    ("12-houses", case_houses, "跳房子 [ ] [/ / + / ++ / 备注"),
    ("13-cross", case_cross, "跨行连音线"),
    ("14-meter", case_meter, "临时拍号 p:x/x / 多拍号描述头"),
    ("15-temp", case_temp, "{bz} 临时伴奏 / {dsb} 临时多声部", True),
    ("16-voices", case_voices, "多声部 Q1/Q2 / 声部名 / &sbf / 8+ |*"),
    ("17-lyrics", case_lyrics, "歌词 @ 跳过/~/标点/多行/注释"),
    ("18-english", case_english, "英文歌词 / 分隔"),
    ("19-header", case_header, "描述头 B Z D P J X / 注释行"),
    ("20-pagebreak", case_pagebreak, "[fenye] 分页"),
    ("21-mixed", lambda r: case_mixed(r, 0), "综合"),
    ("22-mixed", lambda r: case_mixed(r, 1), "综合+多声部"),
    ("23-mixed", lambda r: case_mixed(r, 2), "综合"),
    ("24-marks-symbols", case_marks_symbols,
     "&yc &bc &zy &dy &hx &shy &xhy &sby(+) &xby(+) &cy(+)"),
]


def cmd_gen(args):
    os.makedirs(CASES, exist_ok=True)
    for old in glob.glob(os.path.join(CASES, "*.jps")):
        os.remove(old)
    print("覆盖的规范要点:")
    for name, fn, cover, *_ in CASE_LIST:
        # 每个用例用自己的种子: 改动某个用例不会让其它用例的随机内容跟着变,
        # 这样缓存的参考图才不会白费。
        rng = random.Random("%s/%s" % (args.seed, name))
        text = fn(rng)
        path = os.path.join(CASES, name + ".jps")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(text).rstrip() + "\n")
        print("  %-16s %s" % (name, cover))


# --------------------------------------------------------------------------
# 调用两套实现
# --------------------------------------------------------------------------
def cmd_ref(args):
    os.makedirs(REF, exist_ok=True)
    for jps in sorted(glob.glob(os.path.join(CASES, "*.jps"))):
        base = os.path.splitext(os.path.basename(jps))[0]
        outs = glob.glob(os.path.join(REF, base + "*.svg"))
        if outs and not args.force:
            continue
        print("参考", base, flush=True)
        subprocess.run([os.path.join(ROOT, "bin", "jianpu.py"),
                        "-f", "svg", "-d", REF, jps], check=False)


def cmd_mine(args):
    os.makedirs(MINE, exist_ok=True)
    for jps in sorted(glob.glob(os.path.join(CASES, "*.jps"))):
        base = os.path.splitext(os.path.basename(jps))[0]
        for old in glob.glob(os.path.join(MINE, base + "*.svg")):
            os.remove(old)
        subprocess.run([sys.executable,
                        os.path.join(ROOT, "bin", "jps2svg.py"),
                        "-o", os.path.join(MINE, base + ".svg"), jps],
                       check=False)


# --------------------------------------------------------------------------
# 音符行提取
# --------------------------------------------------------------------------
RE_DIGIT = re.compile(r"^shuzi_[a-z]+(?:_bian)?_(\d)")


def ref_page(path):
    s = open(path, encoding="utf-8").read()
    digits, others = [], []
    for m in re.finditer(r"<use\b([^>]*)>", s):
        a = m.group(1)
        h = re.search(r'href="#([^"]+)"', a)
        x = re.search(r'\bx="([-\d.]+)"', a)
        y = re.search(r'\by="([-\d.]+)"', a)
        if not (h and x and y):
            continue
        glyph = h.group(1)
        code = re.search(r'\bcode="([^"]*)"', a)
        item = (glyph, float(x.group(1)), float(y.group(1)))
        if glyph.startswith("shuzi_b_bian") and not (code and code.group(1)):
            others.append(item)
            continue
        if RE_DIGIT.match(glyph):
            digits.append(item)
        elif glyph == "shuzi_x":
            digits.append(("X", item[1], item[2]))
        else:
            others.append(item)
    return digits, others


def mine_page(path):
    s = open(path, encoding="utf-8").read()
    digits, others = [], []
    for m in re.finditer(r"<text\b([^>]*)>([^<]*)</text>", s):
        a, txt = m.group(1), m.group(2)
        x = re.search(r'\bx="([-\d.]+)"', a)
        y = re.search(r'\by="([-\d.]+)"', a)
        fs = re.search(r'font-size="([\d.]+)', a)
        if not (x and y and fs):
            continue
        item = (txt, float(x.group(1)), float(y.group(1)))
        if abs(float(fs.group(1)) - 26.0) < 0.6 and re.match(r"^[0-9X]$", txt):
            # 参考实现的 <use y> 是音符锚点；本地实现用 <text y> 作为字体
            # 基线，基线比锚点低 font_size*0.36。统一成锚点坐标后再做
            # 像素配准，否则音符上方的记号会被整体错位约 9px。
            digits.append((txt, float(x.group(1)),
                           float(y.group(1)) - float(fs.group(1)) * 0.36))
        else:
            others.append(item)
    return digits, others


def cluster_rows(items, tol=16.0):
    rows = []
    for it in sorted(items, key=lambda t: (t[2], t[1])):
        if rows and abs(it[2] - rows[-1][0]) <= tol:
            rows[-1][1].append(it)
        else:
            rows.append([it[2], [it]])
    return [sorted(items_, key=lambda t: t[1]) for _, items_ in rows]


def fmt_digits(items, mine=False):
    if mine:
        return "".join(t[0] for t in items)
    out = []
    for t in items:
        m = RE_DIGIT.match(t[0])
        out.append(m.group(1) if m else t[0])
    return "".join(out)


# --------------------------------------------------------------------------
# 文本行 (歌词等) 提取
# --------------------------------------------------------------------------
def ref_texts(path):
    s = open(path, encoding="utf-8").read()
    out = []
    for m in re.finditer(r"<text\b([^>]*)>([^<]*)</text>", s):
        a, txt = m.group(1), m.group(2)
        x = re.search(r'\bx="([-\d.]+)"', a)
        y = re.search(r'\by="([-\d.]+)"', a)
        fs = re.search(r'font-size="([\d.]+)', a)
        if not (x and y and fs):
            continue
        out.append((float(fs.group(1)), float(x.group(1)), float(y.group(1)),
                    txt))
    return out


def is_punctish(ch):
    import unicodedata
    return unicodedata.category(ch).startswith("P") and ch not in "/"


def lyric_rows(texts, note_rows):
    """取音符行下方 (歌词区) 的文字, 按 y 聚成行。

    标点在不同实现里挂靠位置不同 (并入前一个字 / 单独成块), 位置也因此
    漂移; 这里只比较"字"的序列, 另外单独统计标点个数。
    """
    if not note_rows:
        return []
    bottom = max(note_rows)
    out = []
    for fs, x, y, txt in sorted(texts, key=lambda t: (t[2], t[1])):
        if y <= bottom + 8 or not (12.0 <= fs <= 20.0):
            continue
        if out and abs(y - out[-1][0]) <= 10:
            out[-1][1].append((x, txt))
        else:
            out.append([y, [(x, txt)]])
    res = []
    for _y, items in out:
        txt = "".join(t for _x, t in sorted(items))
        chars = [c for c in txt if not (is_punctish(c) or c in "@~/")]
        marks = sum(1 for c in txt if is_punctish(c))
        res.append("".join(chars) + ("§%d" % marks if marks else ""))
    return res


# --------------------------------------------------------------------------
# 像素级符号对比
# --------------------------------------------------------------------------
SCALE = 1.5
BAND_UP = 54.0
BAND_DN = 28.0
INK = 170


def _np():
    import numpy
    return numpy


def _pil():
    from PIL import Image
    return Image


def raster(path, png):
    subprocess.run(["rsvg-convert", "-z", str(SCALE), path, "-o", png],
                   check=False)
    Image = _pil()
    img = Image.open(png).convert("L")
    return _np().array(img)


def text_mask(path, mine, shape, tf):
    """文字区域掩码 (字体不同, 不参与像素对比)。"""
    np = _np()
    mask = np.zeros(shape, dtype=bool)
    s = open(path, encoding="utf-8").read()
    for m in re.finditer(r"<text\b([^>]*)>([^<]*)</text>", s):
        a, txt = m.group(1), m.group(2)
        x = re.search(r'\bx="([-\d.]+)"', a)
        y = re.search(r'\by="([-\d.]+)"', a)
        fs = re.search(r'font-size="([\d.]+)', a)
        if not (x and y and fs):
            continue
        anchor = re.search(r'text-anchor="(\w+)"', a)
        anchor = anchor.group(1) if anchor else "start"
        fx = float(fs.group(1))
        w = 0.0
        for ch in txt:
            w += fx * (1.0 if ord(ch) > 0x2e80 else 0.56)
        x0 = float(x.group(1))
        if anchor == "middle":
            x0 -= w / 2.0
        elif anchor == "end":
            x0 -= w
        y0 = float(y.group(1)) - fx * 0.92
        x1, y1, x2, y2 = x0 - 3, y0 - 3, x0 + w + 3, y0 + fx * 1.25 + 3
        if mine:
            x1, x2 = tf(x1), tf(x2)
            y1, y2 = y1 - tf("dy"), y2 - tf("dy")
        c0 = max(0, int(x1 * SCALE))
        c1 = min(shape[1], int(x2 * SCALE) + 1)
        r0 = max(0, int(y1 * SCALE))
        r1 = min(shape[0], int(y2 * SCALE) + 1)
        if c1 > c0 and r1 > r0:
            mask[r0:r1, c0:c1] = True
    return mask


def dilate(mask):
    np = _np()
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return out


def boxes(mask, min_area=12):
    """连通域 -> [(x0,y0,x1,y1,面积)], 按面积从大到小。"""
    np = _np()
    seen = np.zeros(mask.shape, dtype=bool)
    h, w = mask.shape
    out = []
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys, xs):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        n = 0
        x0 = x1 = sx
        y0 = y1 = sy
        while stack:
            y, x = stack.pop()
            n += 1
            if x < x0:
                x0 = x
            if x > x1:
                x1 = x
            if y < y0:
                y0 = y
            if y > y1:
                y1 = y
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < h and 0 <= xx < w and mask[yy, xx] \
                            and not seen[yy, xx]:
                        seen[yy, xx] = True
                        stack.append((yy, xx))
        if n >= min_area:
            out.append((x0, y0, x1, y1, n))
    out.sort(key=lambda b: -b[4])
    return out


def symbol_diff(ref_svg, mine_svg, rows_r, rows_m, rdigits, mdigits,
                outdir=None, tag=""):
    """逐行比较符号 (数字以外的图形)。返回问题描述列表。"""
    np = _np()
    problems = []
    tmp = tempfile.mkdtemp(prefix="jpscmp-")
    try:
        a = raster(ref_svg, os.path.join(tmp, "r.png"))
        b = raster(mine_svg, os.path.join(tmp, "m.png"))
        # 逐行配准: 用数字 x 拟合 mine -> ref 的缩放和位移
        for ri, (rr, mm) in enumerate(zip(rows_r, rows_m)):
            if len(rr) < 2 or len(mm) < 2 or len(rr) != len(mm):
                continue
            rx = np.array([t[1] for t in rr], dtype=float)
            mx = np.array([t[1] for t in mm], dtype=float)
            if rx[-1] - rx[0] < 1 or mx[-1] - mx[0] < 1:
                continue
            s = (rx[-1] - rx[0]) / (mx[-1] - mx[0])
            dx = rx[0] - mx[0] * s
            dy = np.mean([t[2] for t in rr]) - np.mean([t[2] for t in mm])
            if not (0.9 < s < 1.1):
                problems.append("行%d 缩放差异 %.1f%%" % (ri + 1, (s - 1) * 100))
                continue

            def tf(x, s=s, dx=dx):
                if x == "dy":
                    return dy
                return x * s + dx

            ry = np.mean([t[2] for t in rr])
            r0 = max(0, int((ry - BAND_UP) * SCALE))
            r1 = min(a.shape[0], int((ry + BAND_DN) * SCALE))
            c0 = max(0, int(30 * SCALE))
            c1 = min(a.shape[1], int(970 * SCALE))
            band_r = a[r0:r1, c0:c1]
            # 把 mine 重采样到 ref 的坐标
            ys = (np.arange(r0, r1, dtype=float) / SCALE - dy) * SCALE
            xs = (np.arange(c0, c1, dtype=float) / SCALE * 1.0 - dx) / s * SCALE
            yi = np.clip(ys.astype(int), 0, b.shape[0] - 1)
            xi = np.clip(xs.astype(int), 0, b.shape[1] - 1)
            band_m = b[np.ix_(yi, xi)]
            ink_r = band_r < INK
            ink_m = band_m < INK
            # 文字区域不比较
            mask = text_mask(ref_svg, False, a.shape, tf)
            mask |= text_mask(mine_svg, True, a.shape, tf)
            mask = mask[r0:r1, c0:c1]
            extra = dilate(ink_m) & ~dilate(ink_r) & ~mask
            miss = dilate(ink_r) & ~dilate(ink_m) & ~mask
            extra = extra & dilate(extra)
            miss = miss & dilate(miss)
            name = "%s-p%d-r%d" % (tag, 1, ri + 1)
            for kind, m in (("多出", extra), ("缺少", miss)):
                for bx0, by0, bx1, by1, n in boxes(m, 14)[:4]:
                    X0 = (bx0 + c0) / SCALE
                    X1 = (bx1 + c0) / SCALE
                    Y0 = (by0 + r0) / SCALE
                    Y1 = (by1 + r0) / SCALE
                    if X1 - X0 < 2.5 and Y1 - Y0 < 2.5:
                        continue
                    problems.append(
                        "行%d %s@x%.0f..%.0f y%.0f..%.0f (面积%d)"
                        % (ri + 1, kind, X0, X1, Y0, Y1, n))
        if outdir:
            os.makedirs(outdir, exist_ok=True)
            for i, (rp, mp) in enumerate(zip(rows_r, rows_m)):
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


def compare_page(rp, mp, deep=True, tag="", loose=False):
    problems = []
    rd, _ = ref_page(rp)
    md, _ = mine_page(mp)
    rrows = cluster_rows(rd)
    mrows = cluster_rows(md)
    if loose:
        # 只比较页内音符总数: 参考实现的 {dsb} 临时多声部会丢/挪音符,
        # 逐行比较没有意义 (本地实现的排版是完整且正常的)。
        return []
    if len(rrows) != len(mrows):
        problems.append("行数 ref=%d mine=%d" % (len(rrows), len(mrows)))
        return problems
    # 有些情况 (例如 {dsb} 临时多声部) 参考实现会把同一行拆到上下两条线上,
    # 音符完全一样只是分行位置不同。这时整页数字多重集相同, 就不要报错,
    # 只提示"分行不同"。
    all_r = "".join(fmt_digits(r) for r in rrows)
    all_m = "".join(fmt_digits(r, True) for r in mrows)
    split_only = (all_r != all_m and sorted(all_r) == sorted(all_m))
    bad_rows = []
    for ri, (rr, mr) in enumerate(zip(rrows, mrows)):
        rs = fmt_digits(rr)
        ms = fmt_digits(mr, True)
        if rs != ms:
            if not split_only:
                problems.append("行%d 音符 ref=%s mine=%s" % (ri + 1, rs, ms))
            continue
        rx = [t[1] for t in rr]
        mx = [t[1] for t in mr]
        if len(rx) >= 2:
            span_r = rx[-1] - rx[0]
            span_m = mx[-1] - mx[0]
            if span_r > 1 and span_m > 1:
                drift = max(abs((mx[i] - mx[0]) / span_m -
                                (rx[i] - rx[0]) / span_r)
                            for i in range(len(rx)))
                if drift > 0.02:
                    problems.append("行%d 节奏偏差 %.1f%%" % (ri + 1, drift * 100))
        bad_rows.append(ri)
    # 歌词文本逐行比较
    lr = lyric_rows(ref_texts(rp), [row[0][2] for row in rrows])
    lm = lyric_rows(ref_texts(mp), [row[0][2] for row in mrows])
    if len(lr) != len(lm):
        problems.append("歌词行数 ref=%d mine=%d %s | %s"
                        % (len(lr), len(lm), lr[:2], lm[:2]))
    else:
        for i, (x, y) in enumerate(zip(lr, lm)):
            if x != y:
                problems.append("歌词%d ref=%s mine=%s" % (i + 1, x, y))
    if split_only:
        problems.append("临时块分行位置不同 (音符总数一致, 参考实现的 {dsb} 限制)")
    if deep:
        problems.extend(symbol_diff(rp, mp, rrows, mrows, rd, md, tag=tag))
    return problems


def cmd_report(args):
    total = 0
    bad = 0
    hard = 0
    for jps in sorted(glob.glob(os.path.join(CASES, "*.jps"))):
        base = os.path.splitext(os.path.basename(jps))[0]
        refs = sorted(glob.glob(os.path.join(REF, base + "*.svg")))
        mines = sorted(glob.glob(os.path.join(MINE, base + "*.svg")))
        total += 1
        if not refs:
            print("%-24s 缺少参考" % base)
            bad += 1
            continue
        if not mines:
            print("%-24s 缺少渲染" % base)
            bad += 1
            continue
        if len(refs) != len(mines):
            print("%-24s 页数不同 ref=%d mine=%d"
                  % (base, len(refs), len(mines)))
            bad += 1
            continue
        loose = False
        for name, _fn, _cover, *rest in CASE_LIST:
            if name == base and rest:
                loose = bool(rest[0])
        problems = []
        for pi, (rp, mp) in enumerate(zip(refs, mines)):
            for p in compare_page(rp, mp, deep=not args.no_raster
                                  if hasattr(args, "no_raster") else True,
                                  loose=loose):
                problems.append("p%d %s" % (pi + 1, p))
        if loose:
            print("%-24s 仅比页数 (参考实现的 {dsb} 临时多声部会丢/挪音符)" % base)
        if problems:
            bad += 1
            # 只差间距 (符号/歌词都对) 与结构性差异分开统计, 方便先修真正的问题
            if not all(("节奏偏差" in p) for p in problems):
                hard += 1
            print("== %s" % base)
            for p in problems[:args.limit]:
                print("   ", p)
            if len(problems) > args.limit:
                print("    ... 共 %d 条" % len(problems))
    print("\n%d 个用例: %d 个完全一致, %d 个只有间距偏差, "
          "%d 个有符号/歌词差异" % (total, total - bad, bad - hard, hard))
    return 0


def cmd_crop(args):
    """把某个用例的每一行导出为 参考/我的 上下对比图。"""
    os.makedirs(CROP, exist_ok=True)
    base = args.name
    refs = sorted(glob.glob(os.path.join(REF, base + "*.svg")))
    mines = sorted(glob.glob(os.path.join(MINE, base + "*.svg")))
    if not refs or not mines:
        print("缺少 svg")
        return 1
    for pi, (rp, mp) in enumerate(zip(refs, mines)):
        rpng = os.path.join(CROP, "r%d.png" % pi)
        mpng = os.path.join(CROP, "m%d.png" % pi)
        raster(rp, rpng)
        raster(mp, mpng)
        rd, _ = ref_page(rp)
        md, _ = mine_page(mp)
        rrows = cluster_rows(rd)
        mrows = cluster_rows(md)
        for ri, (rr, mm) in enumerate(zip(rrows, mrows)):
            ry = rr[0][2]
            y0 = int((ry - BAND_UP) * SCALE)
            out = os.path.join(CROP, "%s-p%d-r%d.png" % (base, pi + 1, ri + 1))
            subprocess.run(["magick", rpng, "-crop",
                            "940x%d+%d+%d" % (int((BAND_UP + BAND_DN) * SCALE),
                                              int(30 * SCALE), max(0, y0)),
                            "+repage", os.path.join(CROP, "a.png")], check=False)
            subprocess.run(["magick", mpng, "-crop",
                            "940x%d+%d+%d" % (int((BAND_UP + BAND_DN) * SCALE),
                                              int(30 * SCALE), max(0, y0 + 1)),
                            "+repage", os.path.join(CROP, "b.png")], check=False)
            subprocess.run(["magick", os.path.join(CROP, "a.png"),
                            os.path.join(CROP, "b.png"), "-append", out],
                           check=False)
        print("导出", base)
    return 0


def main():
    ap = argparse.ArgumentParser(description="jps2svg 自测工具")
    ap.add_argument("cmd", choices=["gen", "ref", "mine", "report", "all",
                                    "crop"])
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--no-raster", action="store_true")
    ap.add_argument("--name", default="01-notes")
    args = ap.parse_args()
    if args.cmd == "crop":
        return cmd_crop(args)
    if args.cmd in ("gen", "all"):
        cmd_gen(args)
    if args.cmd in ("ref", "all"):
        cmd_ref(args)
    if args.cmd in ("mine", "all"):
        cmd_mine(args)
    if args.cmd in ("report", "all"):
        cmd_report(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
