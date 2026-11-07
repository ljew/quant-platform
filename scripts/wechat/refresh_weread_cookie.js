#!/usr/bin/env node
/**
 * 微信读书 Cookie 自动续期（每日 06:00，launchd 调用；必须早于 06:20 的采集）
 *
 * 为什么需要：wr_skey 有效期约 36 小时（2026-11-02 19:10 扫码 → 11-04 早报
 * -2012 登录超时）。采集脚本只在过期后报错，而看门狗要等「34 小时无新文章」
 * 才 WARN，中间会漏掉一到两天的文章。
 *
 * 做法：把 data/wechat/weread_cookie.json 里的 cookie 注入一个一次性浏览器
 * 实例，打开 weread.qq.com 让前端把 skey 续期，再把 cookie 原子写回同一文件。
 * 只要文件里的 wr_skey 还没过期，这一步完全无感，不需要重新扫码。
 *
 * 历史（2026-11-07 改）：原先用持久化 profile（data/wechat/weread-profile）存
 * 登录态。但 Playwright 下载的 Chrome for Testing 是 ad-hoc 签名（TeamIdentifier
 * 为空），每次读 macOS 钥匙串里的 Chrome Safe Storage 密钥，系统都会弹窗索要
 * Mac 登录密码；而本脚本每天 06:00 被 launchd 拉起，等于每天弹一次。
 * 故改为「cookie JSON 为唯一真相源 + 临时浏览器实例」，全程不碰钥匙串。
 * 旧的 weread-profile 目录已不再使用，保留仅作应急回滚。
 *
 * 退出码：0 成功；2 cookie 已失效（必须人工扫码）；1 其他异常。
 */
const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const EXE = process.env.WEREAD_CHROME ||
  "/Users/happyljew/Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing";
const ROOT = path.resolve(__dirname, "../..");           // quant-platform/
const OUT = process.env.WEREAD_COOKIE ||
  path.join(ROOT, "data/wechat/weread_cookie.json");
const STATE = path.join(ROOT, "data/wechat/state/cookie_refresh.json");

function save(o) {
  fs.mkdirSync(path.dirname(STATE), { recursive: true });
  fs.writeFileSync(STATE, JSON.stringify({ ...o, at: new Date().toISOString() }, null, 1));
}

// addCookies 只吃这几个字段；sameSite 必须是 Strict / Lax / None 之一
function sanitize(list) {
  const out = [];
  for (const c of list) {
    if (!c || !c.name || !c.domain) continue;
    const s = String(c.sameSite || "");
    out.push({
      name: c.name,
      value: String(c.value ?? ""),
      domain: c.domain,
      path: c.path || "/",
      expires: typeof c.expires === "number" ? c.expires : -1,
      httpOnly: !!c.httpOnly,
      secure: !!c.secure,
      sameSite: s === "Strict" || s === "None" ? s : "Lax",
    });
  }
  return out;
}

(async () => {
  let seeds;
  try {
    seeds = JSON.parse(fs.readFileSync(OUT, "utf8"));
  } catch (e) {
    save({ ok: false, reason: "读不到 weread_cookie.json，需要人工扫码" });
    console.error("NO_COOKIE_FILE:", String(e).slice(0, 200));
    process.exitCode = 2;
    return;
  }

  const cookies = sanitize(seeds);
  if (!cookies.some((c) => c.name === "wr_skey")) {
    save({ ok: false, reason: "cookie 文件里没有 wr_skey，需要人工扫码" });
    console.error("NO_SKEY_IN_FILE");
    process.exitCode = 2;
    return;
  }

  const browser = await chromium.launch({
    executablePath: EXE,
    headless: true,
    // 关键：不读 macOS 登录钥匙串（Chrome for Testing 无官方签名，一读就弹窗）
    args: ["--use-mock-keychain", "--password-store=basic"],
  });

  try {
    const ctx = await browser.newContext({
      viewport: { width: 1280, height: 900 },
      userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      locale: "zh-CN",
    });
    await ctx.addCookies(cookies);

    const p = await ctx.newPage();
    await p.goto("https://weread.qq.com/", { waitUntil: "domcontentloaded", timeout: 60000 });
    await p.waitForTimeout(4000);        // 等前端把 wr_skey 续期写回

    const ck = await ctx.cookies();
    const vid = ck.find((c) => c.name === "wr_vid");
    const skey = ck.find((c) => c.name === "wr_skey");
    if (!vid || !skey) {
      save({ ok: false, reason: "注入的 cookie 已失效，需要人工扫码" });
      console.error("NO_LOGIN: 注入后仍无 wr_vid/wr_skey");
      process.exitCode = 2;
      return;
    }

    // 原子写：先写 .tmp 再 rename，避免正在跑的采集读到半截 JSON
    const tmp = OUT + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(ck, null, 1));
    fs.renameSync(tmp, OUT);

    save({
      ok: true,
      vid: vid.value,
      count: ck.length,
      injected: cookies.length,
      skey_expires: skey.expires ? new Date(skey.expires * 1000).toISOString() : null,
    });
    console.log(`OK vid=${vid.value} cookies=${ck.length} injected=${cookies.length}`);
  } finally {
    await browser.close();
  }
})().catch((e) => {
  save({ ok: false, reason: String(e).slice(0, 200) });
  console.error("ERROR:", String(e).slice(0, 200));
  process.exitCode = 1;
});
