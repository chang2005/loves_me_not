# Sample data

[中文](README.md) · **English**

Every file here is a **hand-written synthetic conversation** covering the various
export formats and edge cases. **None of it is anyone's real chat log** — feel free to
read, modify and regenerate.

| File | Format | What it covers |
|---|---|---|
| [`demo_two_block_warm.txt`](demo_two_block_warm.txt) | two-line timestamps | Both sides initiate, late-night messages, pet names (29 days) |
| [`demo_two_block_cooling.txt`](demo_two_block_cooling.txt) | two-line timestamps | Gradual cooling: slower replies, pet names fading, one-sided effort (203 days) |
| [`demo_paste_oneline.txt`](demo_paste_oneline.txt) | single-line paste style | Includes a recall notice |
| [`demo_table.csv`](demo_table.csv) | table | Column-order and Chinese-header matching paths |
| [`demo_too_short.txt`](demo_too_short.txt) | two-line, only 5 messages | **Insufficient sample**: verifies "say so when there isn't enough, never fabricate" |
| [`demo_export.json`](demo_export.json) | structured JSON | Session object + 14 message types; includes a "sync record from my second device" |
| [`demo_export_webfile.html`](demo_export_webfile.html) | single-file web page | Data embedded in `window.WEFLOW_DATA`; bodies carry time labels, images, inline emoji and quotes |

## Try them

```bash
python -m loves_me_not inspect "samples/demo_two_block_cooling.txt"
python -m loves_me_not analyze "samples/demo_two_block_cooling.txt" \
    --me "我" --peer "阿澈" -o out/report.html
```

Three are worth running individually: **`demo_too_short.txt`** triggers the
"⚠ insufficient sample" banner and marks all 8 dimensions `N/A`;
**`demo_export.json`** verifies speaker direction and message-type mapping;
**`demo_export_webfile.html`** verifies web data extraction (the time label wrapping
each body must be stripped, or the statistics get polluted by timestamp strings).

## Using your own records

Drop your export into `data/` or `samples/local/` (both are in `.gitignore`) and run
as described in the [README](../README.en.md#-usage).
