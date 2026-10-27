#!/usr/bin/env python3
"""restamp_words.py — put the pauses back into a transcript that was never aligned, measured from its own audio.

WHY THIS EXISTS. The Intel-Mac listener (scripts/transcribe-nofw.py) is faster-whisper on its own: no wav2vec2
alignment pass, because that needs torch and torch has no current Intel-Mac build. Its word times come from
Whisper's attention, and they fail in one consistent direction: a word said after a pause is stamped as starting
INSIDE the pause. Measured on 85 minutes of the creator's own takes against the aligned transcript, a word after a
pause started a median 363 ms early (89 in 100 more than 100 ms early), and 87 in 100 word pairs touched with no gap
at all (1 in 100 on the aligned route). Every cut step downstream was built for an aligned transcript:
transcript-cut.py splits lines and drops dead air at the gaps, snap-to-sound.py looks for each word's onset just
around its stamp. With the gap folded into the next word, a cut opens on the dead air, a breath, or the tail of a
dropped retake, and an Intel buyer hears the line start chopped. That was the report this exists for.

WHAT IT DOES. For each pair of neighbouring words it looks for held quiet between the earlier word's end stamp
and the later word's end stamp (the end stamps are close: a median 47 ms early). When it finds some, the earlier
word ends where the first quiet stretch begins and the later word starts where the last quiet stretch ends, so a
click or a breath in the middle of a pause does not split it. Quiet is measured the way snap-to-sound.py measures
it (RMS per 6 ms frame, 36 ms held), against whichever line is higher: 16 dB under the pair's speaking level, or
8 dB over the background level around them. The second line is what finds a pause in a room with steady noise,
which 16 dB under the voice never reaches. No quiet found: the pair is left exactly as transcribed.

The 8 dB was chosen by replaying 507 of the creator's real cuts through the unchanged snap-to-sound.py and
judging every edge with the neutral room-tone judge in dev-tools/cut-timing/before-after.py. Against the stamps as
faster-whisper gave them: cut starts that open on stray sound fell from 48 in 100 to 15, cut ends that clip their
word from 59 to 29, cut starts that clip their word stayed at 6 to 7 in 100, and 92 more joints became real cuts
instead of pauses left in. 6.3 dB (her room tone's own wander) clipped fewer ends but kept more breaths; 10 and 12
clipped more ends.

Only the unaligned route calls this. An aligned transcript already has its gaps, and nothing here touches one.
The one exception is a STRANDED word (see strays() below), which every route checks once.

    python3 scripts/restamp_words.py repair <words.json> <media_dir> <out.json>
    python3 scripts/restamp_words.py strays <words.json> <media_dir> <out.json>

repairs a transcript the Intel route wrote before this existed, which the rough cut would otherwise reuse with
its old stamps for good (word-timings.sh hands a heard job's words.json back). Exit 0 with <out.json> written when
a clip was repaired, 3 when there was nothing to do (already re-stamped, aligned, or footage missing), anything
else on an error. A clip it has seen carries "restamped" afterwards, so it is never looked at twice.

Standard library only, so the engine's tests run it without the speech tools; numpy is used for the loudness
envelope when it is there (it always is next to faster-whisper), plain Python when it is not.
"""
import json
import math
import os
import subprocess
import sys

RATE = 16000            # Hz, as snap-to-sound.py reads it
FRAME = 0.006           # snap-to-sound.py FRAME: one cycle of her low regular pitch, in whole ms
HOLD_FRAMES = 6         # snap-to-sound.py HOLD_FRAMES: 36 ms of quiet in a row is a gap
QUIET_DB = 16.0         # snap-to-sound.py QUIET_DB: this far under the speaking level is quiet
FLOOR_DB = 8.0          # ...or this close to the background level around the pair (see the docstring)
FLOOR_CONTEXT = 2.0     # seconds either side of the pair that set its background level
SPEAKING_PCT = 0.80     # the pair's speaking level: its loud end, so the pause inside its span cannot drag it down
FLOOR_PCT = 0.10        # its background level
SHORTEST_WORD = 0.102   # snap-to-sound.py FOLD_TAKE: a word keeps at least this much of itself
LEAD = 0.10             # a pause may begin this far before the earlier word's end stamp (ends run early)
SILENCE_FLOOR = 1e-9


def envelope(samples):
    """Loudness in dBFS, one reading per FRAME, from mono float samples at RATE."""
    size = int(round(FRAME * RATE))
    try:
        import numpy as np
        x = np.asarray(samples, dtype=np.float64)
        k = len(x) // size
        if k == 0:
            return []
        x = x[:k * size].reshape(k, size)
        return list(20 * np.log10(np.sqrt((x * x).mean(axis=1)) + SILENCE_FLOOR))
    except ImportError:
        out = []
        for k in range(len(samples) // size):
            total = 0.0
            for v in samples[k * size:(k + 1) * size]:
                total += v * v
            out.append(20 * math.log10(math.sqrt(total / size) + SILENCE_FLOOR))
        return out


def _pct(values, q):
    ranked = sorted(values)
    return ranked[int(q * (len(ranked) - 1))]


def _quiet_runs(readings, threshold):
    """(first, stop) index pairs of each run under threshold that lasts HOLD_FRAMES or longer, stop exclusive."""
    found, k, n = [], 0, len(readings)
    while k < n:
        if readings[k] < threshold:
            j = k
            while j < n and readings[j] < threshold:
                j += 1
            if j - k >= HOLD_FRAMES:
                found.append((k, j))
            k = j
        else:
            k += 1
    return found


def restamp(words, db):
    """A copy of `words` with each pause measured into place. `db` is envelope() of the same clip.

    Returns (words, moved): moved counts the word pairs whose boundary was set from the audio."""
    out = [dict(w) for w in words]
    n = len(db)
    at = lambda t: max(0, min(n, int(round(t / FRAME))))
    moved = 0
    for i in range(len(out) - 1):
        a, b = out[i], out[i + 1]
        lo = at(max(a["start"] + SHORTEST_WORD, a["end"] - LEAD))
        hi = at(b["end"] - SHORTEST_WORD)
        if hi - lo < HOLD_FRAMES:
            continue
        span = db[at(a["start"]):at(b["end"])]
        around = db[at(a["start"] - FLOOR_CONTEXT):at(b["end"] + FLOOR_CONTEXT)]
        if len(span) < 5 or len(around) < 5:
            continue
        threshold = max(_pct(span, SPEAKING_PCT) - QUIET_DB, _pct(around, FLOOR_PCT) + FLOOR_DB)
        runs = _quiet_runs(db[lo:hi], threshold)
        if not runs:
            continue
        end = round(max(a["start"] + 0.02, (lo + runs[0][0]) * FRAME), 3)
        start = round(min(b["end"] - 0.02, (lo + runs[-1][1]) * FRAME), 3)
        if start < end:
            continue
        if abs(end - a["end"]) > 0.0005 or abs(start - b["start"]) > 0.0005:
            moved += 1
        a["end"], b["start"] = end, start
    return out, moved


# STRANDED WORDS (every route, aligned included). The aligned listener has its own way of getting one word wrong: a
# short word it could barely hear ("and", score 0.001) gets squeezed onto the tail of the word before it, while she
# really says it after the pause, right before the next word. The cut then keeps it with the wrong line, its caption
# shows a line early, and the next line's piece opens partway into the real word. Found 2026-10-16 ("takeover, and |
# then a count up": "and" stamped 17.96-18.02 on the ring of "takeover", said 18.46-18.80). A word is moved only when
# everything agrees: it is short, the listener was unsure of it, it touches one neighbour, a real pause separates it
# from the other, and inside that pause, right against the other neighbour, there is voice no word accounts for.
STRAY_LONGEST = 0.12    # seconds: a stranded word is stamped this short or shorter (it was squeezed)
STRAY_SCORE = 0.3       # ...and the listener's own confidence in it is under this
STRAY_TOUCH = 0.05      # ...and it sits this close to the neighbour it was squeezed onto
STRAY_GAP = 0.30        # ...and a pause at least this long separates it from the other neighbour
STRAY_VOICE = 0.15      # ...where voice no word accounts for runs at least this long, against that neighbour
STRAY_LOUD_DB = 6.0     # ...and reaches this far over the quiet line (a breath or a click does not)


def strays(words, db):
    """A copy of `words` with each stranded word moved onto the voice it really is. `db` is envelope() of the clip.

    Returns (words, moved)."""
    out = [dict(w) for w in words]
    n = len(db)
    at = lambda t: max(0, min(n, int(round(t / FRAME))))
    moved = 0
    for i in range(1, len(out) - 1):
        a, b, c = out[i - 1], out[i], out[i + 1]
        score = b.get("score")
        if score is None or score >= STRAY_SCORE or b["end"] - b["start"] > STRAY_LONGEST:
            continue
        span = db[at(a["start"]):at(c["end"])]
        around = db[at(a["start"] - FLOOR_CONTEXT):at(c["end"] + FLOOR_CONTEXT)]
        if len(span) < 5 or len(around) < 5:
            continue
        threshold = max(_pct(span, SPEAKING_PCT) - QUIET_DB, _pct(around, FLOOR_PCT) + FLOOR_DB)
        if b["start"] - a["end"] <= STRAY_TOUCH and c["start"] - b["end"] >= STRAY_GAP:
            # squeezed onto the word before; the real word is the voice just before the next one
            lo, hi = at(b["end"]), at(c["start"])
            runs = _quiet_runs(db[lo:hi], threshold)
            if not runs:
                continue
            onset = (lo + runs[-1][1]) * FRAME
            voice = db[lo + runs[-1][1]:hi]
            if c["start"] - onset < STRAY_VOICE or not voice or max(voice) < threshold + STRAY_LOUD_DB:
                continue
            b["was"] = [b["start"], b["end"]]
            b["start"], b["end"] = round(onset, 3), round(max(onset + SHORTEST_WORD, c["start"] - 0.01), 3)
            moved += 1
        elif c["start"] - b["end"] <= STRAY_TOUCH and b["start"] - a["end"] >= STRAY_GAP:
            # squeezed onto the word after; the real word is an island of voice between two quiets after the one before
            lo, hi = at(a["end"]), at(b["start"])
            runs = _quiet_runs(db[lo:hi], threshold)
            if len(runs) < 2:
                continue
            v0, v1 = lo + runs[0][1], lo + runs[1][0]
            voice = db[v0:v1]
            if (v1 - v0) * FRAME < STRAY_VOICE or not voice or max(voice) < threshold + STRAY_LOUD_DB:
                continue
            b["was"] = [b["start"], b["end"]]
            b["start"], b["end"] = round(v0 * FRAME, 3), round(v1 * FRAME, 3)
            moved += 1
    return out, moved


def place_strays(words_path, media_dir, out_path):
    """Every route: move each stranded word onto its real voice, once per clip ("strays_checked")."""
    with open(words_path, encoding="utf-8") as f:
        data = json.load(f)
    moved_all, looked = 0, 0
    for clip in data.get("clips", []):
        # a clip with no words (silent, or no sound at all) has nothing to move, so its audio is never read
        if "strays_checked" in clip or not clip.get("words") or unaligned(clip["words"]):
            continue
        media = os.path.join(media_dir, os.path.basename(clip.get("clip", "")))
        if not os.path.exists(media):
            media = clip.get("path", "")
        if not media or not os.path.exists(media):
            continue
        clip["words"], moved = strays(clip["words"], envelope(decode(media)))
        clip["strays_checked"] = moved
        moved_all += moved; looked += 1
    if not looked:
        return 3
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, out_path)
    if moved_all:
        print(f"[word-timings] moved {moved_all} word(s) the listener had stamped on the wrong side of a pause", file=sys.stderr)
    return 0


TOUCHING = 0.005        # snap-to-sound.py EDGE_TO_EDGE: closer than this, two stamps were never measured apart
UNALIGNED_SHARE = 0.5   # an aligned transcript has about 1 in 100 touching pairs, an unaligned one about 87


def unaligned(words):
    """True when most neighbouring words touch, the mark of a transcript that never had an alignment pass."""
    pairs = list(zip(words, words[1:]))
    if len(pairs) < 10:
        return False
    return sum(b["start"] - a["end"] < TOUCHING for a, b in pairs) / len(pairs) > UNALIGNED_SHARE


def decode(path, seconds=None):
    """Mono float samples at RATE, read with ffmpeg (every route has it). The first audio stream, always: an iPhone
    clip can carry a second, spatial one (codec "apac") flagged as the default, which ffmpeg cannot decode.
    `seconds` reads only that much from the start of the clip (scripts/quiet_first_line.py reads the lead-in)."""
    import array
    head = ["-t", f"{float(seconds):.3f}"] if seconds else []
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, *head, "-map", "0:a:0?", "-vn",
                          "-af", "pan=mono|c0=c0", "-ar", str(RATE), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    samples = array.array("f")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 4])
    return samples


def repair(words_path, media_dir, out_path):
    with open(words_path, encoding="utf-8") as f:
        data = json.load(f)
    repaired = 0
    for clip in data.get("clips", []):
        if "restamped" in clip or not unaligned(clip.get("words", [])):
            continue
        media = os.path.join(media_dir, os.path.basename(clip.get("clip", "")))
        if not os.path.exists(media):
            media = clip.get("path", "")
        if not media or not os.path.exists(media):
            continue
        clip["words"], clip["restamped"] = restamp(clip["words"], envelope(decode(media)))
        repaired += 1
    if not repaired:
        return 3
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, out_path)
    print(f"[word-timings] put the pauses back into {repaired} clip(s) of this older transcript", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "repair":
        sys.exit(repair(*sys.argv[2:]))
    if len(sys.argv) == 5 and sys.argv[1] == "strays":
        sys.exit(place_strays(*sys.argv[2:]))
    sys.exit("usage: restamp_words.py repair|strays <words.json> <media_dir> <out.json>")

