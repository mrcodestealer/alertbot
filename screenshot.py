"""Capture a screenshot of an alert's detail modal (the 👁 "查看详情" window).

Uses Playwright (sync API). This runs inside the watcher thread, which is a
plain thread with no asyncio loop, so the sync API is safe here.

The whole thing is best-effort: any failure returns None and the caller simply
sends the text card without an image.
"""
from __future__ import annotations

import logging

from config import CONFIG

log = logging.getLogger("alertbot.screenshot")

# The detail modal panel: a white rounded card with a big shadow, rendered
# inside a fixed full-screen overlay. Confirmed against the live DOM.
_MODAL_SELECTOR = "div.fixed.inset-0.z-50 div.rounded-lg.shadow-xl"


def capture_alert_detail(alert_id: int | str) -> str | None:
    if not CONFIG.enable_screenshot:
        return None
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed; skipping screenshot. Run: pip install playwright && playwright install chromium")
        return None

    try:
        CONFIG.screenshot_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(CONFIG.screenshot_dir / f"alert_{alert_id}.png")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 960},
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.set_default_timeout(20000)
            try:
                _login(page)
                _open_detail(page, alert_id)
                panel = page.locator(_MODAL_SELECTOR).first
                panel.wait_for(state="visible", timeout=15000)
                page.wait_for_timeout(900)  # let modal content + fonts settle
                _fit_viewport(page)         # make 90vh big enough for the content
                _expand_modal(page)         # then drop any remaining height caps
                page.wait_for_timeout(400)  # let the relayout settle
                panel.screenshot(path=out_path)
                log.info("Captured detail screenshot for alert %s -> %s", alert_id, out_path)
                return out_path
            except PWTimeout:
                log.warning("Timed out capturing detail modal for alert %s", alert_id)
                return None
            finally:
                context.close()
                browser.close()
    except Exception:  # pragma: no cover - defensive; never break the pipeline
        log.exception("Screenshot capture failed for alert %s", alert_id)
        return None


_EXPAND_JS = """
() => {
  const panel = document.querySelector('div.fixed.inset-0.z-50 div.rounded-lg.shadow-xl');
  if (!panel) return false;
  // The overlay is fixed to the viewport, which caps how tall the panel can be.
  const overlay = panel.closest('div.fixed.inset-0.z-50');
  if (overlay) {
    overlay.style.position = 'absolute';
    overlay.style.height = 'auto';
    overlay.style.minHeight = '0';
    overlay.style.alignItems = 'flex-start';
  }
  // Let the panel itself grow past max-h-[90vh].
  panel.style.maxHeight = 'none';
  panel.style.height = 'auto';
  panel.style.overflow = 'visible';
  // Grow every clipped descendant to its full content height. Setting an
  // explicit height (rather than just overflow:visible) is what matters: with
  // overflow:visible alone the text spills OUTSIDE the panel's box and an
  // element screenshot, which captures only that box, still cuts it off.
  // Repeat a few passes because growing a child reflows its parents.
  for (let pass = 0; pass < 5; pass++) {
    let changed = false;
    panel.querySelectorAll('*').forEach((el) => {
      if (el.scrollHeight > el.clientHeight + 2) {
        el.style.maxHeight = 'none';
        el.style.height = el.scrollHeight + 'px';
        el.style.overflowY = 'visible';
        changed = true;
      }
    });
    if (!changed) break;
  }
  document.body.style.overflow = 'visible';
  return Math.round(panel.getBoundingClientRect().height);
}
"""


_MEASURE_JS = """
() => {
  const panel = document.querySelector('div.fixed.inset-0.z-50 div.rounded-lg.shadow-xl');
  if (!panel) return 0;
  // The panel is capped at max-h-[90vh]; its CONTENT can be much taller.
  let needed = panel.scrollHeight;
  panel.querySelectorAll('*').forEach((el) => {
    needed = Math.max(needed, el.scrollHeight);
  });
  return Math.ceil(needed);
}
"""


def _fit_viewport(page) -> None:
    """Grow the viewport until the modal's 90vh cap no longer clips the content.

    The panel is ``max-h-[90vh]``, so the only reliable way to show a long
    description is to make the viewport tall enough that 90vh exceeds it.
    """
    try:
        needed = int(page.evaluate(_MEASURE_JS) or 0)
    except Exception:
        return
    if needed <= 0:
        return
    # +header/padding, then undo the 90vh factor, and clamp to something sane.
    target = int(min(max((needed + 260) / 0.9, 960), 6000))
    current = (page.viewport_size or {}).get("height", 0)
    if target > current:
        log.debug("Growing viewport %s -> %spx for %spx of content", current, target, needed)
        page.set_viewport_size({"width": 1280, "height": target})
        page.wait_for_timeout(400)


def _expand_modal(page) -> None:
    """Remove the modal's height cap and inner scrollbars.

    The detail panel is ``max-h-[90vh] overflow-hidden`` with a scrolling body,
    so a long description (e.g. a service table) is cut off at the viewport edge.
    Un-clipping it first means the element screenshot captures the whole thing.
    """
    try:
        height = page.evaluate(_EXPAND_JS)
        log.debug("Expanded detail modal to %spx", height)
    except Exception:
        log.warning("Could not expand the detail modal; screenshot may be clipped", exc_info=True)


def _login(page) -> None:
    page.goto(f"{CONFIG.monitor_base_url}/", wait_until="domcontentloaded")
    # A fresh browser context is always unauthenticated. The login form is
    # rendered client-side (React SPA), so it may not exist at domcontentloaded —
    # wait for it to mount rather than inferring auth state from an instant count().
    page.wait_for_selector('input[type="password"]', state="visible", timeout=20000)
    page.fill('input[placeholder="请输入用户名"]', CONFIG.monitor_username)
    page.fill('input[type="password"]', CONFIG.monitor_password)
    page.click('button[type="submit"]')
    # Login is an XHR + client-side redirect; the password field detaches on success.
    page.wait_for_selector('input[type="password"]', state="detached", timeout=20000)


def _open_detail(page, alert_id: int | str) -> None:
    sev = CONFIG.severity_filter
    url = f"{CONFIG.monitor_base_url}/alerts?page=1&page_size={CONFIG.monitor_page_size}"
    if sev:
        url += f"&severity={sev}"
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector("tbody tr", timeout=15000)
    # Locate the table row whose ID cell exactly equals this alert id, then click
    # its "查看详情" (eye) button — <button title="查看详情"> with an SVG icon.
    row = page.locator(f'tbody tr:has(td:text-is("{alert_id}"))').first
    row.wait_for(state="visible", timeout=15000)
    row.locator('button[title="查看详情"]').first.click()
