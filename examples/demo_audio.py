"""Prepare the bundled Mandarin demonstration locally with an installed voice."""
import csv
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from review.store import verified_audio


def run_command(args, *, text=None):
    result = subprocess.run(args, input=text, encoding="utf-8", errors="replace",
                            capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or "语音引擎运行失败")
    return result.stdout


def write_with_espeak(executable, rows, stage):
    listing = run_command([executable, "--voices"])
    languages = {cells[1].lower() for line in listing.splitlines()
                 if len(cells := line.split()) >= 2 and cells[0].isdigit()}
    language = next((v for v in ("cmn", "zh", "zh-cn") if v in languages), None)
    if not language:
        raise ValueError("eSpeak NG 没有安装普通话语音数据")
    for row in rows:
        wav = stage / row["audio"]
        wav.parent.mkdir(parents=True, exist_ok=True)
        pitch = 44 if row.get("speaker_id") == "SPK-B" else 54
        speed = 160
        if row.get("intended_label") == "开心":
            speed, pitch = 172, pitch + 5
        elif row.get("intended_label") == "惊讶":
            speed, pitch = 150, pitch + 12
        run_command([executable, "-v", language, "-s", str(speed), "-p", str(pitch),
                     "-b", "1", "-w", str(wav), "--stdin"], text=row["text"] + "\n")


def stage_audio(rows, stage, kit_root):
    errors = []
    powershell = shutil.which("powershell.exe") if os.name == "nt" else None
    if powershell:
        destination = stage / "system-speech"
        destination.mkdir()
        with (destination / "collection.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        try:
            run_command([powershell, "-NoProfile", "-NonInteractive", "-File",
                         str(kit_root / "examples" / "make_demo_audio.ps1"),
                         "-Project", str(destination)])
            return destination
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            errors.append(str(exc))
    executable = shutil.which("espeak-ng")
    if executable:
        destination = stage / "espeak"
        destination.mkdir()
        try:
            write_with_espeak(executable, rows, destination)
            return destination
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            errors.append(str(exc))
    detail = "\n".join(errors)
    raise ValueError("需要 Windows 已安装的 System.Speech 普通话声音，或带普通话声音的 eSpeak NG。"
                     + ("\n" + detail if detail else ""))


def ensure_demo_audio(project, kit_root):
    """Complete a demo in its work directory without replacing existing recordings."""
    project = project.resolve()
    with (project / "collection.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows or not {"audio", "text"} <= rows[0].keys():
        raise ValueError("演示清单缺少录制文本或音频路径")
    missing = []
    for row in rows:
        relative = Path(row["audio"])
        target = (project / relative).resolve()
        if (relative.is_absolute() or relative.drive or ".." in relative.parts or
                project not in target.parents or target.suffix.lower() != ".wav"):
            raise ValueError(f"演示清单包含无效音频路径：{relative}")
        if target.exists():
            if problem := verified_audio(target):
                raise ValueError(f"{relative} 的现有音频需要手动检查：{problem}")
        else:
            missing.append(row)
    if not missing:
        return
    missing_names = ", ".join(row["audio"] for row in missing)
    if (project / ".review" / "review.sqlite3").is_file():
        raise ValueError(f"该项目已有听审数据库，请在 {project} 恢复原始音频：{missing_names}")
    # Keep interrupted synthesis outside the listening project and on the same volume.
    with tempfile.TemporaryDirectory(dir=project.parent) as tmp:
        try:
            generated = stage_audio(missing, Path(tmp), kit_root)
        except ValueError as exc:
            raise ValueError(f"{exc}\n也可自行将对应的 WAV 放到 {project}：{missing_names}") from exc
        # Validate the complete generated batch before exposing any new file.
        for row in missing:
            if problem := verified_audio(generated / row["audio"]):
                raise ValueError(f"{row['audio']}：本机生成的 WAV 未通过检查，{problem}")
        for row in missing:
            source = generated / row["audio"]
            target = project / row["audio"]
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                # Both operations fail if another process just supplied this file.
                if os.name == "nt":
                    source.rename(target)
                else:
                    os.link(source, target)
            except FileExistsError:
                if problem := verified_audio(target):
                    raise ValueError(f"{target.name}：请检查同时写入的 WAV，{problem}")
