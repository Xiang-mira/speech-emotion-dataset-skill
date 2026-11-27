"""Start a private local acceptance copy of the bundled listening project."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from examples.demo_audio import ensure_demo_audio


ROOT = Path(__file__).resolve().parent


def demo_project(mode):
    """Create a fresh working copy once. Restarting preserves actual votes."""
    import json
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    target = work / ("demo" if mode == "joint" else "demo-third")
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            copy = Path(temporary) / "demo"
            shutil.copytree(ROOT / "examples" / "demo", copy, ignore=shutil.ignore_patterns(".review"))
            config = copy / "config.json"
            settings = json.loads(config.read_text(encoding="utf-8"))
            settings["review_mode"] = mode
            config.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                copy.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
    if not (target / "collection.csv").is_file() or not (target / "config.json").is_file():
        raise ValueError(f"演示工作目录缺少清单或配置，请检查 {target}")
    ensure_demo_audio(target, ROOT)
    return target


def main():
    parser = argparse.ArgumentParser(description="语音情绪评测集 · 本地验收")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="打开有六段合成音频的本地演示")
    demo.add_argument("--mode", choices=("joint", "third"), default="joint")
    review = commands.add_parser("review", help="打开自己的项目目录")
    review.add_argument("--project", required=True, type=Path)
    for command in (demo, review):
        command.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        project = demo_project(args.mode) if args.command == "demo" else args.project.resolve()
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    return subprocess.call(
        [sys.executable, "-B", str(ROOT / "review" / "server.py"), "--project", str(project), "--port", str(args.port)],
        env={**os.environ, "PYTHONUTF8": "1"},
    )


if __name__ == "__main__":
    raise SystemExit(main())
