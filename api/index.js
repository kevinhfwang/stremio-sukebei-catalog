// Stremio 精选 Catalog 插件（Vercel serverless）
// 冷启动时从 GitHub raw 拉取 catalog.json 并驻留内存，响应极快、零数据库
// 可选配置：安装页填 Real-Debrid API key → 生成带配置的 manifest URL，
// stream 直接返回 RD 云端直链（秒播）；不配置则返回裸 infoHash（走 Stremio 内置 RD）
const { addonBuilder, serveHTTP } = require("stremio-addon-sdk");
const https = require("https");

const CATALOG_URL =
  process.env.CATALOG_URL ||
  "https://raw.githubusercontent.com/kevinhfwang/stremio-sukebei-catalog/main/data/catalog.json";
const CACHE_TTL_MS = 10 * 60 * 1000; // 10 分钟

let cache = { data: null, at: 0 };
let catData = null; // builder 初始化后置位，供 stream 直连逻辑用

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

// ---------- Real-Debrid ----------

const RD_API = "https://api.real-debrid.com/rest/1.0";
const rdIdCache = new Map(); // infoHash(小写) → {id, at}，24h，避免每次 addMagnet

async function rdApi(path, key, opts = {}) {
  const r = await fetch(RD_API + path, {
    method: opts.method || "GET",
    headers: { Authorization: `Bearer ${key}` },
    body: opts.body,
    signal: AbortSignal.timeout(10000),
  });
  if (r.status === 204) return null;
  const j = await r.json().catch(() => null);
  if (!r.ok) {
    const err = new Error(`RD ${r.status}`);
    err.status = r.status;
    err.body = j;
    throw err;
  }
  return j;
}

// 返回 RD 直链 stream 对象，未就绪/失败返回 null（调用方退回 infoHash）
async function rdResolve(hash, key) {
  try {
    const lk = hash.toLowerCase();
    let e = rdIdCache.get(lk);
    if (!e || Date.now() - e.at > 24 * 3600 * 1000) {
      let id = null;
      try {
        const added = await rdApi("/torrents/addMagnet", key, {
          method: "POST",
          body: new URLSearchParams({ magnet: `magnet:?xt=urn:btih:${lk}` }),
        });
        id = added && added.id;
        // 新 magnet：选全部文件，RD 云端开始离线下载
        try {
          await rdApi(`/torrents/selectFiles/${id}`, key, {
            method: "POST",
            body: new URLSearchParams({ files: "all" }),
          });
        } catch (_) {}
      } catch (err) {
        if (err.status === 400) {
          // 已添加过：从列表按 hash 找回 id
          const list = await rdApi("/torrents?limit=100", key);
          const found = (list || []).find(
            (t) => (t.hash || "").toLowerCase() === lk
          );
          if (!found) return null;
          id = found.id;
        } else {
          return null;
        }
      }
      if (!id) return null;
      e = { id, at: Date.now() };
      rdIdCache.set(lk, e);
    }
    const info = await rdApi(`/torrents/info/${e.id}`, key);
    if (info && info.status === "downloaded" && Array.isArray(info.links) && info.links.length) {
      // links[] 与 files[] 中 selected=true 的条目一一对应（RD 直链 URL 无后缀，必须靠 files[].path 判断）
      const files = Array.isArray(info.files) ? info.files : [];
      const videoExt = /\.(mkv|mp4|avi|mov|wmv|ts|m4v|mpg|webm)$/i;
      const selIdx = files
        .map((f, i) => (f && f.selected ? i : -1))
        .filter((i) => i >= 0);
      let pick = null;
      let pickName = info.filename || "";
      let bestBytes = -1;
      let li = 0;
      for (const i of selIdx) {
        const link = info.links[li];
        li++;
        if (!link) break;
        const path = files[i].path || "";
        const bytes = files[i].bytes || 0;
        if (videoExt.test(path) && bytes > bestBytes) {
          bestBytes = bytes;
          pick = link;
          pickName = path.split("/").pop() || path;
        }
      }
      if (!pick) return null; // 云端里没有视频文件，退回 P2P
      return {
        name: "RD ☁️ 秒播",
        url: pick,
        title: `${pickName}\n☁️ Real-Debrid 云端直链`,
      };
    }
    return null; // RD 云端还在下载，退回 infoHash
  } catch (_) {
    return null;
  }
}

async function buildStreams(item, conf) {
  const streams = [];
  if (conf && conf.rd) {
    const rd = await rdResolve(item.infoHash, conf.rd);
    if (rd) streams.push(rd);
  }
  streams.push({
    name: streams.length ? "P2P 备用" : "P2P",
    infoHash: item.infoHash,
    title: `${item.name}\n👤 ${item.seeders} seeders${
      item.size ? " · " + item.size : ""
    }`,
  });
  return streams;
}

// ---------- 配置编码（安装页生成） ----------

function encodeConf(obj) {
  return Buffer.from(JSON.stringify(obj))
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function parseConf(seg) {
  try {
    const b64 = seg.replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(Buffer.from(b64, "base64").toString("utf8"));
  } catch (_) {
    return null;
  }
}

let builderPromise = null;
function getBuilder() {
  if (!builderPromise) {
    builderPromise = fetchCatalog().then((cat) => {
      catData = cat;
      const keywords = (cat.config && cat.config.keywords) || [];
      const manifest = {
        id: "com.kevinhfwang.sukebei-catalog",
        version: "1.1.0",
        name: "Sukebei 精选",
        description:
          "RSS 精选 Top（每 8 小时更新）· 安装页可配置 Real-Debrid key 云端秒播，未配置则返回 infoHash 走 P2P/客户端 RD",
        resources: ["catalog", "stream", "meta"],
        types: ["movie"],
        catalogs: keywords.map((k) => ({
          type: "movie",
          id: k.slug,
          name: k.catalog_name || k.keyword,
          adult: true,
        })),
        adult: true,
        behaviorHints: { configurable: true, configurationRequired: false },
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

      // 自建 id（裸 infoHash）Cinemeta 查不到，必须自己提供 meta，否则点进条目报
      // "No addons were requested for this meta!" 且不会请求 stream
      builder.defineMetaHandler(({ type, id }) => {
        const item = cat.items[id];
        if (!item) return Promise.reject({ noHandler: true });
        return Promise.resolve({ meta: toMeta(item) });
      });

      builder.defineStreamHandler(({ type, id }) => {
        const item = cat.items[id];
        if (!item) return Promise.resolve({ streams: [] });
        return buildStreams(item, null).then((streams) => ({ streams })); // 无配置路径：仅 infoHash
      });

      return builder;
    });
  }
  return builderPromise;
}

function landing(req, res) {
  const host = req.headers["x-forwarded-host"] || req.headers.host || "localhost";
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.end(`<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sukebei 精选 · 安装</title>
<style>
body{font-family:system-ui;max-width:560px;margin:40px auto;padding:0 20px;color:#eee;background:#141414}
a{color:#4ea1ff}input{width:100%;padding:12px;margin:8px 0;border:1px solid #444;border-radius:8px;background:#1e1e1e;color:#eee;box-sizing:border-box}
button{width:100%;padding:12px;border:0;border-radius:8px;background:#4ea1ff;color:#000;font-weight:600;font-size:15px;cursor:pointer}
#out{margin-top:20px;word-break:break-all;background:#1e1e1e;padding:12px;border-radius:8px;display:none}
.warn{color:#999;font-size:13px}
</style></head><body>
<h1>Sukebei 精选 Catalog 插件</h1>
<p>RSS 定时抓取 · Top/分类 · 每 8 小时更新 · 18+</p>
<h3>配置 Real-Debrid（可选，云端秒播）</h3>
<p class="warn">在 <a href="https://real-debrid.com/apitoken" target="_blank">real-debrid.com/apitoken</a> 复制你的 API key 填入下方。不填则返回裸 infoHash（P2P，或依赖 Stremio 客户端自己登录的 RD）。</p>
<input id="key" type="password" placeholder="Real-Debrid API key" autocomplete="off">
<button onclick="go()">生成安装链接</button>
<div id="out"></div>
<p class="warn">注意：key 会编码在安装 URL 里（仅自己设备使用）。</p>
<script>
function go(){
  const key=document.getElementById('key').value.trim();
  let url='https://${host}/manifest.json';
  if(key){
    const conf=btoa(unescape(encodeURIComponent(JSON.stringify({rd:key})))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
    url='https://${host}/'+conf+'/manifest.json';
  }
  const stremio=url.replace(/^https?:\\/\\//,'stremio://');
  document.getElementById('out').style.display='block';
  document.getElementById('out').innerHTML=
    '<b>安装 URL：</b><br><a href="'+stremio+'">'+url+'</a><br><br>'+
    '<a href="'+stremio+'" style="display:inline-block;padding:10px 16px;background:#4ea1ff;color:#000;border-radius:8px;text-decoration:none;font-weight:600">在 Stremio 中打开</a>';
}
</script>
</body></html>`);
}

module.exports = async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  if (req.method === "OPTIONS") {
    res.statusCode = 204;
    return res.end();
  }
  let path = req.url.split("?")[0];
  // 安装页
  if (path === "/" || path === "") return landing(req, res);

  // 可选配置前缀：/<conf>/manifest.json 或 /<conf>/{catalog,stream,meta}/...
  let conf = null;
  const seg = path.match(/^\/([A-Za-z0-9\-_]{8,})\/(.+)$/);
  if (seg && (seg[2] === "manifest.json" || /^(catalog|stream|meta)\//.test(seg[2]))) {
    conf = parseConf(seg[1]);
    path = "/" + seg[2];
  }

  try {
    const iface = (await getBuilder()).getInterface();
    if (path === "/manifest.json") {
      res.setHeader("Content-Type", "application/json");
      return res.end(JSON.stringify(iface.manifest));
    }
    // /catalog/{type}/{id}.json 或 /catalog/{type}/{id}/{k=v&k=v}.json（stream/meta 同构）
    const m = path.match(/^\/(catalog|stream|meta)\/([^/]+)\/([^/]+?)(?:\/([^/]+))?\.json$/);
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

    // stream 资源带配置时走 RD 解析；catalog/meta 与配置无关
    if (resource === "stream" && conf && catData) {
      const item = catData.items[decodeURIComponent(id)];
      const streams = item ? await buildStreams(item, conf) : [];
      res.setHeader("Content-Type", "application/json");
      return res.end(JSON.stringify({ streams }));
    }
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
