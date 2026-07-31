/**
 * Cloudflare Worker for the prospect screener.
 *
 * Why this exists: a static page cannot call TWSE. The browser blocks it —
 * TWSE sends no CORS headers — so the GitHub Pages build has to route the fetch
 * through a scheduled GitHub Action that commits JSON into the repo. That works,
 * but the data is only as fresh as the last run.
 *
 * A Worker runs on the server, where the same-origin policy does not apply. It
 * can call TWSE directly, on demand, and hand the result to the page as
 * same-origin JSON. That is the whole trick behind any "it pulls live from the
 * regulator's site" demo you'll see on a *.workers.dev URL.
 *
 * Routes:
 *   /api/twse/health          which TWSE dataset answered for each role
 *   /api/twse/index           light index of every listed company
 *   /api/twse/company/<code>  one board: holdings, pay, election dates
 *   everything else           the static files (prospects.html, wealth.html, …)
 *
 * The upstream datasets are whole-market dumps of a few MB, so they are cached
 * at the edge: prices for 15 minutes, the rest for 6 hours.
 */

const TWSE = 'https://openapi.twse.com.tw/v1';
const INDEX_URLS = [`${TWSE}/swagger.json`, `${TWSE}/openapi.json`];

const TTL = { price: 900, valuation: 900, company: 21600, holdings: 21600, remuneration: 21600, index: 21600 };

// Kept deliberately in step with ROLES in fetch_twse.py — same preferred IDs,
// same keyword fallbacks, so both paths resolve identically.
const ROLES = {
  price:        { prefer: ['/exchangeReport/STOCK_DAY_ALL'], keywords: [['每日收盤'], ['收盤行情']] },
  valuation:    { prefer: ['/exchangeReport/BWIBBU_ALL'],    keywords: [['本益比'], ['殖利率']] },
  company:      { prefer: ['/opendata/t187ap03_L'],          keywords: [['公司', '基本資料']] },
  holdings:     { prefer: ['/opendata/t187ap11_L', '/opendata/t187ap10_L'],
                  keywords: [['董事', '持股'], ['董監', '持股'], ['內部人', '持股']] },
  remuneration: { prefer: ['/opendata/t187ap28_L', '/opendata/t187ap29_L'],
                  keywords: [['董事', '酬金'], ['董監', '酬金'], ['酬金']] },
};

const ALIASES = {
  code:    ['Code', '公司代號', '證券代號', '股票代號', 'SecuritiesCompanyCode'],
  name:    ['Name', '公司名稱', '證券名稱', 'CompanyName'],
  nameEn:  ['公司英文簡稱', 'CompanyNameEn', '英文簡稱'],
  close:   ['ClosingPrice', '收盤價', 'Close'],
  capital: ['實收資本額', 'PaidInCapital', '已發行普通股數或TDR原發行股數'],
  pe:      ['PEratio', '本益比'],
  person:  ['姓名', 'Name', '職稱姓名', 'PersonName'],
  title:   ['職稱', 'Title', '身分別'],
  shares:  ['目前持股', '持股數', '本月持有股數', '所持股數', 'Shares', '選任時持股'],
  firstElected: ['初次選任日期', '初次當選日期', '初次選任日', 'FirstElectedDate'],
  elected: ['選任日期', '當選日期', '到職日期', '就任日期', 'ElectedDate', '任期起始日'],
  pay:     ['酬金總額', '領取報酬總額', '個人酬金', '酬金', 'Remuneration', '總酬金'],
  payBand: ['酬金級距', '級距', 'RemunerationRange'],
};

const pick = (row, key) => {
  for (const a of ALIASES[key]) if (row[a] !== undefined && row[a] !== '' && row[a] !== '-') return row[a];
  return null;
};
const toNum = v => {
  if (v === null || v === undefined) return null;
  const n = parseFloat(String(v).replace(/[,\s%元]/g, ''));
  return isFinite(n) ? n : null;
};

/** Taiwan filings mix ROC and Gregorian years: 1090615 is 2020-06-15. */
function parseDate(v) {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  if (!s || s === '-' || s === '0') return null;
  const parts = s.match(/\d+/g);
  if (!parts) return null;
  let y, m, d;
  if (parts.length >= 3) { [y, m, d] = parts.slice(0, 3).map(Number); }
  else {
    const g = parts[0];
    if (g.length === 8)      { y = +g.slice(0, 4); m = +g.slice(4, 6); d = +g.slice(6); }
    else if (g.length === 7) { y = +g.slice(0, 3); m = +g.slice(3, 5); d = +g.slice(5); }
    else if (g.length === 6) { y = +g.slice(0, 2); m = +g.slice(2, 4); d = +g.slice(4); }
    else if (g.length === 3 || g.length === 4) { y = +g; m = 1; d = 1; }
    else return null;
  }
  if (y < 1911) y += 1911;
  if (!(y >= 1900 && y <= 2100 && m >= 1 && m <= 12 && d >= 1 && d <= 31)) return null;
  return `${String(y).padStart(4, '0')}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

const BANDS = [
  [/未達?\s*1[,，]?000/, 500000],
  [/1[,，]?00000\s*以上/, 100000000],
];
function bandMidpoint(text) {
  if (!text) return null;
  const s = String(text);
  for (const [re, mid] of BANDS) if (re.test(s)) return mid;
  const nums = (s.match(/[\d,]{4,}/g) || []).map(toNum).filter(Boolean);
  if (nums.length >= 2) return ((nums[0] + nums[1]) / 2) * 1000;   // bands quoted in NT$ thousands
  return nums.length ? nums[0] * 1000 : null;
}

/* ---------------- cached upstream fetch ---------------- */

async function cachedJson(url, ttl, ctx) {
  const key = new Request(url, { method: 'GET' });
  const cache = caches.default;
  const hit = await cache.match(key);
  if (hit) return hit.json();

  const res = await fetch(url, {
    headers: { 'User-Agent': 'ClientPulse-SoW/1.0', Accept: 'application/json' },
    cf: { cacheTtl: ttl, cacheEverything: true },
  });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  const body = await res.text();
  const store = new Response(body, {
    headers: { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${ttl}` },
  });
  if (ctx) ctx.waitUntil(cache.put(key, store.clone()));
  return JSON.parse(body);
}

let specPromise = null;
function loadSpec(ctx) {
  // One in-flight resolution per isolate; failure is non-fatal, we fall back to
  // the preferred IDs.
  if (!specPromise) {
    specPromise = (async () => {
      for (const u of INDEX_URLS) {
        try {
          const doc = await cachedJson(u, TTL.index, ctx);
          if (doc && doc.paths) return doc;
        } catch { /* try the next */ }
      }
      return null;
    })();
  }
  return specPromise;
}

function resolveRole(spec, role) {
  const paths = (spec && spec.paths) || null;
  const { prefer, keywords } = ROLES[role];
  for (const p of prefer) if (!paths || paths[p]) return { path: p, how: paths ? 'preferred+confirmed' : 'preferred' };
  for (const keys of keywords) {
    for (const p of Object.keys(paths)) {
      const blob = JSON.stringify(paths[p]) + p;
      if (keys.every(k => blob.includes(k))) return { path: p, how: `matched ${keys.join('+')}` };
    }
  }
  return { path: prefer[0], how: 'preferred (unconfirmed)' };
}

async function role(roleName, ctx) {
  const spec = await loadSpec(ctx);
  const { path, how } = resolveRole(spec, roleName);
  try {
    const rows = await cachedJson(TWSE + path, TTL[roleName] || 3600, ctx);
    return { rows: Array.isArray(rows) ? rows : [], path, how };
  } catch (err) {
    return { rows: [], path, how, error: String(err.message || err) };
  }
}

function byCode(rows) {
  const out = new Map();
  for (const r of rows) {
    const c = String(pick(r, 'code') || '').trim();
    if (!c) continue;
    if (!out.has(c)) out.set(c, []);
    out.get(c).push(r);
  }
  return out;
}

/* ---------------- routes ---------------- */

const json = (body, status = 200, ttl = 300) => new Response(JSON.stringify(body), {
  status,
  headers: {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': `public, max-age=${ttl}`,
    'Access-Control-Allow-Origin': '*',
  },
});

async function health(ctx) {
  const out = {};
  for (const name of Object.keys(ROLES)) {
    const r = await role(name, ctx);
    out[name] = { dataset: r.path, resolvedBy: r.how, rows: r.rows.length, error: r.error || null };
  }
  const ok = out.price.rows > 0 && out.holdings.rows > 0;
  return json({
    ok,
    checkedAt: new Date().toISOString(),
    note: ok
      ? 'TWSE reachable and datasets resolved.'
      : 'One or more datasets returned no rows — the dataset may have been renumbered or renamed. Compare "dataset" against openapi.twse.com.tw.',
    roles: out,
  }, 200, 60);
}

async function buildIndex(ctx) {
  const [price, valuation, company, holdings] = await Promise.all(
    ['price', 'valuation', 'company', 'holdings'].map(r => role(r, ctx)));

  const prices = byCode(price.rows), vals = byCode(valuation.rows);
  const infos = byCode(company.rows), holds = byCode(holdings.rows);
  const universe = [...new Set([...prices.keys(), ...infos.keys()])].sort();

  const companies = universe.map(code => {
    const p = (prices.get(code) || [{}])[0];
    const v = (vals.get(code) || [{}])[0];
    const i = (infos.get(code) || [{}])[0];
    const close = toNum(pick(p, 'close'));
    const capital = toNum(pick(i, 'capital'));
    const shares = capital && capital > 1e8 ? capital / 10 : capital;   // NT$10 par value
    return {
      code,
      name: pick(i, 'name') || pick(p, 'name') || code,
      nameEn: pick(i, 'nameEn'),
      price: close,
      sharesOutstanding: shares,
      marketCap: close && shares ? close * shares : null,
      pe: toNum(pick(v, 'pe')),
      directors: (holds.get(code) || []).length,
    };
  });

  return {
    generated_at: new Date().toISOString(),
    source: 'Taiwan Stock Exchange open data, fetched live (openapi.twse.com.tw)',
    currency: 'TWD',
    live: true,
    shardDir: '/api/twse/company',
    shardSuffix: '',
    companies,
  };
}

async function boardFor(code, ctx) {
  const [holdings, remuneration, company] = await Promise.all(
    ['holdings', 'remuneration', 'company'].map(r => role(r, ctx)));

  const info = (byCode(company.rows).get(code) || [{}])[0];
  const capital = toNum(pick(info, 'capital'));

  const directors = [];
  const byName = new Map();
  for (const row of byCode(holdings.rows).get(code) || []) {
    const person = pick(row, 'person');
    if (!person) continue;
    const d = {
      name: String(person).trim(),
      title: String(pick(row, 'title') || '').trim(),
      shares: toNum(pick(row, 'shares')) || 0,
      since: parseDate(pick(row, 'firstElected')) || parseDate(pick(row, 'elected')),
      pay: null,
      payBasis: null,
    };
    directors.push(d);
    byName.set(d.name, d);
  }

  for (const row of byCode(remuneration.rows).get(code) || []) {
    const person = String(pick(row, 'person') || '').trim();
    if (!person) continue;
    let target = byName.get(person);
    if (!target) {
      target = { name: person, title: String(pick(row, 'title') || '').trim(),
                 shares: 0, since: null, pay: null, payBasis: null };
      directors.push(target);
      byName.set(person, target);
    }
    if (!target.since) target.since = parseDate(pick(row, 'firstElected')) || parseDate(pick(row, 'elected'));
    const exact = toNum(pick(row, 'pay'));
    const band = bandMidpoint(pick(row, 'payBand'));
    if (exact) { target.pay = exact; target.payBasis = 'disclosed'; }
    else if (band) { target.pay = band; target.payBasis = 'band midpoint'; }
  }

  directors.sort((a, b) => (b.shares || 0) - (a.shares || 0));
  return {
    code,
    name: pick(info, 'name') || code,
    nameEn: pick(info, 'nameEn'),
    sharesOutstanding: capital && capital > 1e8 ? capital / 10 : capital,
    directors,
  };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (!path.startsWith('/api/')) {
      // Everything else is a static file from the repo root.
      return env.ASSETS ? env.ASSETS.fetch(request) : new Response('Not found', { status: 404 });
    }

    try {
      if (path === '/api/twse/health') return await health(ctx);
      if (path === '/api/twse/index') return json(await buildIndex(ctx), 200, 900);

      const m = path.match(/^\/api\/twse\/company\/(\w+)$/);
      if (m) {
        const board = await boardFor(m[1], ctx);
        if (!board.directors.length) {
          return json({ error: 'no board on file', code: m[1] }, 404, 300);
        }
        return json(board, 200, 3600);
      }
      return json({ error: 'unknown route', path }, 404, 60);
    } catch (err) {
      // Never 500 silently — the page falls back to committed JSON, and this
      // message is what tells you why.
      return json({ error: String(err.message || err) }, 502, 30);
    }
  },
};
