from __future__ import annotations


def _relaunch_under_playwright_python():
    # 当前解释器缺 playwright 时，按 ABI 安全的方式进程替换到带 playwright 的 Python（C 扩展不能跨次版本注入）
    import importlib.util
    import os
    import shutil
    import subprocess
    import sys

    if getattr(sys, "frozen", False):
        return

    try:
        if importlib.util.find_spec("playwright") is not None:
            return
    except Exception:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    conda_active = os.environ.get("CONDA_PREFIX")
    candidates = [
        os.path.join(conda_active, "python.exe") if conda_active else None,
        os.path.join(here, "download", "Scripts", "python.exe"),
        shutil.which("python"),
        shutil.which("python3"),
    ]
    for py in candidates:
        if not py or not os.path.exists(py):
            continue
        try:

            test = (
                    "import sys; sys.path.insert(0, %r);"
                    "import playwright, yt_dlp, requests, collector_core, cookie_manager"
                    % here
            )
            r = subprocess.run([py, "-c", test], capture_output=True, text=True, timeout=30)
        except Exception:
            continue
        if r.returncode == 0:

            try:
                os.execv(py, [py, *sys.argv])
            except Exception:
                pass

            subprocess.Popen([py, *sys.argv], close_fds=True)
            os._exit(0)


_relaunch_under_playwright_python()

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from queue import Queue

_SSE_LOCK = threading.Lock()
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, urlunparse, quote

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _MEI = sys._MEIPASS

    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(_MEI, "ms-playwright"))
    _ff = os.path.join(_MEI, "ffmpeg")
    if os.path.isdir(_ff) and _ff not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _ff + os.pathsep + os.environ.get("PATH", "")

    os.environ["VLC_APP_ROOT"] = os.path.dirname(os.path.abspath(sys.executable))

import cookie_manager as cm
from collector_core import (
    HttpClient,
    BilibiliSearcher,
    KuaishouSearcher,
    ShareResolver,
    BrowserSearcher,
    VideoLink,
    PLATFORM_DOMAIN,
    BROWSER_SEARCH,
    build_markdown,
    validate_collection,
    DEFAULT_UA,
    resolve_xhs_media_url,
    download_url_to_file,
    netscape_to_header,
    resolve_xhs_links,
)

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    ROOT = sys._MEIPASS
    _DATA_ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
    _DATA_ROOT = ROOT
FRONTEND_DIR = os.path.join(ROOT, "frontend")
FRONTEND = os.path.join(FRONTEND_DIR, "index.html")
RESULTS_DIR = os.path.join(_DATA_ROOT, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

PLATFORMS = cm.PLATFORMS
STORE = cm.CookieStore()

DOWNLOAD_DIR = os.path.join(_DATA_ROOT, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COVER_DIR = os.path.join(DOWNLOAD_DIR, "cover")
os.makedirs(COVER_DIR, exist_ok=True)

HISTORY_FILE = os.path.join(DOWNLOAD_DIR, "history.jsonl")
HISTORY_LOCK = threading.Lock()


class DownloadHub:

    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self) -> Queue:
        q = Queue(maxsize=4000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def broadcast(self, event: str, data: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((event, data))
            except Exception:
                pass


HUB = DownloadHub()

DL_LOCK = threading.Lock()
DL_TASKS: dict = {}
DL_BATCHES: dict = {}


def _dl_task_key(task_id, index):
    return f"{task_id}:{str(index)}"


def _dl_register(task_key, info):
    with DL_LOCK:
        info = dict(info)
        info.setdefault("status", "queued")
        info["proc"] = None
        if "stop_event" not in info or info["stop_event"] is None:
            info["stop_event"] = threading.Event()
        DL_TASKS[task_key] = info
    return info


def _dl_set_status(task_key, status, extra=None):
    with DL_LOCK:
        t = DL_TASKS.get(task_key)
        if t is None:
            return
        t["status"] = status
        if extra:
            t.update(extra)
        _t = dict(t)

    HUB.broadcast("task-update", {"task_key": task_key, "status": status,
                                  "title": _t.get("title"), "url": _t.get("url"),
                                  "platform": _t.get("platform"), "task_id": _t.get("task_id"),
                                  "cover": _t.get("cover"), "cover_file": _t.get("cover_file")})


def _dl_terminate(task_key):
    with DL_LOCK:
        t = DL_TASKS.get(task_key)
        if t is None:
            return
        se = t.get("stop_event")
        if se:
            se.set()
        proc = t.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass


def _dl_unregister(task_key):
    with DL_LOCK:
        DL_TASKS.pop(task_key, None)


def _finish_stopped(emit, tag, task_key):
    emit("task-stopped", {"tag": tag, "task_key": task_key})


def _dl_emit_all(self, task_id, event, data):
    d = dict(data);
    d["task_id"] = task_id
    _sse_emit(self, event, d)
    HUB.broadcast(event, d)


def run_one_task(self, task_id, task_key):
    with DL_LOCK:
        t = DL_TASKS.get(task_key)
        if not t:
            return
        idx = t["index"];
        e = t["entry"];
        body = t["body"]
        fmt = t["fmt"];
        container = t["container"];
        audio_only = t["audio_only"]
        vcodec = t["vcodec"];
        height_cap = t["height_cap"];
        delogo = t["delogo"]
        stop_event = t["stop_event"]

    if stop_event.is_set():
        return
    url = (e.get("url") or "").strip()
    if not url:
        return
    platform, cookie_file, cookie_label = _pick_cookie(url, (e.get("platform") or "").strip())
    route_platform = platform or _platform_of_url(url)
    _dl_set_status(task_key, "downloading")

    def emit_all(event, data):
        _dl_emit_all(self, task_id, event, data)

    def emit(event, data):
        d = dict(data);
        d["index"] = idx;
        d["task_id"] = task_id
        if event == "done":
            pass
        _sse_emit(self, event, d)
        HUB.broadcast(event, d)

    cover = e.get("thumbnail") or e.get("cover") or ""
    meta = {"title": e.get("title") or "", "author": e.get("author") or e.get("uploader") or "",
            "likes": e.get("likes") or 0, "views": e.get("views") or 0,
            "duration": e.get("duration") or 0}
    if route_platform == "xiaohongshu":
        _run_xhs_download(emit, url, cookie_file, cookie_label, tag=str(idx),
                          profile_root=cm.PROFILE_DIR,
                          media_url=e.get("media_url") or "",
                          cover=cover, task_id=task_id,
                          title=meta["title"], author=meta["author"], likes=meta["likes"],
                          views=meta["views"], duration=meta["duration"],
                          task_key=task_key, stop_event=stop_event)
    elif route_platform == "bilibili":
        media_url = (e.get("media_url") or "").strip()
        if not media_url:
            _bv = _bili_bvid_of(url)
            if _bv:
                _mu = bili_api_durl(_bv)
                if _mu:
                    media_url = _mu
                    _m = bili_api_meta(_bv)
                    if _m:
                        if not meta["title"] and _m["title"]: meta["title"] = _m["title"]
                        if not meta["author"] and _m["author"]: meta["author"] = _m["author"]
                        if not meta["likes"] and _m["likes"]: meta["likes"] = _m["likes"]
                        if not meta["views"] and _m["views"]: meta["views"] = _m["views"]
                        if not meta["duration"] and _m["duration"]: meta["duration"] = _m["duration"]
                        if not cover and _m["pic"]: cover = _m["pic"]
        if media_url:
            _run_direct_download(emit, url, media_url, "bilibili", cookie_file, cookie_label,
                                 tag=str(idx), task_id=task_id, title=meta["title"],
                                 author=meta["author"], likes=meta["likes"], views=meta["views"],
                                 duration=meta["duration"], cover=cover, vcodec=vcodec,
                                 task_key=task_key, stop_event=stop_event)
        else:
            _run_single_download(emit, url, "bilibili", cookie_file, cookie_label,
                                 fmt, container, audio_only, tag=str(idx),
                                 task_id=task_id, title=meta["title"], author=meta["author"],
                                 likes=meta["likes"], cover=cover,
                                 views=meta["views"], duration=meta["duration"],
                                 delogo=bool((body or {}).get("bilibili_delogo")),
                                 playlist=bool(e.get("is_playlist")), vcodec=vcodec,
                                 height_cap=height_cap, task_key=task_key, stop_event=stop_event)
    elif route_platform in ("kuaishou", "douyin"):
        media_url = (e.get("media_url") or "").strip()
        rv = None
        if not media_url:
            rv = _resolve_media_via_browser(route_platform, url, cookie_file, cm.PROFILE_DIR)
            if rv:
                media_url = (rv.media_url or "").strip()
        if media_url:
            if rv:
                if not meta["title"] and rv.title: meta["title"] = rv.title
                if not meta["author"] and rv.author: meta["author"] = rv.author
                if not meta["likes"] and rv.likes: meta["likes"] = rv.likes
                if not meta["views"] and rv.views: meta["views"] = rv.views
                if not meta["duration"] and rv.duration: meta["duration"] = rv.duration
                if not cover and rv.cover: cover = rv.cover
            _run_direct_download(emit, url, media_url, route_platform, cookie_file, cookie_label,
                                 tag=str(idx), task_id=task_id, title=meta["title"],
                                 author=meta["author"], likes=meta["likes"], views=meta["views"],
                                 duration=meta["duration"], cover=cover, vcodec=vcodec,
                                 task_key=task_key, stop_event=stop_event)
        elif route_platform == "kuaishou":
            emit("error", {"message": "无法获取快手视频直链：yt-dlp 不支持快手，需浏览器登录态解析。"
                                      "请确认已配置快手登录态。", "tag": str(idx)})
        else:

            _run_single_download(emit, url, route_platform, cookie_file, cookie_label,
                                 fmt, container, audio_only, tag=str(idx),
                                 task_id=task_id, title=meta["title"], author=meta["author"],
                                 likes=meta["likes"], cover=cover,
                                 views=meta["views"], duration=meta["duration"],
                                 delogo=bool((body or {}).get("bilibili_delogo")),
                                 playlist=bool(e.get("is_playlist")), vcodec=vcodec,
                                 height_cap=height_cap, task_key=task_key, stop_event=stop_event)
    else:
        _run_single_download(emit, url, route_platform or platform, cookie_file, cookie_label,
                             fmt, container, audio_only, tag=str(idx),
                             task_id=task_id, title=meta["title"], author=meta["author"],
                             likes=meta["likes"], cover=cover,
                             views=meta["views"], duration=meta["duration"],
                             delogo=bool((body or {}).get("bilibili_delogo")),
                             playlist=bool(e.get("is_playlist")), vcodec=vcodec,
                             height_cap=height_cap, task_key=task_key, stop_event=stop_event)
    emit_all("entry-done", {"index": idx, "total": 0})


def _record_history(rec: dict) -> None:
    rec = dict(rec)
    rec.setdefault("id", uuid.uuid4().hex[:12])
    rec.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    rec.setdefault("status", "done")
    rec.setdefault("progress", 100 if rec.get("status") == "done" else 0)
    line = json.dumps(rec, ensure_ascii=False)

    key = _norm_url(rec.get("url", ""))
    try:
        with HISTORY_LOCK:
            existing = []
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if ln:
                            existing.append(ln)
            except FileNotFoundError:
                existing = []
            if key:
                kept = []
                replaced = False
                for ln in existing:
                    try:
                        o = json.loads(ln)
                    except Exception:
                        kept.append(ln)
                        continue
                    if (not replaced) and _norm_url(o.get("url", "")) == key:
                        replaced = True

                        rec["created_at"] = o.get("created_at") or rec.get("created_at")
                        continue
                    kept.append(ln)
                if replaced:
                    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                        f.write(("\n".join(kept) + "\n") if kept else "")
                        f.write(line + "\n")
                    return
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def _touch_history(url: str, platform: str = "") -> None:
    key = _norm_url(url)
    nid = _xhs_nid(url) if platform == "xiaohongshu" else ""
    if not key and not nid:
        return
    try:
        with HISTORY_LOCK:
            lines = []
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    lines = [ln.strip() for ln in f if ln.strip()]
            except FileNotFoundError:
                return
            kept = []
            touched = None
            for ln in lines:
                try:
                    o = json.loads(ln)
                except Exception:
                    kept.append(ln)
                    continue
                match = (key and _norm_url(o.get("url", "")) == key) or (
                        nid and platform == "xiaohongshu" and _xhs_nid(o.get("url", "")) == nid
                )
                if match and touched is None:
                    o["finished_at"] = datetime.now().isoformat(timespec="seconds")
                    touched = json.dumps(o, ensure_ascii=False)
                    continue
                kept.append(ln)
            if touched is not None:
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    f.write(("\n".join(kept) + "\n") if kept else "")
                    f.write(touched + "\n")
    except Exception:
        pass


def _load_history(limit: int = 600) -> list:
    out = []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    out.reverse()
    return out[:limit]


def _norm_url(u: str) -> str:
    if not u:
        return ""
    try:
        p = urlparse(u.strip())
        scheme = (p.scheme or "http").lower()
        host = p.netloc.lower()
        path = p.path.rstrip("/")
        return urlunparse((scheme, host, path, "", p.query, ""))
    except Exception:
        return u.strip().lower()


def _xhs_nid(u: str) -> str:
    m = re.search(r"(?:explore|discovery/item)/([0-9a-fA-F]{16,32})", u or "")
    return m.group(1).lower() if m else ""


def _find_existing(url: str, platform: str = None) -> dict | None:
    if not url:
        return None
    key = _norm_url(url)
    nid = _xhs_nid(url) if platform == "xiaohongshu" else ""
    if not key and not nid:
        return None
    for rec in _load_history(3000):
        if not rec.get("path") or not os.path.isfile(rec["path"]):
            continue

        if key and _norm_url(rec.get("url", "")) == key:
            if platform and rec.get("platform") and rec["platform"] != platform:
                continue
            return rec

        if nid and platform == "xiaohongshu":
            rnid = _xhs_nid(rec.get("url", ""))
            if rnid and rnid == nid:
                return rec
    return None


def _yt_platform_of(j: dict) -> str:
    ek = (j.get("extractor_key") or j.get("extractor") or "")
    ek = str(ek).split(".")[-1].lower()
    if "douyin" in ek:
        return "douyin"
    if "tiktok" in ek:
        return "tiktok"
    return ek


def _yt_author(j: dict, platform: str = "") -> str:
    if not isinstance(j, dict):
        return ""
    if not platform:
        platform = _yt_platform_of(j)
    if platform in ("douyin", "tiktok"):
        return (j.get("channel") or j.get("uploader") or j.get("creator")
                or j.get("uploader_id") or "")
    return j.get("uploader") or j.get("channel") or j.get("creator") or ""


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _author_looks_like_handle(author: str) -> bool:
    a = (author or "").strip()
    if not a:
        return True
    if _CJK_RE.search(a):
        return False
    return True


def _douyin_real_author(url: str, cookie_file: str = "", timeout: int = 20) -> str:
    try:
        res: dict = {}
        stop = threading.Event()

        def _r() -> None:
            try:
                rv = _resolve_media_via_browser("douyin", url, cookie_file, cm.PROFILE_DIR)
                if rv and getattr(rv, "author", ""):
                    res["a"] = rv.author
            except Exception:
                pass

        t = threading.Thread(target=_r, daemon=True)
        t.start()
        t.join(timeout)
        return res.get("a", "")
    except Exception:
        return ""


def _read_ytdlp_info_json(media_path: str) -> dict:
    info: dict = {"title": "", "author": "", "likes": 0, "views": 0, "duration": 0, "thumbnail": ""}
    if not media_path or not os.path.isfile(media_path):
        return info
    fp = os.path.splitext(media_path)[0] + ".info.json"
    if not os.path.isfile(fp):
        d = os.path.dirname(media_path) or DOWNLOAD_DIR
        cands = []
        try:
            for fn in os.listdir(d):
                if fn.endswith(".info.json"):
                    p = os.path.join(d, fn)
                    try:
                        if os.path.getmtime(p) > time.time() - 180:
                            cands.append(p)
                    except OSError:
                        pass
        except OSError:
            pass
        cands.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        fp = cands[0] if cands else None
    if not fp or not os.path.isfile(fp):
        return info
    try:
        with open(fp, "r", encoding="utf-8") as f:
            j = json.load(f)
        info["title"] = j.get("title") or ""
        info["author"] = _yt_author(j)
        info["likes"] = int(j.get("like_count") or j.get("track_likes") or 0)
        info["views"] = int(j.get("view_count") or 0)
        info["duration"] = int(j.get("duration") or 0)
        info["thumbnail"] = j.get("thumbnail") or ""
    except Exception:
        return info
    finally:
        try:
            os.remove(fp)
        except OSError:
            pass
    return info


PROG_TAG = "@@PROG@@"
PP_TAG = "@@PP@@"

_PY_BASE = os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
VENV_PY = os.path.join(_PY_BASE, "envs", "default", "Scripts", "python.exe")
_DOWNLOAD_VENV_PY = os.path.join(ROOT, "download", "Scripts", "python.exe")
_YTDLP_CANDIDATES = [sys.executable, _DOWNLOAD_VENV_PY, VENV_PY]


def resolve_ytdlp_cmd():
    for exe in _YTDLP_CANDIDATES:
        if exe and os.path.isfile(exe):
            try:
                out = subprocess.run([exe, "-c", "import yt_dlp; print(yt_dlp.version.__version__)"],
                                     capture_output=True, text=True, timeout=20)
                if out.returncode == 0 and out.stdout.strip():
                    return [exe, "-m", "yt_dlp"], out.stdout.strip()
            except Exception:
                continue
    return None, None


def _ver_stamp(v: str | None) -> str | None:
    if not v:
        return None
    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", v)
    return m.group(0) if m else v


def fetch_latest_ytdlp_version() -> str | None:
    import urllib.request
    import urllib.error
    sources = [
        ("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest", "github"),
        ("https://pypi.org/pypi/yt-dlp/json", "pypi"),
    ]
    for url, kind in sources:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "video-link-collector"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            if kind == "github":
                tag = (data or {}).get("tag_name")
                if tag:
                    return tag.lstrip("vV")
            else:
                ver = ((data or {}).get("info") or {}).get("version")
                if ver:
                    return ver
        except Exception:
            continue
    return None


def handle_ytdlp_update(action: str) -> dict:
    cmd, cur = resolve_ytdlp_cmd()
    latest = fetch_latest_ytdlp_version()
    base = {"available": bool(cmd), "current": cur, "latest": latest}
    if action == "check":
        base["ok"] = True
        base["up_to_date"] = bool(
            _ver_stamp(cur) and _ver_stamp(latest) and _ver_stamp(cur) == _ver_stamp(latest))
        base["error"] = None if cmd else "未检测到 yt-dlp"
        return base

    if not cmd:
        return {"ok": False, "available": False, "current": None, "latest": latest,
                "updated": False, "error": "未检测到 yt-dlp，无法自动更新；请先 pip install yt-dlp 后重启服务。"}
    exe = cmd[0]
    try:
        proc = subprocess.run([exe, "-m", "pip", "install", "-U", "yt-dlp"],
                              capture_output=True, text=True, timeout=300,
                              env={**os.environ, "PYTHONUTF8": "1"})
    except Exception as e:
        return {"ok": False, "current": cur, "latest": latest, "updated": False,
                "output": "", "error": f"更新命令执行失败：{e}"}
    _, new_ver = resolve_ytdlp_cmd()
    out = (proc.stdout or "") + (proc.stderr or "")
    return {
        "ok": True,
        "previous": cur,
        "current": new_ver,
        "latest": latest,
        "updated": (new_ver != cur),
        "up_to_date": bool(_ver_stamp(new_ver) and _ver_stamp(latest)
                           and _ver_stamp(new_ver) == _ver_stamp(latest)),
        "output": out[-3000:],
        "error": None if proc.returncode == 0 else "pip 返回非零退出码（详见 output）",
    }


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return os.path.dirname(exe)

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        _ff = os.path.join(sys._MEIPASS, "ffmpeg")
        if os.path.isdir(_ff) and os.path.isfile(os.path.join(_ff, "ffmpeg.exe")):
            return _ff
        if os.path.isfile(_ff):
            return os.path.dirname(_ff)
    for d in ("C:/ffmpeg/bin", "C:/Program Files/ffmpeg/bin"):
        if os.path.isfile(os.path.join(d, "ffmpeg.exe")):
            return d

    import glob as _gl
    hits = _gl.glob(os.path.join(ROOT, "download", "Lib", "site-packages",
                                 "imageio_ffmpeg", "binaries", "ffmpeg*.exe"))
    if hits:
        return os.path.dirname(hits[0])
    return None


def _sanitize_filename(name: str, maxlen: int = 120) -> str:
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(". ")
    if not name:
        name = "video"
    if len(name) > maxlen:
        name = name[:maxlen].rstrip()
    return name


def _dur_tag(sec) -> str:
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        sec = 0
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}：{m:02d}：{s:02d}" if h else f"{m:02d}：{s:02d}"


def _final_name(title: str, author: str, duration, ext: str) -> str:
    bt = _sanitize_filename((title or "").strip(), 90)
    ap = _sanitize_filename((author or "").strip(), 40) if (author or "").strip() else ""
    core = f"{bt} - {ap}" if ap else bt
    dur = _dur_tag(duration)
    return f"{core}{(' [' + dur + ']') if dur else ''}.{(ext or 'mp4').lstrip('.')}"


def _unique_path(fname: str) -> str:
    cand = os.path.join(DOWNLOAD_DIR, fname)
    if not os.path.exists(cand):
        return cand
    base, ext = os.path.splitext(fname)
    i = 2
    while True:
        cand = os.path.join(DOWNLOAD_DIR, f"{base} ({i}){ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def _resolve_downloaded_file(title: str, author: str, ddir: str) -> str:
    if not ddir or not os.path.isdir(ddir):
        return ""
    _exts = (".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi",
             ".mp3", ".m4a", ".ogg", ".wav", ".m4v", ".ts")
    try:
        _all = [f for f in os.listdir(ddir) if f.lower().endswith(_exts)]
    except Exception:
        return ""
    if not _all:
        return ""
    _title = (title or "").strip()
    _author = (author or "").strip()
    if _title:
        for _fn in _all:
            if _title in _fn and (_author == "" or _author in _fn):
                return os.path.join(ddir, _fn)

    _now = datetime.now()
    _recent = sorted(
        (f for f in _all
         if (_now - datetime.fromtimestamp(os.path.getmtime(os.path.join(ddir, f)))).total_seconds() <= 120),
        key=lambda f: os.path.getmtime(os.path.join(ddir, f)), reverse=True)
    for _fn in _recent:
        return os.path.join(ddir, _fn)
    return ""


def _cover_relpath(title: str, author: str) -> str:
    t = (title or "").strip()
    a = (author or "").strip()
    if t and a:
        return _sanitize_filename(f"{t} - {a}", 130)
    return _sanitize_filename(t or a or "video", 90)


def _save_cover(cover_url: str = "", src_local: str = "", title: str = "",
                author: str = "", referer: str = "") -> str:
    try:
        os.makedirs(COVER_DIR, exist_ok=True)
        base = _cover_relpath(title, author)
        ext = ".jpg"

        if src_local and os.path.isfile(src_local):
            try:
                with open(src_local, "rb") as f:
                    head = f.read(8)
                if head[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = ".png"
                elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                    ext = ".webp"
                elif head[:3] == b"GIF":
                    ext = ".gif"
            except Exception:
                pass
        elif cover_url and str(cover_url).startswith("http"):
            _sfx = os.path.splitext(urlparse(str(cover_url)).path)[1].lower()
            if _sfx in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = _sfx

        def _dst_for(b, e):
            return os.path.join(COVER_DIR, b + e)

        dst = _dst_for(base, ext)
        if os.path.exists(dst):
            _i = 2
            while _i <= 9999:
                _cand = _dst_for(f"{base} ({_i})", ext)
                if not os.path.exists(_cand):
                    dst = _cand
                    break
                _i += 1
        if src_local and os.path.isfile(src_local):

            try:
                shutil.move(src_local, dst)
            except Exception:
                try:
                    shutil.copyfile(src_local, dst)
                    os.remove(src_local)
                except Exception:
                    return ""
            return os.path.relpath(dst, DOWNLOAD_DIR).replace(os.sep, "/")
        elif cover_url and str(cover_url).startswith("http"):

            if not os.path.exists(dst) or os.path.getsize(dst) == 0:
                download_url_to_file(str(cover_url), dst, referer=referer)
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return os.path.relpath(dst, DOWNLOAD_DIR).replace(os.sep, "/")
    except Exception:
        return ""
    return ""


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        h = (urlparse(url).netloc or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _platform_of_url(url: str) -> str:
    host = _host_of(url)
    if not host:
        return ""
    for plat, meta in cm.PLATFORM_META.items():
        dom = (meta.get("domain") or "").lstrip(".")
        if dom and (host == dom or host.endswith("." + dom)):
            return plat
    return ""


def _cookies_match_host(rec, host: str) -> bool:
    if not host:
        return False
    for m in rec.meta.values():
        d = (m.get("domain") or "").lstrip(".").lower()
        if d and (host == d or host.endswith("." + d) or d.endswith(host)):
            return True
    return False


def _pick_cookie(url: str, platform: str):
    if platform and STORE.load(platform):
        return platform, STORE.netscape_path(platform), cm.PLATFORM_META.get(platform, {}).get("label", platform)
    for p in cm.PLATFORMS:
        dom = cm.PLATFORM_META.get(p, {}).get("domain", "")
        if dom and dom.lstrip(".") in url and STORE.load(p):
            return p, STORE.netscape_path(p), cm.PLATFORM_META.get(p, {}).get("label", p)
    host = _host_of(url)
    if host:
        try:
            for fn in os.listdir(cm.COOKIE_DIR):
                if not fn.endswith(".json") or fn[:-5] in cm.PLATFORMS:
                    continue
                rec = STORE.load(fn[:-5])
                if rec and _cookies_match_host(rec, host):
                    return fn[:-5], STORE.netscape_path(fn[:-5]), fn[:-5]
        except OSError:
            pass
    return platform, None, "无（匿名）"


def _num(v, nd: int = 0):
    try:
        if v is None:
            return 0
        return round(float(v), nd) if nd else int(round(float(v)))
    except Exception:
        return 0


def _norm_format(f: dict) -> dict:
    vc = (f.get("vcodec") or "none").split(".")[0].strip()
    ac = (f.get("acodec") or "none").split(".")[0].strip()
    exact = f.get("filesize")
    size = exact or f.get("filesize_approx") or 0
    dr = (f.get("dynamic_range") or "").strip()
    return {
        "format_id": str(f.get("format_id") or ""),
        "ext": (f.get("ext") or "").strip(),
        "height": _num(f.get("height")),
        "width": _num(f.get("width")),
        "fps": _num(f.get("fps")),
        "vcodec": "" if vc in ("none", "", "?") else vc,
        "acodec": "" if ac in ("none", "", "?") else ac,
        "tbr": _num(f.get("tbr")),
        "vbr": _num(f.get("vbr")),
        "abr": _num(f.get("abr")),
        "asr": _num(f.get("asr")),
        "filesize": _num(size),
        "approx": bool(not exact and size),
        "note": (f.get("format_note") or "").strip(),
        "hdr": "" if dr in ("SDR", "") else dr,
        "lang": (f.get("language") or "").strip(),
        "proto": (f.get("protocol") or "").split("_")[0],
    }


def _split_formats(raw_formats: list) -> dict:
    video, audio, muxed = [], [], []
    for f in raw_formats or []:
        if not f.get("format_id"):
            continue

        if (f.get("ext") or "") in ("mhtml", "none"):
            continue
        item = _norm_format(f)
        has_v, has_a = bool(item["vcodec"]), bool(item["acodec"])
        if has_v and has_a:
            muxed.append(item)
        elif has_v:
            video.append(item)
        elif has_a:
            audio.append(item)
    video.sort(key=lambda x: (x["height"], x["fps"], x["tbr"], x["filesize"]), reverse=True)
    audio.sort(key=lambda x: (x["abr"] or x["tbr"], x["asr"], x["filesize"]), reverse=True)
    muxed.sort(key=lambda x: (x["height"], x["tbr"], x["filesize"]), reverse=True)
    return {"video": video, "audio": audio, "muxed": muxed}


def download_info(body: dict) -> dict:
    url = (body.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "请提供视频链接"}
    platform, cookie_file, cookie_label = _pick_cookie(url, (body.get("platform") or "").strip())

    if platform == "xiaohongshu":
        xck = cookie_file or (STORE.netscape_path("xiaohongshu") if STORE.load("xiaohongshu") else "")
        resolved = resolve_xhs_links([url], profile_root=cm.PROFILE_DIR, cookie_file=xck)
        r = resolved[0] if resolved else {}
        if not r.get("title") and not r.get("media_url"):
            return {"ok": False, "error": "小红书解析失败（可能登录态失效或链接无效，请在 Cookie 面板重新登录）"}
        return {
            "ok": True, "platform": "xiaohongshu", "cookie": cookie_label,
            "title": r.get("title") or "(无标题)", "duration": r.get("duration") or 0,
            "thumbnail": r.get("cover") or "", "webpage_url": url,
            "uploader": r.get("author") or "", "extractor": "xiaohongshu",
            "ext": "mp4", "video": [], "audio": [], "muxed": [], "count": 0,
            "recommend": {}, "ffmpeg": False,
        }
    cmd, _ = resolve_ytdlp_cmd()
    if not cmd:
        return {"ok": False, "error": "未检测到 yt-dlp，请先安装：pip install yt-dlp"}
    args = list(cmd) + ["-J", "--no-playlist", "--no-warnings", "--skip-download"]
    ffmpeg_dir = find_ffmpeg()
    if ffmpeg_dir:
        args += ["--ffmpeg-location", ffmpeg_dir]
    args.append(url)
    with _cookie_args(cookie_file) as _ck:
        try:
            out = subprocess.run(args + _ck, capture_output=True, text=True, timeout=90,
                                 encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "解析超时（90s），请稍后重试"}
        except Exception as e:
            return {"ok": False, "error": f"yt-dlp 执行失败：{e}"}
    if out.returncode != 0:

        _bvid = _bili_bvid_of(url) if (("bilibili.com" in url) or ("b23.tv" in url) or platform == "bilibili") else None
        if _bvid:
            _meta = bili_api_meta(_bvid)
            if _meta and _meta.get("title"):
                _fmts = bili_api_formats(_bvid, _meta.get("cid"))
                if not _fmts:
                    _fmts = bili_api_formats(_bvid, _meta.get("cid"), force=True)
                _video = (_fmts or {}).get("video") or []
                _audio = (_fmts or {}).get("audio") or []
                return {
                    "ok": True, "platform": "bilibili", "cookie": cookie_label,
                    "title": _meta["title"], "duration": _meta["duration"],
                    "thumbnail": _meta["pic"], "webpage_url": url,
                    "uploader": _meta["author"], "extractor": "BiliBili",
                    "ext": "mp4", "video": _video, "audio": _audio, "muxed": [],
                    "count": len(_video) + len(_audio),
                    "recommend": {}, "ffmpeg": bool(find_ffmpeg()),
                    "media_url": "", "media_unavailable": not _video and not _audio,
                    "note": ("B 站视频信息已通过官方 API 获取（绕开服务器 IP 风控 412），"
                             "已列出全部音视频轨，可在下方逐轨精选后下载。" if (_video or _audio) else
                             "已获取视频信息，但直链获取失败，可稍后重试或检查链接。"),
                }
        if _is_douyin_url(url):
            _rv = _resolve_media_via_browser("douyin", url, cookie_file, cm.PROFILE_DIR)
            _mu = (_rv.media_url or "") if _rv else ""
            if _rv and _mu and "ies-music" not in _mu and (".mp4" in _mu or "douyinvod" in _mu):
                return {
                    "ok": True, "platform": "douyin", "cookie": cookie_label,
                    "title": _rv.title or "(无标题)", "duration": int(_rv.duration or 0),
                    "thumbnail": _rv.cover or "", "webpage_url": url,
                    "uploader": _rv.author or "", "extractor": "Douyin",
                    "ext": "mp4", "video": [], "audio": [], "muxed": [], "count": 1,
                    "recommend": {}, "ffmpeg": bool(ffmpeg_dir),
                    "media_url": _mu, "media_unavailable": False,
                    "note": "抖音视频直链已通过浏览器通道获取（绕开失效的 yt-dlp）。点击「开始下载」即直连保存。",
                }

            if _rv:
                _note = "该抖音内容为图文（无视频可下载），浏览器仅抓到背景音乐，无法作为视频下载。"
            else:
                _note = DOUYIN_ANTIBOT_NOTE
            return {
                "ok": True, "platform": "douyin", "cookie": cookie_label,
                "title": ((_rv.title if _rv else "") or "(无标题)"),
                "duration": int((_rv.duration if _rv else 0) or 0),
                "thumbnail": ((_rv.cover if _rv else "") or ""), "webpage_url": url,
                "uploader": ((_rv.author if _rv else "") or ""), "extractor": "Douyin",
                "ext": "mp4", "video": [], "audio": [], "muxed": [], "count": 0,
                "recommend": {}, "ffmpeg": bool(ffmpeg_dir),
                "media_unavailable": True,
                "note": _note,
            }
        return {"ok": False, "error": (out.stderr or out.stdout or "未知错误").strip()[:600]}
    try:
        j = json.loads(out.stdout)
    except Exception:
        return {"ok": False, "error": "无法解析视频信息 JSON"}

    tracks = _split_formats(j.get("formats") or [])
    rec_v = tracks["video"][0]["format_id"] if tracks["video"] else ""
    rec_a = tracks["audio"][0]["format_id"] if tracks["audio"] else ""
    rec_m = tracks["muxed"][0]["format_id"] if tracks["muxed"] else ""
    _uploader = _yt_author(j)

    if platform == "douyin" and _author_looks_like_handle(_uploader):
        _real = _douyin_real_author(url, cookie_file)
        if _real:
            _uploader = _real
    info = {
        "ok": True, "platform": platform, "cookie": cookie_label,
        "title": j.get("title"), "duration": j.get("duration"),
        "thumbnail": j.get("thumbnail"), "webpage_url": j.get("webpage_url"),
        "uploader": _uploader,
        "extractor": j.get("extractor_key") or j.get("extractor") or "",
        "ext": j.get("ext"),
        "likes": j.get("like_count") or j.get("track_likes") or 0,
        "video": tracks["video"], "audio": tracks["audio"], "muxed": tracks["muxed"],
        "count": len(tracks["video"]) + len(tracks["audio"]) + len(tracks["muxed"]),
        "recommend": {"video": rec_v, "audio": rec_a, "muxed": rec_m},
        "ffmpeg": bool(ffmpeg_dir),
    }

    meta = _collect_meta_timed(url, platform, cookie_file, cm.PROFILE_DIR, 25)
    if meta:
        info["title"] = meta.get("title") or info["title"]
        info["uploader"] = meta.get("author") or info["uploader"]
        info["thumbnail"] = meta.get("cover") or info["thumbnail"]
        info["likes"] = meta.get("likes") or info["likes"]
        info["duration"] = meta.get("duration") or info["duration"]
        info["views"] = meta.get("views")
        info["meta_source"] = meta.get("source", "collector")
    return info


def collect_meta_for(url: str, platform: str, cookie_file: str = "", profile_root: str = "") -> dict | None:
    try:
        if platform == "bilibili":

            m = re.search(r"(BV[0-9A-Za-z]+|av\d+)", url, re.IGNORECASE)
            if not m:
                return None
            bvid = m.group(1)
            client = HttpClient(delay=0.5)
            for plat, header in cm.load_all_headers(STORE).items():
                client.set_domain_cookies(PLATFORM_DOMAIN[plat], header)
            it = VideoLink(platform="bilibili", keyword="", title="",
                           url=f"https://www.bilibili.com/video/{bvid}")
            BilibiliSearcher(client).enrich([it])
            if not it.title and not it.author:
                return None
            return {"title": it.title, "author": it.author, "likes": it.likes,
                    "views": it.views, "duration": it.duration, "cover": it.cover,
                    "platform": "bilibili", "source": "collector"}
        if platform == "xiaohongshu":
            resolved = resolve_xhs_links([url], profile_root=profile_root or cm.PROFILE_DIR,
                                         cookie_file=cookie_file)
            r = resolved[0] if resolved else {}
            if not r.get("title") and not r.get("author"):
                return None
            return {"title": r.get("title", ""), "author": r.get("author", ""),
                    "likes": int(r.get("likes", 0) or 0), "views": int(r.get("views", 0) or 0),
                    "duration": int(r.get("duration", 0) or 0), "cover": r.get("cover", ""),
                    "platform": "xiaohongshu", "source": "collector",
                    "media_url": r.get("media_url", "")}
        if platform in ("douyin", "kuaishou"):
            return None
    except Exception as e:
        print(f"[meta] collect_meta_for {platform} 失败: {e}")
    return None


def _collect_meta_timed(url: str, platform: str, cookie_file: str = "",
                        profile_root: str = "", timeout: int = 25) -> dict | None:
    res: dict = {}
    stop = threading.Event()

    def runner() -> None:
        try:
            res["v"] = collect_meta_for(url, platform, cookie_file, profile_root)
        except Exception:
            res["v"] = None
        stop.set()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    if not stop.is_set():
        print(f"[meta] collect_meta_for {platform} 超时（{timeout}s），退回 yt-dlp")
        return None
    return res.get("v")


def extract_links_info(urls: list, cookie_file: str | None = None) -> tuple[list, list]:
    items: list[dict] = []
    notes: list[str] = []
    xhs_urls = [u for u in urls if "xiaohongshu.com" in u]
    other = [u for u in urls if "xiaohongshu.com" not in u]
    if xhs_urls:
        xck = cookie_file or (STORE.netscape_path("xiaohongshu") if STORE.load("xiaohongshu") else "")
        resolved = resolve_xhs_links(xhs_urls, profile_root=cm.PROFILE_DIR, cookie_file=xck)
        for r in resolved:
            if not r.get("title") and not r.get("media_url"):
                notes.append(f"小红书未解析到内容：{r.get('url')}")
                continue
            items.append({
                "platform": "xiaohongshu",
                "keyword": "(链接解析)",
                "title": r.get("title") or "",
                "author": r.get("author") or "",
                "duration": r.get("duration") or 0,
                "likes": r.get("likes") or 0,
                "views": r.get("views") or 0,
                "url": r.get("url"),
                "cover": r.get("cover") or "",
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            })
    if not other:
        return items, notes
    cmd, _ = resolve_ytdlp_cmd()
    if not cmd:
        return items, notes + ["未检测到 yt-dlp，无法解析其余链接：pip install yt-dlp"]
    ffmpeg_dir = find_ffmpeg()
    for u in other:
        args = list(cmd) + ["-J", "--no-playlist", "--no-warnings", "--skip-download"]
        if ffmpeg_dir:
            args += ["--ffmpeg-location", ffmpeg_dir]
        args.append(u)
        with _cookie_args(cookie_file) as _ck:
            try:
                out = subprocess.run(args + _ck, capture_output=True, text=True, timeout=90,
                                     encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                notes.append(f"解析超时（跳过）：{u}")
                continue
            except Exception as e:
                notes.append(f"解析失败：{u} —— {e}")
                continue
        if out.returncode != 0:
            err = (out.stderr or out.stdout or "").strip()[:140]
            if _is_douyin_url(u):

                _rv = _resolve_media_via_browser("douyin", u, cookie_file, cm.PROFILE_DIR)
                _mu = (_rv.media_url or "") if _rv else ""
                _is_video = bool(_rv and _mu and "ies-music" not in _mu
                                 and (".mp4" in _mu or "douyinvod" in _mu))
                items.append({
                    "platform": "douyin",
                    "keyword": "(链接解析)",
                    "title": (_rv.title if _rv else "") or "(无标题)",
                    "author": (_rv.author if _rv else "") or "",
                    "duration": int((_rv.duration if _rv else 0) or 0),
                    "likes": int((_rv.likes if _rv else 0) or 0),
                    "views": int((_rv.views if _rv else 0) or 0),
                    "url": (_rv.url if _rv else u),
                    "cover": (_rv.cover if _rv else "") or "",
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "media_url": _mu if _is_video else "",
                    "media_unavailable": not _is_video,
                })
                if _is_video:
                    notes.append("抖音视频直链已通过浏览器通道获取，可正常下载。")
                elif _rv:
                    notes.append("该抖音内容为图文（无视频可下载），已抓回元数据。")
                else:
                    notes.append(DOUYIN_ANTIBOT_NOTE)
            else:
                notes.append(f"无法解析（{err or '未知错误'}）：{u}")
            continue
        try:
            j = json.loads(out.stdout)
        except Exception:
            notes.append(f"解析结果异常：{u}")
            continue
        ek = j.get("extractor_key") or j.get("extractor") or "unknown"
        plat = str(ek).split(".")[-1].lower() if "." in str(ek) else str(ek).lower()
        _author = _yt_author(j)

        if plat == "douyin" and _author_looks_like_handle(_author):
            _real = _douyin_real_author(u, cookie_file)
            if _real:
                _author = _real
        items.append({
            "platform": plat,
            "keyword": "(链接解析)",
            "title": j.get("title") or "",
            "author": _author,
            "duration": j.get("duration") or 0,
            "likes": j.get("like_count") or j.get("track_likes") or 0,
            "views": j.get("view_count") or 0,
            "url": j.get("webpage_url") or j.get("url") or u,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        })
    return items, notes


def _ytdlp_info_one(url: str, cookie_file: str | None = None) -> dict | None:
    if ("bilibili.com" in url) or ("b23.tv" in url):
        _bv = _bili_bvid_of(url)
        if _bv:
            _m = bili_api_meta(_bv)
            if _m and _m.get("title"):
                return {"title": _m["title"], "author": _m["author"],
                        "duration": _m["duration"], "likes": _m["likes"],
                        "views": _m["views"]}
    cmd, _ = resolve_ytdlp_cmd()
    if not cmd:
        return None
    args = list(cmd) + ["-J", "--no-playlist", "--no-warnings", "--skip-download"]
    ffmpeg_dir = find_ffmpeg()
    if ffmpeg_dir:
        args += ["--ffmpeg-location", ffmpeg_dir]
    args.append(url)
    with _cookie_args(cookie_file) as _ck:
        try:
            out = subprocess.run(args + _ck, capture_output=True, text=True, timeout=30,
                                 encoding="utf-8", errors="replace")
        except Exception:
            return None
    if out.returncode != 0:
        return None
    try:
        j = json.loads(out.stdout)
    except Exception:
        return None
    return {
        "title": j.get("title") or "",
        "author": _yt_author(j),
        "duration": int(j.get("duration") or 0),
        "likes": int(j.get("like_count") or j.get("track_likes") or 0),
        "views": int(j.get("view_count") or 0),
    }


def enrich_items_metadata(items: list, only_missing: bool = True) -> int:
    targets = [(i, v) for i, v in enumerate(items)
               if isinstance(v, VideoLink)
               and (not only_missing or (not v.title or (not v.likes and not v.views)))]
    if not targets:
        return 0
    cmd, _ = resolve_ytdlp_cmd()
    if not cmd:
        return 0

    def worker(idx_v):
        idx, v = idx_v
        _, cookie_file, _ = _pick_cookie(v.url, v.platform)
        return idx, _ytdlp_info_one(v.url, cookie_file)

    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for idx, meta in ex.map(worker, targets):
            if not meta:
                continue
            v = items[idx]
            if not v.title and meta["title"]:
                v.title = meta["title"]
            if not v.author and meta["author"]:
                v.author = meta["author"]
            if not v.duration and meta["duration"]:
                v.duration = meta["duration"]
            if not v.likes and meta["likes"]:
                v.likes = meta["likes"]
            if not v.views and meta["views"]:
                v.views = meta["views"]
            done += 1
    return done


def stream_download(self, body: dict) -> None:
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
    self.send_header("Cache-Control", "no-cache")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("X-Accel-Buffering", "no")
    self.end_headers()
    url = (body.get("url") or "").strip()
    if not url:
        _sse_emit(self, "error", {"message": "请提供视频链接"});
        return
    platform, cookie_file, cookie_label = _pick_cookie(url, (body.get("platform") or "").strip())

    route_platform = platform or _platform_of_url(url)
    fmt, container, audio_only, _ = _fmt_from_body(body)
    vcodec = (body.get("vcodec") or "").strip()
    custom = (body.get("format_id") or "").strip()
    _qmap = {"1080p": 1080, "720p": 720, "360p": 360}
    height_cap = _qmap.get((body.get("quality") or "best").strip(), 0) if (not custom and not audio_only) else 0
    if not fmt:
        _sse_emit(self, "error", {"message": "非法的格式编号"});
        return

    task_id = uuid.uuid4().hex[:8]

    def emit(event, data):
        d = dict(data);
        d["task_id"] = task_id
        _sse_emit(self, event, d)
        HUB.broadcast(event, d)

    existing = _find_existing(url, platform)
    if existing:
        emit("already", {"title": existing.get("title") or os.path.basename(existing.get("path", "")),
                         "path": existing.get("path"), "id": existing.get("id"),
                         "message": "该视频已下载过，已跳过重复下载"})
        return

    meta = {k: (body.get(k) or "") for k in ("title", "author", "cover")}
    likes = body.get("likes") or 0
    views = body.get("views") or 0
    duration = body.get("duration") or 0

    task_key = _dl_task_key(task_id, 0)
    _dl_register(task_key, {
        "task_id": task_id, "index": 0, "entry": body, "body": body,
        "fmt": fmt, "container": container, "audio_only": audio_only,
        "vcodec": vcodec, "height_cap": height_cap,
        "delogo": bool((body or {}).get("bilibili_delogo")),
        "url": url, "platform": route_platform,
        "title": meta.get("title") or body.get("name") or body.get("title") or url,
        "cover": meta.get("cover") or "",
    })
    _dl_set_status(task_key, "queued", {
        "title": meta.get("title") or body.get("name") or body.get("title") or url,
        "url": url, "platform": route_platform, "cover": meta.get("cover") or "",
    })

    collector_meta: dict = {}
    if not (meta["title"] and meta["author"] and views and duration):
        def _fill_meta() -> None:
            try:
                m = collect_meta_for(url, platform, cookie_file, cm.PROFILE_DIR)
                if m:
                    collector_meta.update(m)
            except Exception:
                pass

        threading.Thread(target=_fill_meta, daemon=True).start()

    if route_platform == "bilibili":
        _fmt_id = (body.get("format_id") or "").strip()
        _bv = _bili_bvid_of(url)
        if _fmt_id and _bv:
            _cid = None
            _m0 = bili_api_meta(_bv)
            if _m0:
                _cid = _m0.get("cid")
            v_url, a_url = _bili_fmt_url(_bv, _cid, _fmt_id)
            if v_url:
                _run_bili_tracks_download(emit, url, v_url, a_url, "bilibili",
                                          cookie_file, cookie_label, tag="", task_id=task_id,
                                          title=meta["title"], author=meta["author"],
                                          likes=likes, views=views, duration=duration,
                                          cover=meta["cover"], task_key=task_key,
                                          stop_event=DL_TASKS[task_key]["stop_event"],
                                          vcodec=vcodec)
                return
        media_url = (body.get("media_url") or "").strip()
        if not media_url:
            if _bv:
                _mu = bili_api_durl(_bv)
                if _mu:
                    media_url = _mu
                    _m = bili_api_meta(_bv)
                    if _m:
                        if not meta["title"] and _m["title"]: meta["title"] = _m["title"]
                        if not meta["author"] and _m["author"]: meta["author"] = _m["author"]
                        if not likes and _m["likes"]: likes = _m["likes"]
                        if not views and _m["views"]: views = _m["views"]
                        if not duration and _m["duration"]: duration = _m["duration"]
                        if not meta["cover"] and _m["pic"]: meta["cover"] = _m["pic"]
        if media_url:
            _run_direct_download(emit, url, media_url, "bilibili", cookie_file, cookie_label,
                                 tag="", task_id=task_id, title=meta["title"], author=meta["author"],
                                 likes=likes, views=views, duration=duration, cover=meta["cover"],
                                 vcodec=vcodec, task_key=task_key,
                                 stop_event=DL_TASKS[task_key]["stop_event"])
            return
        emit("error", {"message": "B 站直链获取失败，请稍后重试或检查链接。", "tag": ""})
        return
    if route_platform in ("kuaishou", "douyin"):
        media_url = (body.get("media_url") or "").strip()
        rv = None
        if not media_url:
            rv = _resolve_media_via_browser(route_platform, url, cookie_file, cm.PROFILE_DIR)
            if rv:
                media_url = (rv.media_url or "").strip()
        if media_url:
            if rv:
                if not meta["title"] and rv.title: meta["title"] = rv.title
                if not meta["author"] and rv.author: meta["author"] = rv.author
                if not likes and rv.likes: likes = rv.likes
                if not views and rv.views: views = rv.views
                if not duration and rv.duration: duration = rv.duration
                if not meta["cover"] and rv.cover: meta["cover"] = rv.cover
            _run_direct_download(emit, url, media_url, route_platform, cookie_file, cookie_label,
                                 tag="", task_id=task_id, title=meta["title"], author=meta["author"],
                                 likes=likes, views=views, duration=duration, cover=meta["cover"],
                                 vcodec=vcodec, task_key=task_key,
                                 stop_event=DL_TASKS[task_key]["stop_event"])
            return
        if route_platform == "kuaishou":
            emit("error", {"message": "无法获取快手视频直链：yt-dlp 不支持快手，需浏览器登录态解析。"
                                      "请确认已在 Cookie 面板配置快手登录态后重试。", "tag": ""})
            return

        _run_single_download(emit, url, route_platform, cookie_file, cookie_label,
                             fmt, container, audio_only, tag="",
                             task_id=task_id, title=meta["title"], author=meta["author"],
                             likes=likes, cover=meta["cover"], views=views, duration=duration,
                             collector_meta=collector_meta,
                             delogo=bool((body or {}).get("bilibili_delogo")), vcodec=vcodec,
                             height_cap=height_cap, task_key=task_key,
                             stop_event=DL_TASKS[task_key]["stop_event"])
        return
    if route_platform == "xiaohongshu":
        _run_xhs_download(emit, url, cookie_file, cookie_label, tag="",
                          profile_root=cm.PROFILE_DIR,
                          media_url=body.get("media_url", "") or "",
                          cover=body.get("cover", "") or "",
                          task_id=task_id, title=meta["title"], author=meta["author"], likes=likes,
                          views=views, duration=duration, task_key=task_key,
                          stop_event=DL_TASKS[task_key]["stop_event"])
        return
    _run_single_download(emit, url, route_platform or platform, cookie_file, cookie_label,
                         fmt, container, audio_only, tag="",
                         task_id=task_id, title=meta["title"], author=meta["author"],
                         likes=likes, cover=meta["cover"], views=views, duration=duration,
                         collector_meta=collector_meta,
                         delogo=bool((body or {}).get("bilibili_delogo")), vcodec=vcodec,
                         height_cap=height_cap, task_key=task_key,
                         stop_event=DL_TASKS[task_key]["stop_event"])


def stream_batch_download(self, body: dict) -> None:
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
    self.send_header("Cache-Control", "no-cache")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("X-Accel-Buffering", "no")
    self.end_headers()
    entries = [e for e in (body.get("entries") or []) if isinstance(e, dict)]
    settings = body.get("settings") or {}
    quality = (settings.get("quality") or "best").strip()
    vcodec = (settings.get("vcodec") or "").strip()
    custom = (settings.get("format_id") or "").strip()
    if custom and not all(c.isalnum() or c in "+-_.:/[]<>=*" for c in custom):
        _sse_emit(self, "error", {"message": "非法的格式编号"});
        return
    audio_only = bool(settings.get("audio_only")) or (quality == "audio" and not custom)
    fmt = "ba/bestaudio" if audio_only else _build_fmt(quality, vcodec, custom)

    _qmap = {"1080p": 1080, "720p": 720, "360p": 360}
    height_cap = _qmap.get(quality, 0) if (not custom and not audio_only) else 0
    container = (settings.get("container") or "mp4").strip().lower()
    if container not in ("mp4", "mkv", "webm", "mov", "flv", "avi"):
        container = "mp4"
    total = len(entries)
    if not total:
        _sse_emit(self, "error", {"message": "没有可下载的条目（请先在列表中选择）"});
        return
    task_id = uuid.uuid4().hex[:8]

    def emit_all(event, data):
        d = dict(data);
        d["task_id"] = task_id
        _sse_emit(self, event, d)
        HUB.broadcast(event, d)

    emit_all("batch-start", {"total": total})

    DL_BATCHES[task_id] = {"status": "running", "pause_event": threading.Event(), "total": total,
                           "title": entries[0].get("title") if entries else ""}

    CONCURRENT_DOWNLOADS = 3
    done_count = 0
    prog_lock = threading.Lock()

    def download_entry(idx, e):
        nonlocal done_count
        task_key = _dl_task_key(task_id, idx)
        url = (e.get("url") or "").strip()
        if not url:
            emit_all("entry-skipped", {"index": idx, "reason": "空链接"})
        else:
            platform, cookie_file, cookie_label = _pick_cookie(url, (e.get("platform") or "").strip())

            route_platform = platform or _platform_of_url(url)
            title0 = e.get("title") or url

            _dl_register(task_key, {
                "task_id": task_id, "index": idx, "entry": e, "body": body,
                "fmt": fmt, "container": container, "audio_only": audio_only,
                "vcodec": vcodec, "height_cap": height_cap,
                "delogo": bool((body or {}).get("bilibili_delogo")),
                "url": url, "platform": route_platform, "title": title0,
                "cover": e.get("thumbnail") or e.get("cover") or "",
            })
            emit_all("entry-start", {"index": idx, "total": total,
                                     "title": title0, "url": url,
                                     "platform": route_platform,
                                     "cover": e.get("thumbnail") or e.get("cover") or ""})
            _dl_set_status(task_key, "queued", {"title": title0, "platform": route_platform, "url": url,
                                                "task_key": task_key, "task_id": task_id, "index": idx})

            existing = _find_existing(url, platform)
            if existing:

                _touch_history(url, platform)
                emit_all("entry-skipped", {"index": idx, "total": total, "reason": "已下载（跳过重复）",
                                           "title": existing.get("title") or url,
                                           "path": existing.get("path"), "id": existing.get("id"), "url": url})
                _dl_set_status(task_key, "skipped")
            else:

                run_one_task(self, task_id, task_key)
            emit_all("entry-done", {"index": idx, "total": total})

        with prog_lock:
            done_count += 1
            emit_all("batch-progress", {"index": done_count, "total": total})

    with ThreadPoolExecutor(max_workers=min(CONCURRENT_DOWNLOADS, max(1, total))) as ex:
        futs = [ex.submit(download_entry, idx, e) for idx, e in enumerate(entries)]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                pass
    emit_all("batch-done", {"total": total})
    with DL_LOCK:
        _b = DL_BATCHES.get(task_id)
        if _b is not None:
            _b["status"] = "done"
    HUB.broadcast("batch-update", {"task_id": task_id, "status": "done"})


def _sse_emit(self, event: str, data: dict) -> None:
    try:
        with _SSE_LOCK:
            self.wfile.write(f"event: {event}\n".encode("utf-8"))
            self.wfile.write(("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
    except Exception:
        pass


def _build_fmt(quality, vcodec, custom):
    if custom:
        return custom
    if quality == "audio":
        return "ba/bestaudio"
    vc = (vcodec or "").strip().lower()
    codec_filter = {"h264": "[vcodec^=avc1]", "h265": "[vcodec^=hevc]",
                    "hevc": "[vcodec^=hevc]", "av1": "[vcodec^=av01]",
                    "vp9": "[vcodec^=vp9]"}.get(vc, "")
    qmap = {"best": None, "1080p": 1080, "720p": 720, "360p": 360}
    n = qmap.get(quality)
    if n is None:

        if codec_filter:
            return f"bv*{codec_filter}+ba/best"
        return "bv*+ba/best"

    if codec_filter:
        return (f"b[height<={n}]{codec_filter}/"
                f"bv[height<={n}]{codec_filter}+ba/"
                f"best[height<={n}]{codec_filter}/best")
    return (f"b[height<={n}]/bv[height<={n}]+ba/best[height<={n}]/best")


def _height_limited_fmt(url, n, vcodec, cookie_file):
    cmd, _ = resolve_ytdlp_cmd()
    if not cmd:
        return "best"
    args = list(cmd) + ["-J", "--no-playlist", "--no-warnings", "--skip-download"]
    ffmpeg_dir = find_ffmpeg()
    if ffmpeg_dir:
        args += ["--ffmpeg-location", ffmpeg_dir]
    args.append(url)
    try:
        with _cookie_args(cookie_file) as _ck:
            out = subprocess.run(args + _ck, capture_output=True, text=True, timeout=90,
                                 encoding="utf-8", errors="replace")
    except Exception:
        return "best"
    if out.returncode != 0 or not out.stdout.strip():
        return "best"
    try:
        j = json.loads(out.stdout)
    except Exception:
        return "best"
    vc = (vcodec or "").strip().lower()
    vc_sub = {"h264": "avc1", "h265": "hevc", "hevc": "hevc",
              "av1": "av01", "vp9": "vp9"}.get(vc, "")
    raw = j.get("formats") or []
    cands = []
    for f in raw:
        fid = f.get("format_id")
        if not fid:
            continue
        if (f.get("ext") or "") in ("mhtml", "none"):
            continue
        vcodec_f = (f.get("vcodec") or "none")
        if vcodec_f == "none":
            continue
        w = _num(f.get("width"));
        h = _num(f.get("height"))
        if not w or not h:
            continue
        if min(w, h) > n:
            continue
        if vc_sub and vc_sub not in (vcodec_f or ""):
            continue
        cands.append(f)
    if not cands:
        return "best"

    cands.sort(key=lambda f: (
        _num(f.get("width")) * _num(f.get("height")),
        _num(f.get("tbr")) or 0,
        _num(f.get("filesize")) or _num(f.get("filesize_approx")) or 0,
    ), reverse=True)
    best = cands[0]
    fid = str(best.get("format_id"))
    if (best.get("acodec") or "none") != "none":
        return fid

    auds = [f for f in raw if f.get("format_id")
            and (f.get("acodec") or "none") != "none"
            and (f.get("vcodec") or "none") == "none"]
    if auds:
        auds.sort(key=lambda f: (_num(f.get("abr")) or _num(f.get("tbr")) or 0), reverse=True)
        return f"{fid}+{auds[0].get('format_id')}"
    return fid


def _fmt_from_body(body: dict):
    quality = (body.get("quality") or "best").strip()
    vcodec = (body.get("vcodec") or "").strip()
    custom = (body.get("format_id") or "").strip()
    if custom and not all(c.isalnum() or c in "+-_.:/[]<>=*" for c in custom):
        return "", "mp4", False, False
    audio_only = bool(body.get("audio_only")) or (quality == "audio" and not custom)
    fmt = "ba/bestaudio" if audio_only else _build_fmt(quality, vcodec, custom)
    container = (body.get("container") or "mp4").strip().lower()
    if container not in ("mp4", "mkv", "webm", "mov", "flv", "avi"):
        container = "mp4"
    return fmt, container, audio_only, ("+" in fmt)


_VCODEC_LABEL = {"h264": "H.264", "hevc": "H.265/HEVC", "h265": "H.265/HEVC",
                 "av1": "AV1", "vp9": "VP9"}

_VCODEC_ENC = {
    "h264": ("libx264", ["-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p"]),
    "hevc": ("libx265", ["-crf", "26", "-preset", "medium", "-tag:v", "hvc1"]),
    "h265": ("libx265", ["-crf", "26", "-preset", "medium", "-tag:v", "hvc1"]),
    "av1": ("libaom-av1", ["-crf", "30", "-b:v", "0", "-cpu-used", "4", "-tag:v", "av01"]),
    "vp9": ("libvpx-vp9", ["-b:v", "0", "-crf", "30", "-row-mt", "1"]),
}


def _find_ffprobe():
    d = find_ffmpeg()
    if d:
        for name in ("ffprobe.exe", "ffprobe"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return shutil.which("ffprobe")


def _detect_video_codec(filepath: str) -> str:
    ff = _find_ffprobe()
    if not ff or not os.path.isfile(filepath):
        return ""
    try:
        out = subprocess.run([ff, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                              filepath], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30).stdout.strip()
        return out.splitlines()[0].strip() if out else ""
    except Exception:
        return ""


def _run_ffmpeg(args: list, timeout: int = 3600) -> int:
    try:
        _env = dict(os.environ)
        _env["PYTHONUTF8"] = "1"
        _env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=_env, timeout=timeout)
        return p.returncode
    except Exception:
        return -1


def _maybe_transcode(filepath: str, vcodec: str, emit=None, tag: str = "") -> str:
    desired = (vcodec or "").strip().lower()
    if desired not in _VCODEC_ENC:
        return filepath
    if not os.path.isfile(filepath):
        return filepath
    cur = _detect_video_codec(filepath)
    if not cur:
        return filepath

    if cur == desired or (cur in ("hevc", "h265") and desired in ("hevc", "h265")) or (
            cur == "h264" and desired == "h264"):
        return filepath
    ffmpeg_dir = find_ffmpeg()
    if not ffmpeg_dir:
        if emit:
            emit("log", {"line": f"⚠ 需将 {cur.upper()} 转码为 {_VCODEC_LABEL.get(desired, desired)}，"
                                 f"但未检测到 ffmpeg，保留原编码。", "tag": tag})
        return filepath
    enc, params = _VCODEC_ENC[desired]
    if emit:
        emit("log", {"line": f"🔄 原视频编码为 {cur.upper()}，按设置转码为 "
                             f"{_VCODEC_LABEL.get(desired, desired)}…", "tag": tag})
    _d, _fn = os.path.split(filepath)

    _out = os.path.join(_d, os.path.splitext(_fn)[0] + ".mp4")
    _tmp = os.path.join(_d, "." + _fn + ".transcode.tmp.mp4")
    ff = os.path.join(ffmpeg_dir, "ffmpeg.exe") if os.path.isfile(
        os.path.join(ffmpeg_dir, "ffmpeg.exe")) else os.path.join(ffmpeg_dir, "ffmpeg")
    base = [ff, "-y", "-i", filepath, "-c:v", enc, *params, "-c:s", "copy"]

    rc = _run_ffmpeg(base + ["-c:a", "copy", _tmp])
    if rc != 0:
        rc = _run_ffmpeg(base + ["-c:a", "aac", "-b:a", "192k", _tmp])
    if rc == 0 and os.path.isfile(_tmp) and os.path.getsize(_tmp) > 0:
        try:
            os.replace(_tmp, _out)

            if _out != filepath and os.path.isfile(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            if emit:
                emit("log", {"line": f"✅ 已转码为 {_VCODEC_LABEL.get(desired, desired)}"
                                     f"（原 {cur.upper()}），文件已替换。", "tag": tag})
            return _out
        except OSError:
            pass

    try:
        if os.path.isfile(_tmp):
            os.remove(_tmp)
    except OSError:
        pass
    if emit:
        emit("log", {"line": f"⚠ 转码为 {_VCODEC_LABEL.get(desired, desired)} 失败，"
                             f"保留原编码 {cur.upper()}。", "tag": tag})
    return filepath


def _valid_netscape_cookie(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        from http.cookiejar import MozillaCookieJar
        jar = MozillaCookieJar()
        jar.load(path, ignore_discard=True, ignore_expires=True)
        return True
    except Exception:
        return False


def _copy_cookie_tmp(src: str):
    try:
        import tempfile, shutil
        fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="ydcookie_")
        os.close(fd)
        shutil.copyfile(src, tmp)
        return tmp
    except Exception:
        return None


@contextlib.contextmanager
def _cookie_args(cookie_file: str):
    if not cookie_file or not os.path.isfile(cookie_file) or not _valid_netscape_cookie(cookie_file):
        yield []
        return
    tmp = _copy_cookie_tmp(cookie_file)
    if not tmp:
        yield ["--cookies", cookie_file]
        return
    try:
        yield ["--cookies", tmp]
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _run_single_download(emit, url, platform, cookie_file, cookie_label, fmt, container,
                         audio_only, tag="", task_id="", title="", author="", likes=0, cover="",
                         views=0, duration=0, collector_meta=None, delogo=False, playlist=False,
                         vcodec="", height_cap=0, task_key=None, stop_event=None):
    if stop_event is not None and stop_event.is_set():
        return
    cmd, ver = resolve_ytdlp_cmd()
    if not cmd:
        emit("error", {"message": "未检测到 yt-dlp，请先安装：pip install yt-dlp", "tag": tag});
        return
    if not fmt:
        emit("error", {"message": "非法的格式编号", "tag": tag});
        return
    ffmpeg_dir = find_ffmpeg()

    if height_cap and not audio_only:
        try:
            _resolved = _height_limited_fmt(url, height_cap, vcodec, cookie_file)
            if _resolved and _resolved != fmt:
                fmt = _resolved
        except Exception:
            pass
    need_merge = "+" in fmt

    args = list(cmd) + ["--no-playlist", "--newline", "-P", DOWNLOAD_DIR,
                        "-o", "%(title)s - %(uploader)s.%(ext)s"]

    args += ["--write-thumbnail"]

    args += ["--write-info-json"]
    if ffmpeg_dir:
        args += ["--embed-thumbnail"]

    _cookie_arg = None
    if cookie_file and os.path.isfile(cookie_file):
        if _valid_netscape_cookie(cookie_file):
            _cookie_arg = _copy_cookie_tmp(cookie_file) or cookie_file
        else:
            emit("log", {"line": f"⚠ Cookie 文件无效（非 Netscape 格式或为空），已跳过 {cookie_label} "
                                 f"登录态，改为匿名下载：{os.path.basename(cookie_file)}", "tag": tag})
    if _cookie_arg:
        args += ["--cookies", _cookie_arg]
    if ffmpeg_dir:
        args += ["--ffmpeg-location", ffmpeg_dir]
    args += ["-f", fmt]
    if audio_only:
        args += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    elif need_merge:
        args += ["--merge-output-format", container]
        if not ffmpeg_dir:
            emit("log", {"line": "⚠ 未检测到 ffmpeg，音视频合并会失败。请安装 ffmpeg 并加入 PATH。", "tag": tag})

    if platform == "bilibili" and delogo and ffmpeg_dir and need_merge:
        args += ["--postprocessor-args",
                 "ffmpeg:-vf delogo=x=W-220:y=H-80:w=200:h=60:show=0"]

    args += [
        "--progress",
        "--progress-template",
        "download:" + PROG_TAG + "%(progress._percent_str)s|%(progress.downloaded_bytes)s"
                                 "|%(progress.total_bytes,progress.total_bytes_estimate)s"
                                 "|%(progress._speed_str)s|%(progress._eta_str)s",
        "--progress-template", "postprocess:" + PP_TAG + "%(progress.status)s",
        "--print", "after_move:filepath",

        "--print", "after_move:RESMETA|%(height)s|%(format_id)s", url,
    ]
    emit("meta", {"title": url, "platform": platform, "cookie": cookie_label, "yt_dlp": ver,
                  "ffmpeg": bool(ffmpeg_dir), "format": fmt, "cover": cover or "",
                  "merge": need_merge, "container": container if need_merge else "", "tag": tag})

    state = {"filepath": "", "filepaths": [], "skipped": False, "stderr": [],
             "actual_height": None, "actual_fmt": ""}

    def handle_line(line, is_stdout):

        if line.startswith("RESMETA|"):
            _p = line.split("|")
            try:
                _h = _p[1]
                state["actual_height"] = int(_h) if _h not in ("", "None") else None
            except (ValueError, IndexError):
                state["actual_height"] = None
            state["actual_fmt"] = _p[2] if len(_p) > 2 else ""
            return
        if line.startswith(PROG_TAG):
            p = (line[len(PROG_TAG):].split("|") + [""] * 5)[:5]
            emit("progress", {"percent": p[0].strip(), "done": _num(p[1].strip()),
                              "total": _num(p[2].strip()), "speed": p[3].strip(),
                              "eta": p[4].strip(), "tag": tag})
        elif line.startswith(PP_TAG):
            emit("stage", {"status": line[len(PP_TAG):].strip() or "processing", "tag": tag})
        elif "has already been downloaded" in line:
            state["skipped"] = True
            emit("log", {"line": line, "tag": tag})
        elif is_stdout and (os.path.isabs(line) or os.path.sep in line):
            state["filepaths"].append(line)
            if not playlist:
                state["filepath"] = line
        else:
            emit("log", {"line": line, "tag": tag})

    def _spawn(run_args, task_key=None, stop_event=None):

        state["filepath"], state["filepaths"], state["skipped"], state["stderr"] = "", [], False, []
        state["stopped"] = False

        _env = dict(os.environ)
        _env["PYTHONUTF8"] = "1"
        _env["PYTHONIOENCODING"] = "utf-8"
        try:
            p = subprocess.Popen(run_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, bufsize=1, encoding="utf-8", errors="replace",
                                 env=_env)
        except Exception as e:
            emit("error", {"message": f"启动 yt-dlp 失败：{e}", "tag": tag});
            return -1, "", False, []

        if task_key and stop_event is not None:
            with DL_LOCK:
                _t = DL_TASKS.get(task_key)
                if _t is not None:
                    _t["proc"] = p
                    _t["status"] = "downloading"

        def _read_stderr():
            for raw in p.stderr:
                s = raw.rstrip("\n").strip()
                if s:
                    state["stderr"].append(s)
                    handle_line(s, False)

        _t = threading.Thread(target=_read_stderr, daemon=True)
        _t.start()
        for raw in p.stdout:

            if stop_event is not None and stop_event.is_set():
                try:
                    p.terminate()
                except Exception:
                    pass
                state["stopped"] = True
                break
            s = raw.rstrip("\n").strip()
            if s:
                handle_line(s, True)
        p.stdout.close()
        if state.get("stopped"):
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        else:
            rc = p.wait()
        _t.join(timeout=3)
        try:
            p.stderr.close()
        except Exception:
            pass
        return (rc if not state.get("stopped") else -1), state["filepath"], state["skipped"], list(state["stderr"])

    rc, filepath, skipped, err_lines = _spawn(args, task_key, stop_event)
    if state.get("stopped"):
        _finish_stopped(emit, tag, task_key)
        return
    if rc != 0 and not filepath and _cookie_arg:
        emit("log", {"line": "⚠ 带登录态下载失败，正在匿名重试一次（去掉 Cookie）…", "tag": tag})

        _anon, _skip = [], False
        for a in args:
            if _skip:
                _skip = False
                continue
            if a == "--cookies":
                _skip = True
                continue
            _anon.append(a)
        rc, filepath, skipped, err_lines = _spawn(_anon, task_key, stop_event)
        if state.get("stopped"):
            _finish_stopped(emit, tag, task_key)
            return

    if rc == 0 and not (filepath and os.path.isfile(filepath)):
        _real = _resolve_downloaded_file(title or "", author or "", DOWNLOAD_DIR)
        if _real:
            filepath = _real

    if playlist and state["filepaths"]:
        _pl_files = [fp for fp in state["filepaths"] if fp and os.path.isfile(fp)]
        if _pl_files:
            _pl_out = []
            for fp in _pl_files:
                _fn = os.path.basename(fp)
                _sz = os.path.getsize(fp)
                _record_history({
                    "task_id": task_id, "platform": platform,
                    "title": os.path.splitext(_fn)[0].strip(), "author": "",
                    "likes": 0, "views": 0, "duration": 0, "cover": "", "cover_file": "",
                    "url": url or "", "filename": _fn, "path": fp, "size": _sz,
                    "status": "done", "progress": 100,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                })
                _pl_out.append({"filename": _fn, "path": fp, "size": _sz})
            emit("done", {"filename": _pl_out[0]["filename"], "path": _pl_out[0]["path"],
                          "size": _pl_out[0]["size"], "format": fmt, "skipped": False, "tag": tag,
                          "playlist": True, "files": _pl_out})
            if _cookie_arg and _cookie_arg != cookie_file:
                try:
                    os.remove(_cookie_arg)
                except Exception:
                    pass
            return

    cover_file = ""

    cmeta = collector_meta or {}
    if not title and cmeta.get("title"):
        title = cmeta["title"]
    if not author and cmeta.get("author"):
        author = cmeta["author"]
    if not cover and cmeta.get("cover"):
        cover = cmeta["cover"]
    if not likes and cmeta.get("likes"):
        likes = cmeta["likes"]
    if not views and cmeta.get("views"):
        views = cmeta["views"]
    if not duration and cmeta.get("duration"):
        duration = cmeta["duration"]
    if filepath and os.path.isfile(filepath):
        _d, _fn = os.path.split(filepath)
        _base, _ext = os.path.splitext(_fn)
        target_base = None

        if title and author and author not in ("NA", "", "Unknown", "None"):
            cand = _sanitize_filename(f"{title} - {author}")
            if cand and cand != _base:
                target_base = cand

        if target_base is None:
            nb = _base.rstrip()
            if nb.endswith(" - NA"):
                nb = nb[:-4]
            elif nb.endswith(" -"):
                nb = nb[:-2]
            if nb and nb != _base:
                target_base = nb
        if target_base and target_base != _base:
            _new = target_base + _ext
            _np = os.path.join(_d, _new)
            if not os.path.exists(_np):
                try:
                    os.rename(filepath, _np)

                    for te in (".jpg", ".webp", ".png"):
                        _tob = os.path.join(_d, _base + te)
                        if os.path.isfile(_tob):
                            try:
                                os.rename(_tob, os.path.join(_d, target_base + te))
                            except Exception:
                                pass
                    filepath = _np
                except Exception:
                    pass

        _fbase = os.path.splitext(os.path.basename(filepath))[0]
        for te in (".jpg", ".webp", ".png"):
            _cand = os.path.join(_d, _fbase + te)
            if os.path.isfile(_cand):
                cover_file = os.path.basename(_cand)
                break

        if not cover_file and cover and str(cover).startswith("http"):
            try:
                import urllib.parse as _up
                _suffix = os.path.splitext(_up.urlparse(str(cover)).path)[1].lower()
                _ext = _suffix if _suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"
                _cb = os.path.join(_d, _fbase + _ext)
                if not os.path.exists(_cb):
                    download_url_to_file(str(cover), _cb, referer=url)
                    if os.path.isfile(_cb) and os.path.getsize(_cb) > 0:
                        cover_file = os.path.basename(_cb)
            except Exception:
                pass

        if cover_file:
            _rel = _save_cover(src_local=os.path.join(_d, cover_file), cover_url=cover,
                               title=title or _fbase, author=author, referer=url)
            if _rel:
                cover_file = _rel

    if rc != 0 and not filepath:
        if task_key:
            _dl_set_status(task_key, "error")

        tail = "\n".join(err_lines[-25:])
        detail = (tail[:1500] + "…") if len(tail) > 1500 else tail
        used_cookie = "（已自动匿名重试仍失败）" if _cookie_arg else ""
        emit("error", {"message": f"yt-dlp 退出码 {rc}，下载失败{used_cookie}。\n"
                                  f"原因（完整）：\n{detail}\n\n"
                                  f"常见排查：① 链接失效/地区限制/需登录（导入该平台 Cookie 重试）；"
                                  f"② 切换下载格式（如改用「最佳」或单一音/视频）；"
                                  f"③ 该平台解析器异常（可换分享链接解析通道）。", "tag": tag})
        return

    if filepath and os.path.isfile(filepath):
        _info = _read_ytdlp_info_json(filepath)
        if _info:
            if not title and _info.get("title"):
                title = _info["title"]
            if not author and _info.get("author"):
                author = _info["author"]
            if not likes and _info.get("likes"):
                likes = _info["likes"]
            if not views and _info.get("views"):
                views = _info["views"]
            if not duration and _info.get("duration"):
                duration = _info["duration"]
            if not cover and _info.get("thumbnail"):
                cover = _info["thumbnail"]

    if filepath and os.path.isfile(filepath):
        _ext = (os.path.splitext(filepath)[1].lstrip(".") or "mp4")
        _new = _unique_path(_final_name(title, author, duration, _ext))
        if _new != filepath:
            _oldbase = os.path.splitext(filepath)[0]
            try:
                os.rename(filepath, _new)

                for _se in (".jpg", ".png", ".webp"):
                    _sc = _oldbase + _se
                    if os.path.isfile(_sc):
                        try:
                            os.rename(_sc, os.path.splitext(_new)[0] + _se)
                        except OSError:
                            pass
                filepath = _new
            except OSError:
                pass

    if filepath and os.path.isfile(filepath) and vcodec:
        filepath = _maybe_transcode(filepath, vcodec, emit, tag)
    size = os.path.getsize(filepath) if filepath and os.path.isfile(filepath) else 0

    _actual = state.get("actual_height")
    _cap = None
    _m = re.search(r"height<=(\d+)", fmt or "")
    if _m:
        _cap = int(_m.group(1))
    if _cap and _actual and _actual < _cap:
        _cap_label = {"360": "360p", "480": "480p", "720": "720p", "1080": "1080p"}.get(
            str(_cap), f"{_cap}p")
        emit("log", {"line": f"⚠ 实际分辨率 {_actual}p 低于所选 {_cap_label}："
                             f"该平台/账号当前仅提供到 {_actual}p（如 B站未登录或低等级账号只放流到 480p）。"
                             f"请配置具备更高清晰度权限的该平台登录 Cookie 后重试。", "tag": tag})
    _fname = os.path.basename(filepath)

    _title = (title or os.path.splitext(_fname)[0]).strip()
    _record_history({
        "task_id": task_id, "platform": platform, "title": _title, "author": author,
        "likes": int(likes or 0), "views": int(views or 0), "duration": int(duration or 0),
        "cover": cover, "cover_file": cover_file,
        "url": url or "",
        "filename": _fname, "path": filepath,
        "size": size, "status": "skipped" if skipped else "done",
        "progress": 0 if skipped else 100,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    })
    emit("done", {"filename": _fname, "path": filepath, "size": size,
                  "format": fmt, "skipped": skipped, "tag": tag})
    if task_key and not skipped:
        _dl_set_status(task_key, "done")

    if _cookie_arg and _cookie_arg != cookie_file:
        try:
            os.remove(_cookie_arg)
        except Exception:
            pass


def _run_xhs_download(emit, url, cookie_file, cookie_label, tag="", profile_root="",
                      media_url="", cover="", task_id="", title="", author="", likes=0,
                      views=0, duration=0, task_key="", stop_event=None):
    import re as _re
    emit("meta", {"title": url, "platform": "xiaohongshu", "cookie": cookie_label,
                  "yt_dlp": "-", "ffmpeg": False, "format": "原画直连", "merge": False,
                  "container": "", "tag": tag, "cover": cover or ""})
    if task_key:
        if stop_event and stop_event.is_set():
            _dl_set_status(task_key, "stopped");
            return
        _dl_set_status(task_key, "downloading")
    mu = (media_url or "").strip()
    info_title = ""
    info: dict = {}
    if mu and ("xhscdn" in mu or ".mp4" in mu or ".m3u8" in mu):
        emit("log", {"line": "✅ 复用已解析的真实视频直链，开始直连下载…", "tag": tag})
    else:
        emit("log", {"line": "🌐 正在用登录态浏览器解析真实视频直链…", "tag": tag})
        info = resolve_xhs_media_url(url, profile_root=profile_root, cookie_file=cookie_file, headless=True)
        if not info.get("ok"):
            emit("error", {"message": info.get("error", "解析失败"), "tag": tag})
            if task_key: _dl_set_status(task_key, "error")
            return
        mu = info["media_url"]
        info_title = info.get("title", "")
        if not cover:
            cover = info.get("cover", "")
    emit("log", {"line": "✅ 已拿到直链，开始直连下载…", "tag": tag})

    def _xi(v):
        try:
            return int(v or 0)
        except Exception:
            return 0

    likes = _xi(likes) or _xi(info.get("likes"))
    views = _xi(views) or _xi(info.get("views"))
    duration = _xi(duration) or _xi(info.get("duration"))

    if not title:
        title = (info.get("title") or info_title or "").strip()
    if not author:
        author = (info.get("author") or "").strip()

    if not (likes and views and duration and author) and not info:
        try:
            mi = resolve_xhs_media_url(url, profile_root=profile_root,
                                       cookie_file=cookie_file, headless=True)
            if mi.get("ok"):
                if not cover:
                    cover = mi.get("cover", "")
                likes = likes or _xi(mi.get("likes"))
                views = views or _xi(mi.get("views"))
                duration = duration or _xi(mi.get("duration"))
                if not author:
                    author = (mi.get("author") or "").strip()
                if not title:
                    title = (mi.get("title") or "").strip()
        except Exception:
            pass

    nid = _re.search(r"explore/([0-9a-f]{16,32})", url)
    nid = nid.group(1) if nid else "xhs"

    title_final = (title or info_title or "").strip() or nid
    author_final = (author or "").strip()
    ext = "m3u8" if ".m3u8" in mu else "mp4"
    fname = _final_name(title_final, author_final, duration, ext)
    out_path = _unique_path(fname)
    header = netscape_to_header(cookie_file)
    try:
        def prog(done, total):
            pct = (done / total * 100) if total else 0
            emit("progress", {"percent": f"{pct:.1f}%", "done": done, "total": total,
                              "speed": "", "eta": "", "tag": tag})

        size = download_url_to_file(mu, out_path, header, referer=url, emit_progress=prog)
    except Exception as e:
        emit("error", {"message": f"直连下载失败：{e}", "tag": tag})
        if task_key: _dl_set_status(task_key, "error")
        return
    if ext == "m3u8":
        emit("log", {"line": "⚠ 该直链为 HLS(m3u8)，已保存播放列表；如需合成视频请用支持 HLS 的下载器。", "tag": tag})
    _fname = os.path.basename(out_path)

    xhs_cover_file = ""
    if cover and str(cover).startswith("http"):
        _cb = os.path.splitext(out_path)[0] + ".jpg"
        try:
            download_url_to_file(cover, _cb, header, referer=url)
        except Exception:
            pass

        xhs_cover_file = _save_cover(src_local=_cb, cover_url=cover,
                                     title=title_final, author=author_final, referer=url)
    _record_history({
        "task_id": task_id, "platform": "xiaohongshu", "url": url, "title": title_final.strip(),
        "author": author_final, "likes": int(likes or 0), "views": int(views or 0),
        "duration": int(duration or 0), "cover": cover, "cover_file": xhs_cover_file,
        "filename": _fname,
        "path": out_path, "size": size, "status": "done", "progress": 100,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    })
    emit("done", {"filename": _fname, "path": out_path, "size": size,
                  "format": "原画直连", "skipped": False, "tag": tag, "cover": cover or ""})
    if task_key: _dl_set_status(task_key, "done")


def _is_douyin_url(u: str) -> bool:
    return bool(u) and ("douyin.com" in u or "v.douyin" in u)


DOUYIN_ANTIBOT_NOTE = (
    "抖音视频直链因平台反爬升级暂不可用：其 aweme 接口现要求请求携带播放器内部动态生成的 "
    "msToken，yt-dlp 无法提供，故报「Fresh cookies」。这不是登录态问题——重新登录或升级 yt-dlp "
    "均无效。抖音搜索/关键词查找（浏览器通道）仍正常；视频直链下载需等 yt-dlp 官方支持或逆向签名，"
    "短期内不可行。"
)


def _douyin_metadata_fallback(url: str, timeout: int = 18) -> dict | None:
    if not _is_douyin_url(url):
        return None
    if not cm.playwright_available():
        return None
    try:
        headers = cm.load_all_headers(STORE)
        if not headers.get("douyin"):
            return None
    except Exception:
        return None
    res: dict = {}

    def runner() -> None:
        try:
            bs = BrowserSearcher(cookies_by_platform=headers, profile_root=cm.PROFILE_DIR, headless=True)
            if not bs.available:
                return
            v = bs.resolve_one("douyin", url)
            if v and (v.title or v.author):
                res["v"] = {
                    "title": v.title or "",
                    "author": v.author or "",
                    "cover": v.cover or "",
                    "duration": int(v.duration or 0),
                    "likes": int(v.likes or 0),
                    "views": int(v.views or 0),
                    "url": v.url or url,
                }
        except Exception:
            return

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    return res.get("v")


def _resolve_media_via_browser(platform: str, url: str, cookie_file: str = "",
                               profile_root: str = "") -> "object | None":
    if platform not in ("kuaishou", "douyin"):
        return None
    if not cm.playwright_available():
        return None
    _root = profile_root or cm.PROFILE_DIR
    has_auth = bool(cm.load_all_headers(STORE).get(platform)) or os.path.isdir(os.path.join(_root, platform))
    if not has_auth:
        return None
    try:
        bs = BrowserSearcher(cookies_by_platform=cm.load_all_headers(STORE), profile_root=_root, headless=True)
        if not bs.available:
            return None
        return bs.resolve_one(platform, url)
    except Exception:
        return None


def _run_direct_download(emit, url, media_url, platform, cookie_file, cookie_label,
                         tag="", task_id="", title="", author="", likes=0, views=0,
                         duration=0, cover="", profile_root="", vcodec="", task_key="",
                         stop_event=None):
    if not media_url:
        emit("error", {"message": "缺少直连视频地址，无法直连下载。", "tag": tag});
        return
    if task_key:
        if stop_event and stop_event.is_set():
            _dl_set_status(task_key, "stopped");
            return
        _dl_set_status(task_key, "downloading")
    emit("meta", {"title": title or url, "platform": platform, "cookie": cookie_label,
                  "yt_dlp": "-", "ffmpeg": False, "format": "原画直连", "merge": False,
                  "container": "", "tag": tag, "cover": cover or ""})
    _m = re.search(r"short-video/([A-Za-z0-9]+)", url) if platform == "kuaishou" else (
        re.search(r"video/(\d+)", url) if platform == "douyin" else None)
    nid = _m.group(1) if _m else platform
    title_final = (title or "").strip() or nid
    author_final = (author or "").strip()
    ext = "m3u8" if ".m3u8" in media_url else "mp4"
    fname = _final_name(title_final, author_final, duration, ext)
    out_path = _unique_path(fname)
    header = netscape_to_header(cookie_file)
    referer = {"kuaishou": "https://www.kuaishou.com/", "douyin": "https://www.douyin.com/",
               "bilibili": "https://www.bilibili.com/"}.get(platform, url)
    try:
        def prog(done, total):
            pct = (done / total * 100) if total else 0
            emit("progress", {"percent": f"{pct:.1f}%", "done": done, "total": total,
                              "speed": "", "eta": "", "tag": tag})

        size = download_url_to_file(media_url, out_path, header, referer=referer, emit_progress=prog)
    except Exception as e:
        emit("error", {"message": f"直连下载失败：{e}", "tag": tag})
        if task_key: _dl_set_status(task_key, "error")
        return
    _fname = os.path.basename(out_path)
    cover_file = ""
    if cover and str(cover).startswith("http"):
        _cb = os.path.splitext(out_path)[0] + ".jpg"
        try:
            download_url_to_file(cover, _cb, header, referer=referer)
        except Exception:
            pass
        cover_file = _save_cover(src_local=_cb, cover_url=cover, title=title_final,
                                 author=author_final, referer=referer)

    if vcodec and out_path and os.path.isfile(out_path):
        out_path = _maybe_transcode(out_path, vcodec, emit, tag)
        _fname = os.path.basename(out_path)
    _record_history({
        "task_id": task_id, "platform": platform, "url": url, "title": title_final.strip(),
        "author": author_final, "likes": int(likes or 0), "views": int(views or 0),
        "duration": int(duration or 0), "cover": cover, "cover_file": cover_file,
        "filename": _fname, "path": out_path, "size": size, "status": "done", "progress": 100,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    })
    emit("done", {"filename": _fname, "path": out_path, "size": size,
                  "format": "原画直连", "skipped": False, "tag": tag, "cover": cover or ""})
    if task_key: _dl_set_status(task_key, "done")


def _run_bili_tracks_download(emit, url, v_url, a_url, platform, cookie_file, cookie_label,
                         tag="", task_id="", title="", author="", likes=0, views=0,
                         duration=0, cover="", task_key="", stop_event=None, vcodec=""):
    """B 站 DASH 分轨下载：视频轨(+音频轨)下载后 ffmpeg 合并，绕开 yt-dlp 412。"""
    if not v_url:
        emit("error", {"message": "缺少视频轨直链。", "tag": tag})
        return
    if task_key:
        if stop_event and stop_event.is_set():
            _dl_set_status(task_key, "stopped")
            return
        _dl_set_status(task_key, "downloading")
    emit("meta", {"title": title or url, "platform": platform, "cookie": cookie_label,
                  "yt_dlp": "-", "ffmpeg": bool(find_ffmpeg()), "format": "DASH分轨",
                  "merge": bool(a_url), "container": "mp4", "tag": tag, "cover": cover or ""})
    _mm = re.search(r"video/(\d+)", url)
    nid = _mm.group(1) if _mm else platform
    title_final = (title or "").strip() or nid
    author_final = (author or "").strip()
    fname = _final_name(title_final, author_final, duration, "mp4")
    out_path = _unique_path(fname)
    header = netscape_to_header(cookie_file)
    referer = "https://www.bilibili.com/"
    tmpdir = tempfile.mkdtemp(prefix="bili_")
    v_tmp = os.path.join(tmpdir, "video.mp4")
    a_tmp = os.path.join(tmpdir, "audio.m4a") if a_url else ""
    size = 0
    try:
        def prog(done, total):
            pct = (done / total * 100) if total else 0
            emit("progress", {"percent": f"{pct:.1f}%", "done": done, "total": total,
                              "speed": "", "eta": "", "tag": tag})

        size = download_url_to_file(v_url, v_tmp, header, referer=referer, emit_progress=prog)
        if a_url:
            download_url_to_file(a_url, a_tmp, header, referer=referer)
            ff = find_ffmpeg()
            if not ff:
                import shutil as _sh
                _sh.copyfile(v_tmp, out_path)
            else:
                ffmpeg = os.path.join(ff, "ffmpeg.exe") if os.path.isfile(os.path.join(ff, "ffmpeg.exe")) else os.path.join(ff, "ffmpeg")
                cmd = [ffmpeg, "-y", "-i", v_tmp, "-i", a_tmp, "-c", "copy",
                       "-movflags", "+faststart", out_path]
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if p.returncode != 0:
                    cmd2 = [ffmpeg, "-y", "-i", v_tmp, "-i", a_tmp,
                            "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", out_path]
                    p2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=900)
                    if p2.returncode != 0:
                        raise RuntimeError("ffmpeg 合并失败: " + (p2.stderr or "")[-200:])
        else:
            import shutil as _sh
            _sh.copyfile(v_tmp, out_path)
        if not os.path.isfile(out_path):
            raise RuntimeError("输出文件未生成")
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        emit("error", {"message": f"分轨下载失败：{e}", "tag": tag})
        if task_key:
            _dl_set_status(task_key, "error")
        return
    shutil.rmtree(tmpdir, ignore_errors=True)
    _fname = os.path.basename(out_path)
    cover_file = ""
    if cover and str(cover).startswith("http"):
        _cb = os.path.splitext(out_path)[0] + ".jpg"
        try:
            download_url_to_file(cover, _cb, header, referer=referer)
        except Exception:
            pass
        cover_file = _save_cover(src_local=_cb, cover_url=cover, title=title_final,
                                 author=author_final, referer=referer)
    if vcodec and os.path.isfile(out_path):
        out_path = _maybe_transcode(out_path, vcodec, emit, tag)
        _fname = os.path.basename(out_path)
    _record_history({
        "task_id": task_id, "platform": platform, "url": url, "title": title_final.strip(),
        "author": author_final, "likes": int(likes or 0), "views": int(views or 0),
        "duration": int(duration or 0), "cover": cover, "cover_file": cover_file,
        "filename": _fname, "path": out_path, "size": size, "status": "done", "progress": 100,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    })
    emit("done", {"filename": _fname, "path": out_path, "size": size,
                  "format": "DASH分轨", "skipped": False, "tag": tag, "cover": cover or ""})
    if task_key:
        _dl_set_status(task_key, "done")


def _xhs_items_from_resolved(resolved: list[dict]) -> tuple[list, list]:
    items: list[dict] = []
    notes: list[str] = []
    seen: set[str] = set()
    for r in resolved:
        if not r.get("title") and not r.get("media_url") and not r.get("error"):
            notes.append(f"小红书未解析到内容：{r.get('url')}")
            continue
        if r.get("error") and not r.get("title"):
            notes.append(f"小红书解析失败（可能登录态失效或链接无效）：{r.get('url')}")
            continue
        u = r.get("url")
        if u in seen:
            continue
        seen.add(u)
        items.append({
            "id": r.get("nid") or u,
            "title": r.get("title") or "(无标题)",
            "url": u,
            "thumbnail": r.get("cover") or "",
            "duration": r.get("duration") or 0,
            "uploader": r.get("author") or "",
            "extractor": "xiaohongshu",
            "platform": "xiaohongshu",
            "views": r.get("views") or 0,
            "likes": r.get("likes") or 0,
            "media_url": r.get("media_url") or "",
        })

        if r.get("cover") and str(r["cover"]).startswith("http"):
            cf = _save_cover(cover_url=r["cover"], title=r.get("title"),
                             author=r.get("author"), referer=u)
            if cf:
                items[-1]["cover_file"] = cf
    return items, notes


def _bili_playlist_type(url: str) -> dict:
    import urllib.parse as _up
    s = url or ""
    low = s.lower()
    q: dict = {}
    if "?" in s:
        try:
            q = dict(_up.parse_qsl(_up.urlparse(s).query))
        except Exception:
            q = {}
    if "collectiondetail" in low and q.get("sid"):
        return {"is_playlist": True, "playlist_type": "合集", "playlist_id": str(q["sid"]), "url": s}
    if "favlist" in low and q.get("fid"):
        return {"is_playlist": True, "playlist_type": "收藏夹", "playlist_id": str(q["fid"]), "url": s}
    if "bangumi/play" in low:
        m = re.search(r"/(ss\d+|ep\d+)", low)
        return {"is_playlist": True, "playlist_type": "番剧/影视",
                "playlist_id": (m.group(1) if m else ""), "url": s}
    if "video/bv" in low or re.search(r"/video/av\d+", low, re.I):
        return {"is_playlist": False, "playlist_type": "单视频", "playlist_id": "", "url": s,
                "maybe_multipart": bool(q.get("p"))}
    return {"is_playlist": False, "playlist_type": "", "playlist_id": "", "url": s}


def _bili_bvid_of(url: str) -> str | None:
    m = re.search(r"(BV[0-9A-Za-z]+)", url, re.I)
    return m.group(1) if m else None


def bili_api_meta(bvid: str) -> dict | None:
    """B 站视频元数据 API（数据中心 IP 可直连，绕开 412 风控）。"""
    if not bvid:
        return None
    try:
        import urllib.request as _ureq
        api = "https://api.bilibili.com/x/web-interface/view?bvid=" + bvid
        req = _ureq.Request(api, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com",
        })
        with _ureq.urlopen(req, timeout=15) as resp:
            txt = resp.read().decode("utf-8", "replace")
        d = json.loads(txt)
        if d.get("code") not in (0, None):
            return None
        data = d.get("data") or {}
        owner = data.get("owner") or {}
        stat = data.get("stat") or {}
        return {
            "bvid": bvid, "aid": data.get("aid"), "cid": data.get("cid"),
            "title": data.get("title") or "", "author": owner.get("name") or "",
            "duration": int(data.get("duration") or 0),
            "views": int(stat.get("view") or 0), "likes": int(stat.get("like") or 0),
            "pic": data.get("pic") or "",
        }
    except Exception:  # noqa: BLE001
        return None


def bili_api_durl(bvid: str, cid=None, qn: int = 64) -> str | None:
    """B 站视频直链 API（playurl，单文件 flv/mp4），数据中心 IP 可直连。"""
    if not bvid:
        return None
    try:
        import urllib.request as _ureq
        if not cid:
            _m = bili_api_meta(bvid)
            if not _m or not _m.get("cid"):
                return None
            cid = _m["cid"]
        api = ("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=%d&fnval=1&fourk=1"
               % (bvid, cid, int(qn)))
        req = _ureq.Request(api, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com",
        })
        with _ureq.urlopen(req, timeout=15) as resp:
            txt = resp.read().decode("utf-8", "replace")
        d = json.loads(txt)
        if d.get("code") not in (0, None):
            return None
        data = d.get("data") or {}
        durl = data.get("durl") or []
        if durl:
            return (durl[0].get("url") or "").strip() or None
        dash = data.get("dash") or {}
        vids = dash.get("video") or []
        if vids:
            return (vids[0].get("baseUrl") or vids[0].get("base_url") or "").strip() or None
        return None
    except Exception:  # noqa: BLE001
        return None


def bili_api_formats(bvid: str, cid=None, force=False) -> dict | None:
    """B 站 DASH 全轨格式枚举（playurl fnval=4048）：返回 video/audio 全部轨道（含直链）。
    数据中心 IP 可直连，绕开视频页 412 风控。"""
    if not bvid:
        return None
    key = (bvid, cid or 0)
    if not force and key in _BILI_FMT_CACHE:
        return _BILI_FMT_CACHE[key]
    try:
        import urllib.request as _ureq
        if not cid:
            _m = bili_api_meta(bvid)
            if not _m or not _m.get("cid"):
                return None
            cid = _m["cid"]
        api = ("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=127&fnval=4048&fourk=1"
               % (bvid, cid))
        req = _ureq.Request(api, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com",
        })
        with _ureq.urlopen(req, timeout=20) as resp:
            txt = resp.read().decode("utf-8", "replace")
        d = json.loads(txt)
        if d.get("code") not in (0, None):
            return None
        data = d.get("data") or {}
        dash = data.get("dash") or {}
        codec_map = {1: "AVC", 12: "HEVC", 13: "AV1"}
        video, audio = [], []
        for v in dash.get("video") or []:
            h = int(v.get("height") or 0)
            fid = v.get("id")
            if not h or not fid:
                continue
            u = (v.get("baseUrl") or v.get("base_url") or "").strip()
            if not u:
                continue
            video.append({
                "format_id": str(fid), "height": h,
                "width": int(v.get("width") or 0),
                "fps": v.get("frame_rate") or 0,
                "codec": codec_map.get(v.get("codecid"), "AVC"),
                "tbr": int((v.get("bandwidth") or 0) / 1000),
                "filesize": int(v.get("size") or 0),
                "url": u, "note": "B站DASH",
            })
        for a in dash.get("audio") or []:
            fid = a.get("id")
            if not fid:
                continue
            u = (a.get("baseUrl") or a.get("base_url") or "").strip()
            if not u:
                continue
            abr = int((a.get("bandwidth") or 0) / 1000)
            audio.append({
                "format_id": str(fid), "abr": abr,
                "asr": int(a.get("sampling_rate") or 0),
                "codec": "AAC", "filesize": int(a.get("size") or 0),
                "url": u, "note": f"{abr}kbps",
            })
        seen_h: set = set()
        uniq_v = []
        for v in sorted(video, key=lambda x: (x["height"], -x["tbr"]), reverse=True):
            if v["height"] in seen_h:
                continue
            seen_h.add(v["height"])
            uniq_v.append(v)
        audio.sort(key=lambda x: -x["abr"])
        if not uniq_v and not audio:
            return None
        result = {"video": uniq_v, "audio": audio}
        _BILI_FMT_CACHE[key] = result
        return result
    except Exception:  # noqa: BLE001
        return None


def _bili_fmt_url(bvid, cid, fmt_id):
    """按 format_id 找直链；fmt_id 形如 '80' 或 '80+30280'。"""
    if not fmt_id:
        return None, None
    fmts = bili_api_formats(bvid, cid)
    if not fmts:
        return None, None
    v_id = a_id = None
    if "+" in fmt_id:
        v_id, a_id = fmt_id.split("+", 1)
    else:
        v_id = fmt_id
    v_url = next((x["url"] for x in fmts["video"] if x["format_id"] == v_id), "") if v_id else ""
    a_url = next((x["url"] for x in fmts["audio"] if x["format_id"] == a_id), "") if a_id else ""
    return (v_url or None), (a_url or None)


_BILI_FMT_CACHE: dict = {}


def _bili_collection_of(bvid: str):
    if not bvid:
        return None
    try:
        import urllib.request as _ureq
        api = "https://api.bilibili.com/x/web-interface/view?bvid=" + bvid
        req = _ureq.Request(api, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com",
        })
        with _ureq.urlopen(req, timeout=15) as resp:
            txt = resp.read().decode("utf-8", "replace")
        d = json.loads(txt)
        if d.get("code") not in (0, None):
            return None
        data = d.get("data") or {}
        us = data.get("ugc_season") or {}
        if not us.get("id"):
            return None
        episodes = []
        for sec in (us.get("sections") or []):
            for e in (sec.get("episodes") or []):
                arc = e.get("arc") or {}
                bv = e.get("bvid") or arc.get("bvid") or ""
                aid = e.get("aid") or arc.get("aid") or ""
                if not bv and not aid:
                    continue
                stat = arc.get("stat") or {}
                episodes.append({
                    "bvid": bv,
                    "aid": aid,
                    "title": e.get("title") or arc.get("title") or "",
                    "cover": arc.get("pic") or "",
                    "duration": arc.get("duration") or 0,
                    "views": stat.get("view") or 0,
                    "likes": stat.get("like") or 0,
                    "page": e.get("page") or 1,
                })
        if not episodes:
            return None
        return {"id": str(us.get("id")), "title": us.get("title") or "",
                "uploader": (data.get("owner") or {}).get("name") or "",
                "episodes": episodes}
    except Exception:
        return None


def batch_info(urls, platform="", expand_playlist=True):
    xhs_urls = [u for u in urls if "xiaohongshu.com" in u]
    other = [u for u in urls if "xiaohongshu.com" not in u]
    items: list[dict] = []
    notes: list[str] = []
    if xhs_urls:
        cookie_file = STORE.netscape_path("xiaohongshu") if STORE.load("xiaohongshu") else ""
        resolved = resolve_xhs_links(xhs_urls, profile_root=cm.PROFILE_DIR, cookie_file=cookie_file)
        xi, xn = _xhs_items_from_resolved(resolved)
        items += xi
        notes += xn
    if not other:
        return items, notes, []

    cmd, _ = resolve_ytdlp_cmd()
    if not cmd:
        return items, notes + ["未检测到 yt-dlp：pip install yt-dlp"], []
    ffmpeg_dir = find_ffmpeg()
    seen: set[str] = set()
    playlist_detected: list[dict] = []
    for u in other:
        plat, cookie_file, _ = _pick_cookie(u, platform)
        is_bili = ("bilibili.com" in u) or (plat == "bilibili")
        pinfo = _bili_playlist_type(u) if is_bili else {"is_playlist": False}

        _col = None
        if is_bili and not pinfo.get("is_playlist") and pinfo.get("playlist_type") == "单视频":
            _m = re.search(r"(BV[0-9A-Za-z]+)", u, re.I)
            if _m:
                _col = _bili_collection_of(_m.group(1))
                if _col:
                    pinfo = {"is_playlist": True, "playlist_type": "合集",
                             "playlist_id": _col["id"], "title": _col["title"],
                             "url": u, "_collection": _col}

        if is_bili and pinfo.get("playlist_type") == "合集" and pinfo.get("_collection") and expand_playlist:
            _col = pinfo["_collection"]
            playlist_count = len(_col["episodes"])
            _owner = _col.get("uploader") or ""
            for _ep in _col["episodes"]:
                _bv = _ep.get("bvid")
                if not _bv and _ep.get("aid"):
                    _bv = "av" + str(_ep["aid"])
                if not _bv:
                    continue
                _eurl = "https://www.bilibili.com/video/" + _bv
                if _eurl in seen:
                    continue
                seen.add(_eurl)
                items.append({
                    "id": _bv,
                    "title": _ep.get("title") or "(无标题)",
                    "url": _eurl,
                    "thumbnail": _ep.get("cover") or "",
                    "duration": _ep.get("duration") or 0,
                    "uploader": _owner,
                    "extractor": "BiliBili",
                    "platform": "bilibili",
                    "views": _ep.get("views") or 0,
                    "likes": _ep.get("likes") or 0,
                    "playlist_type": "合集",
                    "playlist_id": _col["id"],
                    "meta_source": "bilibili_collection",
                })
            playlist_detected.append({"url": u, "type": "合集",
                                      "count": playlist_count, "title": _col["title"], "expanded": True})
            continue

        if pinfo.get("playlist_type") == "合集" and pinfo.get("_collection") and not expand_playlist:
            pinfo = {"is_playlist": False, "playlist_type": "单视频", "playlist_id": "", "url": u}

        args = list(cmd) + ["-J", "--flat-playlist", "--no-warnings", "--skip-download"]
        if ffmpeg_dir:
            args += ["--ffmpeg-location", ffmpeg_dir]
        args.append(u)

        out = None
        last_err = ""
        with _cookie_args(cookie_file) as _ck:
            _args = args + _ck
            for _attempt in range(2):
                try:
                    out = subprocess.run(_args, capture_output=True, text=True, timeout=45,
                                         encoding="utf-8", errors="replace")
                except subprocess.TimeoutExpired:
                    notes.append(f"解析超时（跳过）：{u}");
                    out = None;
                    break
                except Exception as e:
                    notes.append(f"解析失败：{u} —— {e}");
                    out = None;
                    break
                if out.returncode == 0:
                    break
                last_err = (out.stderr or out.stdout or "").strip()
                if _attempt == 0:
                    continue
        if out is None or out.returncode != 0:
            err = (last_err or "未知错误")[:220]
            if _is_douyin_url(u):
                _m = re.search(r"video/(\d+)", u)
                _rv = None
                try:
                    _rv = _resolve_media_via_browser("douyin", u, cookie_file, cm.PROFILE_DIR)
                except Exception:
                    _rv = None
                if _rv and (_rv.media_url or "").strip():
                    items.append({
                        "id": (_m.group(1) if _m else u),
                        "title": _rv.title or "(抖音视频)",
                        "url": u, "thumbnail": _rv.cover or "",
                        "duration": int(_rv.duration or 0),
                        "uploader": _rv.author or "", "extractor": "Douyin",
                        "platform": "douyin", "views": 0, "likes": 0,
                        "media_url": (_rv.media_url or "").strip(),
                        "media_unavailable": False,
                        "meta_source": "douyin_browser",
                    })
                    notes.append("抖音视频已通过浏览器通道解析（标题/封面/直链已获取）。")
                else:
                    items.append({
                        "id": (_m.group(1) if _m else u),
                        "title": "(抖音视频·下载时解析)",
                        "url": u, "thumbnail": "", "duration": 0,
                        "uploader": "", "extractor": "Douyin", "platform": "douyin",
                        "views": 0, "likes": 0,
                        "media_url": "",
                        "media_unavailable": False,
                        "meta_source": "douyin_browser_pending",
                    })
                    notes.append("抖音视频解析失败，将在下载阶段通过浏览器通道重试。")
            else:
                _bv = _bili_bvid_of(u) if is_bili else None
                if _bv:
                    _m = bili_api_meta(_bv)
                    if _m and _m.get("title"):
                        _mu = bili_api_durl(_bv, _m.get("cid"))
                        items.append({
                            "id": _bv, "title": _m["title"], "url": u,
                            "thumbnail": _m["pic"], "duration": _m["duration"],
                            "uploader": _m["author"], "extractor": "BiliBili",
                            "platform": "bilibili", "views": _m["views"], "likes": _m["likes"],
                            "media_url": _mu or "", "meta_source": "bilibili_api",
                        })
                        notes.append("B 站视频信息已通过官方 API 获取（绕开服务器 IP 风控）。")
                        continue
                notes.append(f"无法解析（{err or '未知错误'}）：{u}")
            continue
        try:
            j = json.loads(out.stdout)
        except Exception:
            notes.append(f"解析结果异常：{u}");
            continue
        ek_root = j.get("extractor") or "unknown"
        raw_entries = [e for e in (j.get("entries") or [j]) if e]

        if is_bili and not pinfo.get("is_playlist") and len(raw_entries) > 1:
            pinfo = {**pinfo, "is_playlist": True, "playlist_type": "分P"}

        playlist_count = len(raw_entries) if (is_bili and pinfo.get("is_playlist")
                                              and not (
                            pinfo.get("playlist_type") == "合集" and pinfo.get("_collection"))) else 0

        if is_bili and pinfo.get("is_playlist") and pinfo.get("playlist_type") != "分P" and not expand_playlist:
            _pl_item = {
                "id": j.get("id") or u,
                "title": j.get("title") or pinfo.get("playlist_type") or "B站播放列表",
                "url": u,
                "thumbnail": j.get("thumbnail") or "",
                "duration": 0,
                "uploader": j.get("uploader") or j.get("channel") or "",
                "extractor": ek_root,
                "platform": "bilibili",
                "views": j.get("view_count") or 0,
                "likes": j.get("like_count") or 0,
                "is_playlist": True,
                "playlist_type": pinfo.get("playlist_type") or "播放列表",
                "playlist_count": playlist_count,
            }
            _pl_item = _merge_batch_meta(_pl_item, cookie_file)
            if _pl_item.get("thumbnail") and str(_pl_item["thumbnail"]).startswith("http"):
                _cf = _save_cover(cover_url=_pl_item["thumbnail"], title=_pl_item.get("title"),
                                  author=_pl_item.get("uploader"), referer=_pl_item.get("url"))
                if _cf:
                    _pl_item["cover_file"] = _cf
            items.append(_pl_item)
            playlist_detected.append({"url": u, "type": _pl_item["playlist_type"],
                                      "count": playlist_count, "title": _pl_item["title"], "expanded": False})
            continue
        for e in raw_entries:
            if not e:
                continue
            eurl = e.get("webpage_url") or e.get("url") or u
            if not eurl or eurl in seen:
                continue
            seen.add(eurl)
            ek = e.get("extractor") or e.get("ie_key") or ek_root
            plat_e = str(ek).split(".")[-1].lower() if "." in str(ek) else str(ek).lower()
            item = {
                "id": e.get("id") or eurl,
                "title": e.get("title") or "(无标题)",
                "url": eurl,
                "thumbnail": e.get("thumbnail") or "",
                "duration": e.get("duration") or 0,

                "uploader": _yt_author(e, plat_e),
                "extractor": ek,
                "platform": plat_e,
                "views": e.get("view_count") or j.get("view_count") or 0,
                "likes": e.get("like_count") or e.get("track_likes") or 0,
            }
            item = _merge_batch_meta(item, cookie_file)
            if is_bili and pinfo.get("is_playlist"):
                item["playlist_type"] = pinfo.get("playlist_type") or ""
                item["playlist_id"] = pinfo.get("playlist_id") or ""

            if item.get("thumbnail") and str(item["thumbnail"]).startswith("http"):
                cf = _save_cover(cover_url=item["thumbnail"], title=item.get("title"),
                                 author=item.get("uploader"), referer=item.get("url"))
                if cf:
                    item["cover_file"] = cf
            items.append(item)
        if is_bili and pinfo.get("is_playlist"):
            playlist_detected.append({"url": u, "type": pinfo.get("playlist_type") or "播放列表",
                                      "count": playlist_count, "title": j.get("title") or "", "expanded": True})
    return items, notes, playlist_detected


def _merge_batch_meta(item: dict, cookie_file: str = "") -> dict:
    plat = (item.get("platform") or "").lower()
    url = item.get("url") or ""
    if plat == "bilibili":
        m = _collect_meta_timed(url, "bilibili", cookie_file, cm.PROFILE_DIR, 8)
    elif plat in ("douyin", "kuaishou"):
        if not (cm.load_all_headers(STORE).get(plat) or os.path.isdir(os.path.join(cm.PROFILE_DIR, plat))):
            return item
        m = _collect_meta_timed(url, plat, cookie_file, cm.PROFILE_DIR, 8)
    else:
        return item
    if m:
        item["title"] = m.get("title") or item["title"]
        item["uploader"] = m.get("author") or item["uploader"]
        item["thumbnail"] = m.get("cover") or item["thumbnail"]
        item["likes"] = m.get("likes") or item["likes"]
        item["views"] = m.get("views") or item["views"]
        item["duration"] = m.get("duration") or item["duration"]
        item["meta_source"] = m.get("source", "collector")
    return item


def _bili_resolve_uid(name_or_uid, client):
    q = (name_or_uid or "").strip()
    if not q:
        return None, ""
    m = re.search(r"space\.bilibili\.com/(\d+)", q)
    if m:
        return m.group(1), ""
    if re.fullmatch(r"\d{4,}", q):
        return q, ""

    for _attempt in range(3):
        try:
            bs = BilibiliSearcher(client)
            bs._ensure_keys()
            params = bs._sign({"keyword": q, "page": 1})
            text, _ = client.get(
                "https://api.bilibili.com/x/web-interface/wbi/search/all/v2", params=params)
        except Exception:
            time.sleep(2)
            continue
        if not text:
            time.sleep(2)
            continue
        try:
            payload = json.loads(text)
        except Exception:
            time.sleep(2)
            continue
        for group in payload.get("data", {}).get("result", []) or []:
            if group.get("result_type") != "bili_user":
                continue
            for item in group.get("data", []) or []:
                if not isinstance(item, dict):
                    continue
                uname = item.get("uname") or item.get("name") or ""
                mid = item.get("mid") or item.get("uid") or item.get("id")
                if mid and uname:
                    return str(mid), uname

        time.sleep(3)
    return None, ""


def _ytdlp_list(url: str, limit: int, platform: str, author_hint: str = "") -> list:
    cmd, _ = resolve_ytdlp_cmd()
    if not cmd:
        return []
    args = cmd + ["--flat-playlist", "--no-warnings", "-J",
                  "--playlist-end", str(max(1, limit)), url]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=240, env=env)
    except Exception:
        return []
    try:
        data = json.loads(p.stdout)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("entries") or []
    out: list[VideoLink] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        eid = e.get("id")
        u = e.get("url") or e.get("webpage_url")
        if not u and eid:
            u = f"https://www.bilibili.com/video/{eid}" if platform == "bilibili" else str(eid)
        if not u:
            continue
        if str(u).startswith("//"):
            u = "https:" + u
        if not str(u).startswith("http"):
            continue
        title = (e.get("title") or "").strip()
        author = (author_hint or e.get("uploader") or e.get("channel") or e.get("channel_id") or "").strip()
        out.append(VideoLink(platform=platform, keyword="", title=title, url=u, author=author))
        if len(out) >= limit:
            break
    return out


def _author_works_bilibili(name_or_uid, limit, client, keyword: str | None = None) -> list:
    uid, disp = _bili_resolve_uid(name_or_uid, client)
    if not uid:
        return []
    bs = BilibiliSearcher(client)
    try:
        bs._ensure_keys()
    except Exception:
        pass
    out: list[VideoLink] = []
    ps = 30

    max_pages = min(200, max(10, (limit + ps - 1) // ps + 2))
    empty_streak = 0
    pn = 1

    def fetch_page(pn_: int) -> list:

        for _attempt in range(5):
            try:
                sp = {"mid": uid, "pn": pn_, "ps": ps, "order": "pubdate"}
                if keyword:
                    sp["keyword"] = keyword
                params = bs._sign(sp)
                text, _ = client.get("https://api.bilibili.com/x/space/wbi/arc/search", params=params)
            except Exception:
                time.sleep(3)
                continue
            if not text:
                time.sleep(3)
                continue
            try:
                j = json.loads(text) or {}
            except Exception:
                time.sleep(3)
                continue
            code = j.get("code", 0)
            d = j.get("data") or {}
            vlist = (d.get("list") or {}).get("vlist") or d.get("vlist") or []
            if vlist:
                return vlist

            time.sleep(6 if code not in (0, None) else 2)
        return []

    while len(out) < limit and pn <= max_pages:
        vlist = fetch_page(pn)
        if not vlist:

            time.sleep(8)
            vlist = fetch_page(pn)
            if not vlist:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                pn += 1
                continue
        empty_streak = 0
        for it in vlist:
            bvid = it.get("bvid")
            if not bvid:
                continue
            out.append(VideoLink(
                platform="bilibili", keyword="",
                title=(it.get("title") or "").strip(),
                url=f"https://www.bilibili.com/video/{bvid}",
                author=(it.get("author") or disp or "").strip(),
            ))
            if len(out) >= limit:
                break

        if len(vlist) < ps:
            break
        pn += 1
        time.sleep(2)
    return out


def _yt_channel_videos_url(q: str) -> str:
    s = q.strip()
    if not s:
        return ""

    if any(k in s for k in ("/watch", "/playlist", "/shorts", "/streams",
                            "/videos", "youtu.be/")):
        return s
    if "youtube.com/" in s:
        base = s.rstrip("/")
        return base + "/videos"
    if s.startswith("@"):
        return "https://www.youtube.com/" + s + "/videos"
    if s.startswith("UC") and len(s) > 10:
        return "https://www.youtube.com/channel/" + s + "/videos"
    if re.fullmatch(r"[A-Za-z0-9_-]{2,}", s):
        return "https://www.youtube.com/@" + s + "/videos"
    return ""


def _author_works_youtube(name_or_uid, limit) -> list:
    url = _yt_channel_videos_url(name_or_uid)
    if not url:
        return []
    return _ytdlp_list(url, limit, "youtube")


def _looks_like_channel_url(q: str, plat: str) -> bool:
    return bool(q) and q.startswith("http") and plat in q.lower()


def _keyword_search_one(plat: str, kw: str, limit: int, ctx: dict) -> list:
    if plat == "kuaishou":
        try:
            return KuaishouSearcher(ctx["make_client"]("kuaishou")).search(kw, limit)
        except Exception:
            return []
    if plat in BROWSER_SEARCH:
        try:
            bs = BrowserSearcher(cookies_by_platform=ctx["saved_headers"],
                                 profile_root=cm.PROFILE_DIR, headless=True)
            if bs._open_browser(plat):
                try:
                    return bs.search_keyword(plat, kw, limit)
                finally:
                    bs.close()
        except Exception:
            return []
    return []


def _json_get(client, url: str):
    try:
        text, _ = client.get(url)
    except Exception:
        return None
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _weibo_ensure_visitor(client) -> None:
    try:
        import re as _re
        probe, _ = client.get("https://m.weibo.cn/api/container/getIndex?containerid=100103type%3D1%26q%3Dtest")
        if not probe or "visitor" not in probe:
            return
        m = _re.search(r'tid["\']?\s*[:=]\s*["\']?([0-9a-fA-F]{16,})', probe)
        if not m:
            return
        client.get("https://passport.weibo.com/visitor/visitor?a=incarnate&cb=visitor_cb&tid=" + m.group(1))
    except Exception:
        return


def _author_works_weibo(name: str, limit: int, ctx: dict) -> list:
    client = ctx.get("client")
    q = (name or "").strip()
    if not client or not q:
        return []
    try:
        import urllib.parse as _up
        try:
            _weibo_ensure_visitor(client)
        except Exception:
            pass
        cid = "100103type%3D3%26q%3D" + _up.quote(q)
        d = _json_get(client, "https://m.weibo.cn/api/container/getIndex?containerid=" + cid)
        if not d:
            return []
        uid, uname = None, ""
        for c in (d.get("data") or {}).get("cards") or []:
            for g in (c.get("card_group") or []):
                u = (g.get("user") or {})
                if u.get("id"):
                    uid, uname = str(u.get("id")), u.get("screen_name") or ""
                    break
            if uid:
                break
        if not uid:
            return []
        out: list[VideoLink] = []
        for page in range(1, 6):
            d2 = _json_get(client,
                           "https://m.weibo.cn/api/container/getIndex?containerid=107603%s&page=%d" % (uid, page))
            if not d2:
                break
            cards2 = (d2.get("data") or {}).get("cards") or []
            hit = False
            for c in cards2:
                mb = c.get("mblog") or {}
                pi = mb.get("page_info") or {}
                if pi.get("type") != "video":
                    continue
                hit = True
                mid = mb.get("mid") or mb.get("id")
                if not mid:
                    continue
                out.append(VideoLink(
                    platform="weibo", keyword="",
                    title=(mb.get("text") or "").replace("\n", " ").strip()[:80],
                    url="https://m.weibo.cn/status/" + str(mid),
                    author=uname or (mb.get("user") or {}).get("screen_name") or "",
                ))
                if len(out) >= limit:
                    break
            if len(out) >= limit or not hit:
                break
            time.sleep(0.5)
        return out[:limit]
    except Exception:
        return []


def _author_works_kuaishou(name: str, limit: int, ctx: dict) -> list:
    q = (name or "").strip()
    if not q:
        return []
    try:
        from collector_core import KuaishouSearcher
        ks = KuaishouSearcher(ctx["make_client"]("kuaishou"))
        cand = ks.search(q, min(60, max(20, limit * 3)))
        if not cand:
            return []
        from collections import Counter
        cnt = Counter((v.author or "").strip() for v in cand if v.author)
        if not cnt:
            return []
        low = q.lower()
        target = None
        for cand_name, _ in cnt.most_common():
            cl = cand_name.lower()
            if cl == low or low in cl or cl in low:
                target = cand_name
                break
        if not target:
            target = cnt.most_common(1)[0][0]
        seen: set[str] = set()
        out: list[VideoLink] = []
        for v in cand:
            if (v.author or "").strip() != target:
                continue
            if v.url in seen:
                continue
            seen.add(v.url)
            out.append(v)
            if len(out) >= limit:
                break
        return out[:limit]
    except Exception:
        return []


def _douyin_secuid_from_text(txt: str, url: str = ""):
    src = (url or "") + "\n" + (txt or "")
    m = re.search(r"[?&]sec_uid=([A-Za-z0-9_-]+)", src)
    if m:
        return m.group(1)
    m = re.search(r"(?:share/)?user/([A-Za-z0-9_-]{16,})", src)
    if m:
        return m.group(1)
    m = re.search(r"sec_uid[\"'\s:=]+([A-Za-z0-9_-]{16,})", src)
    if m:
        return m.group(1)
    return None


def _douyin_resolve(spec: str, ctx: dict):
    s = (spec or "").strip()
    if not s:
        return None, None
    client = ctx.get("client") or HttpClient(delay=0.5)

    if "douyin.com" in s or "tiktok.com" in s:
        try:
            text, final = client.get(s)
            su = _douyin_secuid_from_text(text or "", final or s)
            if su:
                return su, None
        except Exception:
            pass

        return _douyin_secuid_from_text("", s), None

    if re.fullmatch(r"[A-Za-z0-9_-]{16,}", s):
        return s, None

    if re.fullmatch(r"\d{6,}", s):
        try:
            d = _json_get(client, "https://www.iesdouyin.com/web/api/v2/user/info/?uid=" + s)
            if isinstance(d, dict) and d.get("status_code") == 0:
                ui = d.get("user_info") or {}
                su = (ui.get("sec_uid") or "").strip()
                if su:
                    return su, (ui.get("nickname") or "").strip()
        except Exception:
            pass
        return None, None

    try:
        d = _json_get(client, "https://www.iesdouyin.com/web/api/v2/user/info/?unique_id="
                      + quote(s))
        if isinstance(d, dict) and d.get("status_code") == 0:
            ui = d.get("user_info") or {}
            su = (ui.get("sec_uid") or "").strip()
            if su:
                return su, (ui.get("nickname") or "").strip()
    except Exception:
        pass
    return None, None


def _douyin_resolve_sec_uid(spec: str, ctx: dict):
    return _douyin_resolve(spec, ctx)[0]


def _author_works_douyin(spec: str, limit: int, ctx: dict) -> list:
    sec_uid, nickname = _douyin_resolve(spec, ctx)
    if not sec_uid:
        return []
    url = "https://www.douyin.com/user/" + sec_uid
    cookies = dict(ctx.get("saved_headers", {}) or {})
    has_cookie = bool(cookies.get("douyin"))

    def _run(no_cookie: bool, clear_login: bool) -> list:
        bs = BrowserSearcher(cookies_by_platform={} if no_cookie else cookies,
                             profile_root=cm.PROFILE_DIR, headless=True)
        if not bs._open_browser("douyin"):
            return []
        try:
            return bs.list_user_videos("douyin", url, limit, clear_login=clear_login)
        finally:
            bs.close()

    vids = _run(no_cookie=False, clear_login=True)

    if not vids and has_cookie:
        vids = _run(no_cookie=True, clear_login=False)
    return vids[:limit]


def _author_works_other(name: str, plat: str, limit: int, ctx: dict) -> list:
    q = (name or "").strip()
    if not q:
        return []
    if plat == "weibo":
        return _author_works_weibo(q, limit, ctx)
    if plat == "kuaishou":
        return _author_works_kuaishou(q, limit, ctx)
    if _looks_like_channel_url(q, plat):
        return _ytdlp_list(q, limit, plat)
    got = _keyword_search_one(plat, q, limit, ctx)
    low = q.lower()
    out: list[VideoLink] = []
    for v in got:
        a = (v.author or "").strip().lower()
        if a and (low == a or low in a or a in low):
            out.append(v)
        if len(out) >= limit:
            break
    return out


def _search_author_works(author: str, platforms: list, max_per: int, ctx: dict,
                         kw_filter: list[str] | None = None) -> list:
    out: list[VideoLink] = []
    q = (author or "").strip()
    if not q:
        return out
    kws = [k.strip() for k in (kw_filter or []) if k and k.strip()]
    kw_query = " ".join(kws) if kws else None

    window = (max(max_per * 3, 60)) if kws else max_per
    lk = [k.lower() for k in kws]
    seen: set[str] = set()
    for plat in platforms:
        if len(out) >= max_per:
            break
        if plat == "bilibili":

            chunk = _author_works_bilibili(q, max_per - len(out), ctx["client"], keyword=kw_query)
        elif plat == "youtube":
            chunk = _author_works_youtube(q, window - len(out))
        elif plat == "douyin":
            chunk = _author_works_douyin(q, window - len(out), ctx)
        else:
            chunk = _author_works_other(q, plat, window - len(out), ctx)

        if kws:
            chunk = [v for v in chunk if any(k in (v.title or "").lower() for k in lk)]
        for v in chunk:
            if v.url and v.url not in seen:
                seen.add(v.url)
                out.append(v)
    return out[:max_per]


def run_collection(body: dict) -> dict:
    keywords: list[str] = []
    if body.get("keywords"):
        keywords += [k.strip() for k in str(body["keywords"]).replace("\n", ",").split(",") if k.strip()]
    if body.get("keywords_file"):
        for line in str(body["keywords_file"]).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                keywords.append(line)

    platforms = [p.strip().lower() for p in str(body.get("platforms", "bilibili")).split(",") if p.strip()]
    max_per = int(body.get("max_per_keyword", 50))
    enrich = bool(body.get("enrich", True))
    delay = float(body.get("delay", 1.0))
    browser_search = bool(body.get("browser_search", False)) or cm.playwright_available()
    use_saved = bool(body.get("use_saved_cookies", True))

    client = HttpClient(delay=delay)
    notes: list[str] = []

    saved_headers: dict[str, str] = {}
    if use_saved:
        saved_headers = cm.load_all_headers(STORE)
        for plat, header in saved_headers.items():
            client.set_domain_cookies(PLATFORM_DOMAIN[plat], header)
        if saved_headers:
            notes.append("已启用保存的 Cookie：" + "、".join(
                f"{cm.PLATFORM_META[p]['label']}({len(header.split(';'))}条)"
                for p, header in saved_headers.items()))

    raw_cookies = body.get("cookies")
    if raw_cookies and isinstance(raw_cookies, str) and raw_cookies.strip():
        ck, meta, fmt = cm.parse_cookie_text(raw_cookies)
        if ck:
            plat = cm.detect_platform(ck, meta)
            header = "; ".join(f"{k}={v}" for k, v in ck.items())
            if plat:
                client.merge_domain_cookies(PLATFORM_DOMAIN[plat], header)
                saved_headers[plat] = header
                notes.append(f"本次临时 Cookie：{fmt} -> {cm.PLATFORM_META[plat]['label']}（{len(ck)} 条）")
            else:
                client._cookie_header = header
                notes.append(f"本次临时 Cookie：{fmt}（{len(ck)} 条，未识别平台，按全局使用）")
        else:
            notes.append("临时 Cookie 未能解析，已忽略。")

    results: list[VideoLink] = []
    plat_count: dict[str, int] = {}

    def _budget(plat: str) -> int:
        return max(0, max_per - plat_count.get(plat, 0))

    resolve_text = body.get("resolve_text", "")
    if resolve_text:
        results += ShareResolver(client).resolve(str(resolve_text))

    if keywords:
        bili = BilibiliSearcher(client) if "bilibili" in platforms else None
        ks = KuaishouSearcher(client) if "kuaishou" in platforms else None
        browser = None
        if browser_search:
            browser = BrowserSearcher(cookies_by_platform=saved_headers,
                                      profile_root=cm.PROFILE_DIR, headless=True)
            if not browser.available:
                notes.append("未安装 Playwright，浏览器搜索已跳过：pip install playwright && playwright install chromium")

        browser_ok = bool(browser and browser.available)

        need_browser = [p for p in platforms
                        if p != "bilibili" and p in BROWSER_SEARCH]
        unsupported = [p for p in platforms
                       if p != "bilibili" and p not in BROWSER_SEARCH]
        if unsupported:
            notes.append("以下平台暂不支持关键词搜索，请改用分享链接解析：" + "、".join(unsupported))
        if need_browser and not browser_ok:
            notes.append("以下平台的关键词搜索需勾选「浏览器搜索」并安装 Playwright："
                         + "、".join(cm.PLATFORM_META.get(p, {}).get("label", p) for p in need_browser)
                         + "；或改用分享链接解析通道。")

        for p in need_browser:
            if BROWSER_SEARCH.get(p, {}).get("requires_login") and p not in saved_headers:
                notes.append(f"{cm.PLATFORM_META.get(p, {}).get('label', p)} 关键词搜索需登录态 Cookie："
                             f"请先在 Cookie 管理页导入该平台登录态（如 Instagram 的 sessionid/csrftoken），"
                             f"否则会被登录墙拦截返回 0 条。")

        for kw in keywords:
            if bili and _budget("bilibili") > 0:
                links = bili.search(kw, _budget("bilibili"))
                if enrich:
                    bili.enrich(links)
                results += links
                plat_count["bilibili"] = plat_count.get("bilibili", 0) + len(links)

            for plat in need_browser:
                if plat_count.get(plat, 0) >= max_per:
                    continue
                if browser_ok:
                    got = browser.search(plat, kw, _budget(plat))
                    results += got
                    plat_count[plat] = plat_count.get(plat, 0) + len(got)
                    if not got and plat not in saved_headers:
                        notes.append(f"{cm.PLATFORM_META.get(plat, {}).get('label', plat)}"
                                     "未返回数据：建议在 Cookie 面板导入登录态 Cookie 后重试。")
                elif plat == "kuaishou" and ks:

                    got = ks.search(kw, _budget("kuaishou"))
                    results += got
                    plat_count["kuaishou"] = plat_count.get("kuaishou", 0) + len(got)
                    if not got:
                        notes.append("快手纯 HTTP 搜索被签名/风控拦截（预期行为）："
                                     "请勾选「浏览器搜索」或改用分享链接解析。")

    seen: set[str] = set()
    uniq: list[VideoLink] = []
    for v in results:
        if v.url and v.url not in seen:
            seen.add(v.url)
            uniq.append(v)

    if enrich:
        filled = enrich_items_metadata(uniq)
        if filled:
            notes.append(f"已用 yt-dlp 为 {filled} 条缺失元数据的链接补全标题/点赞/播放等信息。")

    issues = validate_collection(uniq)
    if issues:
        notes.append("⚠️ 自检发现 " + str(len(issues)) + " 处问题：")
        for iss in issues[:8]:
            notes.append("   · " + iss)
        if len(issues) > 8:
            notes.append(f"   · …（其余 {len(issues) - 8} 处略）")

    items = [
        {
            "platform": v.platform, "keyword": v.keyword, "title": v.title,
            "author": v.author, "duration": v.duration, "likes": v.likes,
            "views": v.views, "url": v.url, "cover": getattr(v, "cover", ""),
            "fetched_at": v.fetched_at,
        }
        for v in uniq
    ]

    md = build_markdown(uniq, keywords, platforms)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_file = f"links_{ts}.md"
    with open(os.path.join(RESULTS_DIR, md_file), "w", encoding="utf-8") as f:
        f.write(md)

    return {"ok": True, "items": items, "md": md, "md_file": md_file,
            "count": len(items), "notes": notes}


def _prepare_collection(body: dict) -> dict:
    keywords: list[str] = []
    if body.get("keywords"):
        keywords += [k.strip() for k in str(body["keywords"]).replace("\n", ",").split(",") if k.strip()]
    if body.get("keywords_file"):
        for line in str(body["keywords_file"]).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                keywords.append(line)

    platforms = [p.strip().lower() for p in str(body.get("platforms", "bilibili")).split(",") if p.strip()]
    max_per = int(body.get("max_per_keyword", 50))
    enrich = bool(body.get("enrich", True))
    delay = float(body.get("delay", 1.0))
    browser_search = bool(body.get("browser_search", False)) or cm.playwright_available()
    use_saved = bool(body.get("use_saved_cookies", True))

    notes: list[str] = []
    saved_headers: dict[str, str] = {}
    if use_saved:
        saved_headers = cm.load_all_headers(STORE)
        if saved_headers:
            notes.append("已启用保存的 Cookie：" + "、".join(
                f"{cm.PLATFORM_META[p]['label']}({len(header.split(';'))}条)"
                for p, header in saved_headers.items()))

    raw_cookies = body.get("cookies")
    if raw_cookies and isinstance(raw_cookies, str) and raw_cookies.strip():
        ck, meta, fmt = cm.parse_cookie_text(raw_cookies)
        if ck:
            plat = cm.detect_platform(ck, meta)
            header = "; ".join(f"{k}={v}" for k, v in ck.items())
            if plat:
                saved_headers[plat] = header
                notes.append(f"本次临时 Cookie：{fmt} -> {cm.PLATFORM_META[plat]['label']}（{len(ck)} 条）")
            else:
                saved_headers["_global"] = header
                notes.append(f"本次临时 Cookie：{fmt}（{len(ck)} 条，未识别平台，按全局使用）")
        else:
            notes.append("临时 Cookie 未能解析，已忽略。")

    client = HttpClient(delay=delay)
    for plat, header in saved_headers.items():
        if plat == "_global":
            client._cookie_header = header
        elif plat in PLATFORM_DOMAIN:
            client.set_domain_cookies(PLATFORM_DOMAIN[plat], header)

    browser = None
    browser_ok = False
    if browser_search:
        browser = BrowserSearcher(cookies_by_platform=saved_headers,
                                  profile_root=cm.PROFILE_DIR, headless=True)
        browser_ok = bool(browser.available)
        if not browser_ok:
            notes.append("未安装 Playwright，浏览器搜索已跳过：pip install playwright && playwright install chromium")

    need_browser = [p for p in platforms
                    if p != "bilibili" and p in BROWSER_SEARCH]
    unsupported = [p for p in platforms
                   if p != "bilibili" and p not in BROWSER_SEARCH]
    if unsupported:
        notes.append("以下平台暂不支持关键词搜索，请改用分享链接解析：" + "、".join(unsupported))
    if need_browser and not browser_ok:
        notes.append("以下平台的关键词搜索需勾选「浏览器搜索」并安装 Playwright："
                     + "、".join(cm.PLATFORM_META.get(p, {}).get("label", p) for p in need_browser)
                     + "；或改用分享链接解析通道。")

    http_tasks: list[tuple[str, str]] = []
    browser_tasks: list[tuple[str, str]] = []
    if keywords:
        bili = "bilibili" in platforms
        for kw in keywords:
            if bili:
                http_tasks.append(("bilibili", kw))
            if "kuaishou" in platforms and not browser_ok:
                http_tasks.append(("kuaishou", kw))
            for plat in need_browser:

                if plat == "kuaishou" and not browser_ok:
                    http_tasks.append(("kuaishou", kw))
                else:
                    browser_tasks.append((plat, kw))

    def make_client(plat: str = "") -> HttpClient:
        c = HttpClient(delay=delay)
        for p, header in saved_headers.items():
            if p == "_global":
                c._cookie_header = header
            elif p in PLATFORM_DOMAIN:
                c.set_domain_cookies(PLATFORM_DOMAIN[p], header)
        return c

    return {
        "keywords": keywords, "platforms": platforms, "max_per": max_per,
        "enrich": enrich, "notes": notes, "saved_headers": saved_headers,
        "client": client, "browser": browser, "browser_ok": browser_ok,
        "http_tasks": http_tasks, "browser_tasks": browser_tasks,
        "make_client": make_client, "resolve_text": body.get("resolve_text", ""),
    }


def stream_collect(handler, body: dict) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()

    def emit(event, data):
        _sse_emit(handler, event, data)

    ctx = _prepare_collection(body)
    keywords = ctx["keywords"];
    platforms = ctx["platforms"]
    max_per = ctx["max_per"];
    enrich = ctx["enrich"]
    notes = ctx["notes"];
    saved_headers = ctx["saved_headers"]
    make_client = ctx["make_client"]
    http_tasks = ctx["http_tasks"];
    browser_tasks = ctx["browser_tasks"]
    resolve_text = ctx["resolve_text"]

    platform_tasks: dict[str, list[str]] = {}
    for plat, kw in http_tasks + browser_tasks:
        platform_tasks.setdefault(plat, []).append(kw)

    author_q = (body.get("author") or "").strip()

    combo = bool(author_q and keywords)
    if combo:
        total_tasks = 1
    elif author_q:
        total_tasks = 1
    else:
        total_tasks = len(platform_tasks)
    emit("start", {"total_tasks": total_tasks, "has_resolve": bool(resolve_text),
                   "has_author": bool(author_q), "combo": combo,
                   "platforms": platforms, "keywords": keywords})

    results: list[VideoLink] = []
    seen: set[str] = set()
    lock = threading.Lock()

    def _item(v) -> dict:
        return {
            "id": v.url,
            "platform": v.platform, "keyword": v.keyword, "title": v.title,
            "author": v.author, "duration": v.duration, "likes": v.likes,
            "views": v.views, "url": v.url, "cover": getattr(v, "cover", ""),
            "media_url": getattr(v, "media_url", "") or "",
            "fetched_at": v.fetched_at,
        }

    def _ingest(got):
        added = 0
        with lock:
            for v in got:
                if v.url and v.url not in seen:
                    seen.add(v.url);
                    results.append(v)
                    emit("result", _item(v));
                    added += 1
        return added

    def _client_gone() -> bool:
        return getattr(handler.wfile, "closed", False)

    if resolve_text:
        try:
            got = ShareResolver(ctx["client"]).resolve(str(resolve_text))
            if _ingest(got):
                emit("note", {"text": f"分享链接解析得到 {len(got)} 条"})
        except Exception as e:
            notes.append(f"分享链接解析异常：{e}")

    if author_q:
        try:
            got = _search_author_works(
                author_q, platforms, max_per, ctx,
                kw_filter=(keywords if combo else None))
            added = _ingest(got)
            if added:
                label = (f"按作者「{author_q}」在作品内按关键词筛选得到 {added} 条"
                         if combo else f"按作者「{author_q}」搜索得到 {added} 条作品")
                emit("note", {"text": label})
            else:
                if combo:
                    notes.append(f"按作者「{author_q}」的作品中未找到匹配关键词 {keywords} 的视频："
                                 "可尝试更换关键词，或该作者作品数较少。")
                else:
                    notes.append(f"按作者「{author_q}」未找到作品：B站/YouTube 支持按 UID 或作者名精确列出；"
                                 "其余平台为尽力搜索，建议直接粘贴该平台的用户/频道链接。")
        except Exception as e:
            notes.append(f"按作者搜索异常：{e}")
        emit("progress", {"searched": 1, "total": total_tasks, "found": len(results)})

    if platform_tasks and not combo:
        done = 0
        http_plats = {plat for plat, _ in http_tasks}
        browser_plats = {plat for plat, _ in browser_tasks}

        def run_platform(plat):

            collected = []
            seen_p = set()
            budget = max_per
            bs = None
            try:

                if plat in browser_plats:
                    bs = BrowserSearcher(cookies_by_platform=saved_headers,
                                         profile_root=cm.PROFILE_DIR, headless=True)
                    if not bs._open_browser(plat):
                        bs = None
                        notes.append(f"{cm.PLATFORM_META.get(plat, {}).get('label', plat)}"
                                     "浏览器搜索不可用（未安装 Playwright 或启动失败）："
                                     "pip install playwright && playwright install chromium；"
                                     "或改用分享链接解析通道。")
                for kw in platform_tasks[plat]:
                    if budget <= 0:
                        break
                    if plat == "bilibili":
                        c = make_client("bilibili")
                        s = BilibiliSearcher(c)
                        links = s.search(kw, budget)
                        if enrich:
                            s.enrich(links)
                    elif plat in http_plats:
                        links = KuaishouSearcher(make_client("kuaishou")).search(kw, budget)
                    else:
                        links = []
                        if bs and bs._open:
                            links = bs.search_keyword(plat, kw, budget)
                        if not links and bs is not None:
                            notes.append(f"{cm.PLATFORM_META.get(plat, {}).get('label', plat)}关键词搜索返回 0 条")
                    for v in links:
                        if budget <= 0:
                            break
                        if v.url and v.url not in seen_p:
                            seen_p.add(v.url)
                            collected.append(v)
                            budget -= 1
            finally:
                if bs is not None:
                    try:
                        bs.close()
                    except Exception:
                        pass
            return plat, collected

        try:
            http_ex = ThreadPoolExecutor(max_workers=min(8, max(1, len(http_plats))))

            br_ex = ThreadPoolExecutor(max_workers=min(2, max(1, len(browser_plats))))
            futures = {}
            for plat in http_plats:
                futures[http_ex.submit(run_platform, plat)] = plat
            for plat in browser_plats:
                futures[br_ex.submit(run_platform, plat)] = plat
            for fut in as_completed(futures):
                if _client_gone():
                    http_ex.shutdown(wait=False)
                    br_ex.shutdown(wait=False)
                    return
                plat = futures[fut]
                done += 1
                try:
                    _, got = fut.result()
                except Exception as e:
                    got = []
                    notes.append(f"{plat} 搜索异常：{e}")
                _ingest(got)
                emit("progress", {"searched": done, "total": total_tasks, "found": len(results)})
            http_ex.shutdown(wait=False)
            br_ex.shutdown(wait=False)
        except Exception as e:
            notes.append(f"并行搜索异常：{e}")

    targets = [i for i, v in enumerate(results)
               if isinstance(v, VideoLink)
               and (not v.title or (not v.likes and not v.views))]
    if enrich and targets:
        emit("stage", {"stage": "enrich", "total": len(targets)})
        done_e = 0

        def ework(i):
            v = results[i]
            _, cookie_file, _ = _pick_cookie(v.url, v.platform)
            return _ytdlp_info_one(v.url, cookie_file)

        try:
            ex = ThreadPoolExecutor(max_workers=8)
            futs = {ex.submit(ework, i): i for i in targets}
            for fut in as_completed(futs):
                if _client_gone():
                    ex.shutdown(wait=False)
                    return
                i = futs[fut]
                done_e += 1
                meta = None
                try:
                    meta = fut.result()
                except Exception:
                    meta = None
                if meta:
                    v = results[i]
                    if not v.title and meta.get("title"): v.title = meta["title"]
                    if not v.author and meta.get("author"): v.author = meta["author"]
                    if not v.duration and meta.get("duration"): v.duration = meta["duration"]
                    if not v.likes and meta.get("likes"): v.likes = meta["likes"]
                    if not v.views and meta.get("views"): v.views = meta["views"]
                    emit("enrich", {"id": v.url, "item": _item(v)})
                emit("progress", {"enrich_done": done_e, "enrich_total": len(targets),
                                  "found": len(results)})
            ex.shutdown(wait=False)
        except Exception as e:
            notes.append(f"元数据补全异常：{e}")

    uniq = results
    issues = validate_collection(uniq)
    if issues:
        notes.append("⚠️ 自检发现 " + str(len(issues)) + " 处问题：")
        for iss in issues[:8]:
            notes.append("   · " + iss)
        if len(issues) > 8:
            notes.append(f"   · …（其余 {len(issues) - 8} 处略）")

    items = [_item(v) for v in uniq]
    md = build_markdown(uniq, keywords, platforms)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_file = f"links_{ts}.md"
    with open(os.path.join(RESULTS_DIR, md_file), "w", encoding="utf-8") as f:
        f.write(md)

    emit("done", {"ok": True, "count": len(items), "md": md, "md_file": md_file,
                  "notes": notes, "progress": {"found": len(items)}})


def _read_netscape_cookies(path: str):
    objs = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) < 7:
                    continue
                objs.append({
                    "name": cols[5], "value": cols[6],
                    "domain": cols[0].lstrip("."), "path": cols[2] or "/",
                    "secure": (cols[3].lower() == "true"),
                    "httpOnly": (cols[1].lower() == "TRUE"),
                })
    except Exception:
        pass
    return objs


def _open_link_blocking(platform: str, url: str) -> dict:
    import os
    from playwright.sync_api import sync_playwright
    profile = os.path.join(cm.PROFILE_DIR, platform) if os.path.isdir(
        os.path.join(cm.PROFILE_DIR, platform)) else ""
    fail_markers = ("页面不见了", "笔记不存在", "该笔记已丢失", "账号已注销",
                    "登录后查看", "内容不存在", "该笔记已删除", "笔记已删除",
                    "该内容已被删除", "访问的页面不见了")
    accessible, note = None, ""
    with sync_playwright() as p:
        if profile:
            ctx = p.chromium.launch_persistent_context(
                profile, headless=False, user_agent=DEFAULT_UA,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 800})
        else:
            browser = p.chromium.launch(
                headless=False, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1280, "height": 800})
            plat, ckfile, _ = _pick_cookie(url, platform)
            if ckfile and os.path.isfile(ckfile):
                try:
                    ctx.add_cookies(_read_netscape_cookies(ckfile))
                except Exception:
                    pass
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        try:
            txt = (page.inner_text("body") or "")[:800]
        except Exception:
            txt = ""
        accessible = not any(k in txt for k in fail_markers)
        note = "可正常访问" if accessible else "可能无法访问（token 失效 / 需登录 / 笔记已删）"

        try:
            page.wait_for_timeout(300000)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    return {"accessible": accessible, "note": note}


def open_link(platform: str, url: str) -> dict:
    if not cm.playwright_available():
        return {"ok": False, "message": "未安装 Playwright，无法在登录态浏览器中打开。"
                                        "请改用本机浏览器直接打开，或安装：pip install playwright && playwright install chromium"}
    if not url:
        return {"ok": False, "message": "缺少链接"}

    def _run() -> None:
        try:
            res = _open_link_blocking(platform, url)
            print(f"[open] {platform} {url} -> {res}")
        except Exception as e:
            print(f"[open] 打开失败: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "已在已登录浏览器窗口打开，请查看弹出的浏览器。",
            "platform": platform}


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoLinkCollector/2.0"

    def _send(self, code: int, payload: str, content_type: str = "application/json; charset=utf-8",
              extra_headers: dict | None = None) -> None:
        data = payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "ignore") if length else "{}"
        return json.loads(raw) if raw.strip() else {}

    def _send_file(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        except Exception:
            self._send(404, "frontend not found", "text/plain; charset=utf-8")

    def _serve_frontend_asset(self, rel: str) -> None:

        rel = (rel or "").strip()
        if not rel or ".." in rel or rel.startswith("/") or rel.startswith("\\"):
            return self._send(400, "bad path", "text/plain; charset=utf-8")
        fpath = os.path.abspath(os.path.join(FRONTEND_DIR, rel))
        safe_root = os.path.abspath(FRONTEND_DIR) + os.sep
        if not fpath.startswith(safe_root) or not os.path.isfile(fpath):
            return self._send(404, "static not found", "text/plain; charset=utf-8")
        ctype = "text/plain; charset=utf-8"
        if rel.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif rel.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif rel.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                self._send(200, f.read(), ctype,
                           {"Cache-Control": "public, max-age=300"})
        except Exception:
            self._send(404, "static not found", "text/plain; charset=utf-8")

    def _serve_inline_image(self, fname: str) -> None:

        fname = (fname or "").strip()
        if not fname or ".." in fname or fname.startswith("/") or fname.startswith("\\"):
            return self._json({"ok": False, "error": "非法文件名"}, 400)
        fpath = os.path.abspath(os.path.join(DOWNLOAD_DIR, fname))
        safe_root = os.path.abspath(DOWNLOAD_DIR) + os.sep
        if not (fpath.startswith(safe_root) or fpath.startswith(os.path.abspath(COVER_DIR) + os.sep)):
            return self._json({"ok": False, "error": "非法路径"}, 400)
        if not fpath.startswith(safe_root) or not os.path.isfile(fpath):
            return self._json({"ok": False, "error": "文件不存在"}, 404)
        try:
            with open(fpath, "rb") as f:
                data = f.read()
        except Exception:
            return self._json({"ok": False, "error": "读取失败"}, 500)

        ctype = "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ctype = "image/webp"
        elif data[:3] == b"GIF":
            ctype = "image/gif"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _proxy_image(self, url: str) -> None:

        import hashlib
        import urllib.parse as _up
        import urllib.request as _ureq
        url = (url or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return self._json({"ok": False, "error": "非法图片地址"}, 400)
        try:
            h = hashlib.md5(url.encode("utf-8")).hexdigest()
            low = url.lower()
            ext = ".jpg"
            if low.endswith(".png"):
                ext = ".png"
            elif low.endswith(".webp"):
                ext = ".webp"
            elif low.endswith(".gif"):
                ext = ".gif"
            cache_path = os.path.join(COVER_DIR, "proxy_" + h + ext)
            if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
                with open(cache_path, "rb") as f:
                    data = f.read()
            else:
                host = _up.urlparse(url).netloc.lower()
                ref = "https://www.bilibili.com" if "bilibili" in host else (
                        _up.urlparse(url).scheme + "://" + host)
                req = _ureq.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": ref,
                    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                })
                with _ureq.urlopen(req, timeout=20) as r:
                    data = r.read()
                if data:
                    try:
                        with open(cache_path, "wb") as f:
                            f.write(data)
                    except Exception:
                        pass
        except Exception as e:
            return self._send(502, "图片获取失败：" + str(e)[:80], "text/plain; charset=utf-8")
        if not data:
            return self._send(502, "图片为空", "text/plain; charset=utf-8")
        ctype = "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ctype = "image/webp"
        elif data[:3] == b"GIF":
            ctype = "image/gif"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _open_file_folder(self, fname: str) -> None:

        fname = (fname or "").strip()
        if not fname or ".." in fname or "/" in fname or "\\" in fname:
            return self._json({"ok": False, "error": "非法文件名"}, 400)
        fpath = os.path.abspath(os.path.join(DOWNLOAD_DIR, fname))
        safe_root = os.path.abspath(DOWNLOAD_DIR) + os.sep
        if not fpath.startswith(safe_root):
            return self._json({"ok": False, "error": "非法路径"}, 400)
        folder = os.path.dirname(fpath)
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
            return self._json({"ok": True, "folder": folder})
        except Exception as e:
            return self._json({"ok": False, "error": f"打开失败：{e}"}, 500)

    def _resolve_zip_path(self, fn: str, safe_root: str):

        fn = (fn or "").strip()
        if not fn:
            return None

        if os.path.isabs(fn):
            fp = os.path.abspath(fn)
            if fp.startswith(safe_root) and os.path.isfile(fp):
                return fp
            return None

        fp = os.path.abspath(os.path.join(DOWNLOAD_DIR, fn))
        if fp.startswith(safe_root) and os.path.isfile(fp):
            return fp

        base = os.path.basename(fn)
        if not base:
            return None
        for _root, _dirs, _files in os.walk(DOWNLOAD_DIR):
            if base in _files:
                cand = os.path.join(_root, base)
                if cand.startswith(safe_root):
                    return cand
        return None

    def _serve_zip(self, fnames: list, head: bool = False) -> None:

        safe_root = os.path.abspath(DOWNLOAD_DIR) + os.sep
        real: list = []
        for fn in fnames:
            fp = self._resolve_zip_path(fn, safe_root)
            if fp and fp not in real:
                real.append(fp)
        if not real:
            msg = ("没有可打包的文件（选中的文件在下载目录中未找到，可能已被移动/删除，"
                   "或尚未真正下载完成）。请确认文件仍在下载目录后重试。")
            if head:
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                return
            return self._json({"ok": False, "error": msg}, 404)
        if head:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        try:
            buf = io.BytesIO()
            used: set = set()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for fp in real:
                    arc = os.path.basename(fp)
                    if arc in used:
                        _b, _e = os.path.splitext(arc)
                        i = 1
                        while f"{_b}_{i}{_e}" in used:
                            i += 1
                        arc = f"{_b}_{i}{_e}"
                    used.add(arc)
                    z.write(fp, arc)
            data = buf.getvalue()
        except Exception as e:
            return self._json({"ok": False, "error": f"打包失败：{e}"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f'attachment; filename="videos_{datetime.now():%Y%m%d_%H%M%S}.zip"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _sse_stream_downloads(self) -> None:

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = HUB.subscribe()

        try:
            snap = _load_history()
            self.wfile.write(("event: snapshot\ndata: " + json.dumps({"items": snap},
                                                                     ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass
        try:
            while True:
                try:
                    event, data = q.get(timeout=30)
                except Exception:
                    self.wfile.write(b": ping\n\n");
                    self.wfile.flush();
                    continue
                self.wfile.write(f"event: {event}\n".encode("utf-8"))
                self.wfile.write(("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
        except Exception:
            pass
        finally:
            HUB.unsubscribe(q)

    def _fs_browse(self, p: str) -> dict:
        root = os.path.expanduser("~")
        cur = os.path.abspath(p) if p else root
        if not os.path.isdir(cur):
            cur = root
        entries = []
        try:
            for name in os.listdir(cur):
                fp = os.path.join(cur, name)
                try:
                    st = os.stat(fp)
                    is_dir = os.path.isdir(fp)
                    entries.append({"name": name, "is_dir": is_dir,
                                    "size": st.st_size if not is_dir else 0, "mtime": st.st_mtime})
                except Exception:
                    pass
        except Exception:
            pass
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        parent = os.path.dirname(cur) if os.path.dirname(cur) != cur else ""
        return {"ok": True, "path": cur, "parent": parent, "home": root, "entries": entries}

    def _downloads_save(self, body: dict) -> dict:
        paths = body.get("paths") or []
        dest = (body.get("dest") or "").strip()
        if not isinstance(paths, list) or not dest:
            return {"ok": False, "error": "缺少 paths 或 dest"}
        dest = os.path.abspath(dest)
        if not os.path.isdir(dest):
            return {"ok": False, "error": "目标文件夹不存在"}
        results = []
        for p in paths:
            if not isinstance(p, str) or not p.strip():
                continue
            p = os.path.abspath(p)
            if not os.path.isfile(p):
                results.append({"path": p, "ok": False, "error": "文件不存在"});
                continue
            try:
                shutil.copy2(p, dest)
                results.append({"path": p, "ok": True,
                                "dest": os.path.join(dest, os.path.basename(p))})
            except Exception as e:
                results.append({"path": p, "ok": False, "error": str(e)})
        return {"ok": True, "dest": dest, "results": results}

    def _downloads_delete(self, body: dict) -> dict:
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return {"ok": False, "error": "缺少 ids"}
        idset = set(ids)
        items = _load_history(limit=10 ** 9)
        keep = [it for it in items if it.get("id") not in idset]
        removed = [it for it in items if it.get("id") in idset]
        try:
            with HISTORY_LOCK, open(HISTORY_FILE, "w", encoding="utf-8") as f:
                for it in reversed(keep):
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        deleted_files = []
        for it in removed:
            fp = it.get("path") or ""
            if fp and os.path.isfile(fp):
                try:
                    os.remove(fp);
                    deleted_files.append(fp)
                except Exception:
                    pass
        return {"ok": True, "removed_ids": len(removed), "deleted_files": deleted_files}

    def api_dl_control(self, body: dict) -> None:
        action = (body.get("action") or "").strip()
        scope = (body.get("scope") or "task").strip()
        if scope == "batch":
            task_id = (body.get("task_id") or "").strip()
            with DL_LOCK:
                batch = DL_BATCHES.get(task_id)
            if not batch:
                return self._json({"ok": False, "error": "未找到该下载批次"})
            if action == "pause":
                batch["status"] = "paused"
                batch["pause_event"].set()
                paused = 0
                for k, t in list(DL_TASKS.items()):
                    if t.get("task_id") == task_id and t.get("status") in ("downloading", "queued"):
                        _dl_terminate(k)
                        _dl_set_status(k, "paused")
                        paused += 1
                HUB.broadcast("batch-update", {"task_id": task_id, "status": "paused"})
                return self._json({"ok": True, "paused": paused})
            elif action == "resume":
                batch["status"] = "running"
                batch["pause_event"].clear()
                targets = [k for k, t in DL_TASKS.items()
                           if t.get("task_id") == task_id
                           and t.get("status") in ("paused", "stopped", "error", "queued")]
                for k in targets:
                    tk = DL_TASKS.get(k)
                    if not tk:
                        continue
                    tk["stop_event"].clear()
                    _dl_set_status(k, "queued")
                    threading.Thread(target=run_one_task, args=(self, task_id, k),
                                     daemon=True).start()
                HUB.broadcast("batch-update", {"task_id": task_id, "status": "running"})
                return self._json({"ok": True, "resumed": len(targets)})
            return self._json({"ok": False, "error": "未知的批次动作"})

        task_key = (body.get("task_key") or "").strip()
        if not task_key:
            return self._json({"ok": False, "error": "缺少 task_key"})
        with DL_LOCK:
            t = DL_TASKS.get(task_key)
            if not t:
                return self._json({"ok": False, "error": "未找到该任务"})
            t = dict(t)
        if action == "pause":
            if t["status"] not in ("downloading", "queued"):
                return self._json({"ok": True, "no_op": True})
            _dl_terminate(task_key)
            _dl_set_status(task_key, "paused")
            return self._json({"ok": True})
        elif action == "stop":
            if t["status"] not in ("downloading", "queued"):
                return self._json({"ok": True, "no_op": True})
            _dl_terminate(task_key)
            _dl_set_status(task_key, "stopped")
            return self._json({"ok": True})
        elif action == "resume":
            if t["status"] not in ("paused", "stopped", "error"):
                return self._json({"ok": True, "no_op": True})
            t["stop_event"].clear()
            _dl_set_status(task_key, "queued")
            threading.Thread(target=run_one_task, args=(self, t["task_id"], task_key),
                             daemon=True).start()
            return self._json({"ok": True})
        elif action == "delete":
            _dl_terminate(task_key)
            with DL_LOCK:
                DL_TASKS.pop(task_key, None)

            items = _load_history(limit=10 ** 9)
            hit = [it for it in items if (it.get("url") or "") == t.get("url")]
            if hit:
                self._downloads_delete({"ids": [it["id"] for it in hit]})
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "未知动作"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._send_file(FRONTEND)

        if path.startswith("/static/"):
            return self._serve_frontend_asset(path[len("/static/"):])

        # 根路径直接提供前端静态资源（index.html 用相对路径引用 app.js/style.css 时，本地同源也能解析）
        if not path.startswith(("/api/", "/downloads/", "/static/")):
            return self._serve_frontend_asset(path.lstrip("/"))

        if path.startswith("/downloads/"):
            fname = unquote(os.path.basename(path[len("/downloads/"):]))
            if not fname or ".." in fname or "/" in fname or "\\" in fname:
                return self._json({"ok": False, "error": "非法文件名"}, 400)
            fpath = os.path.abspath(os.path.join(DOWNLOAD_DIR, fname))
            safe_root = os.path.abspath(DOWNLOAD_DIR) + os.sep
            if not fpath.startswith(safe_root) or not os.path.isfile(fpath):
                return self._json({"ok": False, "error": "文件不存在"}, 404)
            try:
                size = os.path.getsize(fpath)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", "attachment")
                self.send_header("Accept-Ranges", "none")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(fpath, "rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                self.wfile.flush()
            except Exception:
                try:
                    self._send(404, "file not found", "text/plain; charset=utf-8")
                except Exception:
                    pass
            return

        if path == "/api/health":
            return self._json({"ok": True, "playwright": cm.playwright_available(),
                               "results_dir": RESULTS_DIR})

        if path == "/api/cookies":
            return self._json({"ok": True, "platforms": STORE.status(),
                               "playwright": cm.playwright_available()})

        if path == "/api/cookies/login_status":
            return self._json({"ok": True, **cm.login_status()})

        if path == "/api/cookies/export":
            plat = (query.get("platform") or [""])[0]
            rec = STORE.load(plat)
            if not rec:
                return self._json({"ok": False, "error": "该平台尚未配置 Cookie"}, 404)
            return self._send(
                200, rec.to_netscape(), "text/plain; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="{plat}_cookies.txt"'})

        if path == "/api/ytdlp/check":
            cmd, ver = resolve_ytdlp_cmd()
            return self._json({"ok": True, "available": bool(cmd), "version": ver,
                               "ffmpeg": bool(find_ffmpeg())})

        if path == "/api/ytdlp/update":
            action = (query.get("action") or ["check"])[0]
            return self._json(handle_ytdlp_update(action))

        if path == "/api/open":
            platform = (query.get("platform") or [""])[0]
            url = (query.get("url") or [""])[0]
            return self._json(open_link(platform, url))

        if path == "/api/downloads/history":
            return self._json({"ok": True, "items": _load_history()})

        if path == "/api/downloads/stream":
            return self._sse_stream_downloads()

        if path == "/api/fs/browse":
            p = (query.get("path") or [""])[0]
            return self._json(self._fs_browse(p))

        if path == "/api/files/serve":
            return self._serve_inline_image((query.get("file") or [""])[0])

        if path == "/api/img":
            return self._proxy_image((query.get("u") or [""])[0])

        if path == "/api/files/open":
            return self._open_file_folder((query.get("file") or [""])[0])

        if path == "/api/files/zip":
            fnames = query.get("f") or []
            return self._serve_zip(fnames)

    def do_HEAD(self):

        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/api/files/zip":
            fnames = query.get("f") or []
            return self._serve_zip(fnames, head=True)
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

        return self._json({"ok": False, "error": "not found"}, 404)

    def do_OPTIONS(self):
        # 跨域预检（前端静态托管、后端另部署时，浏览器对跨源 POST 会先发 OPTIONS 预检）
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
        except Exception as e:
            return self._json({"ok": False, "error": f"bad json: {e}"}, 400)

        if path == "/api/download":
            return stream_download(self, body)
        if path == "/api/batch/download":
            return stream_batch_download(self, body)

        try:
            if path == "/api/collect":
                return stream_collect(self, body)

            if path == "/api/cookies/import":
                raw = str(body.get("raw", ""))
                plat = str(body.get("platform", "auto"))
                res = cm.import_cookie_text(raw, plat, source="web", store=STORE)
                res["platforms"] = STORE.status()
                return self._json(res)

            if path == "/api/cookies/login":
                plat = str(body.get("platform", "auto")) or "auto"
                timeout = int(body.get("timeout", 240))
                url = (body.get("url") or "").strip() or None

                if plat not in PLATFORMS and not url:
                    return self._json({"ok": False, "error": f"不支持的平台：{plat}"}, 400)
                headless = bool(body.get("headless", False))
                return self._json(cm.start_login_async(plat, timeout, headless=headless, url=url))

            if path == "/api/cookies/login_capture":
                return self._json(cm.request_capture())

            if path == "/api/cookies/login_stop":
                return self._json(cm.stop_login())

            if path == "/api/cookies/login_click":
                return self._json(cm.login_click(int(body.get("x", 500)), int(body.get("y", 500))))

            if path == "/api/cookies/login_input":
                return self._json(cm.login_input(str(body.get("text", "") or "")))

            if path == "/api/cookies/probe":
                plat = str(body.get("platform", ""))
                res = cm.probe(plat, STORE)
                res["platforms"] = STORE.status()
                return self._json(res)

            if path == "/api/cookies/selfcheck":
                plats = body.get("platforms") or None
                if plats and not isinstance(plats, list):
                    plats = [str(plats)]
                online = bool(body.get("online", True))
                report = cm.self_check(plats, store=STORE, online=online)
                report["text"] = cm.format_self_check(report)
                report["platforms_status"] = STORE.status()
                return self._json(report)

            if path == "/api/cookies/clear":
                plat = str(body.get("platform", ""))
                res = STORE.clear(plat)
                if res.get("blocked"):
                    return self._json({
                        "ok": False, "error": "Cookie 文件未能删除（可能被系统/沙箱拦截）。"
                                              f"被拦截文件：{'；'.join(res['blocked'])}",
                        "detail": res, "platforms": STORE.status()}, 200)
                return self._json({"ok": True, "removed": res.get("removed", False),
                                   "platforms": STORE.status()})

            if path == "/api/download/info":
                return self._json(download_info(body))

            if path == "/api/collect_links":
                raw = str(body.get("urls", ""))
                urls = [u.strip() for u in re.split(r"[\s,，；;]+", raw) if u.strip()]
                if not urls:
                    return self._json({"ok": False, "error": "请提供至少一个链接（每行/逗号分隔）"}, 400)
                plat, cookie_file, _ = _pick_cookie(urls[0], "")
                items, notes = extract_links_info(urls, cookie_file)
                return self._json({"ok": True, "items": items, "notes": notes, "count": len(items)})

            if path == "/api/batch/detect":

                raw = str(body.get("url", "")).strip()
                info = _bili_playlist_type(raw) if raw else {"is_playlist": False}
                if raw and not info.get("is_playlist"):
                    _m = re.search(r"(BV[0-9A-Za-z]+)", raw, re.I)
                    if _m:
                        _col = _bili_collection_of(_m.group(1))
                        if _col:
                            info = {"is_playlist": True, "playlist_type": "合集",
                                    "playlist_id": _col["id"], "title": _col["title"],
                                    "url": raw, "count": len(_col["episodes"])}
                return self._json({"ok": True, **info})

            if path == "/api/batch/info":
                raw = str(body.get("urls", ""))
                urls = [u.strip() for u in re.split(r"[\s,，；;]+", raw) if u.strip()]
                if not urls:
                    return self._json(
                        {"ok": False, "error": "请提供至少一个链接（支持播放列表链接 / 多个链接空格或换行分隔）"}, 400)
                items, notes, pdet = batch_info(urls, (body.get("platform") or "").strip(),
                                                expand_playlist=bool(body.get("expand_playlist", True)))
                return self._json({"ok": True, "items": items, "notes": notes,
                                   "count": len(items), "playlist_detected": pdet})

            if path == "/api/downloads/save":
                return self._json(self._downloads_save(body))

            if path == "/api/downloads/delete":
                return self._json(self._downloads_delete(body))

            if path == "/api/dl/control":
                return self.api_dl_control(body)

            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    base_port = int(os.environ.get("PORT", "8000"))
    srv = None
    last_err = None
    for port in range(base_port, base_port + 10):
        try:
            srv = ThreadingHTTPServer((host, port), Handler)
            break
        except OSError as e:
            last_err = e
            continue
    if srv is None:
        print(f"无法在 {host}:{base_port} 起服（端口被占用）: {last_err}")
        return
    print(f"视频采集 Web 服务已启动: http://{host}:{port}")
    print(f"前端文件: {FRONTEND}")
    print(f"结果目录: {RESULTS_DIR}")
    print(f"下载目录: {DOWNLOAD_DIR}")
    print(f"Cookie 目录: {cm.COOKIE_DIR}")
    print(f"Playwright: {'可用' if cm.playwright_available() else '未安装（浏览器登录/抖音搜索不可用）'}")

    if getattr(sys, "frozen", False):
        import webbrowser
        try:
            webbrowser.open(f"http://{host}:{port}/")
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":

    if getattr(sys, "frozen", False):
        _av = sys.argv[1:]
        if _av and _av[0] == "-m" and len(_av) >= 2 and _av[1] == "yt_dlp":
            from yt_dlp import main as _ytdlp_main

            sys.argv = ["yt_dlp", *_av[2:]]
            sys.exit(_ytdlp_main())
        if _av and _av[0] == "-c" and len(_av) >= 2:
            exec(_av[1])
            sys.exit(0)
    main()
