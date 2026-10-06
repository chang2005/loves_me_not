# 示例报告

这个目录里的报告**全部由 `samples/` 下的合成数据生成**，
不是任何人的真实聊天记录。可以直接双击打开看效果。

| 文件 | 对应数据 | 分数与结论 |
|---|---|---|
| [`report-warm.html`](report-warm.html) | `samples/sample_wechat_warm.txt`（互相都在投入的 29 天） | 63 / 100 · 有点平淡了（样本偏少，已收缩） |
| [`report-cooling.html`](report-cooling.html) | `samples/sample_wechat_cooling.txt`（从热到冷的 203 天） | 54 / 100 · 已经在变淡 |
| [`report-csv.html`](report-csv.html) | `samples/sample_memotrace.csv`（留痕风格 CSV） | 56 / 100 · 有点平淡了 |
| [`report-insufficient-sample.html`](report-insufficient-sample.html) | `samples/sample_tiny.txt`（只有 5 条消息） | **样本不足，无法给结论**——8 个维度全部标 N/A |
| `report-*.json` | 机器可读的完整结果 | 维度分、权重、贡献、分段趋势、证据都在里面 |

重新生成：

```bash
python -m loves_me_not analyze "samples/sample_wechat_cooling.txt" \
    --me "我" --peer "阿澈" -o demo/report-cooling.html --json demo/report-cooling.json
```

> **最值得看的两份**：`report-cooling.html` 展示完整的八维分析，一眼就能看出
> 「回复速度还行、但称呼在消失、收尾全是 TA、情绪在变冷」这条叙事线；
> `report-insufficient-sample.html` 展示产品里最重要的一道闸门——
> 样本不够时**明确说不知道**，而不是编一个数字。
>
> 想试脱敏：给命令加上 `--redact`。合成数据里没有手机号之类的串，
> 所以输出内容与不加时一致；换成你自己的记录就会看到打码效果。

## 看报告时建议留意这几处

1. **顶部**：分数、等级、一句话结论，以及「这个分数有多可信？」的可展开说明。
   样本不足时这里会先出现一条黄色横幅——那是设计里最重要的一道闸门。
2. **维度明细**：每个维度都有双方分数条与子指标数据块；样本不足的维度标 `N/A`，
   并明确写出「这一项没有参与打分」，**不会被当成 0 分**。
3. **支撑结论的原话**：每一句都来自文件，未改写。自己复核比相信分数更重要。
4. **分数是怎么算出来的**：完整的权重与贡献分账，可以逐条追问「这 54 分怎么来的」。
5. **写在这里的话**：分档安慰文案，承认人心会变，不指责、不说教。

## 关于示例数据的说明

`samples/` 里是**手写合成的**对话，用于覆盖各种格式与边界情况：

- `sample_wechat_warm.txt` —— 两行式 header，双方都主动、有深夜消息、有亲昵称呼
- `sample_wechat_cooling.txt` —— 同样的格式，但逐步降温（回复变慢、称呼消失、单方面推进）
- `sample_memotrace.csv` —— 留痕 MemoTrace 风格的 CSV（`localId,Time,Sender,Content`）
- `sample_tiny.txt` —— 只有 5 条消息，用来验证「样本不足时必须明确提示，不得虚构结论」

**这些不是真实记录。** 想分析自己的聊天记录，请按 `README.md` 里的说明导出后本地运行。
