# 示例数据

**中文** · [English](README.en.md)

这里的文件都是**手写合成的对话**，用于覆盖各种导出格式与边界情况。
**不是任何人的真实聊天记录**，可以放心查看、修改、再生成。

| 文件 | 格式 | 覆盖的场景 |
|---|---|---|
| [`demo_two_block_warm.txt`](demo_two_block_warm.txt) | 两行式时间戳 | 双方都主动、有深夜消息、有亲昵称呼（29 天） |
| [`demo_two_block_cooling.txt`](demo_two_block_cooling.txt) | 两行式时间戳 | 逐步降温：回复变慢、称呼消失、单方面推进（203 天） |
| [`demo_paste_oneline.txt`](demo_paste_oneline.txt) | 单行粘贴式 | 含一条撤回系统消息 |
| [`demo_table.csv`](demo_table.csv) | 表格 | 列名顺序与中文表头两种匹配路径 |
| [`demo_too_short.txt`](demo_too_short.txt) | 两行式，仅 5 条 | **样本不足**：验证「不够就说不够，不得虚构结论」 |
| [`demo_export.json`](demo_export.json) | 结构化 JSON | 会话对象 + 14 种消息类型；含「自己另一台设备」的同步记录 |
| [`demo_export_webfile.html`](demo_export_webfile.html) | 网页单文件 | 数据内嵌在 `window.WEFLOW_DATA`；正文带时间标签、图片、内联表情、引用 |

## 试一试

```bash
python -m loves_me_not inspect "samples/demo_two_block_cooling.txt"
python -m loves_me_not analyze "samples/demo_two_block_cooling.txt" \
    --me "我" --peer "阿澈" -o out/report.html
```

三个值得单独跑的：**`demo_too_short.txt`** 会触发「⚠ 样本不足」横幅并把
8 个维度全标 `N/A`；**`demo_export.json`** 验证说话人方向与消息类型映射；
**`demo_export_webfile.html`** 验证网页数据提取（正文外层的时间标签必须剥掉，
否则统计会被时间字符串污染）。

## 换成你自己的记录

把导出的文件放到 `data/` 或 `samples/local/`（两者都已在 `.gitignore` 中），
然后照 [README](../README.md#-使用) 运行即可。
