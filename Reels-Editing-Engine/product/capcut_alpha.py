#!/usr/bin/env python3
"""capcut_alpha.py — every transparent overlay reaches CapCut as STRAIGHT-alpha ProRes 4444.

WHAT CAPCUT ACTUALLY DOES (measured in CapCut, locked). CapCut reads the alpha in a ProRes 4444 clip, and in a
PNG, as STRAIGHT alpha: colour at full strength, alpha saying how much of it shows. That is exactly what a
HyperFrames render is, so a render goes in as it is. Proof, on a real draft over real footage: a white patch at
35% alpha showed as the faint haze it is when handed over straight. The same patch PREMULTIPLIED (colour scaled
down by alpha first) showed as a dark grey disc, so premultiplying darkens every soft edge: fades, shadows,
anti-aliased type, the edge of an emoji or a cutout. An earlier version premultiplied every overlay on the way
in on the belief that CapCut read premultiplied alpha; this module now undoes that instead.

A WebM with transparency (VP9/VP8 alpha, which is what HyperFrames' remove-background writes) is worse: CapCut
drops its alpha entirely, so everything clear turns solid black. So a transparent clip in any other container is
converted to straight ProRes 4444 on its way in, the format CapCut keeps the transparency of.

  for_capcut(path)        -> a path that shows correctly in CapCut (the file itself, or a cached copy)
  fix_in_place(path)      -> put the CapCut-ready pixels into a file already inside a FRESH draft
  install(src, dst)       -> put a prepared copy in place of a draft asset, atomically
  straight_evidence(path) -> True straight / False colour never exceeds alpha / None (nothing to judge by)

WHAT IT CHANGES, AND WHAT IT NEVER DOES.
  * A straight ProRes/QuickTime overlay (every render) is returned untouched: no re-encode, no copy.
  * A clip TAGGED by the old premultiply step (its cached copies, or a draft asset it wrote) is turned back
    to straight, so rebuilding from one of those never carries the dark edges forward.
  * A transparent WebM/MKV/etc. is converted to straight ProRes 4444.
  * Stills (PNG stickers, GIFs) and opaque clips are left exactly as they are.
  * The render itself is never modified: changes are written to a cached COPY with the SAME file name (and
    extension: VectCut copies an overlay in as video_<id>.mp4, QuickTime inside, and that stays its name).
  Decided by probing the stream, never by the file name. A copy is keyed by the source's CONTENT, so an unchanged
  layer is prepared once however many drafts it goes into; entries unused for CACHE_DAYS are pruned.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, time

RECIPE = "v6-faint-and-midtone-edges"                  # bump when the ffmpeg recipe changes: invalidates the cache
TAG = "capcut-alpha:premultiplied"              # what the OLD premultiply step wrote: such a file is undone
STRAIGHT_TAG = "capcut-alpha:straight"          # what this module writes on a copy it prepared
# ffmpeg 8 tracks whether a frame is premultiplied and its unpremultiply filter does NOTHING to a frame not marked
# so, which a ProRes file never is: mark it first. ffmpeg before 8 has no such option (and no such check), so the
# plain chain is the fallback there. Both are tried in order; the first that runs is used.
UNPREMULT_VFS = ("setparams=alpha_mode=premultiplied,format=gbrap16le,unpremultiply=inplace=1,format=yuva444p12le",
                 "format=gbrap16le,unpremultiply=inplace=1,format=yuva444p12le")
CONVERT_VF = "format=yuva444p12le"
# A browser render keeps colour premultiplied in 8 bits, so a pixel only a few percent opaque (the far edge of a soft
# shadow or glow) comes back with a random hue once un-premultiplied: (36,0,0) red and (20,21,0) yellow inside one
# dark-brown shadow. CapCut draws those as a rainbow ring around every card (2026-10-18, three looks). So below
# EDGE_A the colour fades to its own grey (the hue is noise there), back to full colour by EDGE_FULL. Measured on her
# renders: hue noise at alpha <= 16/255, true colour from ~40/255.
EDGE_A, EDGE_FULL = 24, 48
# Greying alone turned her rainbow ring WHITE (2026-10-18): a dark shadow can only darken the footage under it, so a
# lighter ring means CapCut ADDS a faint pixel's colour on top instead of blending it in. So a DARK pixel (a shadow,
# dark ink's soft edge) is also scaled by its own alpha: at 3% it carries ~3% of its colour, nothing to add, and it
# reads the same either way CapCut blends. Light colours (white type, white cards, a butter glow) are left as they are.
# A buyer's pack (2026-10-21) still showed a white halo and a dotted outline on soft text edges: its shadow was a
# mid-tone plum (110,77,139, luma ~88), over the old DARK_LUMA of 64, and its glow's faint sliver kept full colour.
# So "dark" now reaches mid-tones (a shadow-coloured pixel scaled by its alpha only gets darker, which is what a
# shadow does), and EVERY colour below FAINT_A fades by (a/FAINT_A)^2. That leaves the measured case alone: white at
# 35% alpha (89/255, over FAINT_A) still goes in straight, which is what CapCut shows correctly. Premultiplying
# EVERYTHING (that buyer's local patch) is the v2.0.18-20 step the A/B above measured darkening every light soft edge,
# so it stays off unless CAPCUT_ALPHA_PREMULT=1 says this computer's CapCut needs it.
DARK_LUMA = 140
FAINT_A = 64
CACHE_DAYS = 14
_TOL = 3                                        # 8-bit rounding through yuva 4:4:4 can put colour 1-2 over alpha


def cache_root():
    return os.environ.get("REEL_CAPCUT_ALPHA_CACHE") or os.path.expanduser(
        "~/.cache/reels-editing-engine/capcut-alpha")


def has_alpha(pix_fmt):
    p = (pix_fmt or "").lower()
    return p.startswith("yuva") or p.startswith("ya") or "rgba" in p or "argb" in p or "bgra" in p or "abgr" in p


def _probe(path):
    """-> dict(pix, dur, w, h, comment, fmt, codec, vpx_alpha). vpx_alpha: a VP8/VP9 stream whose alpha rides
    in a side channel (ffprobe reports yuv420p plus an ALPHA_MODE=1 tag, so pix_fmt alone never shows it)."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=pix_fmt,width,height,codec_name,r_frame_rate:stream_tags:format=duration,format_name"
                              ":format_tags=comment", "-of", "json", path], capture_output=True, text=True).stdout
        o = json.loads(out or "{}")
        st = (o.get("streams") or [{}])[0]
        fmt = o.get("format") or {}
        stags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        codec = st.get("codec_name") or ""
        return dict(pix=st.get("pix_fmt") or "", dur=float(fmt.get("duration") or 0),
                    w=int(st.get("width") or 0), h=int(st.get("height") or 0),
                    comment=(fmt.get("tags") or {}).get("comment", ""), fmt=fmt.get("format_name") or "",
                    codec=codec, fps=st.get("r_frame_rate") or "30", vpx_alpha=codec in ("vp8", "vp9") and str(stags.get("alpha_mode", "")) == "1")
    except Exception:
        return dict(pix="", dur=0.0, w=0, h=0, comment="", fmt="", codec="", fps="30", vpx_alpha=False)


def _is_movie(format_name):
    """A QuickTime-family container (.mov, or the same thing named .mp4/.m4v). Stills (png_pipe, image2, gif,
    webp_pipe) are not, and neither is WebM/Matroska."""
    return "mov" in (format_name or "").split(",")


def _is_still(format_name):
    f = (format_name or "").lower()
    return any(k in f for k in ("png", "image2", "gif", "webp", "jpeg", "bmp", "tiff"))


def _plan(path):
    """What this file needs before CapCut sees it: None, "unpremultiply" or "convert"."""
    p = _probe(path)
    if _is_still(p["fmt"]):
        return None
    if p["vpx_alpha"]:
        return "convert"                        # CapCut drops VP8/VP9 alpha: it would show black
    if not has_alpha(p["pix"]):
        return None                             # opaque
    if _is_movie(p["fmt"]):
        if p["comment"] == TAG:
            return "unpremultiply"
        return "clean" if p["comment"] != STRAIGHT_TAG else None   # every render: its faint edges' hue noise cleaned
    return "convert"                            # a transparent clip in a container CapCut is not given overlays in


def needs_preparing(path):
    return bool(path and os.path.isfile(path) and _plan(path))


def is_tagged(path):
    """True when the file still carries the OLD premultiplied tag."""
    return _probe(path)["comment"] == TAG


def straight_evidence(path, samples=8):
    """Sample frames across the clip. True as soon as any pixel's colour exceeds its alpha (straight alpha, the
    correct form for CapCut); False when semi-transparent pixels were seen and every one is <= alpha (consistent
    with premultiplied); None when the samples held no semi-transparent pixel at all (nothing to judge by)."""
    p = _probe(path)
    w, h, dur = p["w"], p["h"], p["dur"]
    if not w or not h:
        return None
    dec = _decoder(p)
    seen_semi = False
    for i in range(samples):
        t = dur * (i + 0.5) / samples if dur > 0 else 0
        buf = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}"] + dec + ["-i", path, "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgba", "-"], capture_output=True).stdout
        if len(buf) < w * h * 4:
            continue
        a = buf[3::4]
        for c in range(3):
            if any(x > y + _TOL for x, y in zip(buf[c::4], a)):
                return True
        if not seen_semi:
            seen_semi = any(0 < y < 255 for y in a)
    return False if seen_semi else None


def _decoder(p):
    """ffmpeg's native VP8/VP9 decoders ignore the alpha side channel; libvpx reads it."""
    if p.get("vpx_alpha"):
        return ["-c:v", "libvpx-vp9" if p["codec"] == "vp9" else "libvpx"]
    return []


def _content_key(path):
    h = hashlib.sha256((RECIPE + ("+premult-all" if premult_all() else "")).encode())
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()[:24]


def _prune(root, keep):
    cutoff = time.time() - CACHE_DAYS * 86400
    try:
        for e in os.listdir(root):
            p = os.path.join(root, e)
            if e != keep and os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def premult_all():
    """CAPCUT_ALPHA_PREMULT=1: premultiply every pixel (the opt-in for a CapCut that adds edge colour everywhere)."""
    return os.environ.get("CAPCUT_ALPHA_PREMULT", "0") == "1"


def _clean_edges(src, tmp):
    """Straight ProRes 4444 with the hue noise in its faintest pixels greyed (see EDGE_A) and its dark and faint
    pixels scaled down (DARK_LUMA, FAINT_A). Frames go through numpy a frame at a time over pipes, so a full-length
    captions layer never sits in memory. Its sound is copied."""
    import numpy as np
    p = _probe(src)
    w, h = p["w"], p["h"]
    fps = p.get("fps") or "30"
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", src, "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt",
                            "rgba64le", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba64le", "-s", f"{w}x{h}",
                            "-r", str(fps), "-i", "-", "-i", src, "-map", "0:v:0", "-map", "1:a?", "-vf", CONVERT_VF,
                            "-c:v", "prores_ks", "-profile:v", "4444", "-alpha_bits", "16", "-c:a", "copy",
                            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                            "-metadata", f"comment={STRAIGHT_TAG}", "-f", "mov", tmp], stdin=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    n = w * h * 8
    lo, hi = EDGE_A * 257.0, EDGE_FULL * 257.0
    every = premult_all()
    try:
        while True:
            buf = dec.stdout.read(n)
            if len(buf) < n:
                break
            f = np.frombuffer(buf, dtype="<u2").reshape(h, w, 4).astype(np.float32)
            a = f[..., 3]
            m = a < hi
            if m.any():
                rgb = f[..., :3]
                grey = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
                k = np.clip((a - lo) / (hi - lo), 0.0, 1.0)[..., None]       # 0 = all grey, 1 = its own colour
                f[..., :3] = np.where(m[..., None], grey[..., None] + (rgb - grey[..., None]) * k, rgb)
            rgb = f[..., :3]
            lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
            scale = np.ones_like(a)
            if every:
                scale = a / 65535.0
            else:
                dark = (lum < DARK_LUMA * 257.0) & (a < 65535.0)
                scale = np.where(dark, a / 65535.0, scale)
                faint = a < FAINT_A * 257.0
                scale = np.where(faint, np.minimum(scale, (a / (FAINT_A * 257.0)) ** 2), scale)
            if (scale < 1.0).any():
                f[..., :3] = rgb * scale[..., None]
            enc.stdin.write(f.astype("<u2").tobytes())
        enc.stdin.close()
    finally:
        dec.wait()
    err = enc.stderr.read().decode(errors="replace")
    return enc.wait(), err


def _have_numpy():
    import importlib.util
    return importlib.util.find_spec("numpy") is not None


def _uv():
    exe = "uv.exe" if os.name == "nt" else "uv"
    found = shutil.which("uv") or os.path.join(os.path.expanduser("~"), ".local", "bin", exe)
    return found if os.path.isfile(found) else None


# The interpreter a builder runs under (plain python3 from a job's driver, say) may not have numpy, and then the
# clean never ran: every layer went in as rendered, faint edges and all. uv is on every setup, so it brings numpy
# for this one step (the same pins capcut_handoff.py asks for: numpy<2 on an Intel Mac, which has no newer wheel).
NUMPY_WITH = ("numpy<2; sys_platform == 'darwin' and platform_machine == 'x86_64'", "numpy")


def _clean_edges_via_uv(src, tmp):
    uv = _uv()
    if not uv:
        return 1, "numpy isn't installed for this Python, and uv (which would bring it) wasn't found"
    cmd = [uv, "run", "--no-project", "--quiet"]
    for spec in NUMPY_WITH:
        cmd += ["--with", spec]
    cmd += ["python", os.path.abspath(__file__), "--clean-edges", src, tmp]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        return 1, f"numpy isn't installed for this Python, and uv could not run: {e}"
    return r.returncode, (r.stderr or "").strip() or ("" if r.returncode == 0 else "uv could not bring numpy")


def prepare(src, dst, how):
    """Encode src to dst as straight-alpha ProRes 4444, tagged. how = "unpremultiply" (undo the old premultiply)
    or "convert" (re-wrap a WebM/other transparent clip). Atomic: dst is either the finished file or absent."""
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".mov", dir=os.path.dirname(os.path.abspath(dst)))
    os.close(fd)
    if how == "clean":
        rc, err = _clean_edges(src, tmp) if _have_numpy() else _clean_edges_via_uv(src, tmp)
        if rc != 0 or not os.path.getsize(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RuntimeError(f"could not clean {os.path.basename(src)}'s edges for CapCut: {err.strip()[-300:]}")
        os.chmod(tmp, 0o644)
        os.replace(tmp, dst)
        return
    vfs = UNPREMULT_VFS if how == "unpremultiply" else (CONVERT_VF,)
    # Any sound the clip carries comes through (copied when QuickTime can hold it, else re-encoded to PCM).
    a_codec = "copy" if how == "unpremultiply" else "pcm_s16le"
    for vf in vfs:
        r = subprocess.run(["ffmpeg", "-v", "error", "-y"] + _decoder(_probe(src)) + ["-i", src, "-map", "0:v:0",
                            "-map", "0:a?", "-vf", vf, "-c:v", "prores_ks", "-profile:v", "4444", "-alpha_bits",
                            "16", "-c:a", a_codec, "-metadata", f"comment={STRAIGHT_TAG}", "-f", "mov", tmp],
                           capture_output=True, text=True)
        if r.returncode == 0 and os.path.getsize(tmp):
            break
    if r.returncode != 0 or not os.path.getsize(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(f"could not prepare {os.path.basename(src)} for CapCut's transparency: "
                           f"{(r.stderr or '').strip()[-300:]}")
    os.chmod(tmp, 0o644)                        # mkstemp makes it owner-only
    os.replace(tmp, dst)


def for_capcut(path):
    """A path to hand CapCut for this clip. Straight ProRes overlays, stills and opaque clips come back unchanged;
    a clip the old step premultiplied, or a transparent WebM, comes back as a cached straight ProRes 4444 copy
    with the SAME file name. The source is never modified."""
    if not path or not os.path.isfile(path):
        return path
    how = _plan(path)
    if not how:
        return path
    root = cache_root()
    key = _content_key(path)
    entry = os.path.join(root, key)
    out = os.path.join(entry, os.path.basename(path))
    if os.path.isfile(out) and os.path.getsize(out):
        os.utime(entry)                         # keep a used entry out of the prune
        return out
    os.makedirs(entry, exist_ok=True)
    print(f"  · preparing {os.path.basename(path)} for CapCut's transparency (once per layer, kept for next time)",
          flush=True)
    try:
        prepare(path, out, how)
    except Exception as e:
        if how != "clean":
            raise
        # The edge clean is polish: when it can't run here (no numpy and no uv, a decode hiccup), the render goes in
        # as it is, exactly as before. Never a missing layer for it, and never silent: one line here, one in the log.
        why = str(e).strip().splitlines()[-1][:160] if str(e).strip() else type(e).__name__
        print(f"  · {os.path.basename(path)} went into CapCut as rendered, without its edge clean-up, so a faint ring "
              f"can show around its soft edges ({why})", flush=True)
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from fallbacks import note
            note("capcut-alpha", f"{os.path.basename(path)}: the edge clean-up could not run ({why}); "
                                 f"put the layer in as rendered")
        except Exception:
            pass
        shutil.rmtree(entry, ignore_errors=True)
        return path
    _prune(root, key)
    return out


def fix_in_place(path):
    """Prepare a transparent clip that already sits in a FRESH draft's assets folder (VectCut copies media in
    byte-for-byte, so by finalize the overlay is local). Never call this on a draft she may have opened: a media
    change there goes to a new version. Returns True when the file was replaced."""
    src = for_capcut(path)
    if src == path:
        return False
    install(src, path)
    return True


def install(src, dst):
    """Copy src over dst atomically (a new file, never a write into the old one, so a hard link to dst
    elsewhere keeps its original pixels)."""
    fd, tmp = tempfile.mkstemp(suffix=".mov", dir=os.path.dirname(os.path.abspath(dst)))
    os.close(fd)
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dst)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--clean-edges":     # the uv route above: numpy is here, clean one file
        _rc, _err = _clean_edges(sys.argv[2], sys.argv[3])
        sys.stderr.write(_err)
        raise SystemExit(_rc)
    if len(sys.argv) < 2:
        print("usage: python3 product/capcut_alpha.py <overlay.mov|.mp4|.webm> ...   # prints the CapCut-ready path")
        raise SystemExit(2)
    for p in sys.argv[1:]:
        q = for_capcut(p)
        print(q if q != p else f"{p}  (already fine for CapCut)")
