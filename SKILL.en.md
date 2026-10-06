---
name: loves-me-not
description: >-
  Analyze chat logs exported from WeChat / QQ to compute a 0-100 emotional
  investment score with a tier verdict, and render a single-file HTML visual
  report (score gauge, 8-dimension radar, interaction trends, key data cards,
  representative quotes and a gentle closing). Use when the user wants to
  analyze a chat log, asks "does TA love me or not", wants reply speed /
  initiative / message length / sentiment statistics for both sides, or needs
  a chat log turned into a visual report. Runs fully locally; the chat log
  never leaves the device.
---

# Does TA love me or not · chat log analysis

[中文](SKILL.md) · **English**

Turns one exported chat log into a relationship observation report
**you can check yourself**.

## What this Skill is, and isn't

| | |
|---|---|
| **Is** | An entertainment and self-reflection tool. It draws interaction patterns into charts using statistical regularities. |
| **Isn't** | A relationship judge, and not mind-reading. It can count who spoke first, who replied faster and who ended the conversation, but it cannot see people. |
| **Is** | Fully local. No network, no uploads, no cloud AI. |
| **Isn't** | A WeChat database decryption tool. It only accepts files the user exported. |

**Before running, you must:**

1. Confirm the user has the participants' consent, or that the records are their own;
2. Never ask for, and never send, the raw chat log to any online service;
3. Keep the fixed disclaimer in the report — **do not remove it**.

## Quick start

```bash
# 1) Verify the parse first (strongly recommended: a wrong input makes the score meaningless)
python -m loves_me_not inspect "path/to/chat.txt"

# 2) Generate the report
python -m loves_me_not analyze "path/to/chat.txt" --me "my nickname" --peer "their nickname" \
    -o out/report.html --json out/result.json

# 3) Sharing the report? Redact first
python -m loves_me_not analyze "path/to/chat.txt" --me "me" --redact -o out/report.html
```

No third-party dependencies; Python 3.9+ and the standard library are enough.

## Input formats and parsing

`parse_file()` chooses a path by extension first, then by content:

| Extension | Path | Implementation |
|---|---|---|
| `.txt` `.log` `.text` | line-based text | `parser.parse_text` |
| `.csv` | table | `parser.parse_csv` |
| `.json` | structured | `structured.parse_structured` |
| `.html` `.htm` | embedded web data | `structured.parse_structured` |
| anything else / no extension | content sniffing | `parser._detect_format` |

Text tolerance: two-line style (timestamp + nickname, then body), `nickname(12345678)`,
bracketed timestamps, nickname before the timestamp, time-only `[21:33:02]` (dates are
inferred and roll over midnight), single-line `nickname: text`; multi-line messages are
merged; `撤回了一条消息` / `加入群聊` and similar system notices are flagged separately;
`[图片]` / `[表情]` don't count toward message length; UTF-8 / GB18030 / Big5 / UTF-16 are
auto-detected; group chats degrade to "you + the most interactive person" with a note in the report.

### Structured parsing notes (`structured.py`)

- **Time**: `parse_time_value()` handles Unix seconds/milliseconds, ISO 8601,
  `2024/01/01 08:00`, `2024年1月1日 08:00`. Unrecognized input returns `None` — no guessing.
- **Body**: the first present key from `TEXT_KEYS`. A single-letter weak field (`b`)
  is only accepted when other message traits are present (`t`+`s`, or
  `createTime`/`isSend` etc.); otherwise it errors — so an arbitrary JSON with a `b`
  field is never mistaken for a chat log.
- **Speaker**: truthy `isSend` → "me"; otherwise compared against the session's `wxid`.
  The peer's name comes from `remark` > `displayName` > `nickname`.
  **Records whose origin can't be determined are excluded as system messages**
  rather than forced onto one side (a sync record from your own second device is
  exactly this case).
- **Body HTML**: `_html_to_text()` must **strip the `message-time` tag first** —
  otherwise every message body starts with a timestamp and the length, topic and
  sentiment statistics are all polluted. Images become `[图片]`; `alt="图片消息"` is
  normalized to `图片`; quoted blocks keep their text.
- **Embedded data extraction**: `extract_embedded_json()` uses **brace balancing**
  rather than regex-matching the whole object (braces inside message bodies would
  truncate it). It scans twice: once by JSON rules (only double quotes delimit strings),
  then by JS rules (single quotes too), to cover JavaScript literals.
- **When it can't tell, it errors**: ordinary web pages, JSON without a message array,
  empty arrays and truncated JSON all raise `ParseError`. It never generates a
  fabricated report.

## The eight descriptive dimensions

| Dimension | What it computes |
|---|---|
| Reply interval and speed | Median / mean reply latency, share replied within 5 minutes, share over 1 hour, asymmetry ratio |
| Initiating conversations | Who opens each conversation round (6h of silence starts a new round), share of consecutive runs, daily first-message ownership |
| Average length and investment | Mean length on both an all-messages and a "substantive" basis; share of very short messages (≤3 chars) |
| Questions and follow-ups | Question share, follow-up rate within 10 minutes, share of questions never answered |
| Pet names and emoji | Pet-name tier and usage, emoji rate, upgrades and downgrades over time |
| Who ends conversations | Last-message ownership per round, rate of walking away from an unanswered question, closing-phrase count |
| Late-night and time-of-day activity | 23:00–03:00 share, each side's active hours |
| Sentiment over time | Local lexicon scoring per message (with negation / intensifier correction), first-half vs second-half trend |

## The five quantified metrics (their behaviour only)

This set is more focused than the eight-dimension model and answers
"how strong are their signals of investment?".
**Definitions and data sources are printed in the report** and must stay in sync with the code.

| Metric | Definition | Source |
|---|---|---|
| Reply speed | Median gap between each of their replies and your previous message (≤24h counts as a reply); 5 minutes ≈ 50 points | timestamps + speakers |
| Messages per 5 min | Bucketed into 5-minute windows; mean across **windows containing messages**; peak also reported | timestamps |
| Late-night messages | Count in 23:00–03:00 and its share of their total (20% ≈ 100 points) | timestamp hours |
| Breaking silence | After a ≥24h silence, who speaks first; their breaks ÷ all breaks | session segmentation (30-min gap) + timestamps |
| Getting the last word | Who sent the final message of each conversation; 50% is most balanced | session segmentation + speakers |

Weights: **reply 30% / icebreak 25% / last word 20% / burst 15% / late night 10%**.
Total = 8-dimension model 60% + quantified metrics 40%; **if one side lacks samples the
other is used — never scored as 0**.

> Two deliberate, counter-intuitive choices:
> 1. **Late night carries the lowest weight** — 3 a.m. messages signal emotional
>    intensity, not investment.
> 2. **Icebreaking and last-word are symmetric** (`balance_score`) — 50/50 is full
>    marks; only one-sided responsibility loses points.
>    "They always get the last word" does not mean "they love you more".

## Chat footprint and key moments

| Metric | Definition |
|---|---|
| Chat span | First message → today; the first-to-last interval is also reported |
| Best chatting hours | The highest-volume three consecutive hours in the 24-hour distribution |
| Busiest / quietest month | By monthly message count |
| Most balanced month | Among months with ≥25% of the peak volume, the one where their share is highest (**proxy metric**) |
| Total chat duration | Sum of session durations (sessions split at gaps >30 min) |
| Longest conversation | The single longest session |
| Longest silence | The largest gap between sessions, plus who broke it |
| Key moments | First / last message, longest chat, longest silence, busiest / longest day, busiest / quietest month, warmest / coldest day, heat turning points |

Heat turning points use **centred windows** comparing the average volume of the two
surrounding months, requiring ≥4 messages on each side and a ≥50% change —
otherwise noise would be reported as a turning point.

## Visualizations

| Chart | Encoding |
|---|---|
| Score ring | `stroke-dasharray` progress (**don't** go back to "arc + needle + ticks" — that needs three angle measures kept consistent and pushes ticks outside the `viewBox`) |
| Calendar heatmap | One cell per day, **shade = sum of conversation durations that day**. **Do not reference any specific platform's styling or name** |
| Topic word cloud | Size = weighted frequency, colour = who mainly raised it; local n-grams + seed words + redundancy suppression, inline SVG |
| "What kind of person TA is" | 8 behavioural persona cards with strength bars and supporting numbers, **explicitly stating they are not a character judgement** |

> The heatmap deliberately uses "sum of conversation durations" rather than
> "first-to-last span": one message in the morning and one at night is not a whole
> day of talking.

Colours have exactly one definition: `PALETTE` in `loves_me_not/visuals.py`, which
`report.PALETTE` references directly. **Change colours in that one place only.**

## Page structure and interaction

A single HTML file, 14 pages, fixed left sidebar:

```text
01 verdict → 02 quantified metrics → 03 investment & key data → 04 footprint
→ 05 calendar heatmap → 06 word cloud → 07 who TA is → 08 key moments
→ 09 8-dimension radar → 10 dimension detail → 11 interaction trends
→ 12 quotes & evidence → 13 how the score was computed → 14 notes & disclaimer
```

**Paging relies on CSS scroll snapping, not JavaScript animation**:

- `html { scroll-snap-type: y proximity }` plus `min-height: 100vh;
  scroll-snap-align: start` on every `.section`
- **It must be `proximity`; don't change it back to `mandatory`** — that recomputes
  the snap point on every scroll and drags the viewport with it, which is the main
  cause of wheel jank
- **Don't write `scroll-behavior: smooth`** — it turns every scroll into an animation
  that fights the snap engine and makes nav clicks get pulled back. Smooth scrolling
  is enabled temporarily only inside `jumpTo()`
- Keyboard paging: `↓` `↑` `PageDown` `PageUp` `Space` `Home` `End`
- Each page shows its number in the corner, e.g. `03 / 14`

**Responsive**: two columns on wide screens (sidebar + content); below `< 1000px` it
collapses to a horizontal top tab bar plus a single column, and below `< 620px` it
tightens further. Implemented in `_css()` with `@media (max-width: 999px)` and
`(max-width: 620px)`; tests assert both breakpoints exist (so nobody deletes them and
crams the layout on phones).

**Sidebar clicks are handled by script** (`jumpTo()`): temporarily disable snapping →
compute the target document coordinate → `scrollTo` → poll until scrolling settles,
then restore snapping. Don't fall back to native anchor behaviour — that is exactly
what caused "clicking does nothing".

Nav titles and section headings share one source, `_nav_items()`. Scroll highlighting
**only touches the DOM when the active item actually changes** (compared via `activeId`).

### Animation safety (read before changing this)

"Content permanently invisible because of its entrance animation" is the most
expensive class of bug in this project; it has happened three times:

1. **Elements are visible by default.** The hidden state hangs off `html.anim-ready`,
   added by the head script only after checking the environment. When JS fails the page
   is a normal document without animations, not a blank screen.
   **Never put `opacity: 0` in the base `[data-reveal]` rule.**
2. **Revealing depends on neither a single event nor a per-frame sweep.**
   An earlier version left "key moments" entirely blank because the scroll callback
   never fired; changing it to "walk every element each frame" then made each scroll do
   dozens of forced synchronous layouts and caused jank. The main path is now
   `IntersectionObserver` (zero layout reads) with a **bounded** in-viewport sweep
   (`sweepLeft`) and a final unconditional `showAll` as backup.
   Don't remove the fallbacks or restore an unbounded poll.
3. **The visible state must beat the hidden state, and must not depend on a transition
   completing.** The hidden rule `html.anim-ready [data-reveal]` (specificity 0,2,1)
   once outranked the visible rule `[data-reveal].is-in` (0,2,0), so elements with
   `.is-in` stayed at `opacity: 0` — the cards inside "quantified metrics" went blank,
   and because section headings remained, an audit that only checked "is the section
   blank" missed it entirely. Now the visible state carries `!important` and the
   `html.anim-ready` prefix, and the entrance uses `@keyframes revealIn` plus
   `animation ... both`, with the **end state written into the keyframe** rather than
   interpolated. A test, `test_revealed_state_beats_hidden_state`, guards both.

The heatmap ripple animation hangs off `.is-in` (played only once revealed); otherwise
it finishes during page load and nobody sees it.

> **When auditing output, don't only check "is the section blank"** — inspect the
> computed opacity of every `[data-reveal]` element, or you'll miss the half-blank
> state where the section is present and its inner cards are all hidden.

## Scoring model (explainable, not a black box)

- Each dimension computes sub-metrics → mapped to 0–100 → weighted into a dimension
  score → weighted into the total.
- **Weights are per perspective**: judging "does TA love me" uses the TA perspective
  (reply 24%, initiative 22%, length 16%, questions 12%, pet names 10%, sentiment 10%,
  last word 8%, late night 6%).
- **No information ≠ 0 points**: an uncomputable dimension is **removed and the weights
  renormalized**, recorded as `skipped` and shown as `N/A` in the report.
- **Insufficient samples narrow the conclusion**: the score shrinks toward 50,
  confidence is downgraded, and a "⚠ insufficient sample" banner appears at the top —
  strong conclusions are refused.
- **Never fabricate**: if no messages parse it exits with an error; empty and binary
  files never produce a report.

### Tier verdicts

| Score | Tier | Score | Tier |
|---|---|---|---|
| 85–100 | still very much in love | 40–54 | already fading |
| 70–84 | warmth is there | 20–39 | basically over |
| 55–69 | a little flat | 0–19 | nearly time to let go |

With too few messages it outputs "insufficient sample, no conclusion".

## Closing copy

Beyond the tier-based comfort copy (`scoring.COMFORT`, one passage per tier), the report
uses `timeline.build_personal_note()` to **grow one sentence out of the real data**.

Eight candidate observations: still replying late at night, one unusually long
conversation, a peak month, replying fast / slow, breaking the silence after a cold war,
a message sent specifically on a holiday, always reaching out first, and one
especially warm day.

- Each candidate is built on numbers already computed; the digits in the copy must match
  the statistics;
- Candidates are scored by "how far from normal" and the highest wins, so different
  records get different sentences;
- A high score lands on praise, a low one on gentle encouragement — neither blames the
  user nor decides for them;
- When nothing stands out it **returns `None` and only the generic copy is shown** —
  better one sentence fewer than a fabricated one.

Tier landing points: **still very much in love** "enjoy it, but don't make it your only
source of security" · **warmth is there** "relationships ebb and flow; rather than
testing it repeatedly, just ask" · **a little flat** "flat isn't the end; many long
relationships pass through this" · **already fading** "you did nothing wrong; you
deserve someone who reaches out" · **basically over** "you've tried hard; seeing clearly
is where moving on starts" · **nearly time to let go** "this isn't giving up on them,
it's picking yourself back up".

All copy obeys one rule: **acknowledge that feelings change, encourage looking forward,
never preach, never blame**. `scoring.COMFORT` is the single source — edit copy there only.

## Code layout

```text
loves_me_not/
├── parser.py     # tolerant parsing: txt / csv → Message[], plus format detection
├── structured.py # JSON / embedded web data parsing (tolerant field names)
├── lexicon.py    # sentiment lexicon + pet names + question words (auditable)
├── metrics.py    # the eight descriptive dimensions
├── insights.py   # five quantified metrics + sessions + silences + calendar
├── timeline.py   # footprint + key moments + topics + persona
├── visuals.py    # heatmap / word cloud / persona / hour & month charts (inline SVG)
├── scoring.py    # weights, tiers, confidence, comfort copy
├── report.py     # single-file HTML / JSON output
└── __main__.py   # CLI: analyze / inspect
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "couldn't recognize a single message" | The file isn't a chat log export (maybe a database, archive or binary). Use `inspect` to look at the head. |
| "looks like a web page but no chat data was found" | It's an ordinary web page, not a single-file export. |
| "field 'b' is too ambiguous" | The JSON only has single-letter fields like `b` and lacks time/sender traits. |
| Speakers are reversed | Pass `--me` / `--peer` explicitly; match the spelling in the file (partial matching works). |
| A third speaker appears | Usually a group chat; possibly a sync record from your own second device. Passing `--peer` excludes it. |
| Very low score on a short log | Check confidence and the "insufficient sample" banner — short samples are shrunk and downgraded. |
| A dimension or metric shows N/A | That dimension lacked samples and **did not contribute to the score**; the report names it. |
| A heatmap day is dark but few messages | The shade encodes "sum of conversation durations", not message count. |
| The word cloud has nonsense words | Local n-grams without a tokenizer occasionally split badly; fragments are suppressed but not perfectly. |
| Sentiment seems off | An inherent limit of a local lexicon (sarcasm, dialect, memes). Trust the quoted excerpts in the report. |
| Sidebar doesn't highlight | JS is disabled or blocked. The nav is still plain anchors and works. |
| Clicking nav does nothing | Check whether `scroll-behavior: smooth` was written back into the CSS. |
| A page opens blank | Check whether the reveal fallbacks were deleted (keep `IntersectionObserver` + bounded sweep + `showAll`). |
| Wheel scrolling janks | Check whether snapping was reverted to `mandatory`, or revealing back to a per-frame walk. |

## Notes when modifying

- **Don't** add network calls or cloud AI — "fully local" is the only reason users dare
  to run this.
- **Don't** build in WeChat database decryption or anything that circumvents platform
  security.
- **Don't** output strong conclusions from insufficient samples, and never score
  "no information" as 0.
- When changing the late-night window you **must change both** `metrics.LATE_NIGHT_*`
  and `insights.LATE_*` and update the README/SKILL copy — a test locks this consistency.
- When changing the "how long we talked" definition, note that `DayStat` has two fields:
  `span_minutes` (first-to-last span, which overstates) and `chat_minutes` (sum of
  sessions, used by the heatmap).
- **Change colours only in `visuals.PALETTE`**: `report.PALETTE` references it directly.
- Read the three "animation safety" points above before touching animations; afterwards
  open the page with JS disabled to confirm it isn't blank.
- Adding or removing pages means editing `_nav_items()`: nav, section headings and page
  numbers all derive from it, and tests assert "nav items == sections == page numbers"
  with every anchor real.
- When changing the personalized copy, keep the referenced numbers derived from real
  statistics and update the checks in `tests/test_insights.py::TestPersonalNote`.
- When changing scoring definitions, update the weight tables in `README.en.md` and
  `SKILL.en.md`.
- **Keep the Chinese and English docs in sync**: `README.md` ⇄ `README.en.md`,
  `SKILL.md` ⇄ `SKILL.en.md`.
- Finish by running `python -m unittest discover -s tests -v`.
