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
        page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
:root {{ font-family: system-ui,sans-serif; color:#17251d; background:#edf3ef; color-scheme:light dark }} * {{ box-sizing:border-box }} body {{ margin:0 }} main {{ width:min(100% - 2rem,70rem); margin:2rem auto 4rem }} h1 {{ margin-bottom:.35rem }} .lead {{ color:#54665b;margin-top:0 }} section {{ margin-top:1.5rem;padding:1.25rem;border:1px solid #d4e0d8;border-radius:.9rem;background:#fff;box-shadow:0 3px 16px #17301d12 }} h2 {{ margin-top:0 }} form,.actions,.pager {{ display:flex; flex-wrap:wrap; gap:.55rem; align-items:center }} button,.button {{ cursor:pointer;border:0;border-radius:.5rem;padding:.55rem .8rem;font:inherit;font-weight:650;background:#146b50;color:#fff;text-decoration:none }} button.secondary,.button.secondary {{ background:#e0e9e4;color:#193126 }} button.danger {{ background:#b42318 }} button:disabled {{ opacity:.55 }} table {{ width:100%;border-collapse:collapse }} th,td {{ padding:.7rem .45rem;border-bottom:1px solid #e2e9e4;text-align:left }} th {{ color:#52625a;font-size:.85rem }} .pager {{ justify-content:space-between;margin-top:1rem }} .empty {{ color:#52625a }} .notice {{ min-height:1.5em }} .error {{ color:#b42318 }} .ok {{ color:#087443 }} dialog {{ width:min(94vw,55rem);height:min(88svh,48rem);border:0;border-radius:.75rem;padding:1rem;box-shadow:0 14px 60px #0008 }} dialog::backdrop {{ background:#0008 }} .dialog-head {{ height:2.5rem;display:flex;justify-content:space-between;align-items:center }} #preview-content {{ width:100%;height:calc(100% - 2.5rem);border:0;object-fit:contain }} pre {{ white-space:pre-wrap;overflow:auto;height:calc(100% - 2.5rem);margin:0 }} @media(max-width:640px) {{ main {{ width:min(100% - 1rem,70rem);margin-top:1rem }} section {{ padding:.85rem }} th:nth-child(2),td:nth-child(2) {{ display:none }} }}
</style></head><body><main>
<h1>{title}</h1><p class="lead">Tailnet 内限定のファイル共有です。公開用のファイルは <code>data/downloadable</code> に置いてください。</p>
<section><h2>ダウンロード</h2><div id="downloadable-list"></div></section>
<section><h2>アップロード</h2><form id="upload-form"><input id="upload-files" type="file" multiple required><button type="submit">アップロード</button></form><p id="notice" class="notice" aria-live="polite"></p><h3>アップロード済み</h3><div id="uploaded-list"></div></section>
</main><dialog id="preview-dialog"><div class="dialog-head"><strong id="preview-title"></strong><button id="close-preview" class="secondary" type="button">閉じる</button></div><div id="preview-area"></div></dialog>
<script>
const state={{downloadable:1,uploaded:1}}, perPage={PAGE_SIZE};
const api=(area,name,action)=>`/api/files/${{area}}/${{encodeURIComponent(name)}}/${{action}}`;
const bytes=n=>n<1024?`${{n}} B`:n<1048576?`${{(n/1024).toFixed(1)}} KiB`:`${{(n/1048576).toFixed(1)}} MiB`;
const date=value=>new Intl.DateTimeFormat('ja-JP',{{dateStyle:'medium',timeStyle:'short'}}).format(new Date(value));
function makeButton(label,style,handler){{const e=document.createElement('button');e.type='button';e.textContent=label;e.className=style;e.onclick=handler;return e}}
function makeLink(label,href){{const e=document.createElement('a');e.textContent=label;e.href=href;e.className='button secondary';return e}}
async function request(url,options){{const response=await fetch(url,options);const data=await response.json().catch(()=>({{}}));if(!response.ok)throw new Error(data.error||'操作に失敗しました。');return data}}
function render(area,data){{const target=document.querySelector(`#${{area}}-list`);target.replaceChildren();if(!data.items.length){{const p=document.createElement('p');p.className='empty';p.textContent=area==='downloadable'?'公開中のファイルはありません。':'アップロード済みファイルはありません。';target.append(p);return}}const table=document.createElement('table'),thead=document.createElement('thead'),tbody=document.createElement('tbody');thead.innerHTML='<tr><th>ファイル名</th><th>更新日時</th><th>サイズ</th><th>操作</th></tr>';for(const item of data.items){{const row=document.createElement('tr');for(const value of [item.name,date(item.modifiedAt),bytes(item.size)]){{const cell=document.createElement('td');cell.textContent=value;row.append(cell)}}const actions=document.createElement('td');actions.className='actions';if(item.previewable)actions.append(makeButton('プレビュー','secondary',()=>preview(area,item)));actions.append(makeLink('ダウンロード',api(area,item.name,'download')));if(area==='uploaded')actions.append(makeButton('削除','danger',()=>removeUpload(item.name)));row.append(actions);tbody.append(row)}}table.append(thead,tbody);target.append(table);const pages=Math.max(1,Math.ceil(data.total/data.perPage)),pager=document.createElement('div'),prev=makeButton('前へ','secondary',()=>load(area,state[area]-1)),next=makeButton('次へ','secondary',()=>load(area,state[area]+1)),info=document.createElement('span');prev.disabled=data.page<=1;next.disabled=data.page>=pages;info.textContent=`${{data.total}} 件中 ${{data.page}} / ${{pages}} ページ`;pager.className='pager';pager.append(prev,info,next);target.append(pager)}}
async function load(area,page=1){{const data=await request(`/api/files/${{area}}?page=${{page}}&per_page=${{perPage}}`);state[area]=data.page;render(area,data)}}
async function removeUpload(name){{if(!confirm(`「${{name}}」を削除しますか？`))return;try{{await request(`/api/files/uploaded/${{encodeURIComponent(name)}}`,{{method:'DELETE'}});await load('uploaded',state.uploaded)}}catch(error){{alert(error.message)}}}}
async function preview(area,item){{const dialog=document.querySelector('#preview-dialog'),box=document.querySelector('#preview-area'),url=api(area,item.name,'preview');box.replaceChildren();document.querySelector('#preview-title').textContent=item.name;if(item.mimeType.startsWith('image/')){{const e=document.createElement('img');e.id='preview-content';e.src=url;e.alt=item.name;box.append(e)}}else if(item.mimeType==='application/pdf'){{const e=document.createElement('iframe');e.id='preview-content';e.src=url;e.title=item.name;e.sandbox='allow-downloads';box.append(e)}}else{{const e=document.createElement('pre');e.textContent='読み込み中…';box.append(e);try{{const r=await fetch(url);if(!r.ok)throw new Error('プレビューを取得できません。');e.textContent=await r.text()}}catch(error){{e.textContent=error.message}}}}dialog.showModal()}}
document.querySelector('#close-preview').onclick=()=>document.querySelector('#preview-dialog').close();
document.querySelector('#upload-form').onsubmit=async event=>{{event.preventDefault();const input=document.querySelector('#upload-files'),notice=document.querySelector('#notice'),submit=event.currentTarget.querySelector('button');if(!input.files.length)return;const form=new FormData;for(const file of input.files)form.append('files',file,file.name);submit.disabled=true;notice.className='notice';notice.textContent='アップロード中…';try{{const data=await request('/api/files/upload',{{method:'POST',body:form}});notice.className='notice ok';notice.textContent=`${{data.uploaded.join('、')}} をアップロードしました。`;event.currentTarget.reset();await load('uploaded',1)}}catch(error){{notice.className='notice error';notice.textContent=error.message}}finally{{submit.disabled=false}}}};
Promise.all([load('downloadable'),load('uploaded')]).catch(error=>{{const n=document.querySelector('#notice');n.className='notice error';n.textContent=error.message}});
</script></body></html>""".encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(page)


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
