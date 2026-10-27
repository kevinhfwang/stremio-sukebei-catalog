#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py - 从 RSS 源抓取种子元数据，生成 Stremio catalog.json

- 纯标准库（GitHub Actions 免 pip install）
- 数据源/关键词由 data/config.json 配置（本地 catalog_manager.py 管理）
- 同一 infoHash 全局去重，保留 first_seen
- 全部源失败时才放弃，否则沿用已有 catalog（站点不失效）
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_FILE = DATA_DIR / "config.json"
CATALOG_FILE = DATA_DIR / "catalog.json"

DEFAULT_CONFIG = {
    "feed_template": "https://sukebei.nyaa.si/?page=rss&q={keyword}&s=seeders&o=desc",
    "keywords": [
        {"keyword": "吃瓜", "slug": "chigua", "catalog_name": "吃瓜 · Top 150"},
    ],
    "per_keyword_limit": 150,
    "pages_per_keyword": 2,
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_config():
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if cfg.get("keywords"):
                return cfg
        except Exception as e:
            print(f"⚠️ config.json 损坏 ({e})，用内置默认")
    return DEFAULT_CONFIG


def localname(tag):
    """去掉 XML namespace，兼容 nyaa/sukebei 不同域名"""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def fetch(url, attempts=3, timeout=25):
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            wait = 2 ** i
            print(f"   ⚠️ 第{i+1}次失败: {e}，{wait}s 后重试")
            time.sleep(wait)
    return None


def parse_feed(raw, keyword):
    """解析 RSS，返回条目列表；字段按 localname 匹配，适配 nyaa/sukebei"""
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"   ✗ XML 解析失败: {e}")
        return items
    for item in root.iter():
        if localname(item.tag) != "item":
            continue
        d = {}
        desc = ""
        for child in item:
            name = localname(child.tag)
            text = (child.text or "").strip()
            if name == "description":
                desc = text
            elif text:
                d[name] = text
        info_hash = d.get("infoHash", "")
        if not info_hash:
            m = re.search(r"([0-9a-fA-F]{40})", desc)
            info_hash = m.group(1) if m else ""
        if not info_hash:
            continue
        poster = ""
        m = re.search(r'<img[^>]+src="([^"]+)"', desc)
        if m:
            poster = m.group(1)
        title = d.get("title", "")
        # nyaa RSS 转义引号，还原显示
        title = title.replace("&quot;", '"').replace("&amp;", "&").replace("&#34;", '"')
        try:
            seeders = int(d.get("seeders", "0"))
        except ValueError:
            seeders = 0
        items.append({
            "id": info_hash.lower(),
            "name": title,
            "infoHash": info_hash.lower(),
            "seeders": seeders,
            "nyaa_category": d.get("category", ""),
            "link": d.get("guid") or d.get("link", ""),
            "size": d.get("size", ""),
            "poster": poster,
            "keyword": keyword,
            "first_seen": now_iso(),
        })
    return items


def load_catalog():
    if CATALOG_FILE.exists():
        try:
            return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": {}, "catalogs": {}}


def main():
    cfg = load_config()
    template = cfg["feed_template"]
    limit = int(cfg.get("per_keyword_limit", 150))
    # 每个关键词抓多个 URL 变体（sukebei 按 nyaa 分类拆分，突破 RSS 75 条/页上限）
    variants = cfg.get("keyword_variants") or ["", "&c=2_1", "&c=2_2"]
    print(f"📋 数据源模板: {template}")
    print(f"📋 关键词 {len(cfg['keywords'])} 个，每分类 Top {limit}（{len(variants)} 个 URL 变体）")

    old = load_catalog()
    old_items = old.get("items", {})
    failed = []
    total_new = 0
    run_top = {}  # slug -> 本次抓取该关键词的条目

    for kw in cfg["keywords"]:
        keyword = kw["keyword"]
        slug = kw["slug"]
        collected = []
        for vi, variant in enumerate(variants, 1):
            url = template.format(keyword=urllib.parse.quote(keyword)) + variant
            print(f"📡 [{slug}] 变体{vi} ({variant or '全部'})")
            raw = fetch(url)
            if raw is None:
                print(f"   ✗ 抓取失败: {url}")
                if vi == 1:
                    failed.append(keyword)
                break
            got = parse_feed(raw, keyword)
            print(f"   ✓ {len(got)} 条")
            collected.extend(got)
            time.sleep(1)

        # 按 seeders 排序取本次 Top N
        seen = set()
        top = []
        for it in sorted(collected, key=lambda x: -x["seeders"]):
            if it["infoHash"] in seen:
                continue
            seen.add(it["infoHash"])
            top.append(it)
        run_top[slug] = (keyword, top[:limit])

    if all(not t for _, t in run_top.values()) and old_items:
        print("⚠️ 全部源失败，沿用已有 catalog")
        return 0

    # 合并持久池：近 30 天的旧条目 + 本次新条目（infoHash 全局去重，保留 first_seen）
    cutoff = time.time() - 30 * 86400
    pool = {}
    for ih, it in old_items.items():
        try:
            fs = datetime.fromisoformat(it.get("first_seen", "1970-01-01")).timestamp()
        except ValueError:
            fs = 0
        if fs > cutoff:
            pool[ih] = it

    for slug, (keyword, tops) in run_top.items():
        for it in tops:
            ih = it["infoHash"]
            prev = pool.get(ih) or old_items.get(ih)
            if prev:
                it["first_seen"] = prev.get("first_seen", it["first_seen"])
                it["keywords"] = sorted(set(prev.get("keywords", [])) | {keyword})
            else:
                it["keywords"] = [keyword]
                total_new += 1
            pool[ih] = it

    items = pool

    # 组装 catalogs：每个 slug 下所有命中该关键词的 item，按 seeders 排序
    catalogs = {}
    for kw in cfg["keywords"]:
        slug = kw["slug"]
        kws_items = [it for it in items.values() if kw["keyword"] in it.get("keywords", [])]
        kws_items.sort(key=lambda x: -x["seeders"])
        catalogs[slug] = [it["id"] for it in kws_items[:limit]]

    out = {
        "updated_at": now_iso(),
        "config": {
            "feed_template": template,
            "per_keyword_limit": limit,
            "keywords": cfg["keywords"],
        },
        "feeds": [{"name": k["catalog_name"], "keyword": k["keyword"],
                   "url": template.format(keyword=urllib.parse.quote(k["keyword"]))}
                  for k in cfg["keywords"]],
        "count": len(items),
        "items": items,
        "catalogs": catalogs,
    }
    DATA_DIR.mkdir(exist_ok=True)
    CATALOG_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✅ 共 {len(items)} 部（新增 {total_new}），catalogs: "
          + ", ".join(f"{s}={len(v)}" for s, v in catalogs.items()))
    print(f"✅ 已保存 {CATALOG_FILE}")
    if failed:
        print(f"⚠️ 失败关键词: {', '.join(failed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
