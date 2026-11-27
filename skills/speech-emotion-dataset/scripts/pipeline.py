"""Check a speech-emotion collection and export verified human labels. Python 3.10+."""

import argparse
import collections
import csv
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import tempfile
import unicodedata
import wave


PLAN_FIELDS = ("sample_id", "scene", "group", "speaker_id", "text", "intended_label",
               "cue", "pair_id", "audio")
FINAL_FIELDS = ("sample_id", "human_label", "version", "quality")
QUOTED = re.compile(r'“([^”\n]+)”|「([^」\n]+)」|‘([^’\n]+)’|"([^"\n]+)"')


def read_json(path):
    def unique_keys(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError(f"{path.name}: duplicate JSON key {key!r}")
            obj[key] = value
        return obj
    with path.open(encoding="utf-8-sig") as f:
        obj = json.load(f, object_pairs_hook=unique_keys)
    if not isinstance(obj, dict):
        raise ValueError(f"{path.name}: expected a JSON object")
    return obj


def read_csv(path, required):
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, strict=True)
        fields = reader.fieldnames or []
        if not all(fields) or len(fields) != len(set(fields)):
            raise ValueError(f"{path.name}: empty or duplicate column names")
        missing = set(required) - set(fields)
        if missing:
            raise ValueError(f"{path.name}: missing columns {', '.join(sorted(missing))}")
        rows = []
        for row in reader:
            if None in row or None in row.values():
                raise ValueError(f"{path.name}: invalid cell count at line {reader.line_num}")
            rows.append(row)
    if not rows:
        raise ValueError(f"{path.name}: no data rows")
    return fields, rows


def normalized_text(text):
    clean = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in clean if not c.isspace()
                   and not unicodedata.category(c).startswith("P"))


def labels_for(config):
    labels = config.get("labels")
    if (not isinstance(labels, list) or not labels or
            any(not isinstance(v, str) or not v or v != v.strip() for v in labels)
            or len(set(labels)) != len(labels)):
        raise ValueError("config.labels must be a nonempty list of distinct label strings")
    return set(labels)


def strings(config, field):
    value = config.get(field, [])
    if not isinstance(value, list) or any(not isinstance(s, str) or not s for s in value):
        raise ValueError(f"{field}: expected a list of nonempty strings")
    return value


def flag(config, name, default):
    value = config.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name}: expected a boolean")
    return value


def issue(report, code, detail, ids=()):
    report["errors"].append({"code": code, "sample_ids": list(ids), "detail": detail})


def local_wav(root, audio):
    if not audio.strip() or "\\" in audio or PureWindowsPath(audio).drive:
        raise ValueError("use a project-relative WAV path with forward slashes")
    path = Path(audio)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".wav":
        raise ValueError("expected a WAV below the project root")
    full = (root / path).resolve()
    if not full.is_relative_to(root):
        raise ValueError("audio path resolves outside the project")
    return full


def inventory(root, rows, report):
    seen_ids, paths = set(), {}
    resolved = {}
    for row in rows:
        sid = row["sample_id"]
        if not sid or sid != sid.strip():
            issue(report, "invalid_id", "empty or padded sample ID", [sid])
        if sid in seen_ids:
            issue(report, "duplicate_id", "sample ID used more than once", [sid])
        seen_ids.add(sid)
        try:
            path = local_wav(root, row["audio"])
        except (ValueError, OSError, RuntimeError) as exc:
            issue(report, "invalid_audio_path", str(exc), [sid])
            continue
        if path in paths:
            issue(report, "shared_audio_path", row["audio"], [paths[path], sid])
        paths[path] = sid
        resolved[sid] = path
    return resolved


def check_quota(report, name, actual, wanted):
    if not isinstance(wanted, dict):
        raise ValueError(f"{name}: expected a count object")
    for key, target in wanted.items():
        if type(target) is not int or target < 0:
            raise ValueError(f"{name}.{key}: expected a nonnegative integer")
        if actual[key] != target:
            issue(report, "quota_mismatch", f"{name}.{key}: expected {target}, got {actual[key]}")


def pair_sets(rows):
    result = collections.defaultdict(list)
    for row in rows:
        if row.get("pair_id"):
            result[row["pair_id"]].append(row)
    return result


def check_pairs(report, pairs, planning):
    for pid, pair in pairs.items():
        ids = [r["sample_id"] for r in pair]
        if pid != pid.strip() or len(pair) != 2:
            issue(report, "pair_size_or_id", f"{pid!r}: expected exactly two members", ids)
            continue
        first, second = pair
        for key in ("speaker_id", "text"):
            if not first.get(key, "").strip() or first.get(key) != second.get(key):
                issue(report, f"pair_{key}", f"{pid}: must be present and exactly equal", ids)
        if first.get("group") != second.get("group"):
            issue(report, "pair_group", f"{pid}: inconsistent group", ids)
        a, b = first.get("intended_label", ""), second.get("intended_label", "")
        if (planning or a or b) and (not a or not b or a == b):
            issue(report, "pair_preset", f"{pid}: expected two distinct presets", ids)


def check_design(config, rows, report):
    labels = labels_for(config)
    pairs = pair_sets(rows)
    check_pairs(report, pairs, True)
    targets, forms = collections.defaultdict(collections.Counter), collections.defaultdict(list)
    groups, scenes = collections.Counter(), collections.Counter()
    pair_groups = set(strings(config, "pair_groups"))
    new_groups = set(strings(config, "new_recording_groups"))
    existing_groups = set(strings(config, "existing_recording_groups"))
    if new_groups & existing_groups:
        raise ValueError("new_recording_groups and existing_recording_groups overlap")
    forbid = flag(config, "forbid_label_words", False)
    check_quotes = flag(config, "check_cue_quotes", True)
    forbidden = labels | set(strings(config, "label_aliases"))
    for row in rows:
        sid = row["sample_id"]
        for col in ("group", "scene", "text"):
            if not row[col].strip():
                issue(report, "missing_value", col, [sid])
        text = row["text"]
        groups[row["group"]] += 1
        scenes[row["scene"]] += 1
        if row["intended_label"]:
            targets[row["group"]][row["intended_label"]] += 1
            if row["intended_label"] not in labels:
                issue(report, "invalid_preset", row["intended_label"], [sid])
        if row["group"] in new_groups and not row["intended_label"]:
            issue(report, "missing_preset", row["group"], [sid])
        if row["group"] in existing_groups:
            if row["intended_label"]:
                issue(report, "unexpected_preset", "existing recordings use actual transcriptions", [sid])
            if not row.get("source_id", "").strip():
                issue(report, "source_unconfirmed", "expected an existing audio source ID", [sid])
        if row["group"] in pair_groups and not row["pair_id"]:
            issue(report, "missing_pair_id", row["group"], [sid])
        if pair_groups and row["group"] not in pair_groups and row["pair_id"]:
            issue(report, "unexpected_pair_id", row["group"], [sid])
        if forbid:
            matches = sorted(w for w in forbidden if w.casefold() in text.casefold())
            if matches:
                issue(report, "label_in_text", ", ".join(matches), [sid])
        if check_quotes:
            for match in QUOTED.finditer(row["cue"]):
                word = next(w for w in match.groups() if w is not None)
                if word not in text:
                    issue(report, "cue_word_missing", word, [sid])
        key = normalized_text(text)
        if not key:
            issue(report, "empty_text", "text contains no spoken content", [sid])
        forms[key].append(row)
    for same in forms.values():
        if len(same) > 1 and not (len(same) == 2 and same[0]["pair_id"]
                                  and same[0]["pair_id"] == same[1]["pair_id"]):
            issue(report, "unexpected_text_duplicate", "same normalized text",
                  [r["sample_id"] for r in same])
    for name, counts in (("group_quotas", groups), ("scene_quotas", scenes)):
        check_quota(report, name, counts, config.get(name, {}))
    quotas = config.get("label_quotas", {})
    if not isinstance(quotas, dict):
        raise ValueError("label_quotas: expected group -> label -> count")
    for group, by_label in quotas.items():
        check_quota(report, f"label_quotas.{group}", targets[group], by_label)
    if "total_quota" in config:
        check_quota(report, "total_quota", {"all": len(rows)}, {"all": config["total_quota"]})
    if "pair_combinations" in config:
        requested = config["pair_combinations"]
        if (not isinstance(requested, list) or any(not isinstance(pair, list) or len(pair) != 2
                or any(not isinstance(lab, str) or lab not in labels for lab in pair)
                or pair[0] == pair[1] for pair in requested)):
            raise ValueError("pair_combinations: expected a list of two-label combinations")
        observed = collections.Counter(tuple(sorted(r["intended_label"] for r in pair))
                                       for pair in pairs.values())
        expected = collections.Counter(tuple(sorted(pair)) for pair in requested)
        if observed != expected:
            issue(report, "pair_combinations", f"missing: {list((expected-observed).elements())}; "
                  f"extra: {list((observed-expected).elements())}")
    report.update(unique_texts=len(forms), pairs=len(pairs), group_counts=dict(groups),
                  scene_counts=dict(scenes), intended_counts={k: dict(v) for k, v in targets.items()})


def wav_info(path):
    with wave.open(str(path), "rb") as w:
        channels, width, rate, frames = (w.getnchannels(), w.getsampwidth(),
                                         w.getframerate(), w.getnframes())
        if w.getcomptype() != "NONE" or not (channels and width and rate and frames):
            raise ValueError("expected nonempty uncompressed PCM")
        frame_bytes = channels * width
        remaining = frames
        signal = False
        silent_byte = 128 if width == 1 else 0
        while remaining:
            n = min(remaining, 16384)
            block = w.readframes(n)
            if len(block) != n * frame_bytes:
                raise ValueError("truncated PCM payload")
            remaining -= n
            signal = signal or block.count(silent_byte) != len(block)
        if w.readframes(1):
            raise ValueError("partial trailing PCM frame")
        if not signal:
            raise ValueError("all PCM samples are silent")
        return {"channels": channels, "sample_width_bytes": width,
                "sample_rate_hz": rate, "frames": frames}


def check_audio(root, resolved, report):
    files = {}
    for sid, path in resolved.items():
        if not path.is_file():
            issue(report, "missing_audio", path.relative_to(root).as_posix(), [sid])
            continue
        try:
            files[sid] = wav_info(path)
        except (OSError, ValueError, EOFError, wave.Error) as exc:
            issue(report, "invalid_wav", str(exc) or "incomplete WAV header", [sid])
    registered = set(resolved.values())
    extras = []
    for directory, _, names in os.walk(root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if path.suffix.lower() == ".wav" and path.resolve() not in registered:
                extras.append(path.relative_to(root).as_posix())
    report.update(readable_audio=len(files), audio_info=files, unlisted_audio=sorted(extras))


def profile_rules(profile, available):
    fields = profile.get("fields")
    if not isinstance(fields, dict) or not fields or any(
            not key or not isinstance(value, str) or value not in available
            for key, value in fields.items()):
        raise ValueError("profile.fields must map target names to available source column names")
    required = strings(profile, "required")
    if not set(required) <= fields.keys():
        raise ValueError("profile.required must contain target names from profile.fields")
    for source in ("audio", "human_label"):
        if source not in fields.values():
            raise ValueError(f"profile.fields must include a target for {source}")
    mapping = profile.get("label_map")
    if mapping is not None:
        if (not isinstance(mapping, dict) or any(not isinstance(v, str) or not v for v in mapping.values())
                or len(set(mapping.values())) != len(mapping)):
            raise ValueError("label_map must map local labels to distinct nonempty target labels")
    if profile.get("audio_base", "project") not in ("project", "manifest"):
        raise ValueError("audio_base must be project or manifest")
    return fields, set(required), mapping


def receiver_audio_rules(profile):
    rules = profile.get("audio_rules", {})
    if not isinstance(rules, dict) or rules.keys() - {"max_seconds", "require_channel_for_multichannel"}:
        raise ValueError("audio_rules supports max_seconds and require_channel_for_multichannel")
    limit = rules.get("max_seconds")
    if limit is not None and (type(limit) not in (float, int) or not math.isfinite(limit) or limit <= 0):
        raise ValueError("audio_rules.max_seconds must be a positive finite number")
    return limit, flag(rules, "require_channel_for_multichannel", False)


def deliver(args, config, root, plan_fields, rows, resolved, report):
    finals_fields, judgments = read_csv(args.final, FINAL_FIELDS)
    profile = read_json(args.profile)
    labels = labels_for(config)
    fields, required, mapping = profile_rules(profile, set(plan_fields) | set(finals_fields) | {"channel"})
    limit, require_channel = receiver_audio_rules(profile)
    omit_empty = flag(profile, "omit_empty", False)
    pairs = pair_sets(rows)
    check_pairs(report, pairs, False)
    by_id = {}
    for judgment in judgments:
        sid = judgment["sample_id"]
        if sid in by_id:
            issue(report, "duplicate_final", "duplicate final ID", [sid])
        by_id[sid] = judgment
        if sid not in resolved:
            issue(report, "unknown_final", "no matching valid audio path in collection", [sid])
        if judgment["human_label"] not in labels:
            issue(report, "invalid_gold", judgment["human_label"], [sid])
        if not judgment["version"].strip() or judgment["quality"] != "pass":
            issue(report, "unfinished_or_bad_quality", "version and quality=pass required", [sid])
    gold_counts = collections.defaultdict(collections.Counter)
    for row in rows:
        gold_label = by_id.get(row["sample_id"], {}).get("human_label")
        if gold_label in labels:
            gold_counts[row.get("group", "")][gold_label] += 1
    report["gold_counts"] = dict(gold_counts)
    gold_quotas = config.get("gold_quotas", {})
    if not isinstance(gold_quotas, dict):
        raise ValueError("gold_quotas: expected an object keyed by group")
    for group, quotas in gold_quotas.items():
        check_quota(report, f"gold_quotas.{group}", gold_counts[group], quotas)
    match_targets = flag(config, "require_pair_target_labels", False)
    for pid, pair in pairs.items():
        golds = [by_id.get(r["sample_id"], {}).get("human_label") for r in pair]
        if len(golds) == 2 and golds[0] and golds[0] == golds[1]:
            issue(report, "same_pair_gold", f"{pid}: same final label on both sides",
                  [r["sample_id"] for r in pair])
        if match_targets and len(golds) == 2 and all(golds):
            targets = {r.get("intended_label") for r in pair}
            if set(golds) != targets:
                issue(report, "pair_gold_targets", f"{pid}: confirmed labels do not cover the planned combination",
                      [r["sample_id"] for r in pair])
    exported = []
    for row in rows:
        sid = row["sample_id"]
        if sid not in by_id:
            issue(report, "missing_final", "no finalized label for this sample", [sid])
            continue
        # The collection is authoritative for recording metadata. Only review fields
        # may come from a richer final CSV that repeats collection columns.
        source = {**by_id[sid], **row}
        for key in (*FINAL_FIELDS, "initial_disagreement"):
            if key in by_id[sid]:
                source[key] = by_id[sid][key]
        if profile.get("audio_base") == "manifest" and sid in resolved:
            source["audio"] = Path(os.path.relpath(resolved[sid], args.out.resolve().parent)).as_posix()
        info = report["audio_info"].get(sid)
        source["channel"] = row.get("channel", "")
        if info:
            if limit is not None and info["frames"] > limit * info["sample_rate_hz"]:
                issue(report, "receiver_duration_limit", f"receiver maximum {limit} seconds", [sid])
            if source["channel"] and ("channel" in fields.values() or require_channel):
                number = source["channel"]
                if not re.fullmatch(r"0|[1-9][0-9]*", number) or int(number) >= info["channels"]:
                    issue(report, "invalid_channel", "channel must be a valid zero-based index", [sid])
                else:
                    source["channel"] = int(number)
            if info["channels"] > 1 and require_channel and (
                    source["channel"] == "" or "channel" not in fields.values()):
                issue(report, "missing_channel", "receiver needs a mapped speech channel", [sid])
        output = {}
        for target, key in fields.items():
            value = source[key]
            if key in ("intended_label", "human_label") and value and mapping is not None:
                if value not in mapping:
                    issue(report, "missing_label_mapping", value, [sid])
                else:
                    value = mapping[value]
            empty = isinstance(value, str) and not value.strip()
            if target in required and empty:
                issue(report, "missing_target_field", target, [sid])
            if empty and omit_empty:
                continue
            output[target] = value
        exported.append(output)
    if report["errors"]:
        return
    destination = args.out.resolve()
    inputs = {args.final.resolve(), args.profile.resolve(), root / "collection.csv",
              root / "config.json", *resolved.values()}
    if destination in inputs or destination.suffix.lower() != ".jsonl":
        raise ValueError("output must be a separate .jsonl file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", delete=False,
                                         dir=destination.parent) as f:
            temporary = Path(f.name)
            for obj in exported:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        with temporary.open(encoding="utf-8") as f:
            if [json.loads(line) for line in f] != exported:
                raise ValueError("JSONL read-back mismatch")
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        os.replace(temporary, destination)
        report.update(exported=len(exported), verified_jsonl=True, sha256=digest)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "audio", "export"):
        command = sub.add_parser(name)
        command.add_argument("--project", required=True, type=Path)
        if name == "export":
            for param in ("final", "profile", "out"):
                command.add_argument("--" + param, required=True, type=Path)
    args = parser.parse_args(argv)
    report = {"command": args.command, "errors": []}
    try:
        root = args.project.resolve()
        config = read_json(root / "config.json")
        labels_for(config)
        fields, rows = read_csv(root / "collection.csv", PLAN_FIELDS if args.command == "check"
                                else ("sample_id", "audio"))
        report["records"] = len(rows)
        resolved = inventory(root, rows, report)
        if args.command == "check":
            check_design(config, rows, report)
        else:
            check_audio(root, resolved, report)
        if args.command == "export":
            deliver(args, config, root, fields, rows, resolved, report)
    except (OSError, csv.Error, ValueError, RuntimeError) as exc:
        issue(report, "input_error", str(exc))
    report["ok"] = not report["errors"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
