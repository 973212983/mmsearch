import unittest

from mmsearch import embedder
from mmsearch.embedder import Cancelled, Embedder


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.old_poll, embedder.PAUSE_POLL = embedder.PAUSE_POLL, 0.01
        self.emb = Embedder(server=None)
        self.emb.background = True
        self.events = []
        self.emb.on_pause = self.events.append

    def tearDown(self):
        embedder.PAUSE_POLL = self.old_poll

    def test_background_requests_wait_until_the_reason_is_gone(self):
        reasons = iter(["使用电池", "使用电池", None])
        self.emb.pause_reason = lambda: next(reasons)
        with self.emb._request():
            pass
        self.assertEqual(["使用电池", None], self.events)

    def test_stopping_while_paused_cancels(self):
        self.emb.pause_reason = lambda: "全屏应用运行中"
        self.emb.should_stop = lambda: len(self.events) > 0
        with self.assertRaises(Cancelled):
            with self.emb._request():
                pass
        self.assertEqual(["全屏应用运行中", None], self.events)

    def test_searches_are_never_paused(self):
        self.emb.background = False
        self.emb.pause_reason = lambda: "使用电池"
        with self.emb._request():
            pass
        self.assertEqual([], self.events)


if __name__ == "__main__":
    unittest.main()
