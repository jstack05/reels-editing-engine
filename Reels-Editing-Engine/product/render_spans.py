#!/usr/bin/env python3
"""render_spans.py — render a caption layer only where it is on screen.

WHY. A caption layer is often on screen for a fraction of the reel: a reel whose captions move to a new spot
half-way through is built as two layers, each blank for the other's stretch. HyperFrames renders a whole
composition, so each of those layers used to capture every frame of the reel, blank ones included (a 1:52
reel split into a chest layer and a side layer rendered 112 s twice, about two minutes of capture each).
The engine's rule has always been "trim overlay dead space > DEAD_GAP s (one segment per active span)"
(build-hf-captions.py), but it was only applied when the layer was PLACED in CapCut, never at render time.

WHAT THIS DOES.
  1. The builder (build-reel-type.py for a caption-only layer, build-hf-captions.py always) calls write_plan()
     on the composition it just wrote. That reads the clips the page really holds, merges them into the
     stretches where text is on screen (split only where nothing shows for more than GAP seconds) and saves
     them as render-spans.json beside index.html. A layer whose text runs from the first word to the last has
     nothing worth trimming, gets no plan, and renders exactly as before.
  2. reel_render.py render (the one gated renderer) runs the check on the FULL composition as always, then,
     when a plan is there, renders one time-shifted copy per stretch (span_comp(): local time 0 = the
     stretch's start) and assemble()s them, with transparent frames in the gaps, into the SAME full-length
     .mov at the same path. The rendered frames are stream-copied, never re-encoded, so every pixel and every
     frame time is what a full render would have produced.
  3. A sidecar, <out>.spans.json, records where each stretch sits on the reel's timeline. Every consumer
     (composite, build-reel-mp4.py, capcut_handoff.py, a job's own driver) reads the full-length file exactly
     as before and never has to know. build-overlay.py lays one CapCut segment per stretch when the footage
     track keeps more clips than that (the magnet anchors to the track with the most clips), so the empty
     parts of the caption track stay clear.

FALLBACK (locked): if the plan cannot be made, a stretch fails to render, or the reassembly does not come out
frame-exact, the layer is rendered full length the way it always was, and the run says so in one plain line.
A consumer that cannot read the sidecar, or finds it does not match the file, uses the whole file. A reel is
never left without its captions.

REEL_FULL_RENDER=1 turns the span render off for a run (the old full-length render).
"""
import json, math, os, re, shutil, subprocess
from html.parser import HTMLParser

FPS = 30
GAP = 3.0            # same DEAD_GAP build-hf-captions.py uses for placement: split only where text is absent > GAP s
PAD_PRE = 0.10       # small edge pad around each stretch (the same pad the placement spans carry)
PAD_POST = 0.20
PLAN_NAME = "render-spans.json"
CLOCK_LEAD = 0.00001 # a stretch's timeline runs 10 us ahead so a frame never lands a hair before its own moment
# Bumped whenever assemble() changes what it writes. reel_render.py folds it into the render cache key of a layer
# rendered in stretches, so a layer assembled the old way is rendered again rather than reused.
# 2 (2026-10-19): the transparent filler carries the stretch renders' own color tags (see assemble()).
ASSEMBLY = 2
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def frames_for(seconds, fps=FPS):
    """How many frames HyperFrames renders for a composition of this duration (measured: ceil(d * fps);
    1.00 s -> 30, 1.01 s -> 31, 1.05 s -> 32). The tiny tolerance keeps 2.1 s (63.000000000000004) at 63."""
    return int(math.ceil(float(seconds) * fps - 1e-6))


def _dur_attr(frames, fps=FPS):
    """A data-duration that HyperFrames turns back into exactly `frames` frames. Written rounded DOWN to the
    microsecond, so ceil() can never tip it one frame over (41/30 written "1.366667" would render 42)."""
    return f"{math.floor(frames / fps * 1e6) / 1e6:.6f}"


class _RootClips(HTMLParser):
    """The direct children of #root, with their tag, attrs and source offsets (start of the opening tag and
    end of the matching closing tag), so a span copy can drop or shift them without re-serialising the page."""

    def __init__(self, src):
        super().__init__(convert_charrefs=False)
        self.src = src
        # line starts counted on "\n" only, the way HTMLParser counts them (splitlines() also breaks on
        # characters like U+2028 that can sit in caption text, and every offset after one would drift)
        self._lines = [0] + [i + 1 for i, ch in enumerate(src) if ch == "\n"]
        self.root = None          # (start offset of the root's opening tag, attrs)
        self.root_end = None
        self.children = []        # [{"tag","attrs","start","end"}]
        self._depth = 0           # depth INSIDE root (0 = not inside)
        self._open = None

    def _off(self):
        line, col = self.getpos()
        return self._lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.root is None:
            if a.get("id") == "root":
                self.root = (self._off(), a)
                self._depth = 1
            return
        if self._depth == 0 or self.root_end is not None:
            return
        if self._depth == 1:
            self._open = {"tag": tag, "attrs": a, "start": self._off(), "end": None}
            self.children.append(self._open)
        if tag not in _VOID:
            self._depth += 1
        elif self._depth == 1:
            self._open["end"] = self._off() + len(self.get_starttag_text() or "")

    def handle_startendtag(self, tag, attrs):
        if self.root is not None and self.root_end is None and self._depth == 1:
            self.children.append({"tag": tag, "attrs": dict(attrs), "start": self._off(),
                                  "end": self._off() + len(self.get_starttag_text() or "")})

    def handle_endtag(self, tag):
        if self.root is None or self.root_end is not None or self._depth == 0:
            return
        if tag in _VOID:
            return
        self._depth -= 1
        if self._depth == 1 and self._open is not None:
            close = self.src.find(">", self._off())
            self._open["end"] = close + 1
            self._open = None
        elif self._depth == 0:
            self.root_end = self._off()


def read_clips(html):
    """(root_duration, [(start, duration), ...]) for the clips the page holds, or None when the page cannot be
    read with certainty: no #root, a root child that is not a timed clip (it would be on screen the whole
    time), or a sub-composition. None always means "render it full length", never a guess."""
    p = _RootClips(html)
    try:
        p.feed(html)
        p.close()
    except Exception:
        return None
    if p.root is None or p.root_end is None:
        return None
    try:
        dur = float(p.root[1].get("data-duration"))
    except (TypeError, ValueError):
        return None
    if p.root[1].get("data-fps") not in (None, "", str(FPS)):
        return None          # stretches are cut in 30 fps frames; a page at another rate renders whole
    out = []
    for c in p.children:
        a = c["attrs"]
        if c["end"] is None or "clip" not in (a.get("class") or "").split() or "data-composition-src" in a:
            return None
        try:
            out.append((float(a["data-start"]), float(a["data-duration"])))
        except (KeyError, TypeError, ValueError):
            return None
    return dur, out


def plan(html, gap=GAP, fps=FPS):
    """The stretches worth rendering, or None when trimming would not save anything worth a second render
    (the text runs from first word to last, or the page cannot be read). Stretches are whole frames:
    {"duration", "fps", "frames", "spans": [[first_frame, end_frame), ...]}."""
    got = read_clips(html)
    if not got:
        return None
    dur, clips = got
    total = frames_for(dur, fps)
    iv = sorted((s, s + d) for s, d in clips if d > 0)
    if total <= 0 or not iv:
        return None
    merged = []
    for s, e in iv:
        if merged and s - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    spans = []
    for s, e in merged:
        a = max(0, int(math.floor((s - PAD_PRE) * fps + 1e-6)))
        b = min(total, int(math.ceil((e + PAD_POST) * fps - 1e-6)))
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        elif b > a:
            spans.append([a, b])
    if not spans:
        return None
    dead = (total - sum(b - a for a, b in spans)) / fps
    if dead < gap:
        return None          # on screen (almost) the whole time: one full render, exactly as before
    return {"duration": dur, "fps": fps, "frames": total, "spans": spans}


def describe(p):
    """One plain line about a plan, for the builder's output."""
    on = sum(b - a for a, b in p["spans"]) / p["fps"]
    n = len(p["spans"])
    return (f"on screen {on:.1f}s of {p['frames'] / p['fps']:.1f}s in {n} stretch{'es' if n != 1 else ''}: "
            f"the render captures only those frames")


def write_plan(comp_dir, html=None):
    """Called by a builder for a caption layer: save render-spans.json beside index.html, or remove a stale
    one when this page is not worth trimming. Returns the plan (or None). Never raises: a plan that cannot be
    made just means the ordinary full-length render."""
    path = os.path.join(comp_dir, PLAN_NAME)
    try:
        if html is None:
            html = open(os.path.join(comp_dir, "index.html"), encoding="utf-8").read()
        p = plan(html)
    except Exception:
        p = None
    try:
        if p:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"_doc": "stretches where this caption layer is on screen, in frames [first, end); "
                                   "reel_render.py renders only these (product/render_spans.py)", **p}, fh, indent=1)
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        return None
    return p


def clear_plan(comp_dir):
    """A build that is NOT a caption-only layer: drop any plan an earlier caption build left in the folder."""
    try:
        os.remove(os.path.join(comp_dir, PLAN_NAME))
    except OSError:
        pass


def load_plan(comp_dir):
    """The saved plan, checked against the page it was made for; None when absent or no longer true."""
    path = os.path.join(comp_dir, PLAN_NAME)
    if not os.path.exists(path):
        return None
    try:
        saved = json.load(open(path, encoding="utf-8"))
        html = open(os.path.join(comp_dir, "index.html"), encoding="utf-8").read()
    except Exception:
        return None
    fresh = plan(html, fps=int(saved.get("fps", FPS)))
    if not fresh or fresh["spans"] != saved.get("spans") or fresh["frames"] != saved.get("frames"):
        return None          # the page changed after the plan was written: render it whole
    return fresh


_TL_REG = re.compile(r'window\.__timelines\[(["\'])([^"\']+)\1\]\s*=\s*([A-Za-z_$][\w$]*)\s*;')


def span_html(html, first, end, fps=FPS):
    """The page, time-shifted so local 0 is frame `first` and it lasts end - first frames.

    Clips inside the stretch keep their timing relative to its start. Clips outside it are parked: still in
    the page (so every timeline line that animates them still finds its target) but hidden and untimed.
    The page's own GSAP timeline is left exactly as written and driven from a wrapper timeline that plays it
    from the stretch's start, so every set/tween lands where it did in the full reel. Raises ValueError when
    the page cannot be shifted with certainty (a clip straddling the stretch, no timeline registration)."""
    p = _RootClips(html)
    p.feed(html)
    p.close()
    if p.root is None or p.root_end is None:
        raise ValueError("no #root")
    t0, t1 = first / fps, end / fps
    # The last stretch ends where the reel does, and the reel's last caption may run past that (the render
    # stops at the reel's end either way), so on that stretch a clip may end beyond it.
    root_dur = float(p.root[1].get("data-duration") or 0)
    tail = end >= frames_for(root_dur, fps)
    edits, parked = [], []
    for c in p.children:
        a = c["attrs"]
        s, d = float(a["data-start"]), float(a["data-duration"])
        e = s + d
        chunk = html[c["start"]:c["end"]]
        if s >= t0 - 1e-6 and (e <= t1 + 1e-6 or (tail and s < t1)):
            # rounded DOWN to the microsecond: a word that starts exactly on a frame must still be showing on
            # that frame here, as it is in the full render (round-to-nearest could push it a hair later)
            new = re.sub(r'data-start="[^"]*"', f'data-start="{max(0.0, math.floor((s - t0) * 1e6) / 1e6):.6f}"',
                         chunk, count=1)
            edits.append((c["start"], c["end"], new))
        elif e <= t0 + 1e-6 or s >= t1 - 1e-6:
            hidden = re.sub(r'\sdata-(?:start|duration|track-index)="[^"]*"', "", chunk, count=3)
            hidden = re.sub(r'class="([^"]*)"', lambda m: 'class="' + " ".join(
                "hf-span-parked" if k == "clip" else k for k in m.group(1).split()) + '"', hidden, count=1)
            parked.append(hidden)
            edits.append((c["start"], c["end"], ""))
        else:
            raise ValueError(f"a clip ({s:.2f}s-{e:.2f}s) runs across the stretch edge")
    out = html
    for st, en, new in sorted(edits, reverse=True):
        out = out[:st] + new + out[en:]
    # the parked clips go in one hidden holder just inside the root's closing tag
    root_close = p.root_end - sum(en - st - len(new) for st, en, new in edits if st < p.root_end)
    if parked:
        holder = '<div style="display:none" aria-hidden="true">' + "".join(parked) + "</div>\n"
        out = out[:root_close] + holder + out[root_close:]
    # the root's own length becomes the stretch's length
    rs = p.root[0]
    rtag_end = out.find(">", rs) + 1
    rtag = re.sub(r'data-duration="[^"]*"', f'data-duration="{_dur_attr(end - first, fps)}"', out[rs:rtag_end], count=1)
    out = out[:rs] + rtag + out[rtag_end:]
    m = list(_TL_REG.finditer(out))
    if len(m) != 1:
        raise ValueError("the page does not register exactly one timeline")
    m = m[0]
    key, var = m.group(2), m.group(3)
    # The stretch's clock runs CLOCK_LEAD ahead of the reel's: driven through a tween, 12.6 can come out as
    # 12.59999999, and a caption that switches exactly on that frame (it did, on a real reel) showed blank. The
    # lead is far below one frame (33,333 us) and below the 1/100 s steps every builder times its words in, so
    # every frame lands on or just past its own moment, the way the full render seeks it.
    a0, a1 = t0 + CLOCK_LEAD, t1 + CLOCK_LEAD
    wrap = (f'{var}.pause();{var}.time({a0:.6f},true);'
            f'const __hfSpan=gsap.timeline({{paused:true}});'
            f'__hfSpan.to({var},{{time:{a1:.6f},duration:{t1 - t0:.6f},ease:"none",lazy:false}},0);'
            f'window.__timelines["{key}"]=__hfSpan;')
    return out[:m.start()] + wrap + out[m.end():]


def span_comp(src_dir, dst_dir, first, end, fps=FPS):
    """A renderable copy of the composition for one stretch (fonts and assets alongside, as the page expects)."""
    shutil.copytree(src_dir, dst_dir, ignore=shutil.ignore_patterns(".reel_render", "renders", ".DS_Store", PLAN_NAME))
    idx = os.path.join(dst_dir, "index.html")
    html = open(idx, encoding="utf-8").read()
    with open(idx, "w", encoding="utf-8") as fh:
        fh.write(span_html(html, first, end, fps))
    return dst_dir


def _probe_stream(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,profile,width,height,pix_fmt,color_range,color_space,color_primaries,"
                        "color_transfer", "-of", "json", path],
                       capture_output=True, text=True)
    return (json.loads(r.stdout or "{}").get("streams") or [{}])[0]


def count_frames(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_packets", "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return int((r.stdout or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        return -1


def assemble(span_files, p, out, work):
    """Stitch the stretch renders into ONE full-length transparent .mov at `out`: each stretch at its own
    frames, transparent frames between them. Stream copy only (the renders are never re-encoded), then the
    frame count is checked against what a full render makes. Raises RuntimeError on anything off."""
    fps, total, spans = p["fps"], p["frames"], p["spans"]
    if len(span_files) != len(spans):
        raise RuntimeError("one render per stretch is missing")
    for f, (a, b) in zip(span_files, spans):
        n = count_frames(f)
        if n != b - a:
            raise RuntimeError(f"a stretch rendered {n} frames, expected {b - a}")
    st = _probe_stream(span_files[0])
    if st.get("codec_name") != "prores":
        raise RuntimeError(f"stretch renders are {st.get('codec_name')}, not ProRes")
    w, h = st.get("width"), st.get("height")
    os.makedirs(work, exist_ok=True)
    # Transparent filler in the renders' own format: one 30-frame second and one single frame, repeated.
    # Its COLOR TAGS are the renders' own too. Untagged filler between bt709 renders made every switch a change
    # of frame parameters, so ffmpeg rebuilt the whole filtergraph at each one, and the finish's single graph
    # (the overlay plus the sound effects' adelay/amix) stalled there for good on ffmpeg 9: 0% CPU, the same
    # frame every run (a buyer's problem report, 2026-10-18). Same tags throughout = no rebuild.
    tags = []
    for key, flag in (("color_range", "-color_range"), ("color_space", "-colorspace"),
                      ("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc")):
        v = st.get(key)
        if v and v not in ("unknown", "unspecified", "reserved"):
            tags += [flag, v]
    fill = {}
    for n in (fps, 1):
        f = os.path.join(work, f"clear{n}.mov")
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                            f"color=c=black@0.0:s={w}x{h}:r={fps},format=yuva444p10le", "-frames:v", str(n),
                            "-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le", "-alpha_bits", "16",
                            "-vendor", "apl0", *tags, f], capture_output=True, text=True)
        if r.returncode != 0 or count_frames(f) != n:
            raise RuntimeError("could not make the transparent filler: " + (r.stderr or "").strip()[-200:])
        fill[n] = f

    def _q(path):   # concat-list quoting; forward slashes work on Windows too
        return "file '" + os.path.abspath(path).replace("\\", "/").replace("'", "'\\''") + "'\n"

    lines, at = [], 0
    def _gap(n):
        out_ = [_q(fill[fps])] * (n // fps) + [_q(fill[1])] * (n % fps)
        return out_
    for f, (a, b) in zip(span_files, spans):
        lines += _gap(a - at)
        lines.append(_q(f))
        at = b
    lines += _gap(total - at)
    lst = os.path.join(work, "assemble.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    tmp = os.path.join(work, "assembled.mov")
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                        "-movflags", "+faststart", tmp], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError("reassembly failed: " + (r.stderr or "").strip()[-200:])
    n = count_frames(tmp)
    if n != total:
        raise RuntimeError(f"the reassembled layer has {n} frames, a full render has {total}")
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    shutil.move(tmp, out)
    return out


# ── the sidecar: where each stretch sits on the reel's timeline ─────────────────────────────────────────────
def sidecar_path(mov):
    return os.path.splitext(os.path.abspath(mov))[0] + ".spans.json"


def drop_sidecar(mov):
    try:
        os.remove(sidecar_path(mov))
    except OSError:
        pass


def write_sidecar(mov, p):
    fps = p["fps"]
    doc = {"_doc": "where this layer is on screen; the .mov is full length, so a stretch's place in the file IS "
                   "its place on the reel. Written by reel_render.py (product/render_spans.py).",
           "file": os.path.basename(mov), "size": os.path.getsize(mov), "fps": fps, "frames": p["frames"],
           "spans": [{"file": os.path.basename(mov), "at": round(a / fps, 6), "dur": round((b - a) / fps, 6),
                      "frames": [a, b]} for a, b in p["spans"]]}
    with open(sidecar_path(mov), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    return doc


def segments(mov):
    """[(at, dur), ...] seconds where `mov` is on screen, read from its sidecar; None when there is no sidecar
    or it does not belong to this exact file (re-rendered, replaced, copied from elsewhere). None means: use
    the whole file, as before."""
    try:
        doc = json.load(open(sidecar_path(mov), encoding="utf-8"))
        if doc.get("file") != os.path.basename(mov) or int(doc.get("size", -1)) != os.path.getsize(mov):
            return None
        segs = [(float(s["at"]), float(s["dur"])) for s in doc["spans"]]
    except Exception:
        return None
    if not segs or any(d <= 0 or a < 0 for a, d in segs):
        return None
    return segs
