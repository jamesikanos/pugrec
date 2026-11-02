"""Regression gate: fixture edits must keep resolving to exactly the same plan, audio graph and check output.

New features are additions (new keys, new values): none of these may change. When a change to an
existing edit's result is intended and agreed, regenerate with
    PUGREC_REGRESS_UPDATE=1 .venv/bin/python -m unittest tests.test_regress
and say why in the commit. Real edits: `.venv/bin/python -m pugrec.edit.regress save|compare`.
"""

import json
import os
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_edit import add_transcript, make_session  # noqa: E402
from pugrec.edit import regress, resolve as R, script as S  # noqa: E402

EXPECTED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regress.json")
V = [{"name": "main", "size": "1920x1080"}, {"name": "vertical", "size": "1080x1920"}]

# One edit per feature family that exists today (and a couple that mix them).
FIXTURES = {
    "screen": {"scenes": [{"at": 0, "shot": "screen"}]},
    "window-cover-follow": {"scenes": [{"at": 0, "shot": {"window": {"title": "right"}, "follow": True}, "fit": "cover"}]},
    "region-contain": {"scenes": [{"at": 0, "shot": {"window": {"title": "left"}, "region": [0, 0, 0.5, 0.25]},
                                   "fit": "contain", "bubble": False}]},
    "two-outputs": {"outputs": V, "scenes": [{"at": 0, "shot": "screen"}, {"at": 4, "shot": "camera"}]},
    "cuts-timeline": {"timeline": [{"source": "main", "from": 6, "to": 8}, {"source": "main", "from": 1, "to": 2}],
                      "scenes": [{"at": 0, "shot": "screen"}]},
    "transcript-cuts": {"cuts": {"pauses": 1.2, "fillers": True}, "scenes": [{"at": 0, "shot": "screen"}]},
    "move-transition": {"scenes": [{"at": 0, "shot": "screen", "fit": "cover", "bubble": False},
                                   {"at": 5, "shot": {"cursor": {"zoom": 2}}, "transition": {"move": 1.0}, "bubble": False}]},
    "spotlight": {"style": {"spotlight": {"window": {"title": "right"}}}, "scenes": [{"at": 0, "shot": "screen"}]},
    "camera-follow": {"scenes": [{"at": 0, "shot": {"camera": {"zoom": 1.5}, "follow": True}, "push": False}]},
    "camera-default": {"scenes": [{"at": 0, "shot": "camera"}]},
    "bubble-corner": {"scenes": [{"at": 0, "shot": "screen", "bubble": {"corner": "top-right"}}]},
    "inset": {"scenes": [{"at": 0, "shot": {"window": {"title": "left"}}, "fit": "inset", "bubble": False},
                         {"at": 5, "shot": "camera", "fit": "inset", "bubble": False}]},
    "push": {"scenes": [{"at": 0, "shot": "camera", "bubble": False, "push": {"to": 0.9, "over": 4}}]},
    "split": {"outputs": V, "scenes": [{"at": 0, "shot": {"split": ["screen", "camera"]}, "bubble": False}]},
    "punch": {"scenes": [{"at": 0, "shot": "screen", "fit": "cover", "punch": 3.0, "bubble": False},
                         {"at": 5, "shot": "camera", "punch": 6.5, "bubble": False}]},
    "captions": {"style": {"captions": {"max_words": 3, "size": 0.06, "position": "top", "fix": {"demo": "Acme"}}},
                 "outputs": V, "scenes": [{"at": 0, "shot": "screen"}]},
    "captions-off": {"style": {"captions": False}, "scenes": [{"at": 0, "shot": "screen"}]},
    "cards": {"intro": "Hi", "outro": {"title": "Bye", "duration": 1}, "style": {"card": {"title_case": True}},
              "scenes": [{"at": 0, "shot": "screen"}]},
    "callouts": {"scenes": [{"at": 0, "shot": "screen", "bubble": False}],
                 "callouts": [{"at": 1, "title": "Owner", "text": "Who signs it off"},
                              {"at": 3, "title": "Status", "look": "pill"},
                              {"at": 5, "title": "Next", "look": "bar", "pause": 1},
                              {"at": 7, "title": "Section", "look": "section"}]},
    "mixed-vertical": {"outputs": V, "cuts": {"ranges": [[5.3, 5.8]]}, "intro": "Acme demo",
                       "scenes": [{"at": 0, "shot": "camera", "bubble": False},
                                  {"at": 2, "shot": {"window": {"title": "right"}}, "fit": "inset", "push": True},
                                  {"at": 6, "shot": {"split": ["screen", "camera"]}, "punch": 7}],
                       "callouts": [{"at": 2.5, "title": "Acme", "text": "One line"}]},
    "overlays": {"outputs": V, "scenes": [{"at": 0, "shot": "camera", "bubble": False}],
                 "overlays": [{"at": 1, "image": "shot.png", "duration": 2, "credit": "Source: Acme",
                               "vertical": {"size": 0.95}},
                              {"at": 3.5, "text": "Big *news*", "in": "pop", "pause": 0.5},
                              {"at": 5, "image": "shot.png", "place": "full", "scroll": True, "duration": 3},
                              {"at": 8, "image": "shot.png", "place": "face"}]},
}


def snapshots():
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, data in FIXTURES.items():
            d = os.path.join(tmp, name)
            os.makedirs(d)
            make_session(d)
            add_transcript(d)
            open(os.path.join(d, "mic-clean.flac"), "w").close()   # the audio graph's mic-clean branch
            Image.new("RGB", (400, 1200), (200, 40, 40)).save(os.path.join(d, "shot.png"))   # overlays
            path = os.path.join(d, "edit.yaml")
            with open(path, "w") as f:
                json.dump(data, f)            # JSON is YAML
            sc = S.load(path)
            # The temp dir becomes <S> before frames are hashed (an overlay's image is a path).
            snap = {"check": regress.check_output(path),
                    "outputs": {o["name"]: regress.plan_snapshot(
                        json.loads(json.dumps(R.resolve(sc, o["name"], fps=10)).replace(d, "<S>")))
                        for o in sc["outputs"]}}
            out[name] = json.loads(json.dumps(snap).replace(d, "<S>"))
    return out


class Regress(unittest.TestCase):
    def test_fixture_edits_unchanged(self):
        now = snapshots()
        if os.environ.get("PUGREC_REGRESS_UPDATE"):
            with open(EXPECTED, "w") as f:
                json.dump(now, f, indent=1, sort_keys=True)
                f.write("\n")
            self.skipTest("regress.json rewritten")
        with open(EXPECTED) as f:
            expected = json.load(f)
        for name in FIXTURES:
            with self.subTest(name):
                self.assertIn(name, expected, "new fixture: regenerate regress.json")
                self.assertEqual(regress.diff(expected[name], now[name]), [])
