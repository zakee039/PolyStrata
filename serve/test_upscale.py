"""Anime upscale routing, fallback and compatibility tests."""
from __future__ import annotations
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import images
from serve import upscale


class Fake:
    def __init__(self, directory):
        self.cfg = {"upscaler": {"enabled": True, "max_output_side": 3072}}
        self.files = []
        self.directory = directory
    def store(self, data):
        self.files.append(data)
        return f"{len(self.files):032x}.png"


def png(w, h):
    return upscale.PNG_HEADER + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", w,h) + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00"


class Style(unittest.TestCase):
    def job(self, **changes):
        return {"prompt": "a cat", "width": 768, "height": 768,
                "upscale": "auto", "style_hint": "auto", **changes}

    def test_anime_keywords(self):
        for name in ("anime portrait", "Miku by cherry blossom", "初音未来立绘",
                     "二次元角色", "cel-shaded anime"):
            with self.subTest(name=name):
                self.assertEqual(upscale.should_upscale(self.job(prompt=name)), (True, "anime"))

    def test_other_styles_skip(self):
        for s in ("realistic photograph of mountains", "watercolor illustration", "cartoon cat",
                  "pixel art", "photorealistic anime",
                  "non-anime illustration"):
            with self.subTest(s=s):
                self.assertFalse(upscale.should_upscale(self.job(prompt=s))[0])

    def test_priority(self):
        self.assertEqual(upscale.should_upscale(self.job(prompt="anime", upscale="off"))[1], "disabled")
        self.assertEqual(upscale.should_upscale(self.job(prompt="real photo", upscale="4x"))[1], "explicit")
        self.assertEqual(upscale.should_upscale(self.job(prompt="a person", style_hint="anime"))[1], "anime")
        self.assertEqual(upscale.should_upscale(self.job(prompt="anime",style_hint="other"))[1], "anime")
        self.assertEqual(upscale.should_upscale(self.job(prompt="not anime",style_hint="anime"))[1], "explicit_non_anime")
        self.assertEqual(upscale.should_upscale(self.job(prompt="anime",width=1024))[1], "output_size_limit")

    def test_invalid_options(self):
        for option in ("no", None, 10):
            with self.assertRaises(ValueError):
                upscale.options({"strata_upscale": option})
        with self.assertRaises(ValueError):
            upscale.options({"strata_style_hint": "photography"})

    def test_openai_api_defaults_not_changed(self):
        req = images.parse_request({"prompt": "动漫猫咪"}, {})
        self.assertEqual((req["width"], req["height"],req["upscale"]), (1024,1024,"off"))

    def test_success_and_original_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            fake=Fake(folder)
            original=png(768,768)
            output=png(3072,3072)
            with mock.patch("serve.upscale._upscale_one",return_value=(output,1.15)) as proc:
                pictures, info=upscale.apply(fake,[original],self.job(prompt="anime"))
            self.assertEqual(pictures,[output])
            self.assertEqual(fake.files,[original])
            self.assertTrue(info["upscale_applied"])
            self.assertEqual(info["upscale_output_size"],"3072x3072")
            self.assertEqual(info["upscale_seconds"],1.15)
            proc.assert_called_once()

    def test_failed_upscale_falls_back_to_original(self):
        with tempfile.TemporaryDirectory() as folder:
            fake=Fake(folder)
            original=png(768,768)
            with mock.patch("serve.upscale._upscale_one",side_effect=RuntimeError("GPU lost")):
                pictures, info=upscale.apply(fake,[original],self.job(prompt="anime"))
            self.assertEqual(pictures,[original])
            self.assertEqual(fake.files,[])
            self.assertTrue(info["upscale_fallback"])
            self.assertEqual(info["upscale_failures"],1)
            self.assertFalse(info["upscale_applied"])

    def test_disabled_and_unchanged_inputs(self):
        fake=Fake(".")
        orig=png(768,768)
        pictures,info=upscale.apply(fake,[orig],self.job(prompt="realistic landscape"))
        self.assertEqual(pictures,[orig])
        self.assertEqual(info["upscale_reason"],"not_anime")
        fake.cfg.pop("upscaler")
        pictures,info=upscale.apply(fake,[orig],self.job(prompt="anime"))
        self.assertEqual(pictures,[orig])
        self.assertEqual(info["upscale_reason"],"not_configured")

if __name__ == "__main__":
    unittest.main()
