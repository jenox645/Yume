# Gold set results

Measured 2026-11-01 on an RTX 3060 12 GB, faster-whisper 1.2.1, ctranslate2 4.8.4, float16.
39 clips kept of 50: 26 of speech (150 s each) and 13 songs (up to 5 min). References are captions written
by people and timed lyrics from LRCLIB. Errors are counted on readings for JA, ZH and KO, and on words for
RU and AR, through Yume's own filters. 11 clips were dropped: no lyrics, a reference that is not what is
said (over 60% error on the base run), or one too clean to be a person's (under 1.5%).

Base (turbo, beam 5, songs from Demucs vocals): speech 7.7% median, songs 17.1% median, by language:

| | JA | ZH | KO | RU | AR |
|---|---|---|---|---|---|
| Speech | 9.1 | 6.2 | 11.3 | 18.5 | 17.4 |
| Songs | 8.3 | 31.8 | 11.9 | 32.6 | 52.9 |

Change against the base, per clip:

| Change | Clips | Median | Better / worse | Whisper time | Verdict |
|---|---|---|---|---|---|
| Beam 1 | Speech | 0.0 | 9 / 10 | -32% | No: mean +3.2, more loops |
| Beam 2 | Speech | 0.0 | 8 / 7 | -18% | No: mean +1.0 |
| Beam 1 | Songs | +0.4 | 6 / 7 | -18% | No gain |
| large-v3 | Speech | -0.3 | 14 / 11 | ×3.1 | No gain for 3× the GPU time |
| large-v3 | Songs | -1.9 | 11 / 2 | ×2.5 | Yes in principle: see below |
| Mix instead of vocals | Songs | +0.5 | 5 / 7 | same | No: mean +2.7, Demucs stays |
| large-v3 on the mix | Songs | -2.9 | 8 / 5 | ×2.7 | Not better than large-v3 on vocals |
| Mute quiet vocal spans (0.1 of the 95th percentile) | Songs | +4.2 | 4 / 8 | same | No: 56% more words lost |
| Hide lines over compression ratio 2.4 | All | +1.9 speech, 0.0 songs | - | same | No: 34% more song words lost, repeated lyrics are real lines |

Filters: raw Whisper against shown lines improves most clips (one Korean talk 142% → 25%), and costs 0.7 and
1.2 points on two Chinese talks. Nothing to win there.

large-v3 on songs is the only win, and 11 / 2 is p = 0.02 before allowing for 12 comparisons. It also needs
4.4 GB of VRAM against 2.4 GB, and a model swapped per job. Decide it with the VRAM budget of the translator test.

Thin: KO is 3 talks and 2 songs, RU and AR are 2 songs each, and JA songs are mixed (one of the four got worse).
Rerun with `python tests/gold/gold.py run base v3 ...` and `score`; data stays in `tests/gold/data/`.
