import { chromium } from "@playwright/test";
const BASE="http://localhost:5173";
const b = await chromium.launch({ headless: true });
const ctx = await b.newContext({ viewport:{width:1440,height:900}});
const p = await ctx.newPage();
const errs=[];
p.on("pageerror",e=>errs.push("pageerr:"+e.message));
await p.goto(BASE,{waitUntil:"domcontentloaded"});
await p.waitForTimeout(2000);
const sel = async (s)=>({s, n: await p.locator(s).count()});
const checks = {};
for (const s of ['[data-testid="grace2-auth-gate"]','[data-testid="grace2-auth-gate-anonymous"]','[data-testid="grace2-app-shell"]','[data-testid="grace2-cases-new"]','[data-testid="chat-input"]']) {
  checks[s] = (await p.locator(s).count());
}
// click anon if present
const anon = p.locator('[data-testid="grace2-auth-gate-anonymous"]');
if (await anon.count()>0){ await anon.click(); await p.waitForTimeout(1500);}
const checks2 = {};
for (const s of ['[data-testid="grace2-app-shell"]','[data-testid="grace2-cases-new"]','[data-testid="grace2-case-row"]']) {
  checks2[s] = (await p.locator(s).count());
}
const mapGetter = await p.evaluate(()=> typeof window.__grace2GetMap);
console.log(JSON.stringify({preAuth:checks, postAuth:checks2, mapGetter, errs:errs.slice(0,8)},null,2));
await b.close();
