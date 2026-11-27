import csv
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples import demo_audio


def recording(path, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(bytes([marker, 0]) * 160)


class LocalAudioTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name) / "demo"
        self.project.mkdir()
        self.rows = [{"audio": "audio/A.wav", "text": "灯亮了。"},
                     {"audio": "audio/B.wav", "text": "钥匙在这里。"}]
        with (self.project / "collection.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.rows[0])
            writer.writeheader()
            writer.writerows(self.rows)

    def test_manual_files_are_preserved_and_only_missing_files_are_generated(self):
        first = self.project / "audio/A.wav"
        recording(first, 17)
        original = first.read_bytes()

        def generate(rows, stage, _):
            self.assertEqual(rows, self.rows[1:])
            recording(stage / "audio/B.wav", 9)
            return stage

        with patch.object(demo_audio, "stage_audio", side_effect=generate) as synth:
            demo_audio.ensure_demo_audio(self.project, self.project)
            demo_audio.ensure_demo_audio(self.project, self.project)
            self.assertEqual(synth.call_count, 1)
        self.assertEqual(first.read_bytes(), original)

    def test_partial_engine_output_is_not_published(self):
        def generate(rows, stage, _):
            recording(stage / "audio/A.wav", 22)
            return stage  # Simulate a tool that completed without writing its second WAV.

        with patch.object(demo_audio, "stage_audio", side_effect=generate):
            with self.assertRaisesRegex(ValueError, "B.wav"):
                demo_audio.ensure_demo_audio(self.project, self.project)
        self.assertFalse(list(self.project.rglob("*.wav")))

    def test_missing_audio_in_started_review_must_be_restored(self):
        (self.project / ".review").mkdir()
        (self.project / ".review/review.sqlite3").touch()
        with patch.object(demo_audio, "stage_audio") as synth:
            with self.assertRaisesRegex(ValueError, "恢复原始音频"):
                demo_audio.ensure_demo_audio(self.project, self.project)
            synth.assert_not_called()

    def test_espeak_checks_available_mandarin_voice_and_uses_utf8_input(self):
        with patch.object(demo_audio, "run_command", return_value=" 5 cmn --/M Mandarin sit/cmn") as cmd:
            demo_audio.write_with_espeak("espeak-ng", self.rows, self.project)
            self.assertEqual(cmd.call_count, 3)
            for call, row in zip(cmd.call_args_list[1:], self.rows):
                args = call.args[0]
                self.assertEqual(args[args.index("-v")+1], "cmn")
                self.assertEqual(call.kwargs["text"], row["text"] + "\n")
        with patch.object(demo_audio, "run_command", return_value=" 5 en --/M English gmw/en"):
            with self.assertRaisesRegex(ValueError, "普通话"):
                demo_audio.write_with_espeak("espeak-ng", self.rows, self.project)
