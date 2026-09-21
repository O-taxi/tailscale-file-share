#!/usr/bin/env python3
"""Local-only file sharing app published privately with Tailscale Serve."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import shutil
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
ROOTS = {
    "downloadable": BASE_DIR / "data" / "downloadable",
    "uploaded": BASE_DIR / "data" / "uploaded",
}
PAGE_SIZE = 10
MAX_PAGE_SIZE = 50
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_TEXT_PREVIEW_BYTES = 256 * 1024


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def valid_filename(value: str) -> str | None:
    """Allow one filename only; reject path traversal on every platform."""
    if not value or "\x00" in value or "/" in value or "\\" in value:
        return None
    if value in {".", ".."} or len(value.encode("utf-8")) > 240:
        return None
    return value


def get_file(area: str, encoded_name: str) -> Path | None:
    root = ROOTS.get(area)
    name = valid_filename(unquote(encoded_name))
    if root is None or name is None:
        return None
    target = root / name
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return target


def unique_upload_path(filename: str) -> Path:
    root = ROOTS["uploaded"]
    path = root / filename
    stem, suffix, number = path.stem, path.suffix, 2
    while path.exists():
        path = root / f"{stem} ({number}){suffix}"
        number += 1
    return path


def previewable(path: Path) -> bool:
    mime = content_type(path)
    return mime.startswith("image/") or mime == "application/pdf" or mime.startswith("text/")


class AppHandler(BaseHTTPRequestHandler):
    server_version = "TailnetFiles/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{utc_now()}] {self.client_address[0]} {format % args}", flush=True)

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"ok": False, "error": message}, status)

    def list_files(self, area: str, query: dict[str, list[str]]) -> None:
        try:
            page = max(1, int(query.get("page", ["1"])[0]))
            per_page = min(MAX_PAGE_SIZE, max(1, int(query.get("per_page", [str(PAGE_SIZE)])[0])))
        except ValueError:
            page, per_page = 1, PAGE_SIZE
        files = sorted(
            (p for p in ROOTS[area].iterdir() if p.is_file() and not p.name.startswith(".")),
            key=lambda p: (p.name.casefold(), p.name),
        )
        start = (page - 1) * per_page
        items = []
        for path in files[start : start + per_page]:
            stat = path.stat()
            items.append({
                "name": path.name,
                "size": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat().replace("+00:00", "Z"),
                "mimeType": content_type(path),
                "previewable": previewable(path),
            })
        self.send_json({"items": items, "page": page, "perPage": per_page, "total": len(files)})

    def stream_file(self, area: str, encoded_name: str, inline: bool) -> None:
        path = get_file(area, encoded_name)
        if path is None:
            self.fail(HTTPStatus.BAD_REQUEST, "ファイル名が不正です。")
            return
        if not path.is_file():
            self.fail(HTTPStatus.NOT_FOUND, "ファイルが見つかりません。")
            return
        mime = content_type(path)
        if inline and not previewable(path):
            self.fail(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "この形式はプレビューできません。")
            return
        if inline and mime.startswith("text/"):
            if path.stat().st_size > MAX_TEXT_PREVIEW_BYTES:
                self.fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "テキストのプレビューは 256 KiB までです。")
                return
            # Do not execute uploaded HTML/SVG as this application's origin.
            mime = "text/plain; charset=utf-8"
        disposition = "inline" if inline else "attachment"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{quote(path.name, safe='')}")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/healthz":
            self.send_json({"ok": True, "now": utc_now()})
            return
        if path in {"/api/files/downloadable", "/api/files/uploaded"}:
            self.list_files(path.rsplit("/", 1)[1], query)
            return
        parts = path.split("/")
        if len(parts) == 6 and parts[:3] == ["", "api", "files"] and parts[3] in ROOTS:
            if parts[5] == "download":
                self.stream_file(parts[3], parts[4], inline=False)
                return
            if parts[5] == "preview":
                self.stream_file(parts[3], parts[4], inline=True)
                return
        if path == "/":
            self.send_page()
            return
        if path in {"/static/styles.css", "/static/app.js"}:
            self.send_static(path)
            return
        self.fail(HTTPStatus.NOT_FOUND, "見つかりません。")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/files/upload":
            self.fail(HTTPStatus.NOT_FOUND, "見つかりません。")
            return
        self.upload()

    def do_DELETE(self) -> None:  # noqa: N802
        parts = urlparse(self.path).path.split("/")
        if len(parts) != 5 or parts[:4] != ["", "api", "files", "uploaded"]:
            self.fail(HTTPStatus.NOT_FOUND, "見つかりません。")
            return
        path = get_file("uploaded", parts[4])
        if path is None:
            self.fail(HTTPStatus.BAD_REQUEST, "ファイル名が不正です。")
            return
        if not path.is_file():
            self.fail(HTTPStatus.NOT_FOUND, "ファイルが見つかりません。")
            return
        path.unlink()
        self.send_json({"ok": True, "deleted": path.name})

    def upload(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.fail(HTTPStatus.LENGTH_REQUIRED, "Content-Length が必要です。")
            return
        if not 0 < length <= MAX_UPLOAD_BYTES:
            self.fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "アップロードは合計 100 MiB までです。")
            return
        raw_content_type = self.headers.get("Content-Type", "")
        header = Message()
        header["Content-Type"] = raw_content_type
        if header.get_content_type() != "multipart/form-data" or not header.get_param("boundary", header="content-type"):
            self.fail(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "multipart/form-data で送信してください。")
            return
        raw_body = self.rfile.read(length)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {raw_content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw_body
        )
        uploaded: list[str] = []
        for part in message.iter_attachments():
            filename = part.get_filename()
            if not filename:
                continue
            filename = valid_filename(filename)
            if filename is None:
                self.fail(HTTPStatus.BAD_REQUEST, "ファイル名にパスは使えません。")
                return
            data = part.get_payload(decode=True)
            if data is None:
                self.fail(HTTPStatus.BAD_REQUEST, "ファイルを読み取れませんでした。")
                return
            destination = unique_upload_path(filename)
            destination.write_bytes(data)
            uploaded.append(destination.name)
        if not uploaded:
            self.fail(HTTPStatus.BAD_REQUEST, "ファイルを選択してください。")
            return
        self.send_json({"ok": True, "uploaded": uploaded}, HTTPStatus.CREATED)

    def send_page(self) -> None:
        title = html.escape(os.environ.get("APP_TITLE", "Tailscale ファイル共有"))
        page = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8").replace("{{APP_TITLE}}", title).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(page)

    def send_static(self, request_path: str) -> None:
        filenames = {
            "/static/styles.css": "styles.css",
            "/static/app.js": "app.js",
        }
        path = STATIC_DIR / filenames[request_path]
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type(path) + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    for root in ROOTS.values():
        root.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description="Local-only Tailscale file sharing app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Listening on http://{args.host}:{args.port} (local only by default)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
