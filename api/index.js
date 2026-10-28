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

// 关键词集合：英文/数字词 + 中日文二字组，用于文件名近似匹配
function kwSet(name) {
  const s = (name || "").toLowerCase().replace(/\.[a-z0-9]{2,5}$/i, "");
  const words = s.match(/[a-z0-9]{2,}/g) || [];
  const grams = [];
  for (const seg of s.match(/[぀-ヿ㐀-䶿一-鿿]+/g) || []) {
    for (let i = 0; i < seg.length - 1; i++) grams.push(seg.slice(i, i + 2));
  }
  return new Set(words.concat(grams));
}
function kwOverlap(a, b) {
  let n = 0;
  for (const t of a) { if (b.has(t)) n++; }
  return n;
}

// 返回 {streams, error}：成功给直链数组，失败给可读原因（显示在 P2P 源副标题里便于排查）
async function rdResolve(hash, key) {
  try {
    const lk = hash.toLowerCase();
    let e = rdIdCache.get(lk);
    const _vext = /\.(mkv|mp4|avi|mov|wmv|ts|m4v|mpg|webm)$/i;

    // 新任务流程：addMagnet → 等元数据 → 只勾选视频文件（前6大）→ RD 云端只下视频
    const addAndSelect = async () => {
      let id = null;
      try {
        const added = await rdApi("/torrents/addMagnet", key, {
          method: "POST",
          body: new URLSearchParams({ magnet: `magnet:?xt=urn:btih:${lk}` }),
        });
        id = added && added.id;
      } catch (err) {
        if (err.status === 401 || err.status === 403) {
          return { error: `RD key 无效或无权限(HTTP ${err.status})` };
        }
        if (err.status === 400) {
          const list = await rdApi("/torrents?limit=100", key);
          const found = (list || []).find(
            (t) => (t.hash || "").toLowerCase() === lk
          );
          if (!found) return { error: "RD 里找不到该种子(HTTP 400)" };
          id = found.id;
        } else {
          return { error: `addMagnet 失败: HTTP ${err.status}` };
        }
      }
      if (!id) return { error: "RD 未返回任务 id" };
      e = { id, at: Date.now() };
      rdIdCache.set(lk, e);
      // 等元数据列出文件（最多 ~16s；等不到就 all 兜底先让下载跑起来）
      let meta = await rdApi(`/torrents/info/${e.id}`, key);
      for (let i = 0; meta && !((meta.files || []).length) && i < 8; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        meta = await rdApi(`/torrents/info/${e.id}`, key);
      }
      // 匹配：同名（前缀）优先 > 关键词近似（二字组/单词重叠≥2）> 不匹配则跳过 RD
      const baseName = (p) => {
        const b = (p || "").split("/").pop();
        return b.replace(/\.[a-z0-9]{2,5}$/i, "");
      };
      const tname = baseName(meta && meta.filename);
      const tkw = kwSet(tname);
      const ranked = ((meta && meta.files) || [])
        .filter((f) => f.path && _vext.test(f.path))
        .map((f) => {
          const bn = baseName(f.path);
          if (tname && (bn === tname || bn.startsWith(tname))) return { f, tier: 0, ov: 1e9 };
          const ov = kwOverlap(kwSet(bn), tkw);
          return { f, tier: ov >= 2 ? 1 : 2, ov };
        })
        .filter((x) => x.tier < 2)
        .sort((a, b) => (a.tier - b.tier) || (b.ov - a.ov) || ((b.f.bytes || 0) - (a.f.bytes || 0)));
      // 只勾选匹配到的视频（最多6个，多个全列在源列表）
      const wantIds = ranked.slice(0, 6).map((x) => String(x.f.id));
      if (!wantIds.length) {
        return { error: "包内没有同名/关键词匹配的视频，已跳过 RD 下载（请用 P2P 源）" };
      }
      try {
        await rdApi(`/torrents/selectFiles/${e.id}`, key, {
          method: "POST",
          body: new URLSearchParams({ files: wantIds.join(",") }),
        });
        console.log(`[rd] 新任务勾选同名视频: ${wantIds.join(",")}`);
      } catch (err) {
        console.log(`[rd] selectFiles 失败: ${(err && err.message) || err}`);
      }
      await new Promise((r) => setTimeout(r, 1000));
      return { info: await rdApi(`/torrents/info/${e.id}`, key) };
    };

    let info = null;
    if (!e || Date.now() - e.at > 24 * 3600 * 1000) {
      const r = await addAndSelect();
      if (r.error) return r;
      info = r.info;
    } else {
      info = await rdApi(`/torrents/info/${e.id}`, key);
      if (!info) return { error: "RD 任务信息为空" };
      // 匹配：同名 > 关键词近似 > 不匹配（不匹配则删除重加后空选，不再耗流量）
      const baseName = (p) => {
        const b = (p || "").split("/").pop();
        return b.replace(/\.[a-z0-9]{2,5}$/i, "");
      };
      const tname = baseName(info.filename);
      const tkw = kwSet(tname);
      const ranked = ((info.files || []))
        .filter((f) => f.path && _vext.test(f.path))
        .map((f) => {
          const bn = baseName(f.path);
          if (tname && (bn === tname || bn.startsWith(tname))) return { f, tier: 0, ov: 1e9 };
          const ov = kwOverlap(kwSet(bn), tkw);
          return { f, tier: ov >= 2 ? 1 : 2, ov };
        })
        .filter((x) => x.tier < 2)
        .sort((a, b) => (a.tier - b.tier) || (b.ov - a.ov) || ((b.f.bytes || 0) - (a.f.bytes || 0)));
      const wantNow = ranked.slice(0, 6).map((x) => String(x.f.id)).sort().join(",");
      const curNow = ((info.files || []).filter((f) => f.selected).map((f) => String(f.id))).sort().join(",");
      const multiFile = ((info.files || []).filter((f) => f.selected).length > 1);
      if (info.status === "downloaded" && multiFile && wantNow !== curNow) {
        console.log(`[rd] 全选任务改仅视频，删除重加: ${lk.slice(0, 8)}`);
        try { await rdApi(`/torrents/delete/${e.id}`, key, { method: "DELETE" }); } catch (_) {}
        rdIdCache.delete(lk);
        e = null;
        const r = await addAndSelect();
        if (r.error) return r;
        info = r.info;
        if (info && info.status !== "downloaded") {
          return { error: `已改为仅下载视频文件，云端就绪后重试（当前=${info.status}）` };
        }
      }
    }
    if (!info) return { error: "RD 任务信息为空" };
    if (info.status !== "downloaded") {
      return { error: `RD 云端状态=${info.status}（下载完成后重试）` };
    }
    if (!Array.isArray(info.links) || !info.links.length) {
      return { error: "RD 云端无直链" };
    }
      // links[] 与 files[] 中 selected=true 的条目一一对应（RD 直链 URL 无后缀，必须靠 files[].path 判断）
      const files = Array.isArray(info.files) ? info.files : [];
      const videoExt = /\.(mkv|mp4|avi|mov|wmv|ts|m4v|mpg|webm)$/i;
      const selIdx = files
        .map((f, i) => (f && f.selected ? i : -1))
        .filter((i) => i >= 0);
      const vids = [];
      let li = 0;
      for (const i of selIdx) {
        const link = info.links[li];
        li++;
        if (!link) break;
        const path = files[i].path || "";
        const bytes = files[i].bytes || 0;
        if (videoExt.test(path)) {
          vids.push({ link, path, bytes });
        }
      }
      if (!vids.length) return { error: "云端里没选到视频文件" };
      // 大包按大小降序列出，每个视频文件各给一个源，便于定位/挑选
      // 同名 > 关键词重叠 > 大小
      {
        const baseName = (p) => {
          const b = (p || "").split("/").pop();
          return b.replace(/\.[a-z0-9]{2,5}$/i, "");
        };
        const tnm = baseName(info.filename);
        const tkw = kwSet(tnm);
        const rank = new Map();
        for (const v of vids) {
          const bn = baseName(v.path);
          if (tnm && (bn === tnm || bn.startsWith(tnm))) rank.set(v, [0, 1e9]);
          else {
            const ov = kwOverlap(kwSet(bn), tkw);
            rank.set(v, [ov >= 2 ? 1 : 2, ov]);
          }
        }
        vids.sort((a, b) => {
          const ra = rank.get(a), rb = rank.get(b);
          return (ra[0] - rb[0]) || (rb[1] - ra[1]) || (b.bytes - a.bytes);
        });
      }
      const picks = vids.slice(0, 6);
      const streams = [];
      for (let n = 0; n < picks.length; n++) {
        const v = picks[n];
        // torrents/info 的 links 还不是最终直链，必须再过 /unrestrict/link 换 CDN 地址
        const un = await rdApi("/unrestrict/link", key, {
          method: "POST",
          body: new URLSearchParams({ link: v.link }),
        });
        if (!un || !un.download) continue;
        // 以 RD 自己报告的真实文件名为准（files[].path 与 links[] 对位可能错位）
        const fileName = (un.filename || v.path.split("/").pop() || "").trim();
        const isVideo = videoExt.test(fileName);
        const isPacked = /\.(rar|zip|7z)$/i.test(fileName);
        if (!isVideo && !isPacked) {
          console.log(`[rd] 剔除非视频文件: ${fileName}`);
          continue;
        }
        let warn = "";
        let prefix = "";
        if (isPacked) {
          // RD 服务端把敏感资源重打包成 rar（规避审查），内容是真视频但 Stremio 内置播放器解不了
          prefix = "📦 ";
          warn = "\n⚠️ RD 已打包为压缩包，内置播放器只出声不出画\n👉 Stremio 设置→外部播放器→选 VLC 后点本源（VLC 直接播rar）";
        } else if (/\.(wmv|ts)$/i.test(fileName)) {
          warn = "\n⚠️ 该格式(WMV/TS)部分设备无画面，P2P 源可换其他版本";
        }
        streams.push({
          name: streams.length === 0 ? `${prefix}RD ☁️ 秒播` : `${prefix}RD ☁️ #${streams.length + 1}`,
          url: un.download,
          title: `${fileName}\n☁️ Real-Debrid 云端直链${warn}`,
        });
      }
      if (!streams.length) return { error: "云端没有可用文件" };
      return { streams };
    } catch (err) {
    return { error: `RD 请求异常: ${(err && err.message) || err}` };
  }
}

async function buildStreams(item, conf) {
  const streams = [];
  let rdNote = "";
  if (conf && conf.rd) {
    const rd = await rdResolve(item.infoHash, conf.rd);
    if (rd.streams && rd.streams.length) {
      streams.push(...rd.streams);
    } else {
      rdNote = `⚠️ RD未生效：${rd.error || "未知原因"}\n`;
      console.log(`[rd] 失败 ${item.infoHash.slice(0, 8)}: ${rd.error || "未知原因"}`);
    }
  }
  streams.push({
    name: streams.length ? "P2P 备用" : "P2P",
    infoHash: item.infoHash,
    title: `${rdNote}${item.name}\n👤 ${item.seeders} seeders${
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
        version: "1.2.2",
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
