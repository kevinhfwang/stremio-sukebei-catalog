#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
catalog_manager.py - 本地管理 Stremio catalog 插件的 RSS 源与关键词

数据文件: kevinhfwang/stremio-sukebei-catalog 仓库 data/config.json
GitHub Actions 每 8 小时运行 scrape.py 时读取该文件。

用法:
    python3 catalog_manager.py                     # 交互模式
    python3 catalog_manager.py list                # 查看源/关键词/限制
    python3 catalog_manager.py replace-source URL模板   # 换主数据源（须含 {keyword}）
    python3 catalog_manager.py add-keyword 关键词 [slug] [显示名]
    python3 catalog_manager.py remove-keyword 关键词
    加 --run 提交后立即触发 GitHub Actions 重新抓取
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request

REPO = "kevinhfwang/stremio-sukebei-catalog"
FILE_PATH = "data/config.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def gh(args, data=None):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True,
                       input=json.dumps(data) if data else None)
    if r.returncode != 0:
        print(f"✗ gh 调用失败: {r.stderr.strip()[:400]}")
        sys.exit(1)
    return r.stdout.strip()


def load_config():
    out = gh(["api", f"repos/{REPO}/contents/{FILE_PATH}", "--jq", "{content:.content,sha:.sha}"])
    info = json.loads(out)
    return json.loads(base64.b64decode(info["content"]).decode("utf-8")), info["sha"]


def save_config(cfg, sha, message):
    payload = {"message": message, "content": base64.b64encode(
        json.dumps(cfg, ensure_ascii=False, indent=2).encode()).decode(), "sha": sha}
    gh(["api", f"repos/{REPO}/contents/{FILE_PATH}", "-X", "PUT", "--input", "-"], data=payload)
    print("✅ 已提交到 GitHub")


def trigger_run():
    gh(["api", f"repos/{REPO}/actions/workflows/scrape.yml/dispatches", "-X", "POST", "-f", "ref=main"])
    print("🚀 已触发 GitHub Actions 重新抓取")


def check_template(template, sample_kw):
    """本地验证模板可抓取且返回 RSS；返回条目数（0 表示暂无匹配内容，但 feed 有效）"""
    url = template.format(keyword=urllib.parse.quote(sample_kw))
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read().decode("utf-8", errors="ignore")
    if "<item" not in raw and "<channel" not in raw and "<entry" not in raw:
        raise ValueError("不是有效 RSS/Atom feed（需要含 <item> 或 <entry>）")
    return raw.count("<item") + raw.count("<entry")


def find_keyword(cfg, key):
    key = key.strip()
    for i, k in enumerate(cfg["keywords"]):
        if key == k["keyword"] or key == k["slug"]:
            return i
    raise ValueError(f"找不到关键词: {key}")


def print_config(cfg):
    print(f"\n主数据源模板: {cfg['feed_template']}")
    print(f"限制: 每分类 Top {cfg.get('per_keyword_limit',150)}，{cfg.get('pages_per_keyword',2)} 页/关键词")
    print(f"关键词 {len(cfg['keywords'])} 个：")
    for i, k in enumerate(cfg["keywords"], 1):
        print(f"  {i:2d}. {k['keyword']:<10} slug={k['slug']:<12} {k.get('catalog_name','')}")
    print()


def cmd_replace_source(cfg, sha, template, run):
    if "{keyword}" not in template:
        print("✗ 模板必须包含 {keyword} 占位符（会被替换为 URL 编码后的关键词）")
        sys.exit(1)
    sample = cfg["keywords"][0]["keyword"] if cfg["keywords"] else "test"
    n = check_template(template, sample)
    old = cfg["feed_template"]
    cfg["feed_template"] = template
    save_config(cfg, sha, f"config: 主数据源更换")
    print(f"✓ 已更换: {old} → {template}（{sample} 样本 {n} 条验证通过）")
    if run:
        trigger_run()


def cmd_add_keyword(cfg, sha, keyword, slug, name, run):
    if any(k["keyword"] == keyword for k in cfg["keywords"]):
        print(f"✗ 已存在关键词: {keyword}")
        sys.exit(1)
    slug = slug or f"k{len(cfg['keywords']) + 1}"
    if not SLUG_RE.match(slug):
        print(f"✗ slug 只能含小写字母/数字/-/下划线: {slug}")
        sys.exit(1)
    if any(k["slug"] == slug for k in cfg["keywords"]):
        print(f"✗ slug 已存在: {slug}")
        sys.exit(1)
    n = check_template(cfg["feed_template"], keyword)
    cfg["keywords"].append({"keyword": keyword, "slug": slug,
                            "catalog_name": name or f"{keyword} · Top {cfg.get('per_keyword_limit',150)}"})
    save_config(cfg, sha, f"config: 新增关键词 {keyword}")
    print(f"✓ 已新增 [{keyword}] slug={slug}（{n} 条验证通过）")
    if run:
        trigger_run()


def cmd_remove_keyword(cfg, sha, key, run):
    i = find_keyword(cfg, key)
    old = cfg["keywords"].pop(i)
    save_config(cfg, sha, f"config: 删除关键词 {old['keyword']}")
    print(f"✓ 已删除 [{old['keyword']}]")
    if run:
        trigger_run()


def interactive(run):
    cfg, sha = load_config()
    print_config(cfg)
    print("1) 更换主数据源  2) 新增关键词  3) 删除关键词  4) 退出")
    choice = input("选择: ").strip()
    if choice == "1":
        print(f"当前模板: {cfg['feed_template']}")
        print("提示: 模板须含 {keyword}，如 https://sukebei.nyaa.si/?page=rss&q={{keyword}}&s=seeders&o=desc")
        t = input("新模板 URL: ").strip()
        if t:
            cmd_replace_source(cfg, sha, t, run)
    elif choice == "2":
        kw = input("关键词: ").strip()
        slug = input("slug(英文/数字/横线，留空自动）: ").strip()
        name = input("目录显示名（留空自动）: ").strip()
        if kw:
            cmd_add_keyword(cfg, sha, kw, slug, name, run)
    elif choice == "3":
        key = input("要删的关键词或 slug: ").strip()
        if key:
            cmd_remove_keyword(cfg, sha, key, run)
    else:
        print("已退出，未做修改")


def main():
    p = argparse.ArgumentParser(description="管理 Stremio catalog 插件的 RSS 源与关键词")
    p.add_argument("command", nargs="?", choices=["list", "replace-source", "add-keyword", "remove-keyword"], default=None)
    p.add_argument("args", nargs="*")
    p.add_argument("--run", action="store_true", help="提交后立即触发 GitHub Actions")
    a = p.parse_args()

    if a.command is None:
        interactive(a.run)
        return
    if a.command == "list":
        cfg, _ = load_config()
        print_config(cfg)
        return

    cfg, sha = load_config()
    try:
        if a.command == "replace-source" and len(a.args) == 1:
            cmd_replace_source(cfg, sha, a.args[0], a.run)
        elif a.command == "add-keyword" and 1 <= len(a.args) <= 3:
            cmd_add_keyword(cfg, sha, a.args[0], a.args[1] if len(a.args) > 1 else "",
                            a.args[2] if len(a.args) > 2 else "", a.run)
        elif a.command == "remove-keyword" and len(a.args) == 1:
            cmd_remove_keyword(cfg, sha, a.args[0], a.run)
        else:
            p.print_help()
            sys.exit(1)
    except (ValueError, Exception) as e:
        print(f"✗ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
