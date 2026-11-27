import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "speech-emotion-dataset" / "scripts" / "pipeline.py"
spec = importlib.util.spec_from_file_location("speech_collection_pipeline", SCRIPT)
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def csv_file(path, headers, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def invoke_pipeline(args):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        code = pipeline.main(args)
    return code, json.loads(output.getvalue())


class ParcelAndFamilyTravelTest(unittest.TestCase):
    fixture = ROOT / "evals" / "fixtures" / "parcel-and-family-travel"

    def test_recording_plan_matches_each_scene(self):
        code, report = invoke_pipeline(["check", "--project", str(self.fixture)])
        self.assertEqual(code, 0, report["errors"])
        self.assertEqual((report["records"], report["unique_texts"], report["pairs"]), (8, 6, 2))

        _, rows = pipeline.read_csv(self.fixture / "collection.csv", pipeline.PLAN_FIELDS)
        expected_pairs = {"包裹送达": ["开心", "惊讶"], "家人出行": ["开心", "悲伤"]}
        for scene, labels in expected_pairs.items():
            with self.subTest(scene=scene):
                scene_rows = [row for row in rows if row["scene"] == scene]
                singles = [row["intended_label"] for row in scene_rows if not row["pair_id"]]
                pair = [row for row in scene_rows if row["pair_id"]]
                self.assertEqual(singles, ["中性", "中性"])
                self.assertCountEqual([row["intended_label"] for row in pair], labels)
                for field in ("pair_id", "speaker_id", "text"):
                    self.assertEqual(pair[0][field], pair[1][field], field)
        self.assertFalse(any(row.get("human_label") for row in rows))

    def test_swapping_scene_emotions_fails_even_with_same_totals(self):
        fields, rows = pipeline.read_csv(self.fixture / "collection.csv", pipeline.PLAN_FIELDS)
        surprised = next(row for row in rows if row["intended_label"] == "惊讶")
        sad = next(row for row in rows if row["intended_label"] == "悲伤")
        surprised["intended_label"] = "悲伤"
        sad["intended_label"] = "惊讶"

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            shutil.copyfile(self.fixture / "config.json", project / "config.json")
            csv_file(project / "collection.csv", fields, rows)
            code, report = invoke_pipeline(["check", "--project", temp])
        self.assertNotEqual(code, 0)
        self.assertEqual((report["records"], report["unique_texts"], report["pairs"]), (8, 6, 2))
        missing_quotas = [error["detail"].partition(":")[0] for error in report["errors"]
                          if error["code"] == "quota_mismatch"]
        self.assertCountEqual(missing_quotas, ["label_quotas.包裹对照.惊讶", "label_quotas.出行对照.悲伤"])


class PipelineTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "batch"
        self.project.mkdir()
        self.audio = self.project / "audio"
        self.audio.mkdir()
        self.rows = []
        definitions = [
            ("A", "照明", "平静", "甲", "灯还亮着。", "平静", "", ""),
            ("B1", "照明", "对照", "甲", "那盏灯亮了。", "欣喜", "重读“亮了”。", "B"),
            ("B2", "照明", "对照", "甲", "那盏灯亮了。", "意外", "在“灯”后停顿。", "B"),
            ("C", "物品", "平静", "乙", "扣子在这里。", "平静", "", ""),
            ("D1", "物品", "对照", "乙", "扣子回来了。", "欣喜", "", "D"),
            ("D2", "物品", "对照", "乙", "扣子回来了。", "意外", "", "D"),
        ]
        for data in definitions:
            row = dict(zip(pipeline.PLAN_FIELDS, data + (f"audio/{data[0]}.wav",)))
            self.rows.append(row)
        self.save_rows()
        self.conf = {"labels": ["平静", "欣喜", "意外"], "total_quota": 6,
                     "scene_quotas": {"照明": 3, "物品": 3}, "group_quotas": {"平静": 2, "对照": 4},
                     "pair_groups": ["对照"], "label_quotas": {"对照": {"欣喜": 2, "意外": 2}},
                     "pair_combinations": [["意外", "欣喜"], ["欣喜", "意外"]],
                     "forbid_label_words": True}
        self.save_config()
        self.out = self.project / "manifest.jsonl"
        self.final = self.project / "final.csv"
        self.profile = self.project / "profile.json"
        self.mapping = {"fields": {"id": "sample_id", "wav": "audio", "gold": "human_label",
                                   "intended": "intended_label", "pair": "pair_id"},
                        "required": ["id", "wav", "gold"],
                        "label_map": {"平静": "calm", "欣喜": "joy", "意外": "surprise"}}
        self.save_profile()
        self.judgments = [{"sample_id": row["sample_id"], "human_label": row["intended_label"],
                           "version": "v1", "initial_disagreement": "0", "quality": "pass"}
                          for row in self.rows]
        self.save_final()

    def save_rows(self):
        csv_file(self.project / "collection.csv", pipeline.PLAN_FIELDS, self.rows)

    def save_config(self):
        (self.project / "config.json").write_text(json.dumps(self.conf), encoding="utf-8")

    def save_profile(self):
        self.profile.write_text(json.dumps(self.mapping), encoding="utf-8")

    def save_final(self):
        csv_file(self.final, [*pipeline.FINAL_FIELDS, "initial_disagreement"], self.judgments)

    def wave(self, name, payload=b"\x01\x00" * 80, width=2):
        with wave.open(str(self.audio / name), "wb") as w:
            w.setparams((1, width, 8000, 0, "NONE", "not compressed"))
            w.writeframes(payload)

    def all_audio(self):
        for r in self.rows:
            self.wave(r["sample_id"] + ".wav")

    def run_cli(self, command):
        args = [command, "--project", str(self.project)]
        if command == "export":
            args += ["--final", str(self.final), "--profile", str(self.profile), "--out", str(self.out)]
        code, report = invoke_pipeline(args)
        self.assertEqual(code == 0, report["ok"])
        return report

    @staticmethod
    def errors(report):
        return {i["code"] for i in report["errors"]}

    def test_six_tasks_two_pairs_no_audio(self):
        report = self.run_cli("check")
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual((report["records"], report["unique_texts"], report["pairs"]), (6, 4, 2))
        self.assertEqual(len(self.run_cli("audio")["errors"]), 6)
        self.assertFalse(self.out.exists())

    def test_optional_gold_quota_and_pair_combination(self):
        self.all_audio()
        self.conf["gold_quotas"] = {"平静": {"平静": 2}}
        self.conf["require_pair_target_labels"] = True
        self.save_config()
        self.judgments[0]["human_label"] = "欣喜"
        self.judgments[2]["human_label"] = "平静"
        self.save_final()
        self.assertTrue({"quota_mismatch", "pair_gold_targets"} <= self.errors(self.run_cli("export")))
        self.assertFalse(self.out.exists())
        self.judgments[0]["human_label"] = "平静"
        self.judgments[1]["human_label"], self.judgments[2]["human_label"] = "意外", "欣喜"
        self.save_final()
        self.assertTrue(self.run_cli("export")["ok"])

    def test_normalized_duplicate_pair_variants_cue_and_quota(self):
        self.rows[3]["text"] = "灯，还亮着！"
        self.rows[2].update(text="那盏灯亮了！", speaker_id="乙")
        self.rows[1]["cue"] = "重读“终于”。"
        self.rows[5]["text"] += "意外。"
        self.conf["group_quotas"]["对照"] = 8
        self.save_config()
        self.save_rows()
        self.assertTrue({"unexpected_text_duplicate", "pair_text", "pair_speaker_id",
                         "quota_mismatch", "label_in_text", "cue_word_missing"}
                        <= self.errors(self.run_cli("check")))

    def test_audio_integrity_and_extras(self):
        self.all_audio()
        self.wave("B1.wav", b"\x00\x00" * 10)
        self.wave("C.wav", b"\x80" * 10, 1)
        self.wave("other.WAV")
        clip = self.audio / "D1.wav"
        clip.write_bytes(clip.read_bytes()[:-6])
        report = self.run_cli("audio")
        self.assertEqual(report["readable_audio"], 3)
        self.assertEqual(report["unlisted_audio"], ["audio/other.WAV"])
        self.assertEqual(sorted(e["sample_ids"][0] for e in report["errors"]), ["B1", "C", "D1"])

    def test_path_traversal_and_aliases(self):
        self.rows[0]["audio"] = "../outside.wav"
        self.rows[1]["audio"] = "C:/elsewhere.wav"
        self.rows[2]["audio"] = "audio/./C.wav"
        self.save_rows()
        self.assertTrue({"invalid_audio_path", "shared_audio_path"} <= self.errors(self.run_cli("audio")))

    def test_export_joins_by_id_uses_actual_gold_and_reads_back(self):
        self.all_audio()
        self.judgments[1]["human_label"], self.judgments[2]["human_label"] = "意外", "欣喜"
        self.judgments.reverse()
        self.save_final()
        self.mapping["audio_base"] = "manifest"
        self.save_profile()
        self.out = self.project / "delivery" / "manifest.jsonl"
        report = self.run_cli("export")
        self.assertTrue(report["ok"], report["errors"])
        lines = [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[1], {"id": "B1", "wav": "../audio/B1.wav", "gold": "surprise",
                                    "intended": "joy", "pair": "B"})
        self.assertTrue((self.out.parent / lines[1]["wav"]).is_file())
        self.assertEqual(len(lines), report["exported"])
        self.assertTrue(report["verified_jsonl"])

    def test_errors_preserve_existing_output(self):
        self.all_audio()
        self.out.write_bytes(b"PREVIOUS")
        self.judgments.pop(0)
        self.judgments[0].update(human_label="意外", quality="fail", version="")
        self.judgments.append(self.judgments[-1])
        self.save_final()
        errors = self.errors(self.run_cli("export"))
        self.assertTrue({"missing_final", "unfinished_or_bad_quality", "same_pair_gold",
                         "duplicate_final"} <= errors)
        self.assertEqual(self.out.read_bytes(), b"PREVIOUS")
        self.assertEqual(list(self.project.glob("tmp*")), [])

    def test_bad_profile_cannot_silently_relabel(self):
        self.all_audio()
        del self.mapping["label_map"]["意外"]
        self.save_profile()
        self.assertIn("missing_label_mapping", self.errors(self.run_cli("export")))
        self.assertFalse(self.out.exists())

    def test_receiver_constraints_omit_empty_and_numeric_channel(self):
        self.all_audio()
        with wave.open(str(self.audio / "A.wav"), "wb") as w:
            w.setparams((2, 2, 8000, 0, "NONE", "not compressed"))
            w.writeframes(b"\x00\x00\x01\x00" * 80)
        self.mapping.update(omit_empty=True, audio_rules={"max_seconds": 1,
                            "require_channel_for_multichannel": True})
        self.mapping["fields"]["ch"] = "channel"
        self.save_profile()
        self.assertIn("missing_channel", self.errors(self.run_cli("export")))
        self.rows[0]["channel"] = "1"
        self.rows[0]["intended_label"] = ""
        csv_file(self.project / "collection.csv", [*pipeline.PLAN_FIELDS, "channel"], self.rows)
        self.assertTrue(self.run_cli("export")["ok"])
        line = json.loads(self.out.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(line["ch"], 1)
        self.assertNotIn("pair", line)
        self.assertNotIn("intended", line)
        self.rows[0]["channel"] = "2"
        csv_file(self.project / "collection.csv", [*pipeline.PLAN_FIELDS, "channel"], self.rows)
        self.mapping["audio_rules"]["max_seconds"] = .005
        self.save_profile()
        self.assertTrue({"invalid_channel", "receiver_duration_limit"} <= self.errors(self.run_cli("export")))

    def test_reserved_gold_column_in_collection_is_never_used(self):
        self.all_audio()
        for r in self.rows:
            r["human_label"] = "平静"
        csv_file(self.project / "collection.csv", [*pipeline.PLAN_FIELDS, "human_label"], self.rows)
        self.judgments[3]["human_label"] = ""
        self.save_final()
        self.assertIn("invalid_gold", self.errors(self.run_cli("export")))
        self.assertFalse(self.out.exists())

    def test_existing_unpaired_final_requires_no_fabricated_preset(self):
        self.all_audio()
        csv_file(self.project / "collection.csv", ["sample_id", "audio"],
                 [{"sample_id": "C", "audio": "audio/C.wav"}])
        self.judgments = [r for r in self.judgments if r["sample_id"] == "C"]
        self.save_final()
        del self.mapping["fields"]["intended"]
        del self.mapping["fields"]["pair"]
        self.save_profile()
        self.assertTrue(self.run_cli("export")["ok"])


if __name__ == "__main__":
    unittest.main()
