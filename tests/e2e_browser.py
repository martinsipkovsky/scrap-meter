"""Browser part of the end-to-end check: every page in Chromium, in dark and
light mode and at phone width, failing on JavaScript errors, failed requests
and text too faint to read. Not a pytest test; run it in the Playwright image
on the stack's network after e2e_check.py (see docs/TEST_REPORT.md):

    python tests/e2e_browser.py --base http://web:8000 --user admin --password ... --out tests/e2e-browser.json
"""
from __future__ import annotations

import argparse
import json

from playwright.sync_api import sync_playwright

PAGES = ["/", "/stations", "/station/{sid}", "/devices", "/map", "/jobs", "/data", "/scrap", "/chat", "/notifications",
         "/users", "/database", "/rawdb", "/system", "/settings", "/account", "/changelog", "/tutorial", "/tutorial/oee",
         "/tutorial/docs/user-guide"]

# the colour contrast of the page's text against its background (WCAG); a
# rough check over the visible text elements, flagging the worst ones
CONTRAST_JS = """
() => {
  const lum = (c) => { const m = c.match(/[\\d.]+/g); if (!m) return null;
    const [r, g, b] = m.slice(0, 3).map(v => { v = v / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return { L: 0.2126 * r + 0.7152 * g + 0.0722 * b, a: m.length > 3 ? +m[3] : 1 }; };
  const bgOf = (el) => { while (el) { const c = getComputedStyle(el).backgroundColor; const l = lum(c);
    if (l && l.a > 0.5) return l.L; el = el.parentElement; } return lum(getComputedStyle(document.body).backgroundColor).L; };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (!el.childNodes.length || ![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    const r = el.getBoundingClientRect(); if (!r.width || !r.height) continue;
    const st = getComputedStyle(el); if (st.visibility === 'hidden' || +st.opacity < 0.6) continue;
    if (el.closest('.not-producing')) continue;  // grayed on purpose
    if (el.closest('.heat-cell')) continue;  // white marks with a dark outline on any red
    const fg = lum(st.color); if (!fg) continue;
    const bg = bgOf(el); const hi = Math.max(fg.L, bg), lo = Math.min(fg.L, bg);
    const ratio = (hi + 0.05) / (lo + 0.05);
    if (ratio < 3) out.push({ text: el.textContent.trim().slice(0, 40), ratio: +ratio.toFixed(2) });
  }
  return out.slice(0, 8);
}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://web:8000")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--sid", default="1")
    ap.add_argument("--out")
    a = ap.parse_args()
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for mode, viewport, scheme in (("dark", (1280, 800), "dark"), ("light", (1280, 800), "light"),
                                       ("phone", (390, 844), "dark")):
            ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]}, color_scheme=scheme)
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"JS error: {e}"))
            page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
            page.on("requestfailed", lambda r: errors.append(f"request failed: {r.url} {r.failure}"))
            page.on("response", lambda r: errors.append(f"HTTP {r.status}: {r.url}") if r.status >= 500 else None)
            page.goto(a.base + "/login")
            page.fill("input[name=username]", a.user)
            page.fill("input[name=password]", a.password)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            # light: the user's own setting, system mode follows the browser's (light) scheme
            ctx.request.put(a.base + '/api/ui-settings/mine', data={'mode': 'system'} if mode == 'light' else {})
            for path in PAGES:
                path = path.format(sid=a.sid)
                errors.clear()
                page.goto(a.base + path)
                page.wait_for_timeout(2500)
                overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
                faint = page.evaluate(CONTRAST_JS) if mode != "phone" else []
                problems = list(errors)
                if mode == "phone" and overflow > 2 and not path.startswith("/rawdb"):
                    problems.append(f"page is {overflow}px wider than the phone screen")
                if faint:
                    problems.append("low contrast: " + json.dumps(faint))
                results.append({"mode": mode, "page": path, "ok": not problems, "problems": problems})
                print(("PASS " if not problems else "FAIL ") + f"[{mode}] {path}" +
                      (" — " + "; ".join(problems)[:500] if problems else ""), flush=True)
            ctx.request.put(a.base + '/api/ui-settings/mine', data={})
            ctx.close()
        browser.close()
    if a.out:
        with open(a.out, "w") as f:
            json.dump(results, f, indent=1)
    bad = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed")


if __name__ == "__main__":
    main()
