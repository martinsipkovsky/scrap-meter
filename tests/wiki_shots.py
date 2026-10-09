"""Take the tutorial screenshots (docs/wiki/images) from a demo stack
(perf_seed.py + wiki_demo.py). Not a test; run it in the Playwright image on
the stack's network, e.g.

    docker run --rm --network cmwiki_default -v "$PWD:/repo" -w /repo \\
      -e BASE=http://web:8000 -e USER=admin -e PASSWORD=... \\
      mcr.microsoft.com/playwright/python:v1.48.0-jammy \\
      sh -c "pip install -q playwright==1.48.0 && python tests/wiki_shots.py"
"""
from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://web:8000")
OUT = Path(os.environ.get("OUT", "docs/wiki/images"))
W, H = 1280, 800
# ONLY=notifications,settings retakes just those pictures
ONLY = {n for n in os.environ.get("ONLY", "").split(",") if n}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        page.goto(BASE + "/login")
        page.fill("input[name=username]", os.environ["USER"])
        page.fill("input[name=password]", os.environ["PASSWORD"])
        page.click("button[type=submit]")
        page.wait_for_url(BASE + "/")
        page.mouse.click(600, 30)  # a click lets the dashboard play sound (no "click the page" hint)

        def shot(name: str, path: str, wait: float = 2500, full: bool = False, clip=None, before=None) -> None:
            if ONLY and name not in ONLY:
                return
            page.goto(BASE + path)
            page.wait_for_timeout(wait)
            page.mouse.click(700, 30)  # an empty spot: browsers allow sound after a click
            if before:
                before()
                page.wait_for_timeout(800)
            page.screenshot(path=str(OUT / f"{name}.png"), full_page=full, clip=clip)
            print("saved", name)

        shot("dashboard", "/")
        # the OEE bar pinned to the bottom of the dashboard
        shot("oee-bar", "/", clip={"x": 220, "y": H - 230, "width": W - 220, "height": 230})
        # the second row of blocks: amber W03 and red E02 in the pills
        shot("problem-code", "/", clip={"x": 220, "y": 420, "width": W - 220, "height": 210})
        shot("station", "/station/1", wait=3500)
        shot("station-chart", "/station/1", wait=3500,
             before=lambda: page.locator("#chart").scroll_into_view_if_needed())
        shot("stations", "/stations")
        shot("devices", "/devices")
        shot("map", "/map", wait=3500)
        shot("jobs", "/jobs")
        shot("data-log", "/data")
        shot("scrap-statistics", "/scrap", wait=4000)
        shot("scrap-statistics-tables", "/scrap", wait=4000,
             before=lambda: page.mouse.wheel(0, 900))
        shot("chat-room", "/chat", wait=3500)
        shot("notifications", "/notifications", wait=3500)
        shot("notifications-commands", "/notifications", wait=3500,
             before=lambda: page.locator("h2:has-text('Chat commands')").first.scroll_into_view_if_needed())
        shot("settings", "/settings")
        shot("settings-developer", "/settings",
             before=lambda: page.locator("text=Developer options").first.scroll_into_view_if_needed())
        shot("users", "/users")
        shot("database", "/database", wait=3500)
        shot("raw-data", "/rawdb", wait=3000)
        shot("system", "/system", wait=3500)
        shot("changelog", "/changelog")
        browser.close()


if __name__ == "__main__":
    main()
