<div align="center">

# 🌼 loves-me-not

**TA 到底爱不爱我**

撕开聊天记录的花瓣，看看 TA 爱不爱你。

把导出的聊天记录在**本机**算成 0–100 的情感投入指数，
生成一份可直接分享的**单文件 HTML 报告**。

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-0-2fa37a)](#-安装)
[![Local Only](https://img.shields.io/badge/数据-仅本地处理-e1487f)](#-注意事项)
[![Tests](https://img.shields.io/badge/tests-245%20passed-2fa37a)](#-测试)
[![License](https://img.shields.io/badge/license-MIT-8a4fd8)](LICENSE)

**中文** · [English](README.en.md)

[功能](#-功能) · [安装](#-安装) · [使用](#-使用) · [评分](#-评分) ·
[报告](#-报告) · [注意事项](#-注意事项)

</div>

---

> **这是「娱乐 + 自我反思」工具，不是关系裁判。**
> 它不知道对方在想什么，只数得清谁先开口、谁回得快、谁在收尾。
> 分数低不等于他不爱你，分数高也不等于你们会一直在一起。
>
> **全流程在本机完成**：记录不出你的设备，不上传、不联网、不调用云端 AI。

---

## 🎯 功能

**输入**：`.txt` `.log` `.csv` `.json` `.html` —— 容错解析，自动识别说话人、
时间戳、消息内容，自动探测编码（UTF-8 / GB18030 / Big5 / UTF-16）。

**五个量化指标**：回复速度 · 每 5 分钟消息数 · 深夜消息数 · 打破僵局次数 ·
最后发言次数。每一项都印出**计算口径与数据来源**。

**八个描述性维度**：回复间隔 · 主动发起 · 平均字数 · 提问追问 · 称呼与表情 ·
谁在结束对话 · 深夜活跃 · 情绪走向。全部做**双方对比**。

**可解释打分** + **14 页可视化报告**（雷达、趋势、热力图、词云、画像、时间线、
原话证据链、完整权重分账）。单文件 HTML，零外部请求，双击即开。

<p align="center">
  <img src="docs/screenshot-verdict.webp" alt="报告首屏：环形仪表盘、等级结论与可信度" width="820">
</p>

---

## 📦 安装

**零第三方依赖**，只要 Python 3.9+。

```bash
git clone https://github.com/chang2005/loves_me_not.git
cd loves_me_not
python --version     # 需要 3.9+
```

Windows 上若 `python` 不可用，换成 `py`。

> 「记录不出本机」是这个项目唯一值得被信任的理由，所以解析、统计、词典、
> 图表全部自己实现。测试里有一条护栏扫描包内 import，引入网络依赖就直接失败。

---

## 🚀 使用

```bash
# 1. 先核对解析结果（强烈建议）
python -m loves_me_not inspect "path/to/chat.txt"

# 2. 生成报告
python -m loves_me_not analyze "path/to/chat.txt" \
    --me "我的昵称" --peer "TA的昵称" \
    -o out/report.html --json out/result.json

# 3. 要发给别人看？先打码
python -m loves_me_not analyze "path/to/chat.txt" \
    --me "我" --peer "TA" --redact -o out/report.html
```

`inspect` 会打印识别到的**说话人、条数、时间范围、跳过行数**。
**输入错了，分数就没有意义**，所以先看这一步。

| 参数 | 说明 |
|---|---|
| `--me` / `--peer` | 「我」和「TA」的昵称。不指定时按出现顺序推断，并**在报告顶部警告你核对**——搞反了整份结论会颠倒 |
| `-o, --output` | 报告路径，默认 `out/report.html` |
| `--json` | 额外输出机器可读结果（维度分、权重、贡献、趋势、证据） |
| `--redact` | 打码手机号、身份证、卡号、邮箱、账号、地址 |
| `--format` | `auto`（默认）/ `text` / `csv` / `json` / `html` |

退出码：`0` 成功，`2` 解析失败或参数有误。**失败时不生成任何报告。**

### 支持的输入

默认 `auto`：**先看扩展名，再看内容特征**，后缀写错甚至没有扩展名也能认出来。

| 格式 | 关键规则 |
|---|---|
| `.txt` `.log` | 两行式（时间 + 昵称 / 正文）、粘贴式（昵称在前）、`昵称: 内容` 单行式。容错方括号时间戳、`昵称(账号)`、只有时间的 `[21:33:02]`（跨天进位）、多行消息合并；系统消息与撤回提示自动排除 |
| `.csv` | 按列名模糊匹配，支持中英文表头与无表头 |
| `.json` | 时间支持 Unix 秒/毫秒、ISO 8601、`2023年4月1日 21:33`；正文取 `content`/`text`/`msg`/`body` 中第一个存在的；说话人先看 `isSend`，对方名字取 `remark` > `displayName` > `nickname` |
| `.html` | 数据内嵌在页面里，**先把数据抠出来**再解析（支持 `window.X = [...]`、`<script type="application/json">`）；用括号配平扫描，正文里有花括号也不会截断 |

具体样例见 [`samples/`](samples/README.md)。

> [!NOTE]
> 两处刻意的保守设计：
>
> - **判不出来源的记录不会被硬塞给某一方。** 自己另一台设备的同步记录
>   （`isSend` 为 0、但发送人 ID 就是你自己）会当作系统消息排除，
>   而不是算成「对方说了话」——宁可少几条，也不能把关系算反。
> - **认不出来就报错。** 网页里没有聊天数据、JSON 里没有消息数组、
>   文件被截断，一律明确报错，不会编一份出来。

---

## 🧮 评分

```text
总分 = 八维模型 × 60% + 五个量化指标 × 40%     （再按样本量向 50 收缩）
```

### 五个量化指标（只看 TA 的行为信号）

| 指标 | 计算口径 |
|---|---|
| **回复速度** | TA 每次回复与上一条对方消息的时间差（仅统计 ≤24 小时），取**中位数**；5 分钟 ≈ 50 分 |
| **每 5 分钟消息数** | 按 5 分钟分桶，取**有消息的窗口**的平均条数；另给峰值 |
| **深夜消息数** | 23:00–03:00 的条数及占 TA 总量的比例（20% ≈ 100 分） |
| **打破僵局次数** | 沉默 ≥24 小时后由谁先开口；TA 破冰 ÷ 总破冰 |
| **最后发言次数** | 每段对话最后一条的归属；TA 占 50% 最均衡 |

综合权重：**回复 30% / 破冰 25% / 收尾 20% / 爆发 15% / 深夜 10%**。

两个反直觉但刻意的设计：**深夜权重最低**（凌晨消息多是情感浓度，不是投入度）；
**破冰与收尾是对称的**（各半满分，全由一方承担才扣分）——
「TA 每次都收尾」不等于「TA 更爱你」。

### 三条不可让步的规则

| 规则 | 含义 |
|---|---|
| **没有信息 ≠ 0 分** | 维度算不出来时**剔除该维度并重新归一化权重**，报告里显示 `N/A`。全程没有亲昵称呼不代表「TA 不爱你」，只是没有信息 |
| **样本不足要收窄结论** | 分数向 50 收缩、可信度降级、报告顶部打出「⚠ 样本不足」横幅。只有 5 条消息时直接输出「样本不足，无法给结论」，8 个维度全标 `N/A` |
| **绝不虚构** | 解析不出消息直接报错退出（码 2）。被剔除的统计量在卡片上显示「—」并注明原因，不会把「2 次回复的中位数」当结论 |

### 等级结论

| 分数 | 等级 | 分数 | 等级 |
|---|---|---|---|
| 85–100 | 还是很爱你 | 40–54 | 已经在变淡 |
| 70–84 | 热度在线 | 20–39 | 基本没戏 |
| 55–69 | 有点平淡了 | 0–19 | 几乎可以放手了 |

消息太少时输出「样本不足，无法给结论」——任何分数都是噪声。

---

## 🖼 报告

单文件 HTML，**14 页，一页一页往下翻**（CSS 滚动吸附）。
宽屏是左侧固定导航，窄屏折叠为顶部横滑标签栏。
内容超过一屏的页会自然变高，不为了塞进一屏而压缩可读性。

| 操作 | 效果 |
|---|---|
| 滚轮 / 触摸板 | 翻到上/下一页（吸附对齐，不会停在半页） |
| `↓` `↑` / `PageDown` `PageUp` / 空格 | 上下翻页 |
| `Home` `End` | 第一页 / 最后一页 |
| 点击左侧导航 | 直接跳到该页 |

<p align="center">
  <img src="docs/screenshot-quantifiers.webp" alt="五个量化指标，附计算口径与数据来源" width="440">
  <img src="docs/screenshot-heatmap.webp" alt="日历热力图" width="440">
</p>

**五个量化指标**写明口径、来源、权重与贡献，可自行复核。
**日历热力图**的颜色深浅 = 当天各段对话时长之和——不是条数，
也不是首尾跨度（早上说一句、晚上说一句不该算成聊了一整天）。

<p align="center">
  <img src="docs/screenshot-topics.webp" alt="话题词云" width="440">
  <img src="docs/screenshot-timeline.webp" alt="关键节点时间线" width="440">
</p>

**话题词云**字号 = 加权词频，颜色 = 主要由谁说起。
**关键节点**按时间排列第一次说话、最长一次聊天、最长沉默、最暖与最冷的一天、热度转折点。

其余可视化：聊天足迹、八维雷达、互动趋势折线、「TA 是怎样的人」（8 类行为画像，
只描述可观测行为，明确声明不是性格判断）、原话证据链、完整权重分账。

**结尾**：分档安慰文案之外，还有一句**只属于这份记录**的话——从真实统计里
挑偏离常规最远的观察，偏积极时是夸赞，偏弱时是温和鼓励；挑不出来就不写。

> 入场动效有一条硬约束：**元素默认可见**，隐藏态由脚本启用；
> 可见态必须能压过隐藏态且不依赖过渡推进。
> （「内容因动效永久隐形」是这个项目踩过三次的坑，每次都已写成测试。
> 排查细节见 [`SKILL.md`](SKILL.md)。）

---

## ⚠️ 注意事项

聊天记录涉及双方隐私，请确认你已获得对话参与者授权，或这些记录是你自己的；
只在本地处理，不上传到任何网盘、在线服务或公开 demo；不要用它跟踪、监视、
控制任何人。**本项目不读取微信数据库，也不绕过任何平台的安全机制**——
只接受你自己导出的文件，永不内置任何解密功能。

`data/` 与 `out/` 已在 `.gitignore` 中，真实记录不会被提交进版本库。

**本报告基于统计学规律生成，仅供娱乐与自我反思，不代表任何一方的真实情感，
重大情感决策请咨询线下专业人士。**

三点使用提醒：

- 情感分析用**本地词典**，反讽、方言、网络梗会误判；每条结论都附原始片段，请以原话为准。
- 报告**只看得见文字**。沉默可能是冷淡，也可能是他那天加班到十一点、手机没电。
- 与其对着一个数字反复揣测，不如直接问一句。

**已知局限**：中文 n-gram 没有分词器，词云偶尔切出无意义片段；
群聊降级为「你 + 互动最多的那一位」；图片、语音、表情包的内容无法分析，
只作为「发生过一次互动」计入条数。

---

## 🧪 测试

```bash
python -m unittest discover -s tests -v
```

**245 项**。其中一批是**守住产品承诺的护栏**，改动核心逻辑时不要删：

- 无信息的维度必须剔除，而不是当成 0 分
- 样本不足必须降级，不得输出强结论；采样不足的指标 `score` 必须是 `None`
- 更暖的记录必须得分更高（打分方向不能反）
- 深夜时段在 metrics / insights / 报告里必须是同一口径（23:00–03:00）
- 「聊了多久」不能用首尾跨度冒充；「谁收尾」「谁破冰」必须对称
- 行为画像只能说行为，不能出现人格判决措辞
- 个性化文案里的数字必须与真实统计对得上
- 词云不得重叠；仪表盘 dasharray 必须与分数成比例
- 报告必须自包含；`[data-reveal]` 的基础规则里不许出现 `opacity: 0`
- 可见态必须能压过隐藏态；揭示不许每帧遍历全部元素
- 侧栏点击必须由脚本接管；结构化解构的时间标签必须剥掉
- 认不出来的 JSON / 网页必须报错，不得猜
- 内联脚本必须通过 `node --check`；包内不许有任何网络或子进程依赖

---

## 📂 项目结构

```text
loves-me-not/
├── SKILL.md                 # Skill 定义（给 Agent 读的接口说明与边界）
├── README.md / README.en.md # 中 / 英说明
├── docs/                    # README 用的截图
├── loves_me_not/
│   ├── __main__.py          # CLI：analyze / inspect
│   ├── parser.py            # 容错解析：txt / csv → Message[]，含格式识别
│   ├── structured.py        # JSON / 网页内嵌数据解析（字段名容错）
│   ├── lexicon.py           # 情感词典 + 亲昵称呼 + 疑问词
│   ├── metrics.py           # 八个描述性维度
│   ├── insights.py          # 五个量化指标 + 会话分段 + 沉默 + 日历聚合
│   ├── timeline.py          # 聊天足迹 + 关键节点 + 话题 + 行为画像
│   ├── visuals.py           # 热力图 / 词云 / 画像 / 时段与月度图
│   ├── scoring.py           # 可解释打分、等级、安慰文案
│   └── report.py            # 单文件 HTML / JSON 生成
├── samples/                 # 合成示例数据（非真实记录）
├── demo/                    # 由示例数据生成的成品报告
└── tests/                   # 245 项单元测试
```

`demo/` 里是**合成数据**生成的成品报告，双击即开：
[`report-cooling.html`](demo/report-cooling.html)（203 天，58 分）·
[`report-warm.html`](demo/report-warm.html)（29 天，64 分）·
[`report-too-short.html`](demo/report-too-short.html)（5 条 → **样本不足**）

---

## 📈 Star History

<a href="https://www.star-history.com/?repos=chang2005%2Floves_me_not.git&type=timeline&logscale=&legend=bottom-right">
  <picture>
    <source media="(prefers-color-scheme: dark)"
            srcset="https://api.star-history.com/svg?repos=chang2005/loves_me_not&type=Timeline&legend=bottom-right&theme=dark">
    <img alt="Star History Chart"
         src="https://api.star-history.com/svg?repos=chang2005/loves_me_not&type=Timeline&legend=bottom-right"
         width="700">
  </picture>
</a>

<details>
<summary><b>怎么用这张图表</b></summary>

Star History 提供**动态 SVG 接口**，直接当图片用；用链接包一层就能点进交互式图表：

```markdown
[![Star History Chart](https://api.star-history.com/svg?repos=chang2005/loves_me_not&type=Timeline&legend=bottom-right)](https://www.star-history.com/?repos=chang2005%2Floves_me_not.git&type=timeline&logscale=&legend=bottom-right)
```

图片每次打开都重新生成，**Star 涨了会自己更新**，不用改 README。

| 参数 | 作用 |
|---|---|
| `repos=` | 仓库，格式 `用户名%2F仓库名`；多个用 `,` 分隔可对比 |
| `type=` | `timeline`（接口写作 `Timeline`），也支持 `date` |
| `logscale=` | 留空为线性，填 `1` 用对数刻度——Star 从个位数涨到几千时更好看 |
| `legend=` | 图例位置，多仓库对比时才有意义 |

其他方式：**iframe**（可交互，但 GitHub 会拦，适合自己的博客）、
**多仓库对比**（`...?repos=owner/a,owner/b`）、**导出 PNG**（网站图表右上角下载）。

常见坑：图空白（仓库还没被 Star 或非公开）、图不更新（CDN 缓存，加 `&v=2`）、
只有一条直线（Star 太少，试试 `logscale=1`）。

</details>

---

## 🤝 贡献

欢迎 Issue 和 PR。改动核心逻辑时请：

1. **不要**加入任何网络调用或云端 AI——「全本地」是用户敢用的唯一理由；
2. **不要**在样本不足时输出强结论，也不要把「无信息」当成 0 分；
3. **不要**在 `[data-reveal]` 的基础规则里写 `opacity: 0`，
   也不要让可见态特异性低于隐藏态；
4. 改打分口径时同步更新本文件与 `SKILL.md` 的权重表；
5. 跑一遍 `python -m unittest discover -s tests -v`，保持全绿。

---

## 📜 许可

MIT。用它之前，请先对得起聊天记录另一端的那个人。

<div align="center">
<br>
<sub>🌼 she/he loves me, she/he loves me not…</sub>
</div>
