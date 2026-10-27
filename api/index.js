// Stremio 精选 Catalog 插件（Vercel serverless）
// 冷启动时从 GitHub raw 拉取 catalog.json 并驻留内存，响应极快、零数据库
const { addonBuilder, serveHTTP } = require("stremio-addon-sdk");
const https = require("https");

const CATALOG_URL =
  process.env.CATALOG_URL ||
  "https://raw.githubusercontent.com/kevinhfwang/stremio-sukebei-catalog/main/data/catalog.json";
const CACHE_TTL_MS = 10 * 60 * 1000; // 10 分钟

let cache = { data: null, at: 0 };

function fetchCatalog() {
  if (cache.data && Date.now() - cache.at < CACHE_TTL_MS) {
    return Promise.resolve(cache.data);
  }
  return new Promise((resolve, reject) => {
    https
      .get(CATALOG_URL, (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          return reject(new Error(`catalog fetch ${res.statusCode}`));
        }
        let body = "";
        res.on("data", (c) => (body += c));
        res.on("end", () => {
          try {
            cache = { data: JSON.parse(body), at: Date.now() };
            resolve(cache.data);
          } catch (e) {
            reject(e);
          }
        });
      })
      .on("error", reject)
      .setTimeout(15000, function () {
        this.destroy(new Error("catalog fetch timeout"));
      });
  });
}

function toMeta(item) {
  return {
    id: item.id,
    type: "movie",
    name: item.name,
    poster: item.poster || undefined,
    genre: item.nyaa_category ? [item.nyaa_category] : undefined,
    description: [
      item.nyaa_category,
      item.size ? `大小: ${item.size}` : "",
      item.seeders != null ? `做种: ${item.seeders}` : "",
      item.link ? `<a href="${item.link}">来源页面</a>` : "",
    ]
      .filter(Boolean)
      .join(" · "),
  };
}

let builderPromise = null;
function getBuilder() {
  if (!builderPromise) {
    builderPromise = fetchCatalog().then((cat) => {
      const keywords = (cat.config && cat.config.keywords) || [];
      const manifest = {
        id: "com.kevinhfwang.sukebei-catalog",
        version: "1.0.0",
        name: "Sukebei 精选",
        description:
          "RSS 精选 Top（每 8 小时更新）· 只返回 infoHash，配合 Real-Debrid 秒播",
        resources: ["catalog", "stream"],
        types: ["movie"],
        catalogs: keywords.map((k) => ({
          type: "movie",
          id: k.slug,
          name: k.catalog_name || k.keyword,
          adult: true,
        })),
        adult: true,
        behaviorHints: { configurable: false, configurationRequired: false },
      };
      const builder = new addonBuilder(manifest);

      builder.defineCatalogHandler(({ type, id, extra }) => {
        const ids = (cat.catalogs && cat.catalogs[id]) || [];
        const skip = Number(extra && extra.skip) || 0;
        const metas = ids
          .slice(skip, skip + 100)
          .map((ih) => cat.items[ih])
          .filter(Boolean)
          .map(toMeta);
        return Promise.resolve({ metas });
      });

      builder.defineStreamHandler(({ type, id }) => {
        const item = cat.items[id];
        if (!item) return Promise.resolve({ streams: [] });
        return Promise.resolve({
          streams: [
            {
              name: "P2P",
              infoHash: item.infoHash,
              title: `${item.name}\n👤 ${item.seeders} seeders${
                item.size ? " · " + item.size : ""
              }`,
            },
          ],
        });
      });

      return builder;
    });
  }
  return builderPromise;
}

function landing(res) {
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.end(`<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sukebei 精选</title>
<style>body{font-family:system-ui;max-width:640px;margin:60px auto;padding:0 20px;color:#eee;background:#111}
a{color:#4ea1ff}</style></head><body>
<h1>Sukebei 精选 Catalog 插件</h1>
<p>RSS 定时抓取 · Top 150/分类 · 只返回 infoHash，配合 Real-Debrid 秒播 · 18+</p>
<p>安装：Stremio 里添加本 URL 即可。</p>
<p><a href="/manifest.json">manifest.json</a></p>
</body></html>`);
}

module.exports = async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  if (req.method === "OPTIONS") {
    res.statusCode = 204;
    return res.end();
  }
  const path = req.url.split("?")[0];
  try {
    const iface = (await getBuilder()).getInterface();
    // Stremio 部分客户端直接请求根 URL 当 manifest，根路径必须回 JSON
    if (path === "/" || path === "" || path === "/manifest.json") {
      res.setHeader("Content-Type", "application/json");
      return res.end(JSON.stringify(iface.manifest));
    }
    // /catalog/{type}/{id}.json 或 /catalog/{type}/{id}/{k=v&k=v}.json（stream 同构）
    const m = path.match(/^\/(catalog|stream)\/([^/]+)\/([^/]+?)(?:\/([^/]+))?\.json$/);
    if (!m) {
      res.statusCode = 404;
      return res.end("Not Found");
    }
    const [, resource, type, id, extraSeg] = m;
    const extra = {};
    if (extraSeg) {
      for (const kv of extraSeg.split("&")) {
        const i = kv.indexOf("=");
        if (i > 0) extra[decodeURIComponent(kv.slice(0, i))] = decodeURIComponent(kv.slice(i + 1));
      }
    }
    const qi = req.url.indexOf("?");
    if (qi >= 0) new URLSearchParams(req.url.slice(qi + 1)).forEach((v, k) => (extra[k] = v));
    const result = await iface.get(resource, type, decodeURIComponent(id), extra);
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify(result));
  } catch (e) {
    if (e && e.noHandler) {
      res.statusCode = 404;
      return res.end("Not Found");
    }
    res.statusCode = 502;
    res.end("catalog unavailable: " + ((e && e.message) || e));
  }
};

// 本地调试: node api/index.js → http://localhost:7000/manifest.json
if (require.main === module) {
  fetchCatalog()
    .then(() => getBuilder())
    .then((b) => serveHTTP(b.getInterface(), { port: 7000 }))
    .then(() => console.log("local: http://localhost:7000/manifest.json"));
}
