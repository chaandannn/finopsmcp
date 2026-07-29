#!/usr/bin/env python3
"""Render the Product Hunt gallery and deck PNGs from their HTML sources.

    .venv/bin/python scripts/render_ph_assets.py            # render everything
    .venv/bin/python scripts/render_ph_assets.py --check    # fail if any PNG is stale

This exists because it did not. The HTML and the PNGs drifted for weeks with no
way to regenerate: the slides still used Instrument Sans and Bricolage Grotesque
after the site moved to Geist on 2026-07-02, and the CTA still taught
`finops welcome`, which opens the cloud connect wizard, months after the launch
copy moved to the AI surface. Nobody noticed because re-rendering meant doing it
by hand.

--check compares each PNG's mtime against its HTML and the shared stylesheet, so
a source edit without a re-render is a visible failure rather than a slide that
quietly contradicts the landing page.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PH = ROOT / "docs" / "video" / "producthunt"

# (directory, css filename, viewport, device scale). The gallery is authored at
# 1270x760 and shipped at 2x; the deck is authored at its final 2400x1350.
TARGETS = [
    (PH / "gallery", "gallery.css", (1270, 760), 2),
    (PH / "deck", "deck.css", (2400, 1350), 1),
]


def _pairs():
    for d, css, viewport, scale in TARGETS:
        if not d.is_dir():
            continue
        for html in sorted(d.glob("*.html")):
            yield html, html.with_suffix(".png"), d / css, viewport, scale


def check() -> int:
    if not list(_pairs()):
        # docs/video/ is gitignored on purpose (binary marketing assets stay
        # local), so a fresh clone has nothing to check. Say that, rather than
        # printing "all 0 are current", which reads as a pass.
        print(f"No Product Hunt assets found under {PH.relative_to(ROOT)}.")
        print("They are gitignored and live only on the machine that renders them.")
        return 0
    stale = []
    for html, png, css, _, _ in _pairs():
        if not png.exists():
            stale.append(f"{png.relative_to(ROOT)} missing")
        elif png.stat().st_mtime < max(html.stat().st_mtime, css.stat().st_mtime):
            stale.append(f"{png.relative_to(ROOT)} older than its source")
    if stale:
        print("STALE Product Hunt assets:")
        for s in stale:
            print(f"  {s}")
        print("\nRe-render:  .venv/bin/python scripts/render_ph_assets.py")
        return 1
    print(f"All {len(list(_pairs()))} Product Hunt PNGs are current.")
    return 0


def render() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright not installed:  .venv/bin/pip install playwright "
                 "&& .venv/bin/playwright install chromium")

    n = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for html, png, _css, (w, h), scale in _pairs():
            page = browser.new_page(viewport={"width": w, "height": h},
                                    device_scale_factor=scale)
            page.goto(html.as_uri())
            # Webfonts are the whole point of this re-render; screenshotting
            # before they load silently ships the system-font fallback, which
            # looks close enough in a thumbnail to pass review.
            page.wait_for_timeout(1200)
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(300)
            page.screenshot(path=str(png))
            page.close()
            print(f"  {png.relative_to(ROOT)}  {w * scale}x{h * scale}")
            n += 1
        browser.close()
    print(f"\nRendered {n} assets.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if any PNG is older than its source")
    sys.exit(check() if ap.parse_args().check else render())
