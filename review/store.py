"""Project-scoped, persistent human judgements. Uses only the Python standard library."""

import csv
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import wave
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

REVIEWERS = ("r1", "r2")
VOTE_FIELDS = ("label", "quality", "note")


class ReviewError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def verified_audio(path):
    """Read every PCM frame; wave header parsing by itself misses truncated WAVs."""
    try:
        with wave.open(str(path), "rb") as audio:
            expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
            if not expected:
                return "WAV 没有音频帧"
            received = 0
            while received < expected:
                chunk = audio.readframes(32768)
                if not chunk:
                    return "WAV 在最后一个音频帧前中断"
                received += len(chunk)
            if received != expected:
                return "WAV 帧长度不一致"
    except (EOFError, OSError, wave.Error) as exc:
        return f"文件缺失或无法解码为 PCM WAV（{type(exc).__name__}）"
    return ""


def audio_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ReviewStore:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.config = json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))
        if not isinstance(self.config, dict):
            raise ReviewError("config.json 的根节点应为对象")
        labels = self.config.get("labels")
        if (
            not isinstance(labels, list)
            or not labels
            or not all(isinstance(v, str) and v.strip() == v and v for v in labels)
            or len(set(labels)) != len(labels)
        ):
            raise ReviewError("config.json 的 labels 须为互异的非空标签字符串列表")
        self.labels = labels
        self.mode = self.config.get("review_mode", "joint")
        self.show_text = self.config.get("show_text", False)
        self.checkpoint = self.config.get("checkpoint", 10)
        if (
            self.mode not in ("joint", "third")
            or type(self.show_text) is not bool
            or type(self.checkpoint) is not int
            or self.checkpoint <= 0
        ):
            raise ReviewError("请核对 review_mode、show_text 与 checkpoint")
        self.rows = {}
        self.paths = {}
        self.errors = {}
        self.digests = {}
        self.stats = {}
        used_paths = {}
        required_columns = {"sample_id", "audio"}
        if self.show_text:
            required_columns.add("text")
        with (self.root / "collection.csv").open(encoding="utf-8-sig", newline="") as source:
            records = csv.DictReader(source)
            if records.fieldnames and len(set(records.fieldnames)) != len(records.fieldnames):
                raise ReviewError("collection.csv 有重名字段，请先核对表头")
            if not required_columns.issubset(records.fieldnames or []):
                raise ReviewError(
                    "collection.csv 缺少 "
                    + ", ".join(sorted(required_columns - set(records.fieldnames or [])))
                )
            for row in records:
                if None in row or any(v is None for v in row.values()):
                    raise ReviewError(f"collection.csv 第 {records.line_num} 行列数有误")
                sample_id = row["sample_id"]
                if (
                    not sample_id
                    or sample_id.strip() != sample_id
                    or any(ord(c) < 32 for c in sample_id)
                    or sample_id in self.rows
                ):
                    raise ReviewError(f"第 {records.line_num} 行样本编号为空、无效或重复")
                self.rows[sample_id] = row
                relative = Path(row["audio"])
                candidate = (self.root / relative).resolve()
                if (
                    not row["audio"]
                    or relative.is_absolute()
                    or re.match(r"^(?:[A-Za-z]:|[/\\])", row["audio"])
                    or self.root not in candidate.parents
                    or candidate.suffix.lower() != ".wav"
                ):
                    self.errors[sample_id] = "音频路径须为项目目录内的相对 WAV 路径"
                    continue
                self.paths[sample_id] = candidate
                if candidate in used_paths:
                    error = "不同样本共用了同一 WAV 路径，请核对编号映射"
                    self.errors[used_paths[candidate]] = error
                    self.errors[sample_id] = error
                    continue
                used_paths[candidate] = sample_id
                self.errors[sample_id] = verified_audio(candidate)
                if not self.errors[sample_id]:
                    self.digests[sample_id] = audio_digest(candidate)
                    self.stats[sample_id] = self.signature(candidate)
        if not self.rows:
            raise ReviewError("采录清单为空")
        self.directory = self.root / ".review"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.dbpath = self.directory / "review.sqlite3"
        self.initialize()

    @staticmethod
    def signature(path):
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def audio_error(self, sample_id):
        original = self.errors[sample_id]
        if original:
            return original
        try:
            path = self.paths[sample_id]
            if path.resolve() != path:
                return "音频路径已改变，请在新项目版本中重新核对"
            current = self.signature(path)
            if current != self.stats[sample_id]:
                if audio_digest(path) != self.digests[sample_id]:
                    return "音频内容已改变，请在新项目版本中重新核对"
                self.stats[sample_id] = current
        except OSError:
            return "音频文件无法打开"
        return ""

    @contextmanager
    def database(self, write=False):
        db = sqlite3.connect(self.dbpath, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        with self.database(write=True) as db:
            statements = (
                "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS roles(role TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, finished INTEGER DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS votes(sid TEXT, role TEXT, label TEXT, quality TEXT, note TEXT, PRIMARY KEY(sid,role))",
                "CREATE TABLE IF NOT EXISTS decisions(sid TEXT PRIMARY KEY, label TEXT, quality TEXT, reason TEXT, version INTEGER, status TEXT, confirmations TEXT)",
                "CREATE TABLE IF NOT EXISTS events(event_id INTEGER PRIMARY KEY, time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, role TEXT, sid TEXT, action TEXT, detail TEXT)",
            )
            for sql in statements:
                db.execute(sql)
            settings = {"labels": self.labels, "mode": self.mode, "show_text": self.show_text}
            manifest = hashlib.sha256(encode([settings, self.rows]).encode()).hexdigest()
            previous = db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
            has_votes = db.execute("SELECT 1 FROM votes LIMIT 1").fetchone()
            if has_votes and previous and previous[0] != manifest:
                raise ReviewError(
                    "清单或听审配置与已有原判不一致。请保留旧项目文件夹，在新项目目录建立新一轮听审"
                )
            prev_hashes = db.execute("SELECT value FROM meta WHERE key='audio_hashes'").fetchone()
            old = json.loads(prev_hashes[0]) if prev_hashes else {}
            voted_ids = [r[0] for r in db.execute("SELECT DISTINCT sid FROM votes")]
            for sid in voted_ids:
                if old.get(sid) != self.digests.get(sid):
                    raise ReviewError(f"{sid} 的录音与初审时的文件不符。请恢复原文件或新建项目版本")
            for k, v in [("manifest", manifest), ("audio_hashes", encode(self.digests))]:
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
            for role in (*REVIEWERS, *(("adjudicator",) if self.mode == "third" else ())):
                db.execute(
                    "INSERT OR IGNORE INTO roles(role,token) VALUES (?,?)",
                    (role, secrets.token_urlsafe(32)),
                )

    def credentials(self):
        with self.database() as db:
            return {
                r[0]: r[1]
                for r in db.execute("SELECT role,token FROM roles")
                if r[0] in REVIEWERS or self.mode == "third"
            }

    def authenticate(self, token):
        if not token or not token.isascii():
            return None
        return next(
            (r for r, k in self.credentials().items() if hmac.compare_digest(k, token)), None
        )

    def sample(self, payload):
        sid = payload.get("sample_id")
        if not isinstance(sid, str) or sid not in self.rows:
            raise ReviewError("样本编号不存在", 404)
        return sid

    @staticmethod
    def event(db, role, sid, action, detail):
        db.execute(
            "INSERT INTO events(role,sid,action,detail) VALUES (?,?,?,?)",
            (role, sid, action, encode(detail)),
        )

    @staticmethod
    def finished(db):
        return {
            r[0]: bool(r[1])
            for r in db.execute("SELECT role,finished FROM roles WHERE role IN ('r1','r2')")
        }

    def validate_vote(self, sid, payload, note_field="note"):
        label = payload.get("label", "")
        quality = payload.get("quality", "pass")
        note = payload.get(note_field, "")
        if (
            not isinstance(note, str)
            or len(note) > 8192
            or not isinstance(label, str)
            or (label and label not in self.labels)
        ):
            raise ReviewError("请核对标签取值及备注长度")
        note = note.strip()
        if quality not in ("pass", "reject", "uncertain") or (
            quality == "pass" and label not in self.labels
        ):
            raise ReviewError("请提交有效的情绪标签或注明无法判断")
        if quality != "pass" and not note:
            raise ReviewError("请注明音质退回或无法判断的原因")
        error = self.audio_error(sid)
        if quality == "pass" and error:
            raise ReviewError(error)
        return label, quality, note

    def vote(self, role, payloads, offline=False):
        if role not in REVIEWERS:
            raise ReviewError("此角色不参与初审", 403)
        if not isinstance(payloads, list) or not payloads:
            raise ReviewError("需要至少一行判断")
        result = {"inserted": [], "skipped": [], "conflicts": []}
        with self.database(write=True) as db:
            locked = self.finished(db)[role]
            for payload in payloads:
                if not isinstance(payload, dict):
                    raise ReviewError("听审记录应为对象")
                if (payload.get("reviewer") or payload.get("role") or role) != role:
                    raise ReviewError("文件中的听审角色与当前身份不同", 403)
                sid = self.sample(payload)
                values = self.validate_vote(sid, payload)
                old = db.execute(
                    "SELECT label,quality,note FROM votes WHERE sid=? AND role=?", (sid, role)
                ).fetchone()
                if old:
                    if tuple(old) == values:
                        result["skipped"].append(sid)
                    elif offline:
                        result["conflicts"].append(sid)
                        self.event(
                            db, role, sid, "offline_conflict", dict(zip(VOTE_FIELDS, values))
                        )
                    else:
                        raise ReviewError("初审原判已提交，可在两人完成后复核", 409)
                else:
                    if locked:
                        raise ReviewError("已提交整轮初审", 409)
                    db.execute("INSERT INTO votes VALUES (?,?,?,?,?)", (sid, role, *values))
                    self.event(
                        db,
                        role,
                        sid,
                        "offline_vote" if offline else "vote",
                        dict(zip(VOTE_FIELDS, values)),
                    )
                    result["inserted"].append(sid)
        return result

    def finish(self, role):
        if role not in REVIEWERS:
            raise ReviewError("只有初审人可以提交整轮", 403)
        with self.database(write=True) as db:
            if self.finished(db)[role]:
                return
            count = db.execute("SELECT COUNT(*) FROM votes WHERE role=?", (role,)).fetchone()[0]
            if count != len(self.rows):
                raise ReviewError(f"还有 {len(self.rows) - count} 条未提交", 409)
            db.execute("UPDATE roles SET finished=1 WHERE role=?", (role,))
            self.event(db, role, "", "finish", {"count": count})
            if all(self.finished(db).values()):
                matches = db.execute(
                    "SELECT a.sid,a.label FROM votes a JOIN votes b ON a.sid=b.sid WHERE a.role='r1' AND b.role='r2' AND a.quality='pass' AND b.quality='pass' AND a.label=b.label"
                )
                for sid, label in matches:
                    if self.audio_error(sid):
                        continue
                    db.execute(
                        "INSERT INTO decisions VALUES (?,?,?,'双人独立初审一致',1,'final',?)",
                        (sid, label, "pass", encode(list(REVIEWERS))),
                    )
                    self.event(db, "system", sid, "agreement", {"label": label, "version": 1})

    @staticmethod
    def decision(record):
        if not record:
            return {
                "label": "",
                "quality": "",
                "reason": "",
                "version": 0,
                "status": "pending",
                "confirmations": [],
            }
        return {
            **{k: record[k] for k in ("label", "quality", "reason", "version", "status")},
            "confirmations": json.loads(record["confirmations"]),
        }

    def resolve(self, role, payload, confirm=False):
        sid = self.sample(payload)
        if (self.mode == "third" and role != "adjudicator") or (
            self.mode == "joint" and role not in REVIEWERS
        ):
            raise ReviewError("当前复核模式不允许此角色裁定", 403)
        with self.database(write=True) as db:
            if not all(self.finished(db).values()):
                raise ReviewError("两人均提交整轮后方可复核", 409)
            old = self.decision(
                db.execute("SELECT * FROM decisions WHERE sid=?", (sid,)).fetchone()
            )
            if type(payload.get("version")) is not int or payload["version"] != old["version"]:
                raise ReviewError("复核版本已更新，请刷新", 409)
            if confirm:
                if self.mode != "joint" or old["status"] not in ("proposed", "final"):
                    raise ReviewError("没有可共同确认的提案", 409)
                if role in old["confirmations"]:
                    return
                confirmations = old["confirmations"] + [role]
                if old["quality"] == "pass" and self.audio_error(sid):
                    raise ReviewError("当前音频无法核实")
                db.execute(
                    "UPDATE decisions SET confirmations=?,status='final' WHERE sid=?",
                    (encode(confirmations), sid),
                )
                self.event(db, role, sid, "confirm", {"version": old["version"]})
                return
            label, quality, reason = self.validate_vote(sid, payload, "reason")
            if not reason or quality == "uncertain":
                raise ReviewError("请填写复听依据并选定最终标签或退回音频")
            if quality == "reject":
                label = ""
            version = old["version"] + 1
            status = "final" if self.mode == "third" else "proposed"
            db.execute(
                "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?)",
                (sid, label, quality, reason, version, status, encode([role])),
            )
            self.event(
                db,
                role,
                sid,
                "adjudicate" if self.mode == "third" else "propose",
                {"label": label, "quality": quality, "reason": reason, "version": version},
            )

    def state(self, role):
        with self.database() as db:
            finished = self.finished(db)
            visible = all(finished.values())
            votes = {}
            query = "SELECT * FROM votes" if visible else "SELECT * FROM votes WHERE role=?"
            for v in db.execute(query, () if visible else (role,)):
                votes.setdefault(v["sid"], {})[v["role"]] = {k: v[k] for k in VOTE_FIELDS}
            decisions = (
                {r["sid"]: self.decision(r) for r in db.execute("SELECT * FROM decisions")}
                if visible
                else {}
            )
            key = db.execute("SELECT token FROM roles WHERE role=?", (role,)).fetchone()[0]
        ordered = sorted(self.rows, key=lambda s: hmac.digest(key.encode(), s.encode(), "sha256"))
        totals = Counter(total=len(ordered), voted=0, finalized=0, disagreements=0)
        group_totals = {}
        samples = []
        for sid in ordered:
            pair = votes.get(sid, {})
            own = pair.get(role)
            error = self.audio_error(sid)
            sample = {
                "sample_id": sid,
                "audio_url": "/audio/" + quote(sid, safe=""),
                "audio_error": error,
                "own_vote": own,
            }
            if self.show_text:
                sample["text"] = self.rows[sid].get("text", "")
            if own:
                totals["voted"] += 1
            if visible:
                disagreement = pair["r1"]["label"] != pair["r2"]["label"]
                final = decisions.get(sid, self.decision(None))
                sample.update(votes=pair, initial_disagreement=disagreement, final=final)
                totals["disagreements"] += int(disagreement)
                has_gold = final["status"] == "final" and not error and final["quality"] == "pass"
                totals["finalized"] += int(has_gold)
                group = self.rows[sid].get("group", "")
                group_count = group_totals.setdefault(
                    group, Counter(total=0, both_valid=0, agree_valid=0, finalized=0)
                )
                group_count["total"] += 1
                if all(v["quality"] == "pass" for v in pair.values()):
                    group_count["both_valid"] += 1
                    group_count["agree_valid"] += int(not disagreement)
                group_count["finalized"] += int(has_gold)
            samples.append(sample)
        return {
            "role": role,
            "labels": self.labels,
            "review_mode": self.mode,
            "show_text": self.show_text,
            "checkpoint": self.checkpoint,
            "finished": finished,
            "phase": "review" if visible else "independent",
            "counts": dict(totals),
            "samples": samples,
            "group_summary": group_totals,
        }

    def export(self, role, audit=False):
        state = self.state(role)
        if state["phase"] != "review":
            raise ReviewError("两人均完成初审后方可导出", 409)
        by_id = {s["sample_id"]: s for s in state["samples"]}
        if audit:
            with self.database() as db:
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT event_id,time,role,sid AS sample_id,action,detail FROM events ORDER BY event_id"
                    )
                ]
            return ["event_id", "time", "role", "sample_id", "action", "detail"], rows
        fields = ["sample_id", "human_label", "initial_disagreement", "version", "quality"]
        rows = []
        for sid in self.rows:
            s = by_id[sid]
            f = s["final"]
            is_final = f["status"] == "final" and not s["audio_error"]
            rows.append(
                dict(
                    sample_id=sid,
                    human_label=f["label"] if is_final and f["quality"] == "pass" else "",
                    initial_disagreement=str(s["initial_disagreement"]).lower(),
                    version=f["version"],
                    quality=f["quality"] if is_final else "pending",
                )
            )
        return fields, rows
