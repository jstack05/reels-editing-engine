#!/usr/bin/env python3
"""Transparent overlays must reach CapCut as STRAIGHT-alpha ProRes 4444.

Measured in CapCut on a real draft: it reads ProRes 4444 (and PNG) alpha as STRAIGHT, which is how the engine
renders. A premultiplied copy shows every soft edge dark grey, and a WebM with transparency loses its alpha
(clear areas turn black). The exact check: on a semi-transparent pixel of a coloured layer, colour must exceed
alpha (straight), and a fully clear pixel must stay clear.

This also guards the parts around it: a render goes in as a cleaned copy of the same name (the hue noise in its
faintest edge pixels greyed, so no rainbow ring in CapCut; its real colour and clear pixels kept), the render itself
is never changed, the clean never costs her a layer when it can't run, a copy the old premultiply step made is turned back to straight, a prepared layer is not redone,
and a draft she may have opened is never changed in place.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCT = os.path.dirname(HERE)
sys.path.insert(0, PRODUCT)

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
TOL = 3          # 8-bit readback through yuva 4:4:4 may round a channel a step or two
W, H = 108, 192
SRC = "color=c=0xFFEB7D@0.84:s={w}x{h}:d=0.4:r=30,format=rgba,pad={W}:{H}:0:0:color=black@0"

if HAVE_FFMPEG:
    import capcut_alpha
    import capcut_media


def _straight(path, audio=False):
    """A straight-alpha ProRes 4444 clip, the way a render arrives: the left half butter yellow at alpha ~214,
    the right half fully clear. QuickTime inside whatever the name says (VectCut copies an overlay in as
    video_<id>.mp4)."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SRC.format(w=W // 2, h=H, W=W, H=H)]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", "-c:a", "pcm_s16le"]
    cmd += ["-c:v", "prores_ks", "-profile:v", "4444", "-alpha_bits", "16", "-pix_fmt", "yuva444p12le",
            "-f", "mov", path]
    subprocess.run(cmd, check=True)
    return path


def _old_premultiplied(src, path):
    """What v2.0.18-v2.0.20 wrote: the straight clip premultiplied and tagged."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?", "-vf",
                    "format=gbrap16le,premultiply=inplace=1,format=yuva444p12le", "-c:v", "prores_ks",
                    "-profile:v", "4444", "-alpha_bits", "16", "-c:a", "copy",
                    "-metadata", f"comment={capcut_alpha.TAG}", "-f", "mov", path], check=True)
    return path


def _webm(path):
    """A transparent VP9 WebM, the way HyperFrames' remove-background writes a cutout."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SRC.format(w=W // 2, h=H, W=W, H=H),
                    "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "1M", "-auto-alt-ref", "0",
                    "-f", "webm", path], check=True)
    return path


def _pixel(path, x=W // 4):
    """(r, g, b, a) of one pixel on the middle row of the first frame (default: in the coloured half)."""
    buf = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgba", "-"], capture_output=True, check=True).stdout
    i = ((H // 2) * W + x) * 4
    return tuple(buf[i:i + 4])


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _streams(path):
    return subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt",
                           "-of", "csv=p=0", path], capture_output=True, text=True).stdout


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg + ffprobe")
class Straight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"REEL_CAPCUT_ALPHA_CACHE": os.path.join(self.tmp, "cache")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertStraight(self, path, ref):
        r, g, b, a = _pixel(path)
        R, G, B, A = ref
        self.assertTrue(0 < a < 255, "must stay semi-transparent")
        self.assertAlmostEqual(a, A, delta=2, msg="alpha itself must not change")
        self.assertGreater(max(r, g, b), a + TOL, f"{(r, g, b, a)}: premultiplied, CapCut shows it dark")
        for c, C in zip((r, g, b), (R, G, B)):
            self.assertAlmostEqual(c, C, delta=6, msg=f"{(r, g, b)} vs the render {(R, G, B)}")
        self.assertEqual(_pixel(path, x=3 * W // 4)[3], 0, "a clear pixel must stay clear")

    def test_a_render_goes_in_as_a_cleaned_copy_of_the_same_name(self):
        for name in ("captions.mov", "video_03202325ff9fd73a.mp4"):
            src = _straight(os.path.join(self.tmp, name))
            before, ref = _sha(src), _pixel(src)
            out = capcut_alpha.for_capcut(src)
            self.assertNotEqual(out, src, name)
            self.assertEqual(os.path.basename(out), name)
            self.assertEqual(_sha(src), before, "the render itself is never changed")
            self.assertFalse(capcut_alpha.needs_preparing(out), "a cleaned copy is not cleaned again")
            for c, C in zip(_pixel(out)[:3], ref[:3]):
                self.assertAlmostEqual(c, C, delta=6, msg="real colour (alpha ~214) is kept")
            self.assertEqual(_pixel(out, x=3 * W // 4)[3], 0, "a clear pixel stays clear")

    def test_faint_edge_hue_noise_is_greyed(self):
        """you 2026-10-18: a rainbow ring around every card in CapCut, from random hues at alpha <= 16/255."""
        src = os.path.join(self.tmp, "shadow.mov")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=0xFF0000@0.03:s={W}x{H}:d=0.2:r=30,format=rgba", "-c:v", "prores_ks",
                        "-profile:v", "4444", "-alpha_bits", "16", "-pix_fmt", "yuva444p12le", "-f", "mov", src], check=True)
        r, g, b, a = _pixel(capcut_alpha.for_capcut(src))
        self.assertLess(a, capcut_alpha.EDGE_A, "fixture must be a faint pixel")
        self.assertLessEqual(max(r, g, b) - min(r, g, b), 6, f"a faint pixel's hue is noise: grey it, got {(r, g, b)}")

    def test_a_faint_dark_shadow_carries_no_colour_to_add(self):
        """you 2026-10-18: greyed, the ring turned WHITE: CapCut adds a faint pixel's colour, so dark ones are scaled
        by their alpha. White at half alpha (soft white type) is left as it is."""
        src = os.path.join(self.tmp, "dark.mov")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=0x30200F@0.05:s={W}x{H}:d=0.2:r=30,format=rgba", "-c:v", "prores_ks",
                        "-profile:v", "4444", "-alpha_bits", "16", "-pix_fmt", "yuva444p12le", "-f", "mov", src], check=True)
        r, g, b, a = _pixel(capcut_alpha.for_capcut(src))
        self.assertLessEqual(max(r, g, b), 4, f"a 5% dark shadow pixel keeps ~5% of its colour, got {(r, g, b, a)}")
        white = os.path.join(self.tmp, "white.mov")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=0xFFFFFF@0.5:s={W}x{H}:d=0.2:r=30,format=rgba", "-c:v", "prores_ks",
                        "-profile:v", "4444", "-alpha_bits", "16", "-pix_fmt", "yuva444p12le", "-f", "mov", white], check=True)
        self.assertGreaterEqual(min(_pixel(capcut_alpha.for_capcut(white))[:3]), 245, "white type's soft edge stays white")

    def test_the_clean_never_costs_a_layer(self):
        src = _straight(os.path.join(self.tmp, "layer.mov"))
        with mock.patch.object(capcut_alpha, "_clean_edges", side_effect=RuntimeError("no numpy here")):
            self.assertEqual(capcut_alpha.for_capcut(src), src, "can't clean: the render goes in as it is")

    def test_old_premultiplied_copy_is_turned_back_to_straight(self):
        src = _straight(os.path.join(self.tmp, "render.mov"))
        old = _old_premultiplied(src, os.path.join(self.tmp, "video_17c7880777128018.mp4"))
        self.assertIs(capcut_alpha.straight_evidence(old), False, "fixture must be premultiplied")
        out = capcut_alpha.for_capcut(old)
        self.assertNotEqual(out, old)
        self.assertEqual(os.path.basename(out), "video_17c7880777128018.mp4", "the material path must not move")
        self.assertFalse(capcut_alpha.is_tagged(out))
        self.assertStraight(out, _pixel(src))
        with mock.patch.object(capcut_alpha, "prepare", side_effect=AssertionError("prepared twice")):
            self.assertEqual(capcut_alpha.for_capcut(old), out, "an unchanged layer is prepared once")
            self.assertEqual(capcut_alpha.for_capcut(out), out, "a prepared copy is left as it is")

    def test_transparent_webm_becomes_straight_prores(self):
        """CapCut drops VP9 alpha (everything clear turns black), so a cutout WebM is converted."""
        web = _webm(os.path.join(self.tmp, "cutout.webm"))
        self.assertTrue(capcut_alpha.needs_preparing(web), "a WebM's alpha hides in a side channel: probe it")
        out = capcut_alpha.for_capcut(web)
        self.assertNotEqual(out, web)
        self.assertIn("prores", _streams(out))
        self.assertIn("yuva444p", _streams(out))
        ref = _pixel(_straight(os.path.join(self.tmp, "ref.mov")))
        r, g, b, a = _pixel(out)
        self.assertAlmostEqual(a, ref[3], delta=4, msg="the WebM's alpha must survive")
        self.assertGreater(max(r, g, b), a + TOL, "converted straight, not premultiplied")
        self.assertEqual(_pixel(out, x=3 * W // 4)[3], 0, "clear must stay clear, not black")

    def test_sound_on_a_transparent_clip_survives(self):
        src = _straight(os.path.join(self.tmp, "with_sound.mov"), audio=True)
        out = capcut_alpha.for_capcut(_old_premultiplied(src, os.path.join(self.tmp, "video_with_sound.mp4")))
        self.assertIn("audio", _streams(out), "preparing must not silently drop the clip's sound")

    def test_render_itself_is_never_changed(self):
        src = _straight(os.path.join(self.tmp, "r.mov"))
        old = _old_premultiplied(src, os.path.join(self.tmp, "old.mov"))
        before = _sha(old)
        capcut_alpha.for_capcut(old)
        self.assertEqual(_sha(old), before)

    def test_stills_and_opaque_clips_are_untouched(self):
        png = os.path.join(self.tmp, "sticker.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=0xFFEB7D@0.5:s={W}x{H}:d=0.04,format=rgba", "-frames:v", "1", png], check=True)
        mov = os.path.join(self.tmp, "footage.mov")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:s={W}x{H}:d=0.4",
                        "-c:v", "prores_ks", "-profile:v", "2", mov], check=True)
        web = os.path.join(self.tmp, "opaque.webm")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:s={W}x{H}:d=0.4",
                        "-c:v", "libvpx-vp9", "-b:v", "200k", web], check=True)
        for f in (png, mov, web):
            before = _sha(f)
            self.assertEqual(capcut_alpha.for_capcut(f), f, f)
            self.assertEqual(_sha(f), before)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg + ffprobe")
class DraftGuardrail(unittest.TestCase):
    """capcut_media.ensure_shippable is the one place every finalized draft passes through."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"REEL_CAPCUT_ALPHA_CACHE": os.path.join(self.tmp, "cache")})
        self._env.start()
        self.D = os.path.join(self.tmp, "draft")
        os.makedirs(os.path.join(self.D, "assets", "video"))
        with open(os.path.join(self.D, "draft_meta_info.json"), "w", encoding="utf-8") as f:
            json.dump({"draft_materials": []}, f)

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _draft(self, path):
        return {"materials": {"videos": [{"id": "m1", "path": path}], "audios": []},
                "tracks": [{"type": "video", "segments": [{"material_id": "m1"}]}]}

    def test_render_in_a_fresh_draft_gets_its_edges_cleaned_in_place(self):
        local = _straight(os.path.join(self.D, "assets", "video", "video_17c7880777128018.mp4"))
        ref = _pixel(local)
        self.assertEqual(capcut_media.ensure_shippable(self._draft(local), self.D, fresh=True), [])
        self.assertFalse(capcut_alpha.needs_preparing(local), "the draft's own copy is the cleaned one")
        for c, C in zip(_pixel(local)[:3], ref[:3]):
            self.assertAlmostEqual(c, C, delta=6)

    def test_old_premultiplied_asset_in_a_fresh_draft_is_restored(self):
        ref = _pixel(_straight(os.path.join(self.tmp, "render.mov")))
        local = _old_premultiplied(os.path.join(self.tmp, "render.mov"),
                                   os.path.join(self.D, "assets", "video", "video_aa.mp4"))
        d = self._draft(local)
        self.assertEqual(capcut_media.ensure_shippable(d, self.D, fresh=True), [])
        self.assertTrue(os.path.isfile(local), "same name, same material")
        self.assertFalse(capcut_alpha.is_tagged(local))
        r, g, b, a = _pixel(local)
        self.assertGreater(max(r, g, b), a + TOL)
        self.assertAlmostEqual(a, ref[3], delta=2)
        once = _sha(local)
        self.assertEqual(capcut_media.ensure_shippable(d, self.D, fresh=True), [])
        self.assertEqual(_sha(local), once, "a second finalize pass must leave it alone")

    def test_webm_cutout_copied_in_from_outside(self):
        web = _webm(os.path.join(self.tmp, "cutout.webm"))
        before = _sha(web)
        d = self._draft(web)
        self.assertEqual(capcut_media.ensure_shippable(d, self.D), [])
        local = os.path.join(self.D, "assets", "video", "cutout.mov")
        self.assertTrue(os.path.isfile(local), "the converted cutout is QuickTime and named .mov")
        self.assertTrue(d["materials"]["videos"][0]["path"].endswith("/assets/video/cutout.mov"))
        self.assertIn("yuva444p", _streams(local))
        self.assertEqual(_pixel(local, x=3 * W // 4)[3], 0, "clear must stay clear, not black")
        self.assertEqual(_sha(web), before)

    def test_a_draft_she_may_have_opened_is_not_changed_in_place(self):
        local = _old_premultiplied(_straight(os.path.join(self.tmp, "r.mov")),
                                   os.path.join(self.D, "assets", "video", "layer.mov"))
        before = _sha(local)
        self.assertEqual(capcut_media.ensure_shippable(self._draft(local), self.D), [])
        self.assertEqual(_sha(local), before, "a media change on an opened draft belongs in a new version")


class Wiring(unittest.TestCase):
    """Every route into a draft passes through the transparency step; the bake does not."""

    def src(self, name):
        with open(os.path.join(PRODUCT, name), encoding="utf-8") as f:
            return f.read()

    def test_fresh_builds_prepare_inside_the_draft(self):
        for f in ("cleanyap.py", "superyap.py"):
            self.assertIn("ensure_shippable(d, D, fresh=True)", self.src(f), f)

    def test_superyap_prepares_alpha_before_the_draft_is_written(self):
        src = self.src("superyap.py")
        fix = src.index("ensure_shippable(d, D, fresh=True)")
        write = src.index("json.dump(d, open(p,", fix)
        self.assertLess(fix, write, "the transparency pass must run before the draft JSON is written")

    def test_writers_that_skip_finalize_prepare_it_themselves(self):
        for f in ("build-overlay.py", "capcut-slip-reel.py"):
            self.assertIn("capcut_alpha.for_capcut(", self.src(f), f)

    def test_the_bake_reads_straight_alpha_untouched(self):
        for f in ("build-reel-mp4.py", "reel_render.py"):
            self.assertNotIn("capcut_alpha", self.src(f), f)

    def test_nothing_premultiplies_on_the_way_into_capcut(self):
        self.assertNotIn("premultiply=inplace", self.src("capcut_alpha.py").replace("unpremultiply=inplace", ""))


if __name__ == "__main__":
    unittest.main()
