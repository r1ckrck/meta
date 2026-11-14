#!/usr/bin/env python3
"""meta — read, edit and strip image/video metadata in a browser page.

Usage:  meta [file ...]    open meta (or hand files to the running one)
        meta stop          stop it

Starts a local server on 127.0.0.1 in the background, opens it in the default
browser and returns the prompt. Uses exiftool for all reading and writing.
It stops itself after an hour with no meta tab open. Errors go to meta.log in
the per-user temp folder. META_FOREGROUND=1 runs it in the terminal instead.

If meta is already running, files are handed to it instead: it opens a fresh
tab in front with the file selected, and the old tab closes itself.

Files opened by path (command line or the path box) are edited in place;
a backup copy goes to the Trash before each write. Files dropped onto the
page are working copies — the original is never touched; download to keep.
"""

import atexit
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

PORT = 8765
TOKEN = secrets.token_urlsafe(16)
WORKDIR = tempfile.mkdtemp(prefix="meta-")
atexit.register(shutil.rmtree, WORKDIR, True)
EXIFTOOL = shutil.which("exiftool") or "/opt/homebrew/bin/exiftool"

FILES = {}  # id -> {id, path, name, copy, version}
LOCK = threading.Lock()

WEB_IMAGES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
}

# Keeps orientation and colour profile so a stripped image still displays correctly.
STRIP_ALL_ARGS = ["-all=", "--icc_profile:all", "-tagsfromfile", "@", "-Orientation"]

TAG_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(:[A-Za-z0-9][A-Za-z0-9_-]*)?$")


class ApiError(Exception):
    pass


# ---------- exiftool ----------

def exiftool(args):
    return subprocess.run(
        [EXIFTOOL, *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def read_tags(path):
    r = exiftool(["-json", "-G1", "-c", "%+.6f", path])
    if not r.stdout.strip():
        raise ApiError(r.stderr.strip() or "exiftool could not read this file")
    tags = json.loads(r.stdout)[0]
    tags.pop("SourceFile", None)
    return tags


def write_file(f, args):
    """Run an exiftool write on f. Originals get a backup in the Trash first."""
    path = f["path"]
    backup = None
    if not f["copy"]:
        stem, ext = os.path.splitext(os.path.basename(path))
        stamp = time.strftime("%Y-%m-%d %H.%M.%S")
        # Sibling copy, then trash it, so Finder's Put Back returns it to this folder.
        backup = os.path.join(os.path.dirname(path), f"{stem} (meta backup {stamp}){ext}")
        shutil.copy2(path, backup)

    cmd = ["-overwrite_original", "-sep", ", ", *args, path]
    r = exiftool(cmd)
    updated = UPDATED.search(r.stdout) is not None
    missing = [n for g, n in UNDEFINED.findall(r.stderr) if g == "PNG"]
    if missing:
        r = exiftool(["-config", png_text_config(path, missing), *cmd])
        updated = updated or UPDATED.search(r.stdout) is not None
    # "No writable tags set" comes from restoring an Orientation the file never had.
    warnings = [ln.split(":", 1)[1].strip() for ln in r.stderr.splitlines()
                if ln.startswith(("Warning", "Error")) and "No writable tags set" not in ln]

    if backup:
        if updated:
            trash(backup)
        else:
            os.remove(backup)

    if updated:
        f["version"] += 1
    elif not warnings:
        warnings = ["Nothing changed."]
    return updated, warnings


UPDATED = re.compile(r"\b1 (image )?files? updated")
UNDEFINED = re.compile(r"Tag '([\w-]+):([\w-]+)' is not defined")


def png_text_config(path, names):
    """exiftool only writes PNG text chunks it knows. Declare the others (e.g. ComfyUI's
    prompt/workflow) using each chunk's exact keyword, so the existing chunk is replaced."""
    r = exiftool(["-json", "-D", "-G1", *[f"-PNG:{n}" for n in names], path])
    try:
        found = json.loads(r.stdout)[0]
    except (ValueError, IndexError):
        found = {}
    perl = lambda s: "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
    lines = []
    for n in names:
        entry = found.get(f"PNG:{n}")
        keyword = str(entry["id"]) if isinstance(entry, dict) and "id" in entry else n
        lines.append(f"    {perl(keyword)} => {{ Name => {perl(n)} }},")
    cfg = os.path.join(WORKDIR, f"png-{uuid.uuid4().hex[:8]}.config")
    with open(cfg, "w") as fh:
        fh.write("%Image::ExifTool::UserDefined = (\n  'Image::ExifTool::PNG::TextualData' => {\n"
                 + "\n".join(lines) + "\n  },\n);\n1;\n")
    return cfg


def trash(path):
    if os.path.exists("/usr/bin/trash"):
        if subprocess.run(["/usr/bin/trash", path], capture_output=True).returncode == 0:
            return
    shutil.move(path, os.path.join(os.path.expanduser("~/.Trash"), os.path.basename(path)))


# ---------- file registry ----------

def register(path, copy):
    with LOCK:
        for f in FILES.values():
            if f["path"] == path:
                return f
        fid = uuid.uuid4().hex[:12]
        f = {"id": fid, "path": path, "name": os.path.basename(path), "copy": copy, "version": 0}
        FILES[fid] = f
        return f


def get_file(fid):
    f = FILES.get(fid)
    if not f:
        raise ApiError("Unknown file")
    if not os.path.isfile(f["path"]):
        raise ApiError("File no longer exists at " + f["path"])
    return f


def public(f):
    return {"id": f["id"], "name": f["name"], "copy": f["copy"], "version": f["version"],
            "path": None if f["copy"] else f["path"]}


def clean_path(raw):
    p = raw.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "'\"":
        p = p[1:-1]
    p = re.sub(r"\\(.)", r"\1", p)  # shell-escaped spaces from Terminal
    if p.startswith("file://"):
        p = urlparse(p).path
    return os.path.abspath(os.path.expanduser(p))


def preview_file(f):
    ext = os.path.splitext(f["path"])[1].lower()
    if ext in WEB_IMAGES:
        return f["path"], WEB_IMAGES[ext]
    # Quick Look renders HEIC, RAW, TIFF and video frames.
    out = os.path.join(WORKDIR, "preview", f"{f['id']}-{f['version']}")
    png = os.path.join(out, os.path.basename(f["path"]) + ".png")
    if not os.path.exists(png):
        os.makedirs(out, exist_ok=True)
        subprocess.run(["qlmanage", "-t", "-s", "800", "-o", out, f["path"]],
                       capture_output=True, timeout=30)
    if not os.path.exists(png):
        raise ApiError("No preview")
    return png, "image/png"


# ---------- API ----------

def api_open(body):
    path = clean_path(body.get("path", ""))
    if not os.path.isfile(path):
        raise ApiError("No file at " + path)
    return public(register(path, copy=False))


def api_read(body):
    f = get_file(body.get("id"))
    return {**public(f), "tags": read_tags(f["path"])}


def api_write(body):
    f = get_file(body.get("id"))
    changes = body.get("set") or {}
    args = []
    for key, value in changes.items():
        if not TAG_KEY.match(key):
            raise ApiError("Invalid tag name: " + key)
        args.append(f"-{key}=" if value is None else f"-{key}={value}")
    if not args:
        raise ApiError("No changes")
    updated, warnings = write_file(f, args)
    return {**public(f), "tags": read_tags(f["path"]), "updated": updated, "warnings": warnings}


def api_strip(body):
    f = get_file(body.get("id"))
    updated, warnings = write_file(f, STRIP_ALL_ARGS)
    return {**public(f), "tags": read_tags(f["path"]), "updated": updated, "warnings": warnings}


def api_remove(body):
    with LOCK:
        FILES.pop(body.get("id"), None)
    return {}


def api_handoff(body):
    """A second `meta` run sends its files here. A fresh tab comes to the front;
    the old tab sees it and closes itself."""
    selected, skipped = None, []
    for raw in body.get("paths") or []:
        path = clean_path(raw)
        if os.path.isfile(path):
            selected = register(path, copy=False)["id"]
        else:
            skipped.append(raw)
    if not os.environ.get("META_NO_BROWSER"):
        webbrowser.open(SERVER_URL + (f"?select={selected}" if selected else ""))
    return {"skipped": skipped, "selected": selected}


POST_ROUTES = {"/api/ping": lambda body: {}, "/api/open": api_open, "/api/read": api_read, "/api/write": api_write,
               "/api/strip": api_strip, "/api/remove": api_remove, "/api/handoff": api_handoff}


class Handler(BaseHTTPRequestHandler):
    server_version = "meta"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode() if not isinstance(body, str) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype, download_name=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(download_name)}")
        self.end_headers()
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1 << 20)

    def _guard(self, query_token=None):
        # Host check blocks DNS rebinding; token blocks other local pages calling the API.
        global LAST_SEEN
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        ok = query_token == TOKEN or self.headers.get("X-Token") == TOKEN
        if ok:
            LAST_SEEN = time.time()
        return ok

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/favicon.ico":
                return self._send(204, b"")
            if u.path == "/":
                return self._send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
            if not self._guard(q.get("t", [None])[0]):
                return self._send(403, {"error": "Forbidden"})
            if u.path == "/api/files":
                return self._send(200, [public(f) for f in FILES.values()])
            m = re.match(r"^/api/(preview|download)/(\w+)$", u.path)
            if m:
                f = get_file(m.group(2))
                if m.group(1) == "preview":
                    return self._send_file(*preview_file(f))
                return self._send_file(f["path"], "application/octet-stream", f["name"])
            self._send(404, {"error": "Not found"})
        except ApiError as e:
            self._send(404, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if not self._guard():
                return self._send(403, {"error": "Forbidden"})
            length = int(self.headers.get("Content-Length") or 0)
            if u.path == "/api/upload":
                return self._send(200, self._upload(u, length))
            route = POST_ROUTES.get(u.path)
            if not route:
                return self._send(404, {"error": "Not found"})
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, route(body))
        except ApiError as e:
            self._send(400, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # surface unexpected failures in the page
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _upload(self, u, length):
        name = os.path.basename(parse_qs(u.query).get("name", ["file"])[0]) or "file"
        folder = os.path.join(WORKDIR, "copies", uuid.uuid4().hex[:8])
        os.makedirs(folder)
        path = os.path.join(folder, name)
        remaining = length
        with open(path, "wb") as out:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        return public(register(path, copy=True))


# ---------- page ----------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>meta</title>
<style>
/* Spacing scale: 4 · 8 · 12 · 16 · 24 · 32. Tighter = belongs together, wider = separate groups. */
:root {
  --bg: #f5f5f3; --surface: #ffffff; --inset: #f7f7f5; --sunken: #ededea; --line: #e3e3df;
  --text: #22221f; --muted: #6b6b66; --faint: #a2a29c;
  --accent: #3a63d6; --accent-ink: #ffffff;
  --warn: #a8660a; --warn-bg: #fbf1df; --danger: #c0443a; --ok: #2f8a57;
  --radius: 10px;
  --mono: ui-monospace, "SF Mono", Menlo, monospace;
  --label-w: 220px;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1d1e20; --surface: #252629; --inset: #212225; --sunken: #2d2e32; --line: #35363a;
    --text: #dcdbd6; --muted: #9e9d97; --faint: #6e6e6a;
    --accent: #8ca5ee; --accent-ink: #16181d;
    --warn: #d6a35c; --warn-bg: #332b1f; --danger: #e0786d; --ok: #74c494;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Inter", system-ui, sans-serif;
  display: grid; grid-template-columns: 264px 1fr;
  -webkit-font-smoothing: antialiased;
}
button, input, textarea { font: inherit; color: inherit; }

/* ---------- sidebar ---------- */
aside {
  border-right: 1px solid var(--line); padding: 20px 16px; overflow-y: auto;
  height: 100vh; position: sticky; top: 0;
}
.brand { font-weight: 650; letter-spacing: -0.01em; padding: 0 4px; margin-bottom: 24px; }
.brand span { color: var(--faint); font-weight: 400; }
.open { display: grid; gap: 8px; }
.dropzone {
  border: 1.5px dashed var(--line); border-radius: var(--radius); padding: 16px 12px;
  text-align: center; color: var(--muted); font-size: 13px; cursor: pointer;
}
.dropzone:hover { border-color: var(--faint); color: var(--text); }
.pathform input {
  width: 100%; height: 32px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--surface); font-size: 12.5px;
}
.pathform input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.hint { font-size: 11.5px; line-height: 1.45; color: var(--faint); padding: 4px 4px 0; }
.files { display: grid; gap: 2px; margin-top: 32px; }
.files-label {
  font-size: 11px; font-weight: 600; color: var(--faint); text-transform: uppercase;
  letter-spacing: .06em; padding: 0 8px 8px;
}
.file {
  display: flex; align-items: center; gap: 8px; height: 32px; padding: 0 8px; border-radius: 8px;
  cursor: pointer; color: var(--muted);
}
.file:hover { background: var(--sunken); }
.file.active { background: var(--surface); color: var(--text); box-shadow: 0 0 0 1px var(--line); }
.file .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file .kind { font-size: 10.5px; color: var(--faint); text-transform: uppercase; letter-spacing: .04em; }
.file .x { border: 0; background: none; color: var(--faint); cursor: pointer; padding: 0 2px; visibility: hidden; }
.file:hover .x { visibility: visible; }

/* ---------- main ---------- */
main { overflow-y: auto; height: 100vh; }
.wrap { max-width: 900px; margin: 0 auto; padding: 32px 32px 120px; }
.empty { height: 100vh; display: grid; place-items: center; text-align: center; color: var(--muted); }
.empty h1 { font-size: 20px; color: var(--text); font-weight: 600; margin: 0 0 8px; }
.empty p { margin: 0; }

/* header + actions form one group; sections start after a wider gap */
.head { display: flex; gap: 24px; align-items: flex-start; margin-bottom: 24px; }
.thumb {
  width: 136px; height: 102px; border-radius: 8px; background: var(--sunken);
  object-fit: cover; flex: none; border: 1px solid var(--line);
}
.title { min-width: 0; flex: 1; padding-top: 2px; }
.title h1 { font-size: 20px; line-height: 1.3; margin: 0 0 4px; font-weight: 620; word-break: break-all; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-variant-numeric: tabular-nums; }
.where { font: 11.5px/1.5 var(--mono); color: var(--faint); margin-top: 4px; word-break: break-all; }
.badge {
  display: inline-flex; align-items: center; gap: 6px; font-size: 12px; margin-top: 12px;
  height: 24px; padding: 0 10px; border-radius: 999px; background: var(--warn-bg); color: var(--warn);
}
.badge.ok { background: var(--sunken); color: var(--muted); }
.dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex: none; }

.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 32px; }
.actions:has(+ .copynote) { margin-bottom: 12px; }
.copynote { color: var(--muted); font-size: 12.5px; margin-bottom: 32px; }
.btn {
  display: inline-flex; align-items: center; height: 32px; padding: 0 14px;
  border: 1px solid var(--line); background: var(--surface); border-radius: 8px; cursor: pointer; font-size: 13px;
}
.btn:hover { border-color: var(--faint); }
.btn:disabled { opacity: .45; cursor: default; }
.btn:disabled:hover { border-color: var(--line); }
.btn.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 550; }
.btn.danger { color: var(--danger); }
.spacer { flex: 1; }

.notice {
  border-radius: var(--radius); padding: 12px 16px; margin: -8px 0 24px; font-size: 13px;
  background: var(--sunken);
}
.notice.warn { background: var(--warn-bg); color: var(--warn); }
.notice ul { margin: 4px 0 0; padding-left: 18px; }

/* ---------- sections ---------- */
details.sec {
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  margin-bottom: 16px; overflow: hidden;
}
details.sec > summary {
  list-style: none; cursor: pointer; padding: 12px 16px; display: flex; align-items: center; gap: 8px;
  user-select: none;
}
details.sec > summary::-webkit-details-marker { display: none; }
summary .chev { color: var(--faint); transition: transform .15s; width: 12px; font-size: 9px; line-height: 1; display: inline-grid; place-items: center; height: 12px; }
details[open] > summary .chev { transform: rotate(90deg); }
summary .stitle { font-weight: 600; }
summary .count { color: var(--faint); font-size: 12px; font-variant-numeric: tabular-nums; }
summary .flag { margin-left: auto; color: var(--warn); font-size: 12px; display: flex; align-items: center; gap: 6px; }
summary .maplink { margin-left: auto; font-size: 12px; color: var(--accent); text-decoration: none; }
summary .flag + .maplink { margin-left: 16px; }
.rows { border-top: 1px solid var(--line); padding: 6px 0; }

/* rows: no dividers, rhythm comes from padding; values share one column */
.row {
  display: grid; grid-template-columns: var(--label-w) 1fr; gap: 16px; padding: 6px 16px;
  align-items: baseline; min-height: 34px;
}
.row:hover { background: var(--inset); }
.row.sensitive { box-shadow: inset 2px 0 0 var(--warn); }
.k { color: var(--muted); display: flex; gap: 8px; align-items: baseline; min-width: 0; }
.k .lab { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grp {
  font-size: 10px; line-height: 16px; color: var(--faint); border: 1px solid var(--line); border-radius: 4px;
  padding: 0 4px; flex: none; letter-spacing: .02em;
}
.v { word-break: break-word; font-variant-numeric: tabular-nums; }
.v .muted, .muted { color: var(--faint); }
.row.changed, .nrow.changed { background: color-mix(in srgb, var(--accent) 9%, transparent); }
.row.removed .lab { text-decoration: line-through; color: var(--faint); }
.edit { display: flex; gap: 8px; align-items: center; }
.edit input, .nv input, .nv textarea, .addrow input {
  width: 100%; min-width: 0; height: 28px; padding: 0 8px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--inset); font-variant-numeric: tabular-nums;
}
.nv textarea { height: auto; padding: 4px 8px; resize: vertical; line-height: 1.5; display: block; }
.edit input:focus, .nv input:focus, .nv textarea:focus, .addrow input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.edit input:disabled { text-decoration: line-through; color: var(--faint); }
.row:has(.edit) { align-items: center; padding-top: 4px; padding-bottom: 4px; }
.iconbtn {
  border: 0; background: none; color: var(--faint); cursor: pointer; width: 24px; height: 24px;
  border-radius: 6px; font-size: 13px; flex: none;
}
.iconbtn:hover { color: var(--danger); background: var(--sunken); }
.addrow { display: grid; grid-template-columns: var(--label-w) 1fr 24px; gap: 16px; padding: 4px 16px; align-items: center; }
.addrow:last-child { padding: 12px 16px 8px; }
.addrow:last-child .btn { grid-column: 2; justify-self: start; }

/* ---------- JSON fields split into nodes ---------- */
.row.jsonhead { margin-top: 6px; }
.rows > .row.jsonhead:first-child { margin-top: 0; }
.nodes + .row { border-top: 1px solid var(--line); margin-top: 6px; padding-top: 12px; }
.nodes { padding: 4px 16px 12px; display: grid; gap: 8px; }
details.node, div.node { border: 1px solid var(--line); border-radius: 8px; background: var(--inset); min-width: 0; }
div.node { padding: 4px 0; }
details.node > summary {
  list-style: none; cursor: pointer; height: 36px; padding: 0 12px; display: flex; gap: 8px; align-items: center; min-width: 0;
}
details.node > summary::-webkit-details-marker { display: none; }
details.node[open] > summary { border-bottom: 1px solid var(--line); margin-bottom: 4px; }
details.node[open] { padding-bottom: 4px; }
.nid { font: 11px var(--mono); color: var(--faint); flex: none; min-width: 24px; }
.ntype { font-weight: 560; flex: none; }
.ncustom { color: var(--muted); flex: none; }
.npreview { color: var(--faint); font-size: 12.5px; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
details.node[open] .npreview { visibility: hidden; }
/* node label column = section label column minus the card inset, so values line up across the page */
.nrow {
  display: grid; grid-template-columns: calc(var(--label-w) - 13px) 1fr; gap: 16px;
  padding: 5px 12px; align-items: baseline; font-size: 13px;
}
.nrow:has(input, textarea) { align-items: center; padding-top: 3px; padding-bottom: 3px; }
.nk { color: var(--muted); word-break: break-word; }
.nv { word-break: break-word; white-space: pre-wrap; font-variant-numeric: tabular-nums; min-width: 0; }
.nv .ref { color: var(--accent); font-size: 12.5px; }
.npreview.lone { color: var(--text); font-variant-numeric: tabular-nums; }
.loras { padding: 2px 12px 4px; display: grid; gap: 2px; }
.lora { display: grid; grid-template-columns: 8px 1fr auto; gap: 10px; align-items: baseline; font-size: 13px; padding: 2px 0; }
.lora .ldot { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); transform: translateY(-1px); }
.lora.off { color: var(--faint); }
.lora.off .ldot { background: transparent; box-shadow: inset 0 0 0 1.5px var(--faint); }
.lora .lname { word-break: break-all; }
.lora .lstr { font-variant-numeric: tabular-nums; color: var(--muted); }
.lora.off .lstr { color: var(--faint); }
.gen { margin: 4px 16px 16px; border: 1px solid var(--line); border-radius: 8px; background: var(--inset); padding: 4px 0 8px; }
.gen-title { font-size: 11px; font-weight: 600; color: var(--faint); text-transform: uppercase; letter-spacing: .06em; padding: 8px 12px 4px; }
.gen-row { display: grid; grid-template-columns: calc(var(--label-w) - 13px) 1fr; gap: 16px; padding: 5px 12px; align-items: baseline; }
.gen .gk { color: var(--muted); }
.gen .gv { word-break: break-word; font-variant-numeric: tabular-nums; min-width: 0; }
.gen .gv.prose { white-space: pre-wrap; }
.gen .gv .loras { padding: 0; }
.offloras > summary { cursor: pointer; color: var(--faint); font-size: 12.5px; padding-top: 4px; list-style: none; }
.offloras > summary::-webkit-details-marker { display: none; }
.offloras > summary::before { content: "▸ "; }
.offloras[open] > summary::before { content: "▾ "; }
@media (max-width: 760px) { .gen-row { grid-template-columns: 1fr; gap: 2px; } }
.nmore { padding: 8px 12px; color: var(--faint); font-size: 12px; }

/* ---------- compare ---------- */
del { background: color-mix(in srgb, var(--danger) 16%, transparent); color: var(--danger); text-decoration: line-through; text-decoration-thickness: 1px; border-radius: 3px; padding: 0 2px; }
ins { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); text-decoration: none; border-radius: 3px; padding: 0 2px; }
.arrow { color: var(--faint); padding: 0 4px; }
.inl { white-space: pre-wrap; word-break: break-word; }
.legend { font-size: 12px; display: flex; gap: 8px; align-items: center; }
.cmp-head { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
.cmp-file { display: flex; gap: 12px; align-items: center; min-width: 0; padding: 12px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); }
.ab { width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; font-size: 11px; font-weight: 650; flex: none; }
.ab.a { background: color-mix(in srgb, var(--danger) 18%, transparent); color: var(--danger); }
.ab.b { background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }
.cthumb { width: 72px; height: 72px; border-radius: 6px; object-fit: cover; background: var(--sunken); border: 1px solid var(--line); flex: none; }
.cinfo { min-width: 0; }
.cname { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cmpnode .nhead { display: flex; gap: 8px; align-items: center; height: 36px; padding: 0 12px; border-bottom: 1px solid var(--line); margin-bottom: 4px; }
.cmpnode { padding-bottom: 4px; }
.gen-row.same .gv { color: var(--muted); }
.gen-row.same .gk { color: var(--faint); }
.lcmp { display: grid; grid-template-columns: 1fr 56px 56px; gap: 10px; font-size: 13px; padding: 2px 0; font-variant-numeric: tabular-nums; }
.lcmp.lhead { font-size: 11px; color: var(--faint); font-weight: 600; }
.lcmp .lname { word-break: break-all; }
.lcmp:not(.diff):not(.lhead) { color: var(--muted); }
.lcmp .va { color: var(--danger); }
.lcmp .vb { color: var(--ok); }
.file .kind.abmark { font-weight: 650; color: var(--text); }
.picklist { display: grid; gap: 6px; margin-bottom: 8px; }
.pick { display: grid; gap: 2px; text-align: left; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--inset); cursor: pointer; }
.pick:hover { border-color: var(--accent); }
.pname { color: var(--text); }
.pwhere { font-size: 12px; color: var(--faint); word-break: break-all; }
.dfoot .spacer { flex: 1; }
.dfoot label.btn { cursor: pointer; }
@media (max-width: 760px) { .cmp-head { grid-template-columns: 1fr; } }

.offline {
  position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 20;
  background: var(--warn-bg); color: var(--warn); border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent);
  padding: 10px 16px; border-radius: 10px; font-size: 13px; box-shadow: 0 8px 24px rgba(0,0,0,.15);
}
.offline code { font-family: var(--mono); }

/* ---------- raw ---------- */
.rawtools { padding: 12px 16px; border-top: 1px solid var(--line); }
.rawtools input {
  width: 100%; height: 32px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--inset);
}
.rawgroup { padding: 16px 16px 4px; font: 600 11px var(--mono); color: var(--faint); letter-spacing: .04em; }
.rawrow { display: grid; grid-template-columns: var(--label-w) 1fr; gap: 16px; padding: 2px 16px; font: 12px/1.6 var(--mono); }
.rawrow:last-child { padding-bottom: 12px; }
.rawrow .rk { color: var(--muted); overflow: hidden; text-overflow: ellipsis; }
.rawrow .rv { word-break: break-word; }

/* ---------- edit bar ---------- */
.editbar {
  position: fixed; bottom: 24px; left: calc(50% + 132px); transform: translateX(-50%);
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 8px 8px 8px 16px;
  display: flex; gap: 8px; align-items: center; box-shadow: 0 8px 32px rgba(0,0,0,.18);
}
.editbar .n { color: var(--muted); margin-right: 8px; font-variant-numeric: tabular-nums; }

/* ---------- dialog ---------- */
dialog {
  border: 1px solid var(--line); border-radius: 14px; background: var(--surface); color: var(--text);
  padding: 0; width: min(640px, 92vw); box-shadow: 0 24px 64px rgba(0,0,0,.3);
}
dialog::backdrop { background: rgba(0,0,0,.4); }
.dhead { padding: 20px 24px 4px; font-weight: 620; font-size: 16px; }
.dbody { padding: 0 24px; color: var(--muted); max-height: 55vh; overflow-y: auto; }
.dbody p { margin: 4px 0 16px; }
.diff { width: 100%; border-collapse: collapse; font-size: 12.5px; color: var(--text); }
.diff td { padding: 8px 12px 8px 0; border-top: 1px solid var(--line); vertical-align: top; word-break: break-word; }
.diff td:first-child { color: var(--muted); width: 36%; }
.diff td:first-child div { color: var(--faint); margin-top: 2px; }
.diff .old { color: var(--faint); text-decoration: line-through; }
.diff .gone { color: var(--danger); }
.dfoot { display: flex; justify-content: flex-end; gap: 8px; padding: 20px 24px; }

.dragover::after {
  content: "Drop to open"; position: fixed; inset: 12px; border: 2px dashed var(--accent);
  border-radius: 16px; display: grid; place-items: center; font-size: 18px; color: var(--accent);
  background: color-mix(in srgb, var(--bg) 85%, transparent); z-index: 10; pointer-events: none;
}

@media (max-width: 760px) {
  body { grid-template-columns: 1fr; }
  aside { height: auto; position: static; border-right: 0; border-bottom: 1px solid var(--line); }
  main { height: auto; }
  .wrap { padding: 24px 16px 120px; }
  .row, .rawrow, .nrow, .addrow { grid-template-columns: 1fr; gap: 2px; }
  .editbar { left: 50%; }
}
</style>
</head>
<body>
<aside>
  <div class="brand">meta <span>· metadata</span></div>
  <div class="open">
    <label class="dropzone" id="pick">Drop files here or click to choose
      <input type="file" id="fileinput" multiple hidden>
    </label>
    <form class="pathform" id="pathform">
      <input id="pathinput" placeholder="Or paste a path (⌥⌘C in Finder)" autocomplete="off" spellcheck="false">
    </form>
    <div class="hint">A path edits the original. A dropped file edits a copy.</div>
  </div>
  <div class="files" id="files"></div>
</aside>
<main id="main"></main>
<dialog id="dialog"></dialog>

<script>
const TOKEN = "__TOKEN__";

// Sections, in display order. A tag lands in the first section whose pattern matches its name.
const SECTIONS = [
  { id: "location", title: "Location", sensitive: true, open: true,
    re: /^(GPS|Location)|Coordinates|^(City|Country|CountryCode|State|Province-State|Sub-location|Sublocation)$/i },
  { id: "identity", title: "Owner & device", sensitive: true, open: true,
    re: /Serial|^(OwnerName|CameraOwnerName|Artist|Creator|Author|By-line|HostComputer)$/i },
  { id: "dates", title: "Dates", open: true,
    re: /Date|^OffsetTime|^TimeZone|^SubSecTime/i },
  { id: "camera", title: "Camera", open: true,
    re: /^(Make|Model|Lens\w*|FNumber|ExposureTime|ISO|FocalLength|FocalLengthIn35mmFormat|Flash|WhiteBalance|ExposureProgram|ExposureMode|ExposureCompensation|MeteringMode|SceneCaptureType|DigitalZoomRatio)$/i },
  { id: "video", title: "Video & audio", open: true,
    re: /^(Duration|VideoFrameRate|CompressorName|CompressorID|AudioFormat|AudioChannels|AudioSampleRate|AudioBitsPerSample|Rotation|MajorBrand|VideoCodec|AudioCodec)$/i },
  { id: "ai", title: "AI generation", open: true,
    re: /^(Prompt|Workflow|Parameters|NegativePrompt|Sd-metadata|Invokeai\w*|Generation\w*|Dream)$/i,
    alsoIf: (e) => nodesOf(parseJSON(e.value)) !== null },
  { id: "description", title: "Description & rights", open: true,
    re: /^(Title|Description|ImageDescription|Caption-Abstract|Keywords|Subject|UserComment|Rating|Label|Headline|ObjectName|XPTitle|XPComment|XPKeywords|XPSubject|Comment|Copyright|CopyrightNotice|Rights)$/i },
  { id: "image", title: "Image", open: false,
    re: /^(Orientation|ColorSpace|ProfileDescription|BitsPerSample|BitDepth|ColorType|ColorComponents|XResolution|YResolution|ResolutionUnit|ExifImageWidth|ExifImageHeight)$/i },
  { id: "software", title: "Software", open: false,
    re: /^(Software|CreatorTool|Encoder|ProcessingSoftware|HistorySoftwareAgent|Application|EncodingTool|WritingApp|MuxingApp|CompatibleBrands)$/i },
  { id: "other", title: "Other", open: false, re: /./ },
];

// Derived or filesystem tags: shown in the header and raw view, never edited.
const SYSTEM_GROUPS = new Set(["System", "File", "Composite", "ExifTool"]);

// Friendly labels; order here also sorts rows inside a section.
const LABELS = {
  GPSLatitude: "Latitude", GPSLatitudeRef: "Latitude ref", GPSLongitude: "Longitude",
  GPSLongitudeRef: "Longitude ref", GPSAltitude: "Altitude", GPSAltitudeRef: "Altitude ref",
  GPSCoordinates: "Coordinates",
  OwnerName: "Owner", CameraOwnerName: "Camera owner", SerialNumber: "Serial number",
  LensSerialNumber: "Lens serial", InternalSerialNumber: "Internal serial", HostComputer: "Device",
  DateTimeOriginal: "Taken", CreationDate: "Created", CreateDate: "Created (digitized)",
  ModifyDate: "Modified", OffsetTimeOriginal: "Time zone (taken)", OffsetTime: "Time zone",
  OffsetTimeDigitized: "Time zone (digitized)",
  Make: "Make", Model: "Model", LensMake: "Lens make", LensModel: "Lens", FNumber: "Aperture",
  ExposureTime: "Shutter", ISO: "ISO", FocalLength: "Focal length",
  FocalLengthIn35mmFormat: "Focal length (35mm eq.)",
  Duration: "Duration", CompressorName: "Codec", CompressorID: "Codec ID",
  VideoFrameRate: "Frame rate", AudioFormat: "Audio format", AudioSampleRate: "Sample rate",
  AudioChannels: "Channels", Rotation: "Rotation",
  Title: "Title", Description: "Description", ImageDescription: "Description",
  Subject: "Keywords", Keywords: "Keywords",
};
const ORDER = Object.keys(LABELS);

const state = {
  files: [], current: null, data: null, version: 0,
  editing: false, edits: {}, jsonEdits: {}, added: [], open: {}, rawQuery: "", notice: null,
};

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// ---------- tag helpers ----------

function entries(tags) {
  return Object.entries(tags).map(([key, value]) => {
    const i = key.indexOf(":");
    const group = i < 0 ? "" : key.slice(0, i);
    const name = i < 0 ? key : key.slice(i + 1);
    const binary = typeof value === "string" && value.startsWith("(Binary data");
    return { key, group, name, value, binary, editable: !binary && !SYSTEM_GROUPS.has(group) };
  });
}

function rawString(v) {
  return Array.isArray(v) ? v.join(", ") : String(v);
}

function label(name) {
  if (LABELS[name]) return LABELS[name];
  const words = name.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2").split(" ");
  return words.map((w, i) => (i === 0 || /^[A-Z0-9]{2,}$/.test(w) ? w : w.toLowerCase())).join(" ");
}

function groupLabel(g) {
  if (/^(IFD\d|ExifIFD|InteropIFD|GPS|SubIFD\d*)$/.test(g)) return "EXIF";
  if (g.startsWith("XMP")) return "XMP";
  if (g.startsWith("ICC")) return "ICC";
  if (/^Track\d+$/.test(g)) return g.replace("Track", "Track ");
  return g;
}

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

function pretty(e) {
  const v = rawString(e.value);
  if (e.binary) return `<span class="muted">${esc(v.replace(/^\(|, use -b.*$|\)$/g, ""))}</span>`;
  const d = v.match(/^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})(\.\d+)?([+-]\d{2}:\d{2}|Z)?$/);
  if (d) {
    if (d[1] === "0000") return `<span class="muted">Not set</span>`;
    const tz = d[8] ? ` <span class="muted">${esc(d[8] === "Z" ? "UTC" : "UTC" + d[8])}</span>` : "";
    return `${+d[3]} ${MONTHS[+d[2] - 1]} ${d[1]}, ${d[4]}:${d[5]}:${d[6]}${tz}`;
  }
  if (v === "") return `<span class="muted">Empty</span>`;
  return esc(v);
}

function sectionize(ents) {
  const secs = SECTIONS.map((s) => ({ ...s, rows: [] }));
  for (const e of ents) {
    if (SYSTEM_GROUPS.has(e.group)) continue;
    secs.find((s) => s.re.test(e.name) || s.alsoIf?.(e)).rows.push(e);
  }
  const rank = (n) => (ORDER.indexOf(n) + 1 || 999);
  for (const s of secs) s.rows.sort((a, b) => rank(a.name) - rank(b.name));
  return secs;
}

function sensitiveRows(secs) {
  return secs.filter((s) => s.sensitive).flatMap((s) => s.rows);
}

function mapLink(tags) {
  const src = tags["Composite:GPSPosition"] || tags["Keys:GPSCoordinates"] || tags["UserData:GPSCoordinates"];
  const m = src && String(src).match(/([+-]?\d+(?:\.\d+)?),\s*([+-]?\d+(?:\.\d+)?)/);
  return m ? `https://maps.apple.com/?ll=${m[1]},${m[2]}&q=${m[1]},${m[2]}` : null;
}

// ---------- sidebar ----------

function renderFiles() {
  $("#files").innerHTML = (state.files.length ? `<div class="files-label">Files</div>` : "") + state.files.map((f) => `
    <div class="file ${f.id === state.current || f.id === state.compare?.id ? "active" : ""}" data-id="${f.id}" title="${esc(f.path || f.name + " (working copy)")}">
      <span class="name">${esc(f.name)}</span>
      <span class="kind ${state.compare ? "abmark" : ""}">${state.compare ? (f.id === state.current ? "A" : f.id === state.compare.id ? "B" : "") : f.copy ? "copy" : ""}</span>
      <button class="x" data-remove="${f.id}" title="Remove from list">×</button>
    </div>`).join("");
}

$("#files").addEventListener("click", (ev) => {
  const rm = ev.target.closest("[data-remove]");
  if (rm) {
    api("/api/remove", { id: rm.dataset.remove }).catch(() => {});
    if (state.compare?.id === rm.dataset.remove) { state.compare = null; render(); }
    state.files = state.files.filter((f) => f.id !== rm.dataset.remove);
    if (state.current === rm.dataset.remove) {
      state.current = null; state.data = null;
      if (state.files[0]) return select(state.files[0].id);
      renderFiles(); return render();
    }
    return renderFiles();
  }
  const item = ev.target.closest(".file");
  if (item) select(item.dataset.id);
});

function addFile(f) {
  if (!state.files.some((x) => x.id === f.id)) state.files.push(f);
}

async function select(id) {
  if (state.editing && dirtyCount() && !confirm("Discard unsaved edits?")) return;
  state.compare = null;
  state.current = id; state.editing = false; state.edits = {}; state.jsonEdits = {}; state.added = []; state.notice = null;
  renderFiles();
  try {
    state.data = await api("/api/read", { id });
  } catch (err) {
    state.data = null; state.notice = { warn: true, text: err.message };
  }
  render();
}

async function uploadOne(file) {
  try {
    const r = await fetch("/api/upload?name=" + encodeURIComponent(file.name), {
      method: "POST", headers: { "X-Token": TOKEN }, body: file,
    });
    const f = await r.json();
    if (!r.ok) throw new Error(f.error);
    return f;
  } catch (err) { alert(`Could not open ${file.name}: ${err.message}`); return null; }
}

async function uploadFiles(list) {
  let last = null;
  for (const file of list) {
    const f = await uploadOne(file);
    if (f) { addFile(f); last = f.id; }
  }
  if (!last) return;
  if (state.compare && list.length === 1) startCompare(last);
  else select(last);
}

$("#fileinput").addEventListener("change", (ev) => { uploadFiles([...ev.target.files]); ev.target.value = ""; });

$("#pathform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("#pathinput");
  if (!input.value.trim()) return;
  try {
    const f = await api("/api/open", { path: input.value });
    addFile(f); input.value = ""; select(f.id);
  } catch (err) { alert(err.message); }
});

let dragDepth = 0;
addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; document.body.classList.add("dragover"); });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragover"); } });
addEventListener("dragover", (e) => e.preventDefault());
addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; document.body.classList.remove("dragover");
  if (e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]);
});

// ---------- main view ----------

function render() {
  const main = $("#main");
  if (!state.data) {
    main.innerHTML = state.notice
      ? `<div class="wrap"><div class="notice warn">${esc(state.notice.text)}</div></div>`
      : `<div class="empty"><div><h1>Drop an image or video</h1><p>or paste a file path on the left</p></div></div>`;
    return;
  }
  const scroll = main.scrollTop;
  if (state.compare?.data) {
    main.innerHTML = renderCompare();
    main.scrollTop = scroll;
    return;
  }
  const d = state.data;
  const tags = d.tags;
  const ents = entries(tags);
  const secs = sectionize(ents);
  const sens = sensitiveRows(secs);

  const sub = [
    tags["Composite:ImageSize"] && String(tags["Composite:ImageSize"]).replace("x", "×"),
    tags["System:FileSize"], tags["File:FileType"], tags["QuickTime:Duration"],
  ].filter(Boolean).map(esc).join(" · ");

  const badge = sens.length
    ? `<div class="badge"><span class="dot"></span>${sens.length} sensitive field${sens.length > 1 ? "s" : ""}</div>`
    : `<div class="badge ok">No location or owner data</div>`;

  const notice = state.notice ? `
    <div class="notice ${state.notice.warn ? "warn" : ""}">${esc(state.notice.text)}
      ${state.notice.list?.length ? `<ul>${state.notice.list.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    </div>` : "";

  const actions = state.editing ? "" : `
    <div class="actions">
      <button class="btn" data-act="edit">Edit</button>
      <button class="btn" data-act="compare">Compare</button>
      <button class="btn" data-act="privacy" ${sens.length ? "" : "disabled"}>Strip privacy</button>
      <button class="btn danger" data-act="stripall">Strip all</button>
      <span class="spacer"></span>
      ${d.copy ? `<a class="btn primary" href="/api/download/${d.id}?t=${TOKEN}" style="text-decoration:none">Download copy</a>` : ""}
    </div>
    ${d.copy ? `<div class="copynote">Working copy. Your original is untouched — download to keep changes.</div>` : ""}`;

  main.innerHTML = `<div class="wrap">
    <div class="head">
      <img class="thumb" src="/api/preview/${d.id}?t=${TOKEN}&v=${d.version}" alt="" onerror="this.style.visibility='hidden'">
      <div class="title">
        <h1>${esc(d.name)}</h1>
        <div class="sub">${sub}</div>
        ${d.path ? `<div class="where">${esc(d.path.replace(/^\/Users\/[^/]+/, "~"))}</div>` : ""}
        ${badge}
      </div>
    </div>
    ${notice}
    ${actions}
    ${secs.filter((s) => s.rows.length || (state.editing && s.id === "other")).map((s) => renderSection(s, tags)).join("")}
    ${renderRaw(ents)}
  </div>
  ${state.editing ? renderEditBar() : ""}`;
  main.scrollTop = scroll;
}

function renderSection(s, tags) {
  const open = state.editing || (state.open[s.id] ?? s.open);
  const map = s.id === "location" ? mapLink(tags) : null;
  const rows = s.rows.map((e) => renderRow(e, s.sensitive)).join("");
  const add = state.editing && s.id === "other" ? renderAddRows() : "";
  return `<details class="sec" data-sec="${s.id}" ${open ? "open" : ""}>
    <summary>
      <span class="chev">▶</span><span class="stitle">${s.title}</span><span class="count">${s.rows.length}</span>
      ${s.sensitive ? `<span class="flag"><span class="dot"></span>sensitive</span>` : ""}
      ${map ? `<a class="maplink" href="${map}" target="_blank" rel="noopener">Open in Maps ↗</a>` : ""}
    </summary>
    <div class="rows">${rows}${add}</div>
  </details>`;
}

// ---------- JSON values (e.g. ComfyUI prompt / workflow stored in a PNG) ----------

const jsonCache = new Map();
const NUM = "\u0000num:";  // marks a number kept as its exact source text

// Numbers JS can't round-trip exactly (huge seeds, 8.0, 1e5) are kept as marked strings,
// so saving never rounds or reformats values the user didn't touch.
function protectNumbers(text) {
  const re = /"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
  return text.replace(re, (m) => (m[0] === '"' || String(Number(m)) === m ? m : JSON.stringify(NUM + m)));
}

function parseJSON(v) {
  if (typeof v !== "string" || !/^\s*[\[{]/.test(v)) return null;
  if (!jsonCache.has(v)) {
    let o = null;
    try { o = JSON.parse(protectNumbers(v)); } catch {}
    jsonCache.set(v, o && typeof o === "object" ? o : null);
  }
  return jsonCache.get(v);
}

// Python-style separators and \u escapes are matched to the original so the field keeps its shape.
function serialize(o, opts = {}) {
  const sep = opts.py ? ", " : ",", colon = opts.py ? ": " : ":";
  const str = (x) => {
    const j = JSON.stringify(x);
    return opts.ascii ? j.replace(/[\u007f-￿]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0")) : j;
  };
  const go = (v) => {
    if (v === null) return "null";
    if (Array.isArray(v)) return "[" + v.map(go).join(sep) + "]";
    if (typeof v === "object") return "{" + Object.entries(v).map(([k, x]) => str(k) + colon + go(x)).join(sep) + "}";
    if (typeof v === "string" && v.startsWith(NUM)) return v.slice(NUM.length);
    return typeof v === "string" ? str(v) : JSON.stringify(v);
  };
  return go(o);
}

const isObj = (x) => x && typeof x === "object" && !Array.isArray(x);
const isFlatArray = (v) => Array.isArray(v) && v.every((x) => x === null || typeof x !== "object");
const isNum = (v) => typeof v === "number" || (typeof v === "string" && v.startsWith(NUM));
const leafText = (v) => (typeof v === "string" && v.startsWith(NUM) ? v.slice(NUM.length) : String(v));

// Returns [[id, node, path], ...] when the JSON is a graph of nodes, else null.
function nodesOf(o) {
  if (!o) return null;
  if (Array.isArray(o.nodes) && o.nodes.every(isObj)) return o.nodes.map((n, i) => [leafText(n.id ?? ""), n, ["nodes", i]]);
  if (isObj(o)) {
    const vals = Object.values(o);
    if (vals.length > 1 && vals.every(isObj) && vals.some((n) => n.class_type || n.type)) {
      return Object.entries(o)
        .sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true }))
        .map(([k, n]) => [k, n, [k]]);
    }
  }
  return null;
}

// Flattens nested objects to [dotted label, value, path]; arrays of plain values stay one leaf.
function flatten(o, base = [], out = [], depth = 0, prefix = "") {
  const items = Array.isArray(o) ? o.map((v, i) => [i, v]) : Object.entries(o);
  for (const [k, v] of items) {
    const name = Array.isArray(o) ? String(k + 1) : k;
    const lab = prefix ? `${prefix}.${name}` : name;
    if (v && typeof v === "object" && depth < 3 && !isFlatArray(v)) flatten(v, [...base, k], out, depth + 1, lab);
    else out.push([lab, v, [...base, k]]);
  }
  return out;
}

function nodeRows(n, base) {
  if (isObj(n.inputs) && n.class_type) return flatten(n.inputs, [...base, "inputs"]);  // ComfyUI prompt
  if (n.widgets_values !== undefined) {                                                // ComfyUI workflow
    const w = n.widgets_values, wb = [...base, "widgets_values"];
    return Array.isArray(w) ? w.map((v, i) => [`Value ${i + 1}`, v, [...wb, i]]) : flatten(w, wb);
  }
  const { id, type, class_type, _meta, title, ...rest } = n;
  return flatten(rest, base);
}

const editableLeaf = (v) => v === null || typeof v !== "object";

function leaf(v, graph) {
  // [nodeId, outputIndex] in a ComfyUI prompt is a wire from another node.
  if (graph && Array.isArray(v) && v.length === 2 && isNum(v[1]) && graph.has(leafText(v[0]))) {
    return `<span class="ref">← #${esc(leafText(v[0]))} ${esc(graph.get(leafText(v[0])))}</span>`;
  }
  if (v === null) return `<span class="muted">null</span>`;
  if (isFlatArray(v)) return esc(v.map(leafText).join(", "));
  if (typeof v === "object") return esc(serialize(v));
  if (v === "") return `<span class="muted">Empty</span>`;
  return esc(leafText(v));
}

function leafInput(e, path, v, lab) {
  const text = v === null ? "null" : leafText(v);
  const pid = JSON.stringify(path);
  const attrs = `data-jkey="${esc(e.key)}" data-jpath="${esc(pid)}" data-jlabel="${esc(lab)}"`;
  if (typeof v === "string" && !isNum(v) && (v.length > 60 || v.includes("\n"))) {
    const rows = Math.min(10, Math.max(2, Math.ceil(v.length / 90) + v.split("\n").length - 1));
    return `<textarea ${attrs} rows="${rows}">${esc(text)}</textarea>`;
  }
  return `<input ${attrs} value="${esc(text)}">`;
}

function jsonRow(e, k, v, path, graph, lab) {
  const editing = state.editing && e.editable && editableLeaf(v);
  const changed = editing && state.jsonEdits[e.key]?.leaves.has(JSON.stringify(path));
  return `<div class="nrow ${changed ? "changed" : ""}"><div class="nk">${esc(k)}</div>
    <div class="nv">${editing ? leafInput(e, path, v, lab) : leaf(v, graph)}</div></div>`;
}

// UI-only widget fields (rgthree header, "➕ Add Lora" buttons) carry no data.
const isNoiseRow = (k, v) => /HeaderWidget/.test(k) || (v === "" && /^[^\w]/.test(k));

// rgthree Power Lora Loader keeps each LoRA as lora_N: {on, lora, strength}.
function loraLines(n) {
  if (!isObj(n.inputs)) return null;
  const items = Object.entries(n.inputs).filter(([k, v]) => /^lora_\d+$/.test(k) && isObj(v) && "lora" in v);
  if (!items.length) return null;
  return items.map(([key, v]) => ({
    key, on: v.on !== false, name: leafText(v.lora ?? ""),
    strength: v.strength !== undefined ? leafText(v.strength) : "",
  }));
}

function renderLoras(list) {
  return `<div class="loras">${list.map((l) => `
    <div class="lora ${l.on ? "" : "off"}"><span class="ldot"></span><span class="lname">${esc(l.name)}</span><span class="lstr">${esc(l.strength)}</span></div>`).join("")}</div>`;
}

function renderJSON(e, o) {
  const nodes = nodesOf(o);
  if (!nodes) {
    const rows = flatten(o);
    return `<div class="nodes"><div class="node">${rows.slice(0, 400).map(([k, v, p]) => jsonRow(e, k, v, p, null, k)).join("")}
      ${rows.length > 400 ? `<div class="nmore">${rows.length - 400} more in All raw tags</div>` : ""}</div></div>`;
  }
  const graph = new Map(nodes.map(([id, n]) => [id, n._meta?.title || n.title || n.class_type || n.type || ""]));
  const summary = !state.editing ? renderSummary(comfySummary(comfyGraph(o))) : "";
  return `${summary}<div class="nodes">${nodes.map(([id, n, base]) => {
    const type = n.class_type || n.type || "Node";
    const custom = n._meta?.title || n.title;
    const loras = state.editing ? null : loraLines(n);
    let rows = nodeRows(n, base);
    if (!state.editing) rows = rows.filter(([k, v]) => !isNoiseRow(k, v) && !(loras && /^lora_\d+\./.test(k)));
    const text = rows.find(([, v]) => typeof v === "string" && !isNum(v) && v.length > 20);
    const lone = rows.length === 1 && !loras && editableLeaf(rows[0][1]) ? leafText(rows[0][1]) : "";
    const preview = text ? text[1].slice(0, 140) : loras ? `${loras.filter((l) => l.on).length} of ${loras.length} on` : lone;
    const sec = `node:${e.key}:${id}`;
    const open = state.open[sec] ?? !!text;  // nodes holding prompt text start open
    return `<details class="node" data-sec="${esc(sec)}" ${open ? "open" : ""}>
      <summary><span class="nid">#${esc(id)}</span><span class="ntype">${esc(type)}</span>
        ${custom && custom !== type ? `<span class="ncustom">${esc(custom)}</span>` : ""}
        <span class="npreview ${lone && !text ? "lone" : ""}">${esc(preview)}</span></summary>
      ${rows.map(([k, v, p]) => jsonRow(e, k, v, p, graph, `#${id} ${type} › ${k}`)).join("")}
      ${loras ? renderLoras(loras) : ""}
    </details>`;
  }).join("")}</div>`;
}

// ---------- ComfyUI summary: follow wires from the sampler ----------

// API-format prompt ({id: {class_type, inputs}}); the UI workflow format has no wires to follow.
function comfyGraph(o) {
  if (!isObj(o)) return null;
  const vals = Object.values(o);
  return vals.length && vals.every((n) => isObj(n) && n.class_type && isObj(n.inputs)) ? o : null;
}

const isWire = (v, g) => Array.isArray(v) && v.length === 2 && isNum(v[1]) && g[leafText(v[0])] !== undefined;
const wireId = (v) => leafText(v[0]);

// Resolves a value through pass-through nodes (Primitive*, string concatenation).
// Anything computed elsewhere comes back as {ref: nodeId}.
function resolve(v, g, seen = new Set()) {
  if (!isWire(v, g)) return v;
  const id = wireId(v);
  if (seen.has(id)) return { ref: id };
  seen.add(id);
  const ins = g[id].inputs, type = g[id].class_type;
  const keys = Object.keys(ins);
  if (keys.length === 1 && /^(value|string|text|int|float|number|seed)$/.test(keys[0])) return resolve(ins[keys[0]], g, seen);
  if (typeof ins.expression === "string") {  // ComfyMathExpression: values.a, values.b, ...
    const vars = {};
    for (const [k, x] of Object.entries(ins)) {
      if (!k.startsWith("values.")) continue;
      const r = resolve(x, g, new Set(seen));
      if (isObj(r) || !isFinite(Number(leafText(r)))) return { ref: id };
      vars[k.slice(7)] = Number(leafText(r));
    }
    const out = evalMath(leafText(ins.expression), vars);
    return out === null ? { ref: id } : out;
  }
  if (/Concat/i.test(type) && "string_a" in ins && "string_b" in ins) {
    const a = resolve(ins.string_a, g, seen), b = resolve(ins.string_b, g, seen);
    if (!isObj(a) && !isObj(b)) return leafText(a) + leafText(ins.delimiter ?? "") + leafText(b);
  }
  return { ref: id };
}

// Arithmetic only: numbers, variables, + - * / // % **, parentheses and a few math functions.
// Never runs code; anything else returns null. Follows Python semantics, which ComfyUI uses:
// round() rounds halves to even, % and // floor toward negative infinity.
function evalMath(src, vars) {
  const fns = { max: Math.max, min: Math.min, abs: Math.abs, floor: Math.floor, ceil: Math.ceil, round: (x) => { const f = Math.floor(x), d = x - f; return d > 0.5 || (d === 0.5 && f % 2 !== 0) ? f + 1 : f; }, int: Math.trunc, sqrt: Math.sqrt };
  const toks = src.match(/\d+\.?\d*|\.\d+|[A-Za-z_]\w*|\*\*|\/\/|[-+*/%(),]|\S/g) || [];
  let i = 0;
  const peek = () => toks[i], take = () => toks[i++];
  const expect = (t) => { if (take() !== t) throw 0; };
  const atom = () => {
    const t = take();
    if (t === undefined) throw 0;
    if (t === "(") { const v = add(); expect(")"); return v; }
    if (t === "-") return -power();
    if (t === "+") return power();
    if (/^[\d.]/.test(t)) return Number(t);
    if (/^[A-Za-z_]/.test(t)) {
      if (peek() === "(") {
        if (!Object.hasOwn(fns, t)) throw 0;
        take(); const args = [];
        if (peek() !== ")") { args.push(add()); while (peek() === ",") { take(); args.push(add()); } }
        expect(")"); return fns[t](...args);
      }
      if (Object.hasOwn(vars, t)) return vars[t];
    }
    throw 0;
  };
  const power = () => { const b = atom(); return peek() === "**" ? (take(), b ** power()) : b; };
  const mul = () => {
    let v = power();
    while (["*", "/", "//", "%"].includes(peek())) {
      const op = take(), r = power();
      v = op === "*" ? v * r : op === "/" ? v / r : op === "//" ? Math.floor(v / r) : v - r * Math.floor(v / r);
    }
    return v;
  };
  const add = () => { let v = mul(); while (peek() === "+" || peek() === "-") v = take() === "+" ? v + mul() : v - mul(); return v; };
  try { const v = add(); return i === toks.length && isFinite(v) ? v : null; } catch { return null; }
}

function comfySummary(g) {
  if (!g) return null;
  const ids = Object.keys(g).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const isSampler = (id) => /KSampler(?!Select)|SamplerCustom/i.test(g[id].class_type);
  // Prefer the sampler that feeds the saved image; fall back to the first one.
  let sid = null;
  for (const id of ids) {
    if (!/Save|Preview/i.test(g[id].class_type) || !isWire(g[id].inputs.images, g)) continue;
    let cur = wireId(g[id].inputs.images);
    for (let hop = 0; hop < 6 && cur && !isSampler(cur); hop++) {
      const next = g[cur].inputs.samples ?? g[cur].inputs.latent ?? g[cur].inputs.image;
      cur = isWire(next, g) ? wireId(next) : null;
    }
    if (cur && isSampler(cur)) { sid = cur; break; }
  }
  sid = sid ?? ids.find(isSampler);
  if (!sid) return null;

  const S = g[sid].inputs;
  const via = (k) => (isWire(S[k], g) ? g[wireId(S[k])].inputs : {});
  const guider = via("guider");
  const sources = [S, via("sampler"), via("sigmas"), guider];
  const field = (...keys) => {
    for (const src of sources) for (const k of keys) if (k in src) return resolve(src[k], g);
    return undefined;
  };
  const textOf = (v) => {
    if (!isWire(v, g)) return undefined;
    const ins = g[wireId(v)].inputs;
    return "text" in ins ? resolve(ins.text, g) : { ref: wireId(v) };
  };

  // Model chain: walk "model" inputs down to the loader, collecting LoRAs on the way.
  const loras = [];
  let model, cur = S.model ?? guider.model ?? via("sigmas").model;
  for (let hop = 0; hop < 50 && isWire(cur, g); hop++) {
    const n = g[wireId(cur)], ins = n.inputs;
    const lines = loraLines(n);
    if (lines) loras.unshift(...lines.map((l) => ({ ...l, node: wireId(cur) })));
    else if ("lora_name" in ins) loras.unshift({ on: true, name: leafText(ins.lora_name), strength: leafText(ins.strength_model ?? ""), node: wireId(cur) });
    const base = ins.unet_name ?? ins.ckpt_name ?? ins.model_name;
    if (base !== undefined) { model = resolve(base, g); break; }
    cur = ins.model;
  }

  let size;
  if (isWire(S.latent_image, g)) {
    const L = g[wireId(S.latent_image)].inputs;
    if ("width" in L) {
      const w = resolve(L.width, g), h = resolve(L.height, g);
      size = isObj(w) || isObj(h) ? { ref: wireId(S.latent_image) } : `${leafText(w)} × ${leafText(h)}`;
    } else size = { ref: wireId(S.latent_image) };
  }

  return {
    graph: g,
    fields: [
      ["Model", model], ["Positive", textOf(S.positive ?? guider.positive)], ["Negative", textOf(S.negative ?? guider.negative)],
      ["Seed", field("seed", "noise_seed")], ["Steps", field("steps")], ["CFG", field("cfg")],
      ["Sampler", field("sampler_name")], ["Scheduler", field("scheduler")], ["Denoise", field("denoise")], ["Size", size],
    ].filter(([, v]) => v !== undefined),
    loras,
  };
}

function refLabel(id, g) {
  const n = g[id];
  return `← #${id} ${n?._meta?.title || n?.class_type || ""}`;
}

function summaryText(v, g) {
  return isObj(v) && v.ref !== undefined ? refLabel(v.ref, g) : leafText(v);
}

function summaryLoras(list) {
  const on = list.filter((l) => l.on), off = list.filter((l) => !l.on);
  return (on.length ? renderLoras(on) : `<span class="muted">None active</span>`) +
    (off.length ? `<details class="offloras"><summary>${off.length} off</summary>${renderLoras(off)}</details>` : "");
}

function renderSummary(sum) {
  if (!sum) return "";
  const val = (v) => (isObj(v) && v.ref !== undefined ? `<span class="ref">${esc(refLabel(v.ref, sum.graph))}</span>` : esc(leafText(v)));
  const rows = sum.fields.slice(0, 1).concat(sum.loras.length ? [["LoRAs", null]] : [], sum.fields.slice(1));
  return `<div class="gen">
    <div class="gen-title">Summary</div>
    ${rows.map(([k, v]) => `<div class="gen-row"><div class="gk">${k}${k === "LoRAs" ? ` <span class="muted">${sum.loras.filter((l) => l.on).length} of ${sum.loras.length} on</span>` : ""}</div>
      <div class="gv ${/Positive|Negative/.test(k) ? "prose" : ""}">${k === "LoRAs" ? summaryLoras(sum.loras) : val(v)}</div></div>`).join("")}
  </div>`;
}


// ---------- compare ----------

// Derived (Composite) and filesystem tags differ for every pair of files; they'd drown the real differences.
const CMP_SKIP = new Set(["System", "ExifTool", "Composite"]);

function cmpEntries(tags) {
  return new Map(entries(tags).filter((e) => !CMP_SKIP.has(e.group)).map((e) => [e.key, e]));
}

function sectionRank(e) {
  const i = SECTIONS.findIndex((s) => s.re.test(e.name) || s.alsoIf?.(e));
  return i * 1000 + (ORDER.indexOf(e.name) + 1 || 999);
}

function compareTags(ta, tb) {
  const A = cmpEntries(ta), B = cmpEntries(tb);
  const out = { changed: [], onlyA: [], onlyB: [], same: [] };
  for (const [k, ea] of A) {
    const eb = B.get(k);
    if (!eb) out.onlyA.push(ea);
    else if (rawString(ea.value) === rawString(eb.value)) out.same.push(ea);
    else out.changed.push([ea, eb]);
  }
  for (const [k, eb] of B) if (!A.has(k)) out.onlyB.push(eb);
  const by = (f) => (x, y) => sectionRank(f(x)) - sectionRank(f(y));
  out.changed.sort(by((x) => x[0])); out.onlyA.sort(by((x) => x)); out.onlyB.sort(by((x) => x)); out.same.sort(by((x) => x));
  return out;
}

// Word-level diff (LCS). Returns [["=", tok] | ["-", tok] | ["+", tok]] or null when too large.
function diffTokens(a, b) {
  const split = (s) => s.split(/(\s+|,)/).filter((t) => t !== "");
  const A = split(a), B = split(b), n = A.length, m = B.length;
  if (n * m > 4e6) return null;
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { out.push(["=", A[i]]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push(["-", A[i++]]);
    else out.push(["+", B[j++]]);
  }
  while (i < n) out.push(["-", A[i++]]);
  while (j < m) out.push(["+", B[j++]]);
  // Group each removed run before its added run so a replacement reads "old new".
  const grouped = [];
  for (let k = 0; k < out.length;) {
    if (out[k][0] === "=") { grouped.push(out[k++]); continue; }
    const dels = [], adds = [];
    while (k < out.length && (out[k][0] !== "=" || (/^\s+$/.test(out[k][1]) && out[k + 1] && out[k + 1][0] !== "="))) {
      const [op, t] = out[k++];
      if (op === "=") { dels.push(t); adds.push(t); } else (op === "-" ? dels : adds).push(t);
    }
    if (dels.join("").trim()) grouped.push(["-", dels.join("")]);
    if (adds.join("").trim()) grouped.push(["+", adds.join("")]);
  }
  return grouped;
}

function valText(v, g) {
  if (v === undefined) return undefined;
  if (v === null) return "null";
  if (g && isWire(v, g)) return refLabel(wireId(v), g);
  if (isObj(v) && v.ref !== undefined) return refLabel(v.ref, g);
  if (isFlatArray(v)) return v.map(leafText).join(", ");
  if (typeof v === "object") return serialize(v);
  return leafText(v);
}

function diffValue(ta, tb) {
  if (ta === undefined) return `<span class="muted">—</span> <span class="arrow">→</span> <ins>${esc(tb)}</ins>`;
  if (tb === undefined) return `<del>${esc(ta)}</del> <span class="arrow">→</span> <span class="muted">—</span>`;
  if (ta.length + tb.length > 40 || /\s/.test(ta + tb)) {
    const d = diffTokens(ta, tb);
    if (d) return `<div class="inl">${d.map(([op, t]) => (op === "=" ? esc(t) : op === "-" ? `<del>${esc(t)}</del>` : `<ins>${esc(t)}</ins>`)).join("")}</div>`;
  }
  return `<del>${esc(ta)}</del> <span class="arrow">→</span> <ins>${esc(tb)}</ins>`;
}

function graphDiff(oa, ob) {
  const ga = comfyGraph(oa), gb = comfyGraph(ob);
  const na = new Map(nodesOf(oa).map(([id, n]) => [id, n])), nb = new Map(nodesOf(ob).map(([id, n]) => [id, n]));
  const ids = [...new Set([...na.keys(), ...nb.keys()])].sort((x, y) => x.localeCompare(y, undefined, { numeric: true }));
  const out = [];
  for (const id of ids) {
    const a = na.get(id), b = nb.get(id);
    if (!b) { out.push({ id, kind: "onlyA", n: a }); continue; }
    if (!a) { out.push({ id, kind: "onlyB", n: b }); continue; }
    const ra = new Map(nodeRows(a, []).map(([k, v]) => [k, v])), rb = new Map(nodeRows(b, []).map(([k, v]) => [k, v]));
    const rows = [];
    const ta = a.class_type || a.type, tb = b.class_type || b.type;
    if (ta !== tb) rows.push(["node type", ta, tb]);
    for (const k of new Set([...ra.keys(), ...rb.keys()])) {
      const va = valText(ra.get(k), ga), vb = valText(rb.get(k), gb);
      if (va !== vb && !isNoiseRow(k, ra.get(k) ?? rb.get(k))) rows.push([k, va, vb]);
    }
    if (rows.length) out.push({ id, kind: "changed", n: a, rows });
  }
  return out;
}

function nodeTitle(id, n) {
  const type = n.class_type || n.type || "Node", custom = n._meta?.title || n.title;
  return `<span class="nid">#${esc(id)}</span><span class="ntype">${esc(type)}</span>${custom && custom !== type ? `<span class="ncustom">${esc(custom)}</span>` : ""}`;
}

function renderGraphDiff(diffs) {
  if (!diffs.length) return `<div class="nodes"><div class="nmore">Same nodes and values; only formatting differs.</div></div>`;
  return `<div class="nodes">${diffs.map((d) => `
    <div class="node cmpnode">
      <div class="nhead">${nodeTitle(d.id, d.n)}
        <span class="npreview">${d.kind === "onlyA" ? "only in A" : d.kind === "onlyB" ? "only in B" : `${d.rows.length} change${d.rows.length > 1 ? "s" : ""}`}</span></div>
      ${(d.rows || []).map(([k, a, b]) => `<div class="nrow"><div class="nk">${esc(k)}</div><div class="nv">${diffValue(a, b)}</div></div>`).join("")}
    </div>`).join("")}</div>`;
}

function loraState(l) { return !l ? "—" : l.on ? (l.strength || "on") : "off"; }

function renderSummaryDiff(sa, sb) {
  const fa = new Map(sa.fields), fb = new Map(sb.fields);
  const keys = [...new Set([...fa.keys(), ...fb.keys()])];
  const field = (k) => {
    const ta = fa.has(k) ? summaryText(fa.get(k), sa.graph) : undefined, tb = fb.has(k) ? summaryText(fb.get(k), sb.graph) : undefined;
    const same = ta === tb;
    return `<div class="gen-row ${same ? "same" : "diff"}"><div class="gk">${k}</div>
      <div class="gv ${/Positive|Negative/.test(k) ? "prose" : ""}">${same ? esc(ta) : diffValue(ta, tb)}</div></div>`;
  };
  // LoRAs: any LoRA active in either file, plus ones whose state changed.
  const la = new Map(sa.loras.map((l) => [l.name, l])), lb = new Map(sb.loras.map((l) => [l.name, l]));
  const names = [...new Set([...la.keys(), ...lb.keys()])].filter((n) => la.get(n)?.on || lb.get(n)?.on || loraState(la.get(n)) !== loraState(lb.get(n)));
  const loraRows = names.map((n) => {
    const a = loraState(la.get(n)), b = loraState(lb.get(n));
    return `<div class="lcmp ${a === b ? "" : "diff"}"><span class="lname">${esc(n)}</span><span class="${a === b ? "" : "va"}">${esc(a)}</span><span class="${a === b ? "" : "vb"}">${esc(b)}</span></div>`;
  }).join("");
  return `<div class="gen">
    ${keys.slice(0, 1).map(field).join("")}
    <div class="gen-row ${names.some((n) => loraState(la.get(n)) !== loraState(lb.get(n))) ? "diff" : "same"}"><div class="gk">LoRAs</div>
      <div class="gv">${loraRows ? `<div class="lcmp lhead"><span></span><span>A</span><span>B</span></div>${loraRows}` : `<span class="muted">None active</span>`}</div></div>
    ${keys.slice(1).map(field).join("")}
  </div>`;
}

function cmpHead(d, letter) {
  const sub = [d.tags["Composite:ImageSize"] && String(d.tags["Composite:ImageSize"]).replace("x", "×"), d.tags["System:FileSize"], d.tags["File:FileType"]].filter(Boolean).map(esc).join(" · ");
  return `<div class="cmp-file">
    <span class="ab ${letter.toLowerCase()}">${letter}</span>
    <img class="cthumb" src="/api/preview/${d.id}?t=${TOKEN}&v=${d.version}" alt="" onerror="this.style.visibility='hidden'">
    <div class="cinfo"><div class="cname" title="${esc(d.path || d.name)}">${esc(d.name)}</div><div class="sub">${sub}</div></div>
  </div>`;
}

function renderCompare() {
  const A = state.data, B = state.compare.data;
  const c = compareTags(A.tags, B.tags);
  const cmpRow = (ea, eb) => {
    const key = `<div class="k" title="${esc(ea.key)}"><span class="lab">${esc(label(ea.name))}</span><span class="grp">${esc(groupLabel(ea.group))}</span></div>`;
    const ja = parseJSON(ea.value), jb = parseJSON(eb.value);
    if (ja && jb && nodesOf(ja) && nodesOf(jb)) {
      const diffs = graphDiff(ja, jb);
      const sa = comfySummary(comfyGraph(ja)), sb = comfySummary(comfyGraph(jb));
      return `<div class="row jsonhead">${key}<div class="v"><span class="muted">${diffs.length} node${diffs.length === 1 ? "" : "s"} differ</span></div></div>
        ${sa && sb ? renderSummaryDiff(sa, sb) : ""}${renderGraphDiff(diffs)}`;
    }
    return `<div class="row">${key}<div class="v">${diffValue(valText(ea.value), valText(eb.value))}</div></div>`;
  };
  const plainRow = (e) => `<div class="row"><div class="k" title="${esc(e.key)}"><span class="lab">${esc(label(e.name))}</span><span class="grp">${esc(groupLabel(e.group))}</span></div><div class="v">${pretty(e)}</div></div>`;
  const sec = (id, title, count, body, open) => count ? `<details class="sec" data-sec="cmp-${id}" ${(state.open["cmp-" + id] ?? open) ? "open" : ""}>
      <summary><span class="chev">▶</span><span class="stitle">${title}</span><span class="count">${count}</span></summary>
      <div class="rows">${body}</div></details>` : "";
  const nothing = !c.changed.length && !c.onlyA.length && !c.onlyB.length;
  return `<div class="wrap">
    <div class="cmp-head">${cmpHead(A, "A")}${cmpHead(B, "B")}</div>
    <div class="actions">
      <button class="btn" data-act="swap">Swap A and B</button>
      <button class="btn" data-act="endcompare">Done</button>
      <span class="spacer"></span>
      <span class="legend"><del>in A</del> <ins>in B</ins></span>
    </div>
    ${nothing ? `<div class="notice">No metadata differences. ${c.same.length} fields are identical.</div>` : ""}
    ${sec("changed", "Changed", c.changed.length, c.changed.map(([a, b]) => cmpRow(a, b)).join(""), true)}
    ${sec("onlya", "Only in A", c.onlyA.length, c.onlyA.map(plainRow).join(""), true)}
    ${sec("onlyb", "Only in B", c.onlyB.length, c.onlyB.map(plainRow).join(""), true)}
    ${sec("same", "Same", c.same.length, c.same.map(plainRow).join(""), false)}
  </div>`;
}

async function openComparePicker() {
  const others = state.files.filter((f) => f.id !== state.current);
  const where = (f) => (f.copy ? "dropped copy" : (f.path || "").replace(/^\/Users\/[^/]+/, "~").replace(/\/[^/]*$/, ""));
  const dlg = $("#dialog");
  dlg.innerHTML = `
    <div class="dhead">Compare with</div>
    <div class="dbody">
      <p>A is <b>${esc(state.data.name)}</b>. Pick B.</p>
      ${others.length ? `<div class="picklist">${others.map((f) => `
        <button class="pick" data-pick="${f.id}"><span class="pname">${esc(f.name)}</span><span class="pwhere">${esc(where(f))}</span></button>`).join("")}</div>`
        : `<p class="muted">No other files open. Choose one from disk.</p>`}
    </div>
    <div class="dfoot">
      <label class="btn">Choose file…<input type="file" id="cmpfile" hidden></label>
      <span class="spacer"></span>
      <button class="btn" value="cancel">Cancel</button>
    </div>`;
  dlg.onclose = null;
  dlg.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = () => { dlg.close(); startCompare(b.dataset.pick); }));
  dlg.querySelector("button[value=cancel]").onclick = () => dlg.close();
  dlg.querySelector("#cmpfile").onchange = async (ev) => {
    const file = ev.target.files[0];
    dlg.close();
    const f = file && (await uploadOne(file));
    if (f) { addFile(f); startCompare(f.id); }
  };
  dlg.showModal();
}

async function startCompare(id) {
  try {
    state.compare = { id, data: await api("/api/read", { id }) };
  } catch (err) {
    state.notice = { warn: true, text: err.message };
  }
  renderFiles(); render();
  $("#main").scrollTop = 0;
}

// Edit state for one JSON field: a working clone plus the per-value changes for review.
function jsonEditFor(key) {
  if (!state.jsonEdits[key]) {
    const raw = rawString(state.data.tags[key]);
    const base = parseJSON(raw);
    const opts = { py: /"\s*:\s/.test(raw.slice(0, 2000)), ascii: /\\u[0-9a-fA-F]{4}/.test(raw) };
    state.jsonEdits[key] = { base, obj: structuredClone(base), opts, orig: serialize(base, opts), leaves: new Map() };
  }
  return state.jsonEdits[key];
}

const getAt = (o, path) => path.reduce((x, k) => x[k], o);
function setAt(o, path, v) { getAt(o, path.slice(0, -1))[path[path.length - 1]] = v; }

// Keeps the original value's type: numbers stay numbers, true/false stay booleans.
function typedValue(text, orig) {
  const t = text.trim();
  if (isNum(orig) && /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(t)) return String(Number(t)) === t ? Number(t) : NUM + t;
  if (typeof orig === "boolean" && (t === "true" || t === "false")) return t === "true";
  if (orig === null && t === "null") return null;
  return text;
}

function renderRow(e, sensitive) {
  const key = `<div class="k" title="${esc(e.key)}"><span class="lab">${esc(label(e.name))}</span><span class="grp">${esc(groupLabel(e.group))}</span></div>`;
  const json = parseJSON(e.value);
  if (json && (!state.editing || e.editable)) {
    const nodes = nodesOf(json);
    const summary = nodes ? `${nodes.length} nodes` : `${flatten(json).length} values`;
    if (!state.editing) {
      return `<div class="row jsonhead ${sensitive ? "sensitive" : ""}">${key}<div class="v"><span class="muted">${summary}</span></div></div>${renderJSON(e, json)}`;
    }
    const removed = state.edits[e.key] === null;
    return `<div class="row jsonhead ${sensitive ? "sensitive" : ""} ${removed ? "removed" : ""}">${key}
      <div class="v edit"><span class="muted" style="flex:1">${removed ? "Will be removed" : summary}</span>
        <button class="iconbtn" data-del="${esc(e.key)}" title="${removed ? "Keep" : "Remove field"}">${removed ? "↺" : "×"}</button>
      </div></div>${removed ? "" : renderJSON(e, state.jsonEdits[e.key]?.obj ?? json)}`;
  }
  if (!state.editing || !e.editable) {
    return `<div class="row ${sensitive ? "sensitive" : ""}">${key}<div class="v">${pretty(e)}</div></div>`;
  }
  const edited = e.key in state.edits;
  const removed = edited && state.edits[e.key] === null;
  const val = edited && !removed ? state.edits[e.key] : rawString(e.value);
  return `<div class="row ${sensitive ? "sensitive" : ""} ${edited ? "changed" : ""} ${removed ? "removed" : ""}">${key}
    <div class="v edit">
      <input data-key="${esc(e.key)}" value="${esc(val)}" ${removed ? "disabled" : ""}>
      <button class="iconbtn" data-del="${esc(e.key)}" title="${removed ? "Keep" : "Remove field"}">${removed ? "↺" : "×"}</button>
    </div></div>`;
}

function renderAddRows() {
  const rows = state.added.map((a, i) => `
    <div class="addrow">
      <input data-addkey="${i}" value="${esc(a.key)}" placeholder="Tag, e.g. XMP:Title or DateTimeOriginal">
      <input data-addval="${i}" value="${esc(a.value)}" placeholder="Value">
      <button class="iconbtn" data-addrm="${i}" title="Remove">×</button>
    </div>`).join("");
  return rows + `<div class="addrow"><span></span><button class="btn" data-act="addfield">+ Add field</button></div>`;
}

function renderRaw(ents) {
  const q = state.rawQuery.toLowerCase();
  const shown = ents.filter((e) => !q || e.key.toLowerCase().includes(q) || rawString(e.value).toLowerCase().includes(q));
  let lastGroup = null, body = "";
  for (const e of shown) {
    if (e.group !== lastGroup) { body += `<div class="rawgroup">${esc(e.group)}</div>`; lastGroup = e.group; }
    body += `<div class="rawrow"><div class="rk">${esc(e.name)}</div><div class="rv">${esc(rawString(e.value))}</div></div>`;
  }
  const open = state.open.raw ?? false;
  return `<details class="sec" data-sec="raw" ${open ? "open" : ""}>
    <summary><span class="chev">▶</span><span class="stitle">All raw tags</span><span class="count">${ents.length}</span></summary>
    <div class="rawtools"><input id="rawsearch" placeholder="Filter by tag or value" value="${esc(state.rawQuery)}"></div>
    <div>${body || `<div class="rawrow"><div class="rk">No matches</div></div>`}</div>
  </details>`;
}

function dirtyCount() {
  let n = 0;
  for (const [k, v] of Object.entries(state.edits)) n += v !== null && state.jsonEdits[k] ? state.jsonEdits[k].leaves.size : 1;
  return n + state.added.filter((a) => a.key.trim()).length;
}

function renderEditBar() {
  const n = dirtyCount();
  return `<div class="editbar">
    <span class="n">${n ? `${n} change${n > 1 ? "s" : ""}` : "No changes"}</span>
    <button class="btn" data-act="cancel">Cancel</button>
    <button class="btn primary" data-act="review" ${n ? "" : "disabled"}>Review & save</button>
  </div>`;
}

function updateEditBar() {
  const bar = $(".editbar");
  if (bar) bar.outerHTML = renderEditBar();
}

// ---------- interactions ----------

$("#main").addEventListener("toggle", (ev) => {
  const sec = ev.target.dataset?.sec;
  if (sec && (!state.editing || sec.startsWith("node:"))) state.open[sec] = ev.target.open;
}, true);

$("#main").addEventListener("input", (ev) => {
  const t = ev.target;
  if (t.id === "rawsearch") {
    state.rawQuery = t.value;
    const pos = t.selectionStart;
    render();
    const s = $("#rawsearch"); s.focus(); s.setSelectionRange(pos, pos);
    return;
  }
  if (t.dataset.jkey) {
    const key = t.dataset.jkey, pid = t.dataset.jpath, path = JSON.parse(pid);
    const je = jsonEditFor(key);
    const orig = getAt(je.base, path);
    setAt(je.obj, path, typedValue(t.value, orig));
    if (serialize(getAt(je.obj, path)) === serialize(orig)) je.leaves.delete(pid);
    else je.leaves.set(pid, { label: t.dataset.jlabel, old: orig === null ? "null" : leafText(orig), next: t.value });
    const out = serialize(je.obj, je.opts);
    if (out === je.orig) delete state.edits[key]; else state.edits[key] = out;
    t.closest(".nrow").classList.toggle("changed", je.leaves.has(pid));
    return updateEditBar();
  }
  if (t.dataset.key) {
    const original = rawString(state.data.tags[t.dataset.key]);
    if (t.value === original) delete state.edits[t.dataset.key];
    else state.edits[t.dataset.key] = t.value;
    t.closest(".row").classList.toggle("changed", t.dataset.key in state.edits);
    return updateEditBar();
  }
  if (t.dataset.addkey !== undefined) { state.added[+t.dataset.addkey].key = t.value; return updateEditBar(); }
  if (t.dataset.addval !== undefined) { state.added[+t.dataset.addval].value = t.value; return updateEditBar(); }
});

$("#main").addEventListener("click", (ev) => {
  const del = ev.target.closest("[data-del]");
  if (del) {
    const k = del.dataset.del;
    if (state.edits[k] === null) { delete state.edits[k]; delete state.jsonEdits[k]; } else state.edits[k] = null;
    return render();
  }
  const addrm = ev.target.closest("[data-addrm]");
  if (addrm) { state.added.splice(+addrm.dataset.addrm, 1); return render(); }

  const act = ev.target.closest("[data-act]")?.dataset.act;
  if (!act) return;
  if (act === "edit") { state.editing = true; state.notice = null; render(); }
  if (act === "cancel") { state.editing = false; state.edits = {}; state.jsonEdits = {}; state.added = []; render(); }
  if (act === "addfield") { state.added.push({ key: "", value: "" }); render(); $$last("[data-addkey]")?.focus(); }
  if (act === "review") reviewEdits();
  if (act === "compare") openComparePicker();
  if (act === "endcompare") { state.compare = null; renderFiles(); render(); }
  if (act === "swap") {
    const b = state.compare;
    state.compare = { id: state.current, data: state.data };
    state.current = b.id; state.data = b.data;
    renderFiles(); render();
  }
  if (act === "privacy") reviewPrivacy();
  if (act === "stripall") reviewStripAll();
});

function $$last(sel) { const all = document.querySelectorAll(sel); return all[all.length - 1]; }

addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.compare && !$("#dialog").open) { state.compare = null; renderFiles(); return render(); }
  if (e.key === "Escape" && state.editing && !$("#dialog").open) {
    state.editing = false; state.edits = {}; state.jsonEdits = {}; state.added = []; render();
  }
});

// ---------- review dialog ----------

function writeNote() {
  return state.data.copy
    ? "Applies to the working copy. Download afterwards to keep it."
    : "The original file is overwritten. A backup copy goes to the Trash first.";
}

function confirmDialog(title, intro, rows, confirmLabel, danger) {
  const dlg = $("#dialog");
  dlg.innerHTML = `
    <div class="dhead">${esc(title)}</div>
    <div class="dbody">
      <p>${intro}</p>
      ${rows.length ? `<table class="diff">${rows.map((r) => `
        <tr><td title="${esc(r.key)}">${esc(label(r.key.split(":").pop()))} <span class="grp">${esc(groupLabel(r.key.split(":")[0]))}</span>
          ${r.sub ? `<div>${esc(r.sub)}</div>` : ""}</td>
        <td>${r.old !== undefined ? `<div class="old">${esc(r.old)}</div>` : ""}
            ${r.next === null ? `<span class="gone">Removed</span>` : `<div>${esc(r.next)}</div>`}</td></tr>`).join("")}</table>` : ""}
    </div>
    <div class="dfoot">
      <button class="btn" value="cancel">Cancel</button>
      <button class="btn primary" value="ok" ${danger ? `style="background:var(--danger);border-color:var(--danger)"` : ""}>${esc(confirmLabel)}</button>
    </div>`;
  return new Promise((resolve) => {
    dlg.querySelectorAll(".dfoot button").forEach((b) => b.onclick = () => { dlg.close(); resolve(b.value === "ok"); });
    dlg.onclose = () => resolve(false);
    dlg.showModal();
  });
}

async function reviewEdits() {
  const set = { ...state.edits };
  for (const a of state.added) if (a.key.trim()) set[a.key.trim()] = a.value;
  const rows = [];
  for (const [key, next] of Object.entries(set)) {
    const je = state.jsonEdits[key];
    if (je && next !== null) for (const l of je.leaves.values()) rows.push({ key, sub: l.label, old: l.old, next: l.next });
    else rows.push({ key, next, old: key in state.data.tags ? rawString(state.data.tags[key]) : undefined });
  }
  const ok = await confirmDialog("Review changes", esc(writeNote()), rows, "Save");
  if (ok) run("/api/write", { id: state.data.id, set }, `Saved ${rows.length} change${rows.length > 1 ? "s" : ""}`);
}

async function reviewPrivacy() {
  const sens = sensitiveRows(sectionize(entries(state.data.tags))).filter((e) => e.editable);
  const set = Object.fromEntries(sens.map((e) => [e.key, null]));
  const rows = sens.map((e) => ({ key: e.key, old: rawString(e.value), next: null }));
  const ok = await confirmDialog("Strip privacy fields",
    `Removes location, serial numbers, owner and device names. ${esc(writeNote())}`, rows, "Remove " + rows.length);
  if (ok) run("/api/write", { id: state.data.id, set }, `Removed ${rows.length} sensitive field${rows.length > 1 ? "s" : ""}`);
}

async function reviewStripAll() {
  const n = entries(state.data.tags).filter((e) => !SYSTEM_GROUPS.has(e.group)).length;
  const ok = await confirmDialog("Strip all metadata",
    `Removes all ${n} metadata fields. Keeps orientation and colour profile so the image still displays correctly. ` +
    `Some video container fields (track dates, codec info) can't be removed and will remain. ${esc(writeNote())}`,
    [], "Strip all", true);
  if (ok) run("/api/strip", { id: state.data.id }, "Stripped metadata");
}

async function run(path, body, okText) {
  try {
    const r = await api(path, body);
    state.data = r; state.editing = false; state.edits = {}; state.jsonEdits = {}; state.added = [];
    const backup = r.updated && !r.copy ? " Backup copy is in the Trash." : "";
    state.notice = r.updated
      ? { text: okText + "." + backup, list: r.warnings, warn: false }
      : { text: "No changes were written.", list: r.warnings, warn: true };
    const f = state.files.find((x) => x.id === r.id); if (f) f.version = r.version;
  } catch (err) {
    state.notice = { warn: true, text: err.message };
  }
  render();
}

// ---------- boot ----------

// Only one meta tab at a time. A newer tab (from `meta <file>` in Terminal) announces
// itself; older tabs close. Browsers allow window.close() here because this tab has
// a single history entry, so the URL must never gain history (replaceState only).
const channel = new BroadcastChannel("meta");
channel.onmessage = (ev) => {
  if (ev.data !== "takeover") return;
  if (state.editing && dirtyCount()) {
    state.notice = { warn: true, text: "meta opened in a newer tab. Save or cancel your edits here, then close this tab." };
    return render();
  }
  window.close();
  // Still here: the browser refused to close it.
  setTimeout(() => {
    document.body.innerHTML = `<div class="empty" style="grid-column:1/-1"><div><h1>meta moved to a newer tab</h1><p>You can close this one.</p></div></div>`;
  }, 300);
};

// Heartbeat: an open tab keeps the server alive; with no tab for an hour it stops itself.
setInterval(() => {
  api("/api/ping", {}).then(() => $("#offline")?.remove()).catch(() => {
    if ($("#offline")) return;
    document.body.insertAdjacentHTML("beforeend",
      `<div id="offline" class="offline">meta has stopped. Run <code>meta</code> in Terminal to start it again.</div>`);
  });
}, 60000);

(async () => {
  channel.postMessage("takeover");
  const want = new URLSearchParams(location.search).get("select");
  if (want) history.replaceState(null, "", "/");
  state.files = await api("/api/files");
  renderFiles();
  const first = state.files.find((f) => f.id === want) || state.files[0];
  if (first) select(first.id); else render();
})();
</script>
</body>
</html>
"""


# ---------- main ----------

# Where a running instance leaves its port and token, so a second `meta` can find it.
# The per-user temp folder keeps it private and out of sight.
INSTANCE_FILE = os.path.join(tempfile.gettempdir(), "meta-instance.json")
SERVER_URL = ""


def write_instance(port):
    fd = os.open(INSTANCE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"port": port, "token": TOKEN, "pid": os.getpid()}, fh)


def clear_instance():
    try:
        with open(INSTANCE_FILE) as fh:
            if json.load(fh).get("pid") == os.getpid():
                os.remove(INSTANCE_FILE)
    except (OSError, ValueError):
        pass


IDLE_LIMIT = 3600          # seconds without any meta tab before the server stops itself
LOG_FILE = os.path.join(tempfile.gettempdir(), "meta.log")
LAST_SEEN = time.time()


def read_instance():
    try:
        with open(INSTANCE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def call_instance(inst, path, body):
    from urllib.request import Request, urlopen
    req = Request(f"http://127.0.0.1:{inst['port']}{path}", data=json.dumps(body).encode(),
                  headers={"X-Token": inst["token"], "Content-Type": "application/json"})
    with urlopen(req, timeout=3) as r:
        return json.load(r)


def handoff(paths):
    """Send files to an already-running meta. Returns False if none is running."""
    inst = read_instance()
    try:
        result = call_instance(inst, "/api/handoff", {"paths": paths}) if inst else None
    except (OSError, ValueError, KeyError):
        result = None  # stale file from one that crashed
    if result is None:
        return False
    for p in result.get("skipped", []):
        print(f"skipped (not a file): {p}")
    print("Opened in the running meta.")
    return True


def stop():
    inst = read_instance()
    try:
        call_instance(inst, "/api/ping", {})  # confirms the pid really is a live meta
        os.kill(inst["pid"], signal.SIGTERM)
    except (TypeError, OSError, ValueError, KeyError):
        print("meta isn't running.")
        return
    for _ in range(50):
        if read_instance() is None:
            break
        time.sleep(0.1)
    print("meta stopped.")


def launch_background(paths):
    """Start the server as a detached process and return once it's listening."""
    env = {**os.environ, "META_FOREGROUND": "1"}
    with open(LOG_FILE, "w") as log:
        child = subprocess.Popen([sys.executable, os.path.abspath(__file__), *paths], env=env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(100):
        inst = read_instance()
        if inst and inst.get("pid") == child.pid:
            print("meta is open in your browser.  (meta stop to quit)")
            return
        if child.poll() is not None:
            break
        time.sleep(0.1)
    print("meta didn't start. Log:", LOG_FILE)
    try:
        with open(LOG_FILE) as fh:
            print(fh.read()[-2000:])
    except OSError:
        pass
    sys.exit(1)


def idle_watchdog():
    while True:
        time.sleep(60)
        if time.time() - LAST_SEEN > IDLE_LIMIT:
            print("No meta tab for an hour; stopping.", flush=True)
            os.kill(os.getpid(), signal.SIGTERM)


def main():
    if not os.path.exists(EXIFTOOL):
        sys.exit("exiftool not found. Install it with: brew install exiftool")

    paths = sys.argv[1:]
    if paths == ["stop"]:
        return stop()
    if handoff([clean_path(p) for p in paths]):
        return
    if not os.environ.get("META_FOREGROUND"):
        return launch_background(paths)

    for arg in paths:
        path = clean_path(arg)
        if os.path.isfile(path):
            register(path, copy=False)
        else:
            print(f"skipped (not a file): {arg}")

    server = None
    for port in range(PORT, PORT + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not server:
        sys.exit("No free port found")

    global SERVER_URL
    SERVER_URL = f"http://127.0.0.1:{server.server_port}/"
    write_instance(server.server_port)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # run cleanup on kill too
    threading.Thread(target=idle_watchdog, daemon=True).start()
    print(f"meta running at {SERVER_URL}", flush=True)
    if not os.environ.get("META_NO_BROWSER"):
        webbrowser.open(SERVER_URL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        clear_instance()
        print("stopped", flush=True)


if __name__ == "__main__":
    main()
