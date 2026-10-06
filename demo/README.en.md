# Sample reports

[中文](README.md) · **English**

Every report in this directory is **generated from the synthetic data in
[`samples/`](../samples)** — none of it is anyone's real chat log. Double-click to open.

| File | Score and verdict |
|---|---|
| [`report-cooling.html`](report-cooling.html) | 203 days cooling down → **58 / 100 · a little flat** |
| [`report-warm.html`](report-warm.html) | 29 days of mutual investment → 64 / 100 (small sample, shrunk) |
| [`report-too-short.html`](report-too-short.html) | Only 5 messages → **insufficient sample, no conclusion** (all 8 dimensions `N/A`) |
| `report-cooling.json` | Machine-readable full result |

Two are worth a close look: **`report-cooling.html`** is the full example — you can
read the storyline at a glance ("replies are fine, but pet names are disappearing, they
always get the last word, and the mood is cooling"); **`report-too-short.html`** shows
the single most important gate in the product — when the sample is too small it
**says so plainly** instead of inventing a number.

## What to look at

The report flips one page at a time (14 pages in the left sidebar; wheel or arrow keys):

| Page | What's there |
|---|---|
| 1 · Verdict | Gauge, tier, one-line conclusion, and "how trustworthy is this score?". An insufficient sample shows a yellow banner here |
| 2 · Quantified metrics | Every metric states its **definition and data source** so you can check it |
| 5–7 · Heatmap / cloud / persona | Colours and font sizes have explicit encodings, with definitions below each chart |
| 10 · Dimension detail | Undersampled dimensions are marked `N/A` with "did not contribute to the score" — **never counted as 0** |
| 12 · Quotes and evidence | Every line comes from the file, unedited |
| 13 · How the score was computed | Full weight and contribution breakdown, so you can trace that 58 |
| 14 · Closing | Tier-based comfort copy plus one sentence grown from this specific record |

## Regenerating them

```bash
python -m loves_me_not analyze "samples/demo_two_block_cooling.txt" \
    --me "我" --peer "阿澈" \
    -o demo/report-cooling.html --json demo/report-cooling.json
```

Add `--redact` to try masking; the synthetic data holds no sensitive strings, so the
output is identical.

**None of this is a real chat log.** To analyse your own, export it and run locally as
described in the [README](../README.en.md#-usage).
