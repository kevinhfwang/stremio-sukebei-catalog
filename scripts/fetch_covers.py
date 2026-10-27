#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_covers.py - 为 catalog 条目补封面

优先级:
1. 标题提取番号 → JavDB 搜真封面（缓存到 data/javdb_cache.json）
2. Real-Debrid 取出种子内最小 jpg → 推到 covers 仓库
   (https://raw.githubusercontent.com/<COVERS_REPO>/main/<infoHash>.jpg)

需要 secrets: RD_TOKEN, COVERS_PAT；无 RD_TOKEN 时只跑 JavDB 部分。
每轮最多处理 COVER_BATCH 个（默认 40），RD 抽取上限 RD_FETCH_MAX（默认 10）。
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CATALOG_FILE = DATA_DIR / "catalog.json"
CACHE_FILE = DATA_DIR / "javdb_cache.json"

RD_TOKEN = os.environ.get("RD_TOKEN", "")
COVERS_PAT = os.environ.get("COVERS_PAT", "")
COVERS_REPO = os.environ.get("COVERS_REPO", "kevinhfwang/sukebei-covers")
COVER_BATCH = int(os.environ.get("COVER_BATCH", "40"))
RD_FETCH_MAX = int(os.environ.get("RD_FETCH_MAX", "10"))
FETCH_VIA_TORRENT = os.environ.get("FETCH_VIA_TORRENT", "1") == "1"
TORRENT_FETCH_MAX = int(os.environ.get("TORRENT_FETCH_MAX", "6"))
TORRENT_TIMEOUT = int(os.environ.get("TORRENT_TIMEOUT", "240"))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 公共 tracker：magnet 不带 tracker 时只靠 DHT，冷种拉不到 peer
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]

CODE_RE = re.compile(r"\b([A-Z]{2,8}[- ]?(?:PPV[- ])?\d{2,6})\b")
CODE_BLACKLIST = re.compile(
    r"^(X264|X265|HEVC|H264|H265|1080P|720P|4K|HD|UHD|AAC|MP4|MKV|AVC|BLURAY|BDRIP|WEBRIP|GB|CN|ZH)$")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
IMG_PREFER = re.compile(r"cover|poster|banner|front|scr|snap", re.I)


def http_json(url, data=None, headers=None, timeout=30):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def http_bytes(url, headers=None, timeout=60):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def extract_code(title):
    for m in CODE_RE.findall(title.upper()):
        code = m.upper().replace(" ", "-")
        if not CODE_BLACKLIST.match(code.split("-")[0]):
            return code
    return None


# ---------- JavDB ----------

def javdb_search(code, cache):
    if code in cache:
        return cache[code]
    url = None
    try:
        status, raw = http_bytes(f"https://javdb.com/search?q={urllib.parse.quote(code)}&f=all")
        html = raw.decode("utf-8", errors="ignore")
        m = re.search(r'href="(/v/[A-Za-z0-9]+)"', html)
        if m:
            status2, raw2 = http_bytes(f"https://javdb.com{m.group(1)}")
            page = raw2.decode("utf-8", errors="ignore")
            m2 = re.search(r'property="og:image"\s+content="([^"]+)"', page) or \
                 re.search(r'class="video-cover"[^>]*src="([^"]+)"', page) or \
                 re.search(r'class="img-responsive"[^>]*src="([^"]+)"', page)
            if m2:
                url = m2.group(1)
    except Exception as e:
        print(f"   ⚠️ JavDB {code} 失败: {e}")
    cache[code] = url  # None 也缓存，避免重复请求
    return url


# ---------- BitTorrent 直抽（libtorrent，无需 RD） ----------

def torrent_fetch_cover(view_link, ih):
    """取种子元数据：先直下 .torrent，被墙则走 magnet/DHT。然后只拉最小图片文件。返回 bytes 或 None"""
    import tempfile
    try:
        import libtorrent as lt
    except ImportError:
        print("   ⚠️ 无 libtorrent，跳过直抽")
        return None
    tmp_t = None
    try:
        with tempfile.TemporaryDirectory() as td:
            ses = lt.session({"listen_interfaces": "0.0.0.0:6881", "upload_rate_limit": 10000,
                              "announce_to_all_trackers": True, "announce_to_all_tiers": True})
            info = None
            h = None
            # 1) .torrent 直下（最快）
            m = re.search(r"/view/(\d+)", view_link or "")
            if m:
                try:
                    status, raw = http_bytes(f"https://sukebei.nyaa.si/download/{m.group(1)}.torrent", timeout=20)
                    if status == 200 and len(raw) > 100:
                        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as f:
                            f.write(raw)
                            tmp_t = f.name
                        info = lt.torrent_info(tmp_t)
                        h = ses.add_torrent({"ti": info, "save_path": td})
                except Exception:
                    info = None
            # 2) magnet + DHT + 公共 tracker 拿元数据（绕开 nyaa 对云 IP 的 HTTP 封锁）
            if info is None:
                try:
                    magnet = f"magnet:?xt=urn:btih:{ih}" + "".join(
                        f"&tr={urllib.parse.quote(t)}" for t in TRACKERS)
                    h = lt.add_magnet_uri(ses, magnet, {"save_path": td})
                    t0 = time.time()
                    while time.time() - t0 < 90:
                        if h.has_metadata():
                            break
                        time.sleep(2)
                    if not h.has_metadata():
                        print("   ⏱ DHT 元数据超时 (90s)")
                        ses.remove_torrent(h)
                        return None
                    info = h.torrent_file() if hasattr(h, "torrent_file") else h.get_torrent_file()
                except Exception as e:
                    print(f"   ⚠️ magnet 失败: {e}")
                    return None
            fs = info.files()
            imgs = [(i, fs.file_size(i), fs.file_path(i)) for i in range(fs.num_files())
                    if fs.file_path(i).lower().endswith(IMG_EXT) and 5000 < fs.file_size(i) < 2_000_000]
            if not imgs:
                if h:
                    ses.remove_torrent(h)
                return None
            imgs.sort(key=lambda x: (not IMG_PREFER.search(x[2]), x[1]))
            best = imgs[0][0]
            for i in range(fs.num_files()):
                h.file_priority(i, 7 if i == best else 0)
            t0 = time.time()
            ok = False
            while time.time() - t0 < TORRENT_TIMEOUT:
                if h.status().progress >= 1:
                    ok = True
                    break
                if int(time.time() - t0) % 45 < 3:
                    try:
                        h.force_reannounce()
                    except Exception:
                        pass
                time.sleep(3)
            ses.remove_torrent(h)
            if not ok:
                print(f"   ⏱ swarm 拉取超时 ({TORRENT_TIMEOUT}s)")
                return None
            with open(os.path.join(td, fs.file_path(best)), "rb") as f:
                return f.read()
    except Exception as e:
        print(f"   ⚠️ BT 抽图失败: {e}")
        return None
    finally:
        if tmp_t:
            try:
                os.unlink(tmp_t)
            except OSError:
                pass


# ---------- Real-Debrid ----------

def rd_request(path, data=None, timeout=30):
    return http_json(f"https://api.real-debrid.com/rest/1.2{path}", data=data,
                     headers={"Authorization": f"Bearer {RD_TOKEN}"}, timeout=timeout)


def rd_fetch_cover(info_hash, title):
    """从种子内抽取一张图，返回 bytes 或 None"""
    tid = None
    try:
        res = rd_request("/torrents/addMagnet",
                         {"magnet": f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(title[:80])}"})
        tid = res.get("id")
        if not tid:
            return None
        info = rd_request(f"/torrents/info/{tid}")
        files = [f for f in info.get("files", []) if f.get("path", "").lower().endswith(IMG_EXT)]
        if not files:
            rd_request(f"/torrents/delete/{tid}", data={})
            return None
        # 优先文件名像封面的，否则最小的
        files.sort(key=lambda f: (not IMG_PREFER.search(f.get("path", "")), f.get("bytes", 9e9)))
        target = files[0]
        if target.get("bytes", 0) > 2_000_000:  # 图太大跳过
            rd_request(f"/torrents/delete/{tid}", data={})
            return None
        idx = str(target["id"] - 1)  # selectFiles 用 0 起始索引
        rd_request(f"/torrents/selectFiles/{tid}", {"files": idx})
        # 轮询等下载完成（RD 已缓存的热门种很快）
        for _ in range(24):  # 最多 ~4 分钟
            time.sleep(10)
            info = rd_request(f"/torrents/info/{tid}")
            if info.get("status") == "downloaded":
                break
            if info.get("status") in ("dead", "error", "magnet_error"):
                rd_request(f"/torrents/delete/{tid}", data={})
                return None
        else:
            rd_request(f"/torrents/delete/{tid}", data={})
            return None
        links = info.get("links", [])
        if not links:
            rd_request(f"/torrents/delete/{tid}", data={})
            return None
        un = rd_request("/unrestrict/link", {"link": links[0]})
        dl = un.get("download")
        if not dl:
            return None
        status, content = http_bytes(dl, timeout=120)
        if status == 200 and 10_000 < len(content) < 2_000_000:
            return content
    except Exception as e:
        print(f"   ⚠️ RD 失败: {e}")
    finally:
        if tid:
            try:
                rd_request(f"/torrents/delete/{tid}", data={})
            except Exception:
                pass
    return None


# ---------- 图床 ----------

def detect_ext(content):
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:4] == b"RIFF":
        return ".webp"
    return ".jpg"


def catbox_upload(ih, content):
    """catbox.moe 免鉴权图床。成功返回 URL，失败抛异常"""
    boundary = "----pybound"
    fname = ih[:8] + detect_ext(content)
    pre = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"reqtype\"\r\n\r\nfileupload\r\n"
           f"--{boundary}\r\nContent-Disposition: form-data; name=\"fileToUpload\"; filename=\"{fname}\"\r\n"
           f"Content-Type: image/jpeg\r\n\r\n").encode()
    body = pre + content + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        "https://catbox.moe/user/api.php", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        url = r.read().decode().strip()
    if url.startswith("http"):
        return url
    raise ValueError(f"catbox 拒绝: {url[:100]}")


_THIS_REPO = os.environ.get("GITHUB_REPOSITORY", "kevinhfwang/stremio-sukebei-catalog")


def _github_api(url, token, method="GET", data=None, raw=None, ctype="application/json"):
    req = urllib.request.Request(
        url, method=method,
        data=raw if raw is not None else (json.dumps(data).encode() if data is not None else None),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": ctype, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, (json.loads(body) if ctype == "application/json" and body else body)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:200]


def gh_release_upload(ih, content):
    """传到本仓库 Releases（Actions 里自动有 GITHUB_TOKEN，零 secret）。幂等：同名 asset 直接复用"""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("无 GITHUB_TOKEN")
    name = ih[:8] + detect_ext(content)
    base = f"https://api.github.com/repos/{_THIS_REPO}"
    status, rel = _github_api(f"{base}/releases/tags/covers", token)
    if status == 404:
        status, rel = _github_api(f"{base}/releases", token, method="POST",
                                  data={"tag_name": "covers", "name": "covers", "body": "海报图床（自动上传，勿删）"})
    if not isinstance(rel, dict) or "id" not in rel:
        raise ValueError(f"release 不可用: {status} {str(rel)[:120]}")
    for a in rel.get("assets", []):
        if a.get("name") == name:
            return f"https://github.com/{_THIS_REPO}/releases/download/covers/{name}"
    up = f"https://uploads.github.com/repos/{_THIS_REPO}/releases/{rel['id']}/assets?name={name}"
    status, resp = _github_api(up, token, method="POST", raw=content,
                               ctype="application/octet-stream")
    if isinstance(resp, dict) and resp.get("browser_download_url"):
        return resp["browser_download_url"]
    raise ValueError(f"asset 上传失败: {status} {str(resp)[:120]}")


def push_cover(ih, content):
    ext = ".jpg"
    raw = content[:3]
    if raw.startswith(b"\x89P"):
        ext = ".png"
    elif raw.startswith(b"RIFF"):
        ext = ".webp"
    path = f"{ih}{ext}"
    import base64
    api = f"https://api.github.com/repos/{COVERS_REPO}/contents/{path}"
    body = {"message": f"cover: {ih}", "content": base64.b64encode(content).decode(),
            "branch": "main"}
    req = urllib.request.Request(api, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {COVERS_PAT}",
                                          "User-Agent": UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        if e.code == 422:  # 已存在
            pass
        else:
            raise
    return f"https://raw.githubusercontent.com/{COVERS_REPO}/main/{path}"


def main():
    cat = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    items = cat.get("items", {})
    cache = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # 需要封面的条目：无 poster 且未失败过 2 次
    todo = [it for it in items.values()
            if not it.get("poster") and it.get("cover_status", "").split(":")[0] != "failed"]
    # 优先处理 catalog 前排（高 seeders）
    todo.sort(key=lambda x: -x.get("seeders", 0))
    todo = todo[:COVER_BATCH]
    print(f"🖼️ 待补封面 {len(todo)} 个（本轮上限 {COVER_BATCH}；BT 直抽 {TORRENT_FETCH_MAX}，RD 兜底 {RD_FETCH_MAX}）")

    rd_used = torrent_used = 0
    javdb_done = rd_done = bt_done = 0

    def mark_failed(it):
        tries = int(it.get("cover_status", "failed:0").split(":")[-1]) + 1 if it.get("cover_status") else 1
        it["cover_status"] = f"failed:{tries}" if tries < 2 else "failed:9"

    def try_push(it, content, source):
        nonlocal rd_done, bt_done
        try:
            if COVERS_PAT:
                # 自有 covers 仓库（配了 COVERS_PAT 时最耐删）
                it["poster"] = push_cover(it["infoHash"], content)
            else:
                try:
                    it["poster"] = catbox_upload(it["infoHash"], content)
                except Exception as e1:
                    print(f"   ⚠️ catbox 失败（{e1}），改传 GitHub Release")
                    it["poster"] = gh_release_upload(it["infoHash"], content)
            it["cover_status"] = source
            if source == "rd":
                rd_done += 1
            else:
                bt_done += 1
            print(f"✓ 已托管 ({len(content)//1024}KB, {source}): {it['poster']}")
        except Exception as e:
            mark_failed(it)
            print(f"   ✗ 托管失败: {e}")

    for it in todo:
        ih = it["infoHash"]
        title = it.get("name", "")
        code = extract_code(title)
        poster = None
        if code:
            poster = javdb_search(code, cache)
            if poster:
                it["poster"] = poster
                it["cover_status"] = "javdb"
                javdb_done += 1
                print(f"✓ [{code}] JavDB 封面")
        # 1) libtorrent 直抽（免费，无需 RD）
        if not poster and FETCH_VIA_TORRENT and torrent_used < TORRENT_FETCH_MAX:
            torrent_used += 1
            print(f"🧲 [{ih[:8]}] BT 抽图: {title[:40]}")
            content = torrent_fetch_cover(it.get("link", ""), ih)
            if content:
                try_push(it, content, "bt")
            else:
                mark_failed(it)
                print("   ✗ 未抽到图")
        # 2) RD 兜底（配了 RD_TOKEN 时）
        if not it.get("poster") and RD_TOKEN and rd_used < RD_FETCH_MAX \
                and it.get("cover_status", "").split(":")[0] == "failed":
            rd_used += 1
            print(f"☁️ [{ih[:8]}] RD 抽图: {title[:40]}")
            content = rd_fetch_cover(ih, title)
            if content:
                try_push(it, content, "rd")
            else:
                mark_failed(it)
                print("   ✗ RD 未取到")
        time.sleep(1)

    cat["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    CATALOG_FILE.write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✅ 本轮封面: JavDB {javdb_done}，BT {bt_done}，RD {rd_done}")
    if not RD_TOKEN:
        print("ℹ️ 未设 RD_TOKEN（BT 直抽失败的不重试 RD）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
