"""Shared polite fetcher for all collectors.

- ~1 request/sec with jitter (global across the process)
- retries with exponential backoff on network errors / 429 / 5xx
- realistic browser User-Agent
- optional Playwright fallback when the static HTML lacks the data we need
  (e.g. the page renders store lists client-side)

NOTE: this module is named ``http`` per the project layout, which shadows the
stdlib ``http`` package if ``collectors/`` itself is on sys.path. Every
collector script therefore strips its own directory from sys.path and imports
this as ``collectors.http`` (see the bootstrap block at the top of each script).
"""
from __future__ import annotations

import atexit
import logging
import os
import random
import time
from pathlib import Path
from typing import Callable, Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger("fetch")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"  # gitignored; only written when SAVE_RAW=1


class RetryableHTTPError(Exception):
    pass


class Fetcher:
    def __init__(
        self,
        min_interval: float = 1.0,
        jitter: float = 0.5,
        timeout: float = 30.0,
        use_playwright: bool = True,
    ):
        self.min_interval = min_interval
        self.jitter = jitter
        self.timeout = timeout
        self.use_playwright = use_playwright and os.environ.get("NO_PLAYWRIGHT") != "1"
        self.save_raw = os.environ.get("SAVE_RAW") == "1"
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._last = 0.0
        self._pw = None
        self._browser = None
        self.stats = {"requests": 0, "playwright": 0, "errors": 0}
        atexit.register(self.close)

    # ---- rate limiting -------------------------------------------------
    def _throttle(self) -> None:
        wait = self.min_interval + random.uniform(0, self.jitter) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    # ---- plain HTTP ----------------------------------------------------
    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, RetryableHTTPError)),
        wait=wait_exponential_jitter(initial=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get(self, url: str) -> requests.Response:
        self._throttle()
        self.stats["requests"] += 1
        r = self.session.get(url, timeout=self.timeout)
        if r.status_code == 429 or r.status_code >= 500:
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 120))
            raise RetryableHTTPError(f"{r.status_code} for {url}")
        return r

    # ---- browser fallback ---------------------------------------------
    def _render(self, url: str) -> str:
        if self._browser is None:
            from playwright.sync_api import sync_playwright  # lazy import

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        self._throttle()
        self.stats["playwright"] += 1
        page = self._browser.new_page(user_agent=USER_AGENT)
        try:
            page.goto(url, wait_until="networkidle", timeout=int(self.timeout * 2000))
            return page.content()
        finally:
            page.close()

    # ---- public --------------------------------------------------------
    def get(self, url: str, has_data: Optional[Callable[[str], bool]] = None) -> Optional[str]:
        """Return HTML for ``url`` or None on a hard failure (404, retries exhausted).

        If ``has_data`` is given and returns False for the static HTML, the page
        is re-fetched with headless Chromium.
        """
        html: Optional[str] = None
        try:
            r = self._get(url)
            if r.status_code == 404:
                log.warning("404 %s", url)
                return None
            if r.status_code >= 400:
                log.warning("HTTP %s %s", r.status_code, url)
            else:
                html = r.text
        except Exception as e:  # retries exhausted
            self.stats["errors"] += 1
            log.warning("GET failed %s: %s", url, e)

        if self.use_playwright and (html is None or (has_data and not has_data(html))):
            try:
                html = self._render(url)
            except Exception as e:
                self.stats["errors"] += 1
                log.warning("Playwright failed %s: %s", url, e)

        if html is not None and self.save_raw:
            self._save_raw(url, html)
        return html

    def _save_raw(self, url: str, html: str) -> None:
        name = url.split("://", 1)[-1].strip("/").replace("/", "__") or "index"
        path = RAW_DIR / f"{name[:180]}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._browser = self._pw = None
