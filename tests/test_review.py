"""Exercise the public HTTP contract using anonymous audio in disposable projects."""

import csv
import io
import json
import sys
import tempfile
import threading
import unittest
import wave
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))
from server import Handler, ThreadingHTTPServer
from store import ReviewError, ReviewStore


class Project:
    def __init__(self, root, mode, *, missing=False):
        self.root = Path(root)
        (self.root / "config.json").write_text(
            json.dumps(
                dict(
                    labels=["neutral", "surprised"], review_mode=mode, show_text=False, checkpoint=1
                )
            ),
            encoding="utf-8",
        )
        with (self.root / "collection.csv").open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["sample_id", "audio", "text", "intended_label", "cue"])
            for sid in ["A", "B"]:
                writer.writerow(
                    [sid, sid + ".wav", "private transcript", "private preset", "private direction"]
                )
                if missing and sid == "B":
                    continue
                with wave.open(str(self.root / (sid + ".wav")), "wb") as wav:
                    wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                    wav.writeframes(b"\x01\x00" * 1000)
        self.open()

    def open(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.store = ReviewStore(self.root)
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.keys = self.server.store.credentials()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()

    def request(self, role, path, payload=None, *, headers=None, expected=200):
        h = {"Authorization": "Bearer " + self.keys[role]} if role else {}
        if headers:
            h.update(headers)
        if payload is not None:
            h["Content-Type"] = "application/json"
        request = Request(
            self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=h,
        )
        try:
            result = build_opener().open(request, timeout=5)
        except HTTPError as exc:
            result = exc
        with result as response:
            raw = response.read()
            assert response.status == expected, (response.status, raw, expected)
            if response.headers.get_content_type() == "application/json":
                return json.loads(raw)
            return raw

    def cast(self, role, sid, label, quality="pass", note="", expected=200):
        return self.request(
            role,
            "/api/vote",
            dict(sample_id=sid, label=label, quality=quality, note=note),
            expected=expected,
        )


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = temp.name

    def test_joint_and_revisions(self):
        p = Project(self.directory, "joint")
        self.addCleanup(p.stop)
        p.request(None, "/api/state", expected=401)
        p.request(None, "/audio/A", expected=401)
        p.request("r1", "/audio/..%2Fconfig.json", expected=404)
        first = p.request("r1", "/audio/A", headers={"Range": "bytes=0-3"}, expected=206)
        self.assertEqual(first, b"RIFF")
        p.request("r1", "/audio/A", headers={"Range": "bytes=9999999-"}, expected=416)
        if Path(__file__).resolve().parents[1].joinpath("review/index.html").exists():
            browser = build_opener(HTTPCookieProcessor(CookieJar()))
            with browser.open(p.base + "/?key=" + p.keys["r1"]) as page:
                self.assertEqual(page.geturl(), p.base + "/")
                self.assertIn(b"<html", page.read().lower())
            self.assertEqual(json.load(browser.open(p.base + "/api/state"))["role"], "r1")

        for role in ["r1", "r2"]:
            view = p.request(role, "/api/state")
            self.assertFalse(
                any(
                    key in s
                    for s in view["samples"]
                    for key in ("text", "votes", "final", "intended_label", "cue")
                )
            )
        p.cast("r1", "A", "neutral")
        p.request(
            "r2",
            "/api/vote",
            dict(sample_id="A", label="neutral"),
            headers={"X-Review-Role": "r1"},
            expected=409,
        )
        p.cast("r1", "A", "surprised", expected=409)
        p.request("r1", "/api/finish", {}, expected=409)
        rows = [dict(sample_id="A", label="neutral"), dict(sample_id="B", label="neutral")]
        merged = p.request("r1", "/api/offline", {"rows": rows})
        self.assertEqual((merged["inserted"], merged["skipped"]), (["B"], ["A"]))
        conflict = p.request(
            "r1", "/api/offline", {"rows": [dict(sample_id="A", label="surprised")]}
        )
        self.assertEqual(conflict["conflicts"], ["A"])
        p.request(
            "r1",
            "/api/offline",
            {"rows": [dict(reviewer="r2", sample_id="A", label="neutral")]},
            expected=403,
        )
        p.request("r1", "/api/finish", {})
        self.assertFalse(any(s["own_vote"] for s in p.request("r2", "/api/state")["samples"]))
        p.request("r1", "/final.csv", expected=409)
        p.cast("r2", "A", "neutral")
        p.cast("r2", "B", "surprised")
        p.request("r2", "/api/finish", {})
        state = p.request("r2", "/api/state")
        self.assertEqual(state["counts"]["disagreements"], 1)
        self.assertEqual(state["counts"]["finalized"], 1)
        def export():
            raw = p.request("r1", "/final.csv").decode("utf-8-sig")
            return {row["sample_id"]: row for row in csv.DictReader(io.StringIO(raw))}

        self.assertEqual(export()["B"]["human_label"], "")
        proposal = {
            "sample_id": "B",
            "label": "surprised",
            "quality": "pass",
            "reason": "reviewed separately",
            "version": 0,
        }
        p.request("r1", "/api/propose", proposal)
        p.request("r1", "/api/confirm", dict(sample_id="B", version=1))
        self.assertEqual(export()["B"]["human_label"], "")
        p.request("r2", "/api/propose", {**proposal, "label": "neutral", "version": 1})
        p.request("r1", "/api/confirm", dict(sample_id="B", version=1), expected=409)
        p.request("r1", "/api/confirm", dict(sample_id="B", version=2))
        self.assertEqual(export()["B"]["human_label"], "neutral")
        self.assertEqual(export()["B"]["initial_disagreement"], "true")
        p.request("r1", "/api/propose", {**proposal, "sample_id": "A", "version": 1})
        self.assertEqual(export()["A"]["human_label"], "")
        p.request("r2", "/api/confirm", dict(sample_id="A", version=2))
        self.assertEqual(export()["A"]["initial_disagreement"], "false")
        audit = p.request("r1", "/audit.csv").decode("utf-8-sig")
        self.assertIn("offline_conflict", audit)
        self.assertIn("agreement", audit)
        self.assertIn("reviewed separately", audit)
        keys = p.keys.copy()
        p.stop()
        p.open()
        self.assertEqual(p.keys, keys)
        self.assertEqual(export()["A"]["human_label"], "surprised")

    def test_third_and_audio_evidence(self):
        p = Project(self.directory, "third", missing=True)
        self.addCleanup(p.stop)
        self.assertTrue(
            next(s for s in p.request("r2", "/api/state")["samples"] if s["sample_id"] == "B")[
                "audio_error"
            ]
        )
        p.cast("adjudicator", "A", "surprised", expected=403)
        p.request("adjudicator", "/api/propose", dict(sample_id="A", version=0), expected=409)
        p.cast("r1", "B", "neutral", expected=400)
        for role, label in [("r1", "surprised"), ("r2", "neutral")]:
            p.cast(role, "A", label)
            p.cast(role, "B", "", "reject", "no recording")
            p.request(role, "/api/finish", {})
        proposal = dict(
            sample_id="A", quality="pass", version=0, label="neutral", reason="replayed"
        )
        p.request("r2", "/api/propose", proposal, expected=403)
        p.request("adjudicator", "/api/propose", proposal)
        p.request("adjudicator", "/api/propose", proposal, expected=409)
        rows = list(
            csv.DictReader(io.StringIO(p.request("adjudicator", "/final.csv").decode("utf-8-sig")))
        )
        self.assertEqual([r["human_label"] for r in rows], ["neutral", ""])
        path = Path(self.directory) / "A.wav"
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 1
        path.write_bytes(raw)
        p.request("r1", "/audio/A", expected=404)
        with self.assertRaises(ReviewError):
            ReviewStore(self.directory)


if __name__ == "__main__":
    unittest.main()
