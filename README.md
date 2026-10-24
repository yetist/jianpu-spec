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
bin/     工具：jps2svg.py（本地 .jps → SVG 渲染器）、jianpu.py（调用在线接口输出图片/PDF）
```

## 本地工具

```sh
# 渲染单个曲谱为 SVG
bin/jps2svg.py demo/009-排排坐.jps
```

## 相关链接

- 官网：<http://www.jianpu99.net/>
- 编辑器：<http://zhipu.lezhi99.com/>
- 文档：<http://doc.lezhi99.com/zhipu>
