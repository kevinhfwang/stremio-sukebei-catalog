# Stremio Sukebei 精选 Catalog 插件

RSS 定时抓取 → 静态 catalog.json（GitHub 托管）→ Vercel serverless 插件。
只返回 infoHash，配合 Real-Debrid 秒播。**18+，仅个人使用。**

## 架构

- **抓取**: GitHub Actions 每 8 小时运行 `scripts/scrape.py`（纯标准库，零依赖），结果提交到 `data/catalog.json`
- **插件**: Vercel 托管 `api/index.js`（stremio-addon-sdk），冷启动读一次 catalog.json 驻留内存，10 分钟缓存
- **配置**: `data/config.json` 里的 RSS 源模板 + 关键词，用本地脚本管理（见下）

## 部署

### 1. GitHub（抓取端）
本仓库推上 GitHub 即可，Actions 自动按 cron 运行（也可 Actions 页手动 Run）。

### 2. Vercel（插件端）
1. [vercel.com/new](https://vercel.com/new) 导入本仓库（Framework: Other，零配置，`api/` + `vercel.json` 自动识别）
2. 部署后得到 `https://<project>.vercel.app`，Stremio 里添加该 URL 即安装
3. Stremio 内登录 Real-Debrid 后，infoHash 资源直接秒播

### 3. 本地调试
```bash
npm install
node api/index.js   # http://localhost:7000/manifest.json
```

## 更换 RSS 源 / 关键词（本地脚本）

```bash
python3 scripts/catalog_manager.py                     # 交互模式
python3 scripts/catalog_manager.py list                # 查看当前配置
python3 scripts/catalog_manager.py replace-source "https://sukebei.nyaa.si/?page=rss&q={keyword}&s=seeders&o=desc"
python3 scripts/catalog_manager.py add-keyword 新关键词 [slug] [显示名]
python3 scripts/catalog_manager.py remove-keyword 关键词
```

- 模板必须含 `{keyword}` 占位符
- 提交前本地抓取验证；加 `--run` 立即触发 Actions 重新抓取，否则下个 8 小时周期生效

## 注意

- 仓库需 **public**（Vercel 用 raw.githubusercontent 免密读 catalog.json）；要 private 就得给 Vercel 配 token
- catalog.json 是元数据（标题/hash/做种数），不含文件本体
