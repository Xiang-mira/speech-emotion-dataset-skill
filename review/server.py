"""Run the local review application, or apply a reviewer-specific offline CSV."""

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from store import ReviewError, ReviewStore


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Access URLs act as credentials. Do not log URLs, query strings or headers.
        pass

    @property
    def store(self):
        return self.server.store

    def respond(self, status, data=b"", mime="application/json; charset=utf-8", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def json(self, data, status=200):
        self.respond(status, json.dumps(data, ensure_ascii=False).encode())

    def role(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        else:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie["review_access"].value if "review_access" in cookie else ""
        role = self.store.authenticate(token)
        if not role:
            raise ReviewError("请打开启动终端提供的个人听审链接", 401)
        return role

    def audio(self, sid):
        if sid not in self.store.rows:
            raise ReviewError("找不到录音", 404)
        error = self.store.audio_error(sid)
        if error:
            raise ReviewError(error, 404)
        with self.store.paths[sid].open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            start, end = 0, size - 1
            header = self.headers.get("Range")
            if header is not None:
                match = re.fullmatch(r"bytes=([0-9]{0,18})-([0-9]{0,18})", header)
                if match and any(match.groups()):
                    a, b = match.groups()
                    if a:
                        start = int(a)
                        end = min(int(b), size - 1) if b else size - 1
                    else:
                        start = max(0, size - int(b))
                else:
                    start = size
                if start >= size or end < start:
                    self.respond(
                        416, mime="text/plain", extra=[("Content-Range", f"bytes */{size}")]
                    )
                    return
            self.send_response(206 if header else 200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Accept-Ranges", "bytes")
            if header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            remaining = end - start + 1
            while remaining:
                data = f.read(min(64 * 1024, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)

    def read(self):
        path = urlsplit(self.path)
        if path.path == "/" and "key" in parse_qs(path.query):
            key = parse_qs(path.query)["key"][0]
            if not self.store.authenticate(key):
                raise ReviewError("听审链接无效", 401)
            self.respond(
                303,
                extra=[
                    ("Set-Cookie", f"review_access={key}; Path=/; HttpOnly; SameSite=Strict"),
                    ("Location", "/"),
                ],
            )
            return
        role = self.role()
        if path.path == "/":
            self.respond(
                200, Path(__file__).with_name("index.html").read_bytes(), "text/html; charset=utf-8"
            )
        elif path.path == "/api/state":
            self.json(self.store.state(role))
        elif path.path.startswith("/audio/"):
            sid = unquote(path.path[len("/audio/") :])
            self.audio(sid)
        elif path.path in ("/final.csv", "/audit.csv"):
            fields, rows = self.store.export(role, path.path == "/audit.csv")
            s = io.StringIO(newline="")
            writer = csv.DictWriter(s, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            self.respond(
                200,
                s.getvalue().encode("utf-8-sig"),
                "text/csv; charset=utf-8",
                [("Content-Disposition", f'attachment; filename="{path.path[1:]}"')],
            )
        else:
            raise ReviewError("地址不存在", 404)

    def write(self):
        role = self.role()
        expected_role = self.headers.get("X-Review-Role")
        if expected_role and expected_role != role:
            raise ReviewError(
                "当前浏览器已切换听审身份。请使用独立浏览器会话重新打开本人的链接", 409
            )
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            raise ReviewError("仅接受同源页面提交", 403)
        if self.headers.get_content_type() != "application/json":
            raise ReviewError("请使用 JSON 请求", 415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ReviewError("请求长度无效")
        if not 0 < size <= 8 * 1024 * 1024:
            raise ReviewError("请求体为空或过大", 413)
        try:
            payload = json.loads(self.rfile.read(size))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ReviewError("JSON 格式无效")
        if not isinstance(payload, dict):
            raise ReviewError("请求应为 JSON 对象")
        path = urlsplit(self.path).path
        result = {"ok": True}
        if path == "/api/vote":
            result.update(self.store.vote(role, [payload]))
        elif path == "/api/offline":
            result.update(self.store.vote(role, payload.get("rows"), offline=True))
        elif path == "/api/finish":
            self.store.finish(role)
        elif path in ("/api/propose", "/api/confirm"):
            self.store.resolve(role, payload, confirm=path == "/api/confirm")
        else:
            raise ReviewError("地址不存在", 404)
        self.json(result)

    def dispatch(self, operation):
        try:
            operation()
        except ReviewError as exc:
            self.json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except (OSError, sqlite3.Error) as exc:
            print(f"Review request failed ({type(exc).__name__})", file=sys.stderr)
            self.json(
                {"error": "服务端读取或保存失败，请检查项目文件与启动终端"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_GET(self):
        self.dispatch(self.read)

    do_HEAD = do_GET

    def do_POST(self):
        self.dispatch(self.write)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Default: local machine only. Use a trusted network or an HTTPS reverse proxy for remote users.",
    )
    parser.add_argument(
        "--import-offline",
        type=Path,
        metavar="CSV",
        help="Columns: sample_id,label,quality,note; optional reviewer.",
    )
    parser.add_argument("--role", choices=["r1", "r2"], help="Reviewer that owns the offline CSV.")
    args = parser.parse_args()
    if bool(args.import_offline) != bool(args.role):
        parser.error("--import-offline and --role are used together")
    try:
        store = ReviewStore(args.project)
        if args.import_offline:
            with args.import_offline.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if not {"sample_id", "label"}.issubset(reader.fieldnames or []):
                    raise ReviewError("离线 CSV 应包含 sample_id 和 label")
                result = store.vote(args.role, list(reader), offline=True)
            print(json.dumps(result, ensure_ascii=False))
            return 2 if result["conflicts"] else 0
        with ThreadingHTTPServer((args.host, args.port), Handler) as httpd:
            httpd.store = store
            port = httpd.server_address[1]
            host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
            print(
                f"Project: {store.root}\nMode: {store.mode}. Share each private link only with its assigned reviewer.",
                flush=True,
            )
            for role, key in store.credentials().items():
                print(f"{role}: http://{host}:{port}/?key={key}", flush=True)
            for sid, error in store.errors.items():
                if error:
                    print(f"{sid}: {error}", file=sys.stderr)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
        return 0
    except (OSError, ValueError, csv.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
