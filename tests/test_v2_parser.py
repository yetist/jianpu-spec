#! /usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2 解析器的最小验收测试。

直接运行::

    python3 tests/test_v2_parser.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import jps2svg2  # noqa: E402


def parse_text(text: str, *, lenient: bool = False) -> jps2svg2.Document:
    parser = jps2svg2.Parser("<test>", text, lenient=lenient)
    return parser.parse()


def errors(doc: jps2svg2.Document) -> list[jps2svg2.Diagnostic]:
    return [d for d in doc.diagnostics if d.severity == "error"]


class V2ParserAcceptanceTest(unittest.TestCase):
    def test_examples_parse_without_errors(self) -> None:
        for name in ("01-排排坐.jps", "02-综合示例.jps"):
            path = ROOT / "v2" / "examples" / name
            parser = jps2svg2.Parser(str(path), path.read_text(encoding="utf-8"))
            doc = parser.parse()
            self.assertEqual(errors(doc), [], name)
            dumped = json.dumps(jps2svg2.jsonable(doc), ensure_ascii=False)
            self.assertIn('"kind": "Document"', dumped)

    def test_example_02_ast_contains_core_structures(self) -> None:
        path = ROOT / "v2" / "examples" / "02-综合示例.jps"
        doc = jps2svg2.Parser(
            str(path), path.read_text(encoding="utf-8")).parse()
        self.assertEqual(len(doc.systems), 4)
        self.assertEqual(len(doc.slurs), 1)
        self.assertEqual(len(doc.crescendos), 1)

        kinds: set[str] = set()

        def walk(value: object) -> None:
            if hasattr(value, "__dataclass_fields__"):
                kinds.add(type(value).__name__)
                for field in value.__dataclass_fields__:
                    walk(getattr(value, field))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)

        walk(doc)
        for kind in ("TupletEvent", "VoiceBlock", "LyricLine", "Measure",
                     "NoteEvent", "Bar"):
            self.assertIn(kind, kinds)

    def test_diagnostic_format_and_measure_validation(self) -> None:
        doc = parse_text(
            "@version 2.0\n"
            "@key 1=C\n"
            "@time 4/4\n"
            "V1: 1 2 3 |\n")
        found = [d for d in errors(doc) if d.code == "measure-duration"]
        self.assertEqual(len(found), 1)
        rendered = found[0].format("demo.jps")
        self.assertRegex(
            rendered,
            r"^demo\.jps:\d+:\d+: error: .+ \[measure-duration\] \(5\.6\)$")

    def test_lenient_downgrades_recoverable_errors(self) -> None:
        text = (
            "＠version 2.0\n"
            "@key 1=C\n"
            "@time 4/4\n"
            "V1: ｜ 1 2 3 ｜\n")
        strict = parse_text(text)
        self.assertTrue(errors(strict))
        lenient = parse_text(text, lenient=True)
        self.assertEqual(errors(lenient), [])
        self.assertTrue(any(d.code == "fullwidth" for d in lenient.diagnostics))

    def test_comment_lines_do_not_end_system(self) -> None:
        doc = parse_text(
            "@version 2.0\n"
            "@key 1=C\n"
            "@time 4/4\n"
            "V1: 1 2 3 4 |\n"
            "% 注释行不能结束系统\n"
            "L: 一 二 三 四\n")
        self.assertEqual(errors(doc), [])
        self.assertEqual(len(doc.systems), 1)
        self.assertEqual(len(doc.systems[0].lyrics), 1)

    def test_inline_change_carries_to_next_system(self) -> None:
        doc = parse_text(
            "@version 2.0\n"
            "@key 1=C\n"
            "@time 4/4\n"
            "V1: 1 2 3 4 |\\time(3/4)\n"
            "V1: 1 2 3 |\n")
        self.assertEqual(errors(doc), [])
        self.assertEqual(
            doc.systems[1].voices[0].measures[0].expected_duration, 3)

    def test_final_bar_must_be_at_end(self) -> None:
        doc = parse_text(
            "@version 2.0\n"
            "@key 1=C\n"
            "@time 4/4\n"
            "V1: 1 2 3 4 |] 5 6 7 1' |\n")
        self.assertTrue(any(d.code == "final-bar" for d in errors(doc)))

    def test_multi_measure_rest_counts_for_alignment(self) -> None:
        doc = parse_text(
            "@version 2.0\n"
            "@key 1=C\n"
            "@time 4/4\n"
            "V1: | 0\\measure(2) | 1 2 3 4 |\n"
            "V2: | 5, - - - | 5, - - - | 5, - - - |\n")
        self.assertEqual(errors(doc), [])
        self.assertEqual(
            len(doc.systems[0].voices[0].measures), 2)
        self.assertEqual(
            len(doc.systems[0].voices[1].measures), 3)

    def test_x_tie_is_allowed(self) -> None:
        doc = parse_text(
            "@version 2.0\n"
            "@key 1=C\n"
            "@time 4/4\n"
            "V1: x~ x x x |\n")
        self.assertEqual(errors(doc), [])
        notes = [
            ev for ev in doc.systems[0].voices[0].measures[0].events
            if isinstance(ev, jps2svg2.NoteEvent)
        ]
        self.assertTrue(notes[0].tie)
        self.assertTrue(notes[1].tie_from_prev)


if __name__ == "__main__":
    unittest.main()
