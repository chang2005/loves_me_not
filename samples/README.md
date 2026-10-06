# 示例数据

这里的文件都是**手写合成的对话**，用于覆盖各种导出格式与边界情况。
**不是任何人的真实聊天记录**，可以放心查看、修改、再生成。

| 文件 | 格式 | 覆盖的场景 |
|---|---|---|
| [`demo_two_block_warm.txt`](demo_two_block_warm.txt) | 两行式时间戳 | 双方都主动、有深夜消息、有亲昵称呼（29 天） |
| [`demo_two_block_cooling.txt`](demo_two_block_cooling.txt) | 两行式时间戳 | 逐步降温：回复变慢、称呼消失、单方面推进（203 天） |
| [`demo_paste_oneline.txt`](demo_paste_oneline.txt) | 单行粘贴式 `昵称  日期 时间` | 含一条撤回系统消息 |
| [`demo_table.csv`](demo_table.csv) | 表格 `localId,Time,Sender,Content` | 列名顺序与中文表头两种匹配路径 |
| [`demo_too_short.txt`](demo_too_short.txt) | 两行式时间戳，仅 5 条 | **样本不足**：用来验证「不够就说不够，不得虚构结论」 |

## 用它们试一试

```bash
# 先核对解析
python -m loves_me_not inspect "samples/demo_two_block_cooling.txt"

# 生成报告
python -m loves_me_not analyze "samples/demo_two_block_cooling.txt" \
    --me "我" --peer "阿澈" -o out/report.html
```

`demo_too_short.txt` 值得单独跑一次：它会触发报告顶部那条
「⚠ 样本不足」横幅，并把 8 个维度全部标成 `N/A`——
这是这个项目最重要的一道闸门。

## 换成你自己的记录

把导出的文件放到 `data/` 下（该目录已在 `.gitignore` 中，不会被提交），
然后照 [README](../README.md#-使用方法) 里的说明运行即可。

> `samples/local/` 也被忽略，方便你放自己的测试样本而不担心误提交。
