"""Build compositor: the backdrop/inset primitives (gradient, shadow, cropped rounded image)."""

import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pugrec.edit import compose as C  # noqa: E402


def canvas(w=200, h=100):
    return np.zeros((h, w, 3), np.uint8)


class Gradient(unittest.TestCase):
    def test_angle_convention_matches_css(self):
        t = C.gradient_t(100, 50, 90)       # 90 deg: left -> right
        self.assertLess(t[25, 0], 0.02)
        self.assertGreater(t[25, -1], 0.98)
        t = C.gradient_t(100, 50, 180)      # 180 deg: top -> bottom
        self.assertLess(t[0, 50], 0.02)
        self.assertGreater(t[-1, 50], 0.98)
        t = C.gradient_t(100, 100, 135)     # diagonal: the corners are the ends
        self.assertLess(t[0, 0], 0.02)
        self.assertGreater(t[-1, -1], 0.98)

    def test_colours_at_the_ends_and_between(self):
        c = canvas()
        C.paint(c, [{"type": "gradient", "dst": [0, 0, 200, 100], "colors": ["#ff0000", "#0000ff"], "angle": 90}], {})
        self.assertEqual(list(c[50, 0]), [1, 0, 254])      # BGR: red at the left (within a step)
        self.assertEqual(list(c[50, -1]), [254, 0, 1])     # blue at the right
        mid = c[50, 100]
        self.assertTrue(120 < mid[0] < 135 and 120 < mid[2] < 135)

    def test_three_stops_and_rounded_corners(self):
        c = canvas()
        C.paint(c, [{"type": "gradient", "dst": [0, 0, 200, 100], "colors": ["#000000", "#ffffff", "#000000"],
                     "angle": 90, "shape": "round", "radius": 30}], {})
        self.assertGreater(c[50, 100].min(), 240)          # the middle stop
        self.assertEqual(list(c[0, 0]), [0, 0, 0])         # outside the rounded corner: untouched


class Shadow(unittest.TestCase):
    def test_soft_dark_under_the_card_nothing_far_away(self):
        c = np.full((100, 200, 3), 200, np.uint8)
        C.paint(c, [{"type": "fill", "dst": [0, 0, 200, 100], "color": "#c8c8c8"},
                    {"type": "shadow", "dst": [50, 25, 100, 50], "radius": 10, "blur": 20, "opacity": 0.5,
                     "offset": 4}], {})
        self.assertLess(c[50, 100, 0], 120)                # under the card: darkened by ~half
        self.assertLess(c[25 + 50 + 4, 100, 0], 200)       # just below the card edge: soft falloff
        self.assertEqual(c[2, 2, 0], 200)                  # a corner, well away: untouched


class Image(unittest.TestCase):
    def test_crop_and_round(self):
        from PIL import Image as PILImage
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "split.png")
            im = PILImage.new("RGB", (100, 50), (255, 0, 0))
            im.paste((0, 255, 0), (50, 0, 100, 50))        # left half red, right half green
            im.save(path)
            c = canvas(100, 100)
            C.paint(c, [{"type": "image", "img": "a", "dst": [0, 0, 100, 100], "rect": [50, 0, 50, 50],
                         "shape": "round", "radius": 20}], {}, {"a": {"file": path}})
            self.assertEqual(list(c[50, 50]), [0, 255, 0])    # only the green half, scaled up
            self.assertEqual(list(c[0, 0]), [0, 0, 0])        # rounded corner


if __name__ == "__main__":
    unittest.main()


class Stroke(unittest.TestCase):
    """A callout's border: a polyline, part drawn or closed."""

    def test_draws_only_along_the_path(self):
        c = canvas()
        C.draw_stroke(c, {"points": [[20, 50], [120, 50]], "closed": False, "width": 3, "color": "#41b6e6"})
        self.assertGreater(int(c[50, 70, 0]), 150)                  # on the line (BGR: blue is high)
        self.assertEqual(int(c[20, 70].sum()), 0)                   # nothing off it
        self.assertEqual(int(c[50, 160].sum()), 0)                  # nor past its end

    def test_closed_joins_the_ends_and_opacity_blends(self):
        c = canvas()
        C.draw_stroke(c, {"points": [[20, 20], [180, 20], [180, 80], [20, 80]], "closed": True, "width": 2,
                          "color": "#ffffff", "opacity": 0.5})
        self.assertGreater(int(c[50, 20, 0]), 90)                   # the closing side is drawn
        self.assertLess(int(c[20, 100, 0]), 160)                    # at half strength

    def test_left_aligned_text_starts_at_x(self):
        c = canvas(300, 60)
        C.draw_text(c, {"x": 40, "y": 30, "size": 20, "words": [{"w": "Owner", "hi": False}], "color": "#ffffff",
                        "hi": "#ffffff", "bg": "#000000", "bg_opacity": 0.0, "pad": 0.0, "radius": 0.0,
                        "align": "left"})
        cols = np.where(c.sum(axis=(0, 2)) > 0)[0]
        self.assertGreaterEqual(cols.min(), 38)
        self.assertLess(cols.min(), 50)


class Hold(unittest.TestCase):
    """A callout's pause: one still picture, silence under it, then the recording carries on."""

    def frames(self):
        f = [{"src": "main", "st": 1.0 + i / 10, "layers": []} for i in range(5)]
        f += [{"src": "main", "st": 1.5, "hold": True, "layers": []} for _ in range(10)]
        f += [{"src": "main", "st": 1.5 + i / 10, "layers": []} for i in range(5)]
        return f

    def test_a_hold_is_a_run_of_its_own(self):
        r = C.runs(self.frames(), 10)
        self.assertEqual([(a, b) for a, b, _s, _t in r], [(0, 5), (5, 15), (15, 20)])
        self.assertEqual(r[2][3], 1.5)

    def test_silence_under_the_hold_and_audio_either_side(self):
        plan = {"audio": [{"source": "main", "file": "mic.flac"}], "output": {"fps": 10}}
        inputs, graph = C.audio_args(plan, self.frames(), 1)
        self.assertEqual(inputs, ["-i", "mic.flac"])
        self.assertIn("anullsrc=r=48000:cl=mono,atrim=duration=1.0000", graph)
        self.assertIn("atrim=start=1.0000:end=1.5000", graph)
        self.assertIn("atrim=start=1.5000:end=2.0000", graph)
