"""Unit-level checks for explicit manual stop versus ordinary idle unload.

The fake engine does not allocate GPU memory or touch real snapshot files.
"""
import threading
import time
import unittest

from serve.server import GpuBusy, Service


class FakeEngine:
    def __init__(self):
        self.running = True
        self.unloaded = False
        self.info = {}
        self.starts = 0
        self.stops = 0

    def alive(self):
        return self.running

    def unload(self):
        self.running = False
        self.unloaded = True
        self.stops += 1

    def restart(self):
        self.running = True
        self.unloaded = False
        self.starts += 1


def fake_service():
    # Construct only the fields touched by the model lifecycle.
    svc = Service.__new__(Service)
    svc.engine = FakeEngine()
    svc.manual_unloaded = False
    svc.fifo = threading.Lock()
    svc.status_lock = threading.Lock()
    svc.status = {"busy": False, "queued": 0}
    svc.request_trace = threading.local()
    svc.session_v1 = None
    svc.session_v1_kv_enabled = False
    svc.vision = None
    svc.vision_held = False
    svc.vram_reserve = None
    svc.min_free_vram_mib = 0
    svc.before_load = None
    svc.restore_after_load = False
    svc.last_request_at = time.time() - 1000
    svc.started_at = svc.last_request_at
    svc.loaded = lambda: svc.engine.alive()
    svc._vision_down = lambda: False
    svc._v1_save_active = lambda: None
    return svc


class ManualUnloadTests(unittest.TestCase):
    def test_manual_stop_blocks_inference_until_explicit_start(self):
        svc = fake_service()
        self.assertEqual(svc.unload(manual=True), "unloaded")
        self.assertTrue(svc.manual_unloaded)
        with self.assertRaisesRegex(GpuBusy, "manually stopped"):
            svc.load()
        self.assertEqual(svc.engine.starts, 0)
        svc.load(explicit=True)
        self.assertFalse(svc.manual_unloaded)
        self.assertEqual(svc.engine.starts, 1)

    def test_idle_unload_allows_request_auto_load(self):
        svc = fake_service()
        self.assertEqual(svc.unload(idle_for=900), "unloaded")
        self.assertFalse(svc.manual_unloaded)
        svc.load()
        self.assertEqual(svc.engine.starts, 1)

    def test_hold_when_model_already_unloaded(self):
        svc = fake_service()
        self.assertEqual(svc.unload(), "unloaded")
        self.assertEqual(svc.unload(manual=True), "not loaded")
        self.assertTrue(svc.manual_unloaded)
        with self.assertRaises(GpuBusy):
            svc.load()


if __name__ == "__main__":
    unittest.main()
