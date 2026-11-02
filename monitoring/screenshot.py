"""Save a PNG of the Grafana dashboard after the monitoring tests, for the write-up.

usage: python monitoring/screenshot.py OUT.png
"""

import sys

from playwright.sync_api import sync_playwright

URL = "http://localhost:3000/d/fabric/leaf-spine-fabric?orgId=1&from=now-3m&to=now&kiosk"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1100})
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(5000)  # let every panel finish querying
    page.screenshot(path=sys.argv[1], full_page=True)
    browser.close()
print("saved", sys.argv[1])
