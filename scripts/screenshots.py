#!/usr/bin/env python3
"""Sign in to the demo dashboard in a real browser, exercise it, and take the screenshots.

    DASHBOARD_PASSWORD=... uv run --group demo scripts/screenshots.py [--out docs/images]

For the demo stack only (scripts/run_dashboard_demo.sh): the password is the demo-only one made
for that run, read from the environment and typed into the sign-in form by this script, never an
argument and never the real admin password. The pictures are of the seeded, fictional Harborline
Supply Co. state, and the script checks what it is photographing: the "Sample data" pill is on
screen, no hostname or address is, the fonts loaded, and the browser's console shows no error (a
CSP violation is one).

It takes the overview in the default dark theme and, after using the theme toggle, in light, both
at 1440x900 with 2x scale, reduced motion and the cursor hidden (a headless browser draws none).
It also pages the recent decisions, reads the approval queue, switches the range and signs out."""

import argparse
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, ViewportSize, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

VIEWPORT: ViewportSize = {"width": 1440, "height": 900}
HOSTNAME = re.compile(
    r"127\.0\.0\.1|localhost|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|tailscale|\.ts\.net|\.local\b"
)
failures: list[str] = []


def check(condition: bool, what: str) -> None:
    print(f"{'ok   ' if condition else 'FAIL '} {what}")
    if not condition:
        failures.append(what)


def settle(page: Page) -> None:
    """Wait until the page is quiet and its fonts are in, so a picture is of the finished page."""
    page.wait_for_load_state("networkidle")
    states = page.evaluate(
        "document.fonts.ready.then(() => [...document.fonts].map((f) => f.status))"
    )
    # A face the page does not use stays "unloaded"; one that failed says "error".
    check(
        "loaded" in states and "error" not in states,
        f"fonts loaded, none failed ({states.count('loaded')} of {len(states)} faces in use)",
    )


def sign_in(page: Page, base: str, password: str) -> None:
    page.goto(f"{base}/signin")
    page.fill("#password", password)
    page.click("button[type=submit]")
    try:
        page.wait_for_url(f"{base}/")
    except PlaywrightTimeoutError:
        alert = page.locator("[role=alert]")
        shown = alert.first.inner_text() if alert.count() else "no message"
        sys.exit(f"screenshots: sign-in did not reach the overview; at {page.url} ({shown})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:4400")
    parser.add_argument("--out", type=Path, default=Path("docs/images"))
    args = parser.parse_args()
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        sys.exit(
            "screenshots: set DASHBOARD_PASSWORD (the demo stack's, from run_dashboard_demo.sh)"
        )
    args.out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    saved: list[str] = []

    def shot(page_or_element: object, name: str, **options: object) -> None:
        path = args.out / name
        page_or_element.screenshot(path=str(path), animations="disabled", **options)  # type: ignore[attr-defined]
        saved.append(name)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
            reduced_motion="reduce",
            color_scheme="dark",
            locale="en-US",
            timezone_id="UTC",
        )
        page = context.new_page()
        page.on(
            "console",
            lambda m: errors.append(f"{m.type}: {m.text[:200]}") if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:200]}"))

        # The sign-in page, then sign in as the demo admin.
        page.goto(f"{args.url}/signin")
        settle(page)
        check(
            page.locator("html").get_attribute("data-theme") == "dark", "dark is the default theme"
        )
        shot(page, "signin-dark.png")
        sign_in(page, args.url, password)
        page.wait_for_selector("#decisions")
        settle(page)

        # What is on screen is what it should be.
        check(page.locator(".pui-sample-pill").is_visible(), 'the "Sample data" pill is visible')
        body = page.inner_text("body")
        check("Harborline Supply Co. (fictional)" in body, "the company is labelled fictional")
        check(HOSTNAME.search(body) is None, "no hostname or address is on screen")
        for chart in ("volume", "latency", "tools", "layers", "auth"):
            check(page.locator(f"#{chart} svg").count() >= 1, f"the {chart} chart is drawn")
        check("4 waiting" in page.locator("#approvals").inner_text(), "the queue has 4 waiting")
        recent = page.locator("#approvals").inner_text()
        check(
            all(word in recent for word in ("Approved", "Rejected", "Expired")),
            "the queue shows an approved, a rejected and an expired request",
        )

        # The overview in dark: the first screen, and the whole page.
        shot(page, "overview-dark-fold.png")
        shot(page, "overview-dark.png", full_page=True)
        shot(page.locator("#approvals"), "approval-queue-dark.png")

        # The decisions pager: older, then newer.
        first_time = page.locator("#decisions tbody tr").first.locator("td").nth(1).inner_text()
        first_page = page.locator("#decisions tbody").inner_text()
        page.get_by_role("button", name="Older").click()
        page.wait_for_selector("#decisions >> text=page 2")
        page.wait_for_load_state("networkidle")
        older_page = page.locator("#decisions tbody").inner_text()
        check(older_page != first_page, "Older loads a different page of calls")
        check("page 2" in page.locator("#decisions").inner_text(), "the pager says page 2")
        shot(page.locator("#decisions"), "decisions-page-2-dark.png")
        page.get_by_role("button", name="Newer").click()
        page.wait_for_function("!document.querySelector('#decisions').innerText.includes('page 2')")
        check(
            page.locator("#decisions tbody tr").first.locator("td").nth(1).inner_text()
            == first_time,
            "Newer returns to the first page",
        )

        # The range control: a week shows the daily rhythm and the episodes.
        page.get_by_role("link", name="7d").click()
        page.wait_for_url(f"{args.url}/?range=7d")
        page.wait_for_selector("#volume svg")
        settle(page)
        check(
            page.locator("nav[aria-label='Time range'] a[aria-current='true']").inner_text()
            == "7d",
            "7d is the selected range",
        )
        shot(page.locator(".chart-section"), "charts-7d-dark.png")
        page.get_by_role("link", name="24h").click()
        page.wait_for_url(f"{args.url}/?range=24h")
        page.wait_for_selector("#volume svg")
        settle(page)
        shot(page.locator(".chart-section"), "charts-24h-dark.png")

        # The theme toggle: light now, and it stays light after a reload.
        page.get_by_role("button", name="Switch to light theme").click()
        check(
            page.locator("html").get_attribute("data-theme") == "light",
            "the toggle switches to light",
        )
        page.reload()
        settle(page)
        check(
            page.locator("html").get_attribute("data-theme") == "light",
            "light is remembered after a reload",
        )
        shot(page, "overview-light-fold.png")
        shot(page, "overview-light.png", full_page=True)
        shot(page.locator(".chart-section"), "charts-24h-light.png")
        page.goto(f"{args.url}/?range=24h")
        page.get_by_role("button", name="Switch to dark theme").click()
        check(
            page.locator("html").get_attribute("data-theme") == "dark",
            "the toggle switches back to dark",
        )

        # Sign out.
        page.get_by_role("button", name="Sign out").click()
        page.wait_for_url(f"{args.url}/signin")
        check(page.locator("#password").is_visible(), "signing out leaves the sign-in page")
        response = page.request.get(f"{args.url}/api/live")
        check(response.status == 401, "the data endpoint is shut after sign-out")

        check(
            errors == [],
            f"the browser console shows no error{': ' + '; '.join(errors[:3]) if errors else ''}",
        )
        browser.close()

    print("saved:", ", ".join(saved))
    if failures:
        sys.exit(f"FAIL  {len(failures)} check(s) failed: {'; '.join(failures)}")
    print("PASS  the demo dashboard")


if __name__ == "__main__":
    main()
