# 番茄简谱脚本规范

本仓库收录**番茄简谱脚本**（`.jps`，jianpu script）的规范文档及配套工具。
番茄简谱脚本用纯文本描述简谱，可以像打字一样快速录入曲谱。

## 规范版本

- [v1.0 规范](v1/README.md)：现行规范，包含简介、描述头、曲部分、歌词部分等章节。
- [v2.0 规范（草案）](v2/README.md)：重构中的设计草案，包含规范正文、v1 迁移指南、语法摘要和示例。

## 目录结构

```
v1/      v1.0 规范正文（00-封面 ~ 05-建议）、SUMMARY.md 及图片资源
v2/      v2.0 草案：规范正文、v1 迁移指南、grammar.ebnf、examples/
bin/     工具：jps2svg.py（本地 .jps → SVG 渲染器）、jps2svg2.py（v2.0 解析器/AST dump）、jps1to2.py（v1.0 → v2.0 转换器）、jianpu.py（调用在线接口输出图片/PDF）
```

## 本地工具

```sh
# 渲染单个曲谱为 SVG
bin/jps2svg.py demo/009-排排坐.jps

# 按 v2.0 规范解析并输出 AST JSON（默认不做 SVG 渲染）
bin/jps2svg2.py v2/examples/02-综合示例.jps

# 只输出便于人工检查的摘要
bin/jps2svg2.py --summary v2/examples/01-排排坐.jps

# v2 解析器最小验收测试
python3 tests/test_v2_parser.py

# 宽松模式：全角语法字符自动转换，小节时值等可恢复错误降级为告警
bin/jps2svg2.py --lenient v2/examples/01-排排坐.jps

# 把 v1.0 曲谱转换为 v2.0 草案
# 在源文件旁生成 009-排排坐.v2.jps 和 009-排排坐.v2.report.txt
bin/jps1to2.py demo/009-排排坐.jps

# 批量转换到指定目录
bin/jps1to2.py -o out/ demo/*.jps

# 只输出到标准输出（报告仍写到 stderr）
bin/jps1to2.py --stdout demo/009-排排坐.jps
```

`jps1to2.py` 按 [v2.0 迁移指南](v2/02-v1迁移指南.md) 做结构化转换，不是简单字符替换。无法自动判定的地方会写入 `% TODO:` 注释和 `.report.txt`，转换后仍需人工复核。默认不覆盖已有输出；需要覆盖时加 `--force`。

## 相关链接

- 官网：<http://www.jianpu99.net/>
- 编辑器：<http://zhipu.lezhi99.com/>
- 文档：<http://doc.lezhi99.com/zhipu>
