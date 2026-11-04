#!/usr/bin/env node
/**
 * 微信读书 Cookie 自动续期（每日 07:40，launchd 调用）
 *
 * 为什么需要：wr_skey 有效期约 36 小时（2026-11-02 19:10 扫码 → 11-04 早报
 * -2012 登录超时）。采集脚本只在过期后报错，而看门狗要等「34 小时无新文章」
 * 才 WARN，中间会漏掉一到两天的文章。
 *
 * 做法：用持久化 profile 打开一次 weread.qq.com（前端会把 skey 续期写回），
 * 再把 cookie 原子写回 data/wechat/weread_cookie.json。只要 profile 里登录态
 * 还在，这一步完全无感，不需要重新扫码。
 *
 * 退出码：0 成功；2 profile 登录态已失效（必须人工扫码）；1 其他异常。
 */
const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const EXE = process.env.WEREAD_CHROME ||
  "/Users/happyljew/Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing";
// 持久目录（不是 /tmp：macOS 会清理长期未访问的 /tmp 文件，登录态会丢）
const PROFILE = process.env.WEREAD_PROFILE ||
  "/Users/happyljew/quant-platform/data/wechat/weread-profile";
const ROOT = path.resolve(__dirname, "../..");           // quant-platform/
const OUT = process.env.WEREAD_COOKIE ||
  path.join(ROOT, "data/wechat/weread_cookie.json");
const STATE = path.join(ROOT, "data/wechat/state/cookie_refresh.json");

function save(o) {
  fs.mkdirSync(path.dirname(STATE), { recursive: true });
  fs.writeFileSync(STATE, JSON.stringify({ ...o, at: new Date().toISOString() }, null, 1));
}

(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    executablePath: EXE,
    headless: true,
    viewport: { width: 1280, height: 900 },
    userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    locale: "zh-CN",
  });
  try {
    const p = ctx.pages()[0] || await ctx.newPage();
    await p.goto("https://weread.qq.com/", { waitUntil: "domcontentloaded", timeout: 60000 });
    await p.waitForTimeout(4000);        // 等前端把 wr_skey 续期写回

    const ck = await ctx.cookies();
    const vid = ck.find((c) => c.name === "wr_vid");
    const skey = ck.find((c) => c.name === "wr_skey");
    if (!vid || !skey) {
      save({ ok: false, reason: "profile 登录态已失效，需要人工扫码" });
      console.error("NO_LOGIN: profile 里没有 wr_vid/wr_skey");
      process.exitCode = 2;
      return;
    }

    // 原子写：先写 .tmp 再 rename，避免正在跑的采集读到半截 JSON
    const tmp = OUT + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(ck, null, 1));
    fs.renameSync(tmp, OUT);

    save({
      ok: true, vid: vid.value, count: ck.length,
      skey_expires: skey.expires ? new Date(skey.expires * 1000).toISOString() : null,
    });
    console.log(`OK vid=${vid.value} cookies=${ck.length}`);
  } finally {
    await ctx.close();
  }
})().catch((e) => {
  save({ ok: false, reason: String(e).slice(0, 200) });
  console.error("ERROR:", String(e).slice(0, 200));
  process.exitCode = 1;
});
