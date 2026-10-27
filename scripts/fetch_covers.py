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
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

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


def push_cover(info_hash, content):
    ext = ".jpg"
    raw = content[:3]
    if raw.startswith(b"\x89P"):
        ext = ".png"
    elif raw.startswith(b"RIFF"):
        ext = ".webp"
    path = f"{info_hash}{ext}"
    import base64
    api = f"https://api.github.com/repos/{COVERS_REPO}/contents/{path}"
    body = {"message": f"cover: {info_hash}", "content": base64.b64encode(content).decode(),
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
    print(f"🖼️ 待补封面 {len(todo)} 个（本轮上限 {COVER_BATCH}，RD 抽取上限 {RD_FETCH_MAX}）")

    rd_used = 0
    done = javdb_done = rd_done = 0
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
        if not poster and RD_TOKEN and rd_used < RD_FETCH_MAX:
            rd_used += 1
            print(f"🧲 [{ih[:8]}] RD 抽图: {title[:40]}")
            content = rd_fetch_cover(ih, title)
            if content and COVERS_PAT:
                try:
                    it["poster"] = push_cover(ih, content)
                    it["cover_status"] = "rd"
                    rd_done += 1
                    print(f"✓ 已推送 covers 仓库 ({len(content)//1024}KB)")
                except Exception as e:
                    it["cover_status"] = "failed:1"
                    print(f"   ✗ 推送失败: {e}")
            elif content:
                it["cover_status"] = "failed:1"
                print("   ✗ 缺 COVERS_PAT，无法托管")
            else:
                tries = int(it.get("cover_status", "failed:0").split(":")[-1]) + 1 if it.get("cover_status") else 1
                it["cover_status"] = f"failed:{tries}" if tries < 2 else "failed:9"
                print("   ✗ 未抽到图")
        elif not poster and not RD_TOKEN:
            tries = int(it.get("cover_status", "failed:0").split(":")[-1]) + 1 if it.get("cover_status") else 1
            it["cover_status"] = f"failed:{tries}" if tries < 2 else "failed:9"
        time.sleep(1)

    cat["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    CATALOG_FILE.write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✅ 本轮封面: JavDB {javdb_done}，RD {rd_done}")
    if not RD_TOKEN:
        print("⚠️ 未设 RD_TOKEN，仅尝试 JavDB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
