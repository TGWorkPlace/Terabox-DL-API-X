"""
Merged TeraBox API
==================
- Source 1 (watch_link)   : flowvideoplayer.com  → fast_stream_url
- Source 2 (download_link): tboxdownloader.in    → direct download URL via Playwright
Both run concurrently; result is a single unified JSON per video item.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright, Browser, BrowserContext

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("terabox-merged-api")

# ─── Constants ────────────────────────────────────────────────────────────────
FVP_BASE        = "https://flowvideoplayer.com"
FVP_ENDPOINT    = f"{FVP_BASE}/telegram/bot/search/video"
TBOX_BASE       = "https://tboxdownloader.in/"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-M325F) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)

MAX_RETRIES      = 3
RETRY_DELAY      = 1.5   # seconds
HTTP_TIMEOUT     = 20    # seconds
PLAYWRIGHT_TIMEOUT_MS = 30_000
TOTAL_TIMEOUT    = 90    # seconds for the whole merged request

# ─── Browser singleton ───────────────────────────────────────────────────────
_browser: Optional[Browser] = None
_playwright_instance        = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser, _playwright_instance
    _playwright_instance = await async_playwright().start()
    _browser = await _playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
            "--single-process",
        ],
    )
    log.info("Headless browser started")
    yield
    await _browser.close()
    await _playwright_instance.stop()
    log.info("Headless browser stopped")


# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="TeraBox Unified API",
    description="Returns watch_link (stream) + download_link (direct) for TeraBox URLs",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 1 — flowvideoplayer.com  (watch_link / stream URL)
# ═══════════════════════════════════════════════════════════════════════════════

async def _fvp_csrf(client: httpx.AsyncClient) -> Optional[str]:
    resp = await client.get(
        FVP_BASE,
        headers={"User-Agent": MOBILE_UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
    if not m:
        log.warning("FVP: CSRF token not found")
        return None
    return m.group(1)


async def fetch_fvp(terabox_url: str) -> tuple[Optional[list], Optional[str]]:
    """
    Returns (results, error).
    results is a list of dicts with keys:
        file_name, file_size, watch_link, thumbnail
    """
    last_error = "Unknown error"

    for attempt in range(1, MAX_RETRIES + 1):
        log.info("FVP attempt %d/%d", attempt, MAX_RETRIES)
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                csrf = await _fvp_csrf(client)
                if not csrf:
                    last_error = "Could not extract CSRF token"
                    raise ValueError(last_error)

                resp = await client.post(
                    FVP_ENDPOINT,
                    json={"url": terabox_url},
                    headers={
                        "User-Agent": MOBILE_UA,
                        "Content-Type": "application/json",
                        "X-CSRF-TOKEN": csrf,
                        "Referer": f"{FVP_BASE}/",
                        "Origin": FVP_BASE,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                    timeout=HTTP_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()

                if not data.get("status"):
                    last_error = data.get("message") or "FVP returned status=false"
                    log.warning("FVP API error: %s", last_error)
                    return None, last_error   # no point retrying a known-bad URL

                raw = data.get("response") or []
                if not raw:
                    return None, "FVP: no videos found"

                results = [
                    {
                        "file_name":  item.get("file_name"),
                        "file_size":  item.get("file_size"),
                        "watch_link": item.get("fast_stream_url"),   # renamed
                        "thumbnail":  item.get("thumbnail"),
                    }
                    for item in raw
                ]
                log.info("FVP: %d result(s)", len(results))
                return results, None

        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = f"FVP network error: {exc}"
            log.warning("FVP attempt %d failed: %s", attempt, exc)
        except httpx.HTTPStatusError as exc:
            last_error = f"FVP HTTP {exc.response.status_code}"
            log.warning("FVP attempt %d HTTP error: %s", attempt, last_error)
        except ValueError:
            break  # CSRF failure — don't keep retrying
        except Exception as exc:
            last_error = f"FVP unexpected: {exc}"
            log.exception("FVP attempt %d unexpected error", attempt)

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY * attempt)

    return None, last_error


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 2 — tboxdownloader.in  (download_link / direct URL via Playwright)
# ═══════════════════════════════════════════════════════════════════════════════

_DOWNLOAD_KEY_CANDIDATES = [
    "download_link", "dlink", "url", "link",
    "direct_link", "downloadUrl", "download_url",
    "fast_link", "video_url", "directLink",
]


def _extract_from_api_calls(api_calls: list[dict]) -> str:
    for call in api_calls:
        body = call.get("body", {})
        if not isinstance(body, dict):
            continue
        for key in _DOWNLOAD_KEY_CANDIDATES:
            val = body.get(key)
            if val and isinstance(val, str) and val.startswith("http"):
                return val
        for v in body.values():
            if isinstance(v, dict):
                for key in _DOWNLOAD_KEY_CANDIDATES:
                    val = v.get(key)
                    if val and isinstance(val, str) and val.startswith("http"):
                        return val
    return ""


async def fetch_tbox(terabox_url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (download_link, error).
    Uses the shared headless browser to scrape tboxdownloader.in.
    """
    if _browser is None or not _browser.is_connected():
        return None, "Browser not available"

    api_calls: list[dict] = []
    context: BrowserContext = await _browser.new_context(
        user_agent=BROWSER_UA,
        java_script_enabled=True,
        ignore_https_errors=True,
    )

    try:
        page = await context.new_page()

        # Block heavy assets to speed up loading
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,otf,svg,ico}",
            lambda r: r.abort(),
        )
        for pattern in ["**/gtag/**", "**/googletagmanager**", "**/cloudflareinsights**",
                        "**/adsbygoogle**", "**/doubleclick**"]:
            await page.route(pattern, lambda r: r.abort())

        # Intercept all JSON API responses
        async def _capture(response):
            ct = response.headers.get("content-type", "")
            url = response.url
            if "json" in ct or any(k in url for k in ["/api", "download", "file", "terabox"]):
                try:
                    body = await response.json()
                    api_calls.append({"url": url, "status": response.status, "body": body})
                    log.info("TBOX captured API: %s → %d", url, response.status)
                except Exception:
                    pass

        page.on("response", _capture)

        await page.goto(TBOX_BASE, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)

        # Fill URL input and submit
        await page.fill("#urlInput", terabox_url)
        await page.click("#submitBtn")

        # Wait for file items
        try:
            await page.wait_for_selector(".file-item", timeout=PLAYWRIGHT_TIMEOUT_MS)
        except Exception:
            log.warning("TBOX: .file-item not found")

        # Open first file modal
        try:
            await page.click(".file-item", timeout=5_000)
            await page.wait_for_selector("#fileModal:not(.hidden)", timeout=5_000)
        except Exception:
            log.warning("TBOX: could not open file modal")

        # Trigger direct download link generation
        try:
            await page.click("#genDownloadBtn", timeout=5_000)
            await page.wait_for_selector("#downloadResult:not(.hidden)", timeout=PLAYWRIGHT_TIMEOUT_MS)
        except Exception:
            log.warning("TBOX: could not trigger download generation")

        await asyncio.sleep(2)  # let final API response settle

        # Read from DOM
        download_link = ""
        try:
            download_link = await page.locator("#finalDownloadLink").input_value(timeout=5_000)
        except Exception:
            pass

        if not download_link:
            try:
                href = await page.locator("#openDownloadBtn").get_attribute("href", timeout=3_000) or ""
                if href and href != "#":
                    download_link = href
            except Exception:
                pass

        # Fallback: intercepted API calls
        if not download_link:
            download_link = _extract_from_api_calls(api_calls)

        if download_link:
            log.info("TBOX: download_link found")
            return download_link, None

        log.warning("TBOX: download_link not found")
        return None, "TBOX: could not extract download link"

    except Exception as exc:
        log.exception("TBOX scraping error")
        return None, f"TBOX error: {exc}"

    finally:
        await context.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Merge: run both sources concurrently, combine into one response
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_merged(terabox_url: str) -> dict:
    """
    Runs FVP and TBOX concurrently.
    Returns a unified dict ready for JSONResponse.
    """
    fvp_task  = asyncio.create_task(fetch_fvp(terabox_url))
    tbox_task = asyncio.create_task(fetch_tbox(terabox_url))

    (fvp_results, fvp_error), (download_link, tbox_error) = await asyncio.gather(
        fvp_task, tbox_task
    )

    # Build result items
    if fvp_results:
        # Inject download_link into every item from FVP
        for item in fvp_results:
            item["download_link"] = download_link or None

        return {
            "status":        True,
            "count":         len(fvp_results),
            "results":       fvp_results,
            "errors": {
                "watch_link_error":    fvp_error,
                "download_link_error": tbox_error,
            },
        }

    # FVP failed — return minimal response with just download_link if available
    return {
        "status":        bool(download_link),
        "count":         0,
        "results":       [],
        "download_link": download_link or None,
        "errors": {
            "watch_link_error":    fvp_error,
            "download_link_error": tbox_error,
        },
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
async def root():
    browser_ok = _browser is not None and _browser.is_connected()
    return {
        "status":  "ok",
        "message": "TeraBox Unified API is running",
        "browser": browser_ok,
    }


@app.get("/health", tags=["Health"])
async def health():
    browser_ok = _browser is not None and _browser.is_connected()
    return {"status": "ok" if browser_ok else "degraded", "browser": browser_ok}


@app.get("/api", tags=["TeraBox"])
async def api(
    request: Request,
    url: str = Query(..., description="TeraBox share URL"),
):
    url = url.strip()

    if not url:
        return JSONResponse(
            status_code=400,
            content={"status": False, "message": "url parameter is required"},
        )
    if not url.startswith(("http://", "https://")):
        return JSONResponse(
            status_code=400,
            content={"status": False, "message": "Invalid URL — must start with http(s)://"},
        )

    log.info("Request from %s — URL: %s", request.client.host, url)

    try:
        result = await asyncio.wait_for(fetch_merged(url), timeout=TOTAL_TIMEOUT)
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=504,
            content={"status": False, "message": "Request timed out"},
        )
    except Exception as exc:
        log.exception("Unhandled error in /api")
        return JSONResponse(
            status_code=500,
            content={"status": False, "message": f"Internal error: {exc}"},
        )

    status_code = 200 if result.get("status") else 502
    return JSONResponse(status_code=status_code, content=result)
