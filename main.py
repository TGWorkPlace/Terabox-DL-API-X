# main.py
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright, Browser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Browser pool (single shared instance, reused across requests) ---
_browser: Browser | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser
    playwright = await async_playwright().start()
    _browser = await playwright.chromium.launch(
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
    logger.info("Browser started")
    yield
    await _browser.close()
    await playwright.stop()
    logger.info("Browser stopped")

app = FastAPI(title="TeraBox Downloader API", lifespan=lifespan)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


async def scrape_terabox(terabox_url: str, timeout_ms: int = 30000) -> dict:
    """
    Opens tboxdownloader.in in a headless browser, submits the TeraBox URL,
    clicks through the UI, and extracts the direct download link.
    Intercepts all XHR/fetch responses to capture the raw API data too.
    """
    api_calls: list[dict] = []
    context = await _browser.new_context(
        user_agent=HEADERS["User-Agent"],
        java_script_enabled=True,
        ignore_https_errors=True,
    )

    try:
        page = await context.new_page()

        # Block heavy unnecessary resources to speed things up
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,otf}",
            lambda r: r.abort()
        )
        await page.route("**/gtag/**", lambda r: r.abort())
        await page.route("**/googletagmanager**", lambda r: r.abort())
        await page.route("**/cloudflareinsights**", lambda r: r.abort())

        # Capture all API responses
        async def capture_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")
            if "json" in ct or any(
                k in url for k in ["/api", "fetch", "download", "file", "terabox"]
            ):
                try:
                    body = await response.json()
                    api_calls.append({"url": url, "status": response.status, "body": body})
                    logger.info(f"Captured API: {url} → {response.status}")
                except Exception:
                    pass

        page.on("response", capture_response)

        # Load site
        await page.goto("https://tboxdownloader.in/", wait_until="domcontentloaded", timeout=timeout_ms)

        # Fill input and submit
        await page.fill("#urlInput", terabox_url)
        await page.click("#submitBtn")

        # Wait for file items to render
        try:
            await page.wait_for_selector(".file-item", timeout=timeout_ms)
        except Exception:
            logger.warning("No .file-item found after submit")

        # Grab rendered file list from DOM
        files_data: list[dict] = await page.evaluate("""() => {
            const items = document.querySelectorAll('.file-item');
            return Array.from(items).map(item => ({
                name: item.querySelector('.file-name')?.textContent?.trim() || '',
                meta: item.querySelector('.file-meta')?.textContent?.trim() || '',
                thumb: item.querySelector('.file-thumb')?.src || '',
            }));
        }""")

        # Click first file item to open modal
        try:
            await page.click(".file-item", timeout=5000)
            await page.wait_for_selector("#fileModal:not(.hidden)", timeout=5000)
        except Exception:
            logger.warning("Could not open file modal")

        # Click 'Get Direct Download Link'
        try:
            await page.click("#genDownloadBtn", timeout=5000)
            await page.wait_for_selector("#downloadResult:not(.hidden)", timeout=timeout_ms)
        except Exception:
            logger.warning("Could not trigger download link generation")

        # Small buffer for final API response to land
        await asyncio.sleep(2)

        # Read the populated download link from DOM
        download_link = ""
        try:
            download_link = await page.locator("#finalDownloadLink").input_value(timeout=5000)
        except Exception:
            pass

        # Also check the download button href as fallback
        if not download_link:
            try:
                download_link = await page.locator("#openDownloadBtn").get_attribute("href", timeout=3000) or ""
                if download_link == "#":
                    download_link = ""
            except Exception:
                pass

        return {
            "download_link": download_link,
            "files": files_data,
            "api_calls": api_calls,
        }

    finally:
        await context.close()


def _extract_link_from_api_calls(api_calls: list[dict]) -> str:
    """Try to find the download link from intercepted API responses."""
    candidate_keys = [
        "download_link", "dlink", "url", "link",
        "direct_link", "downloadUrl", "download_url",
        "fast_link", "video_url",
    ]
    for call in api_calls:
        body = call.get("body", {})
        if not isinstance(body, dict):
            continue
        for key in candidate_keys:
            val = body.get(key)
            if val and isinstance(val, str) and val.startswith("http"):
                return val
        # Also check one level deep (nested objects)
        for v in body.values():
            if isinstance(v, dict):
                for key in candidate_keys:
                    val = v.get(key)
                    if val and isinstance(val, str) and val.startswith("http"):
                        return val
    return ""


@app.get("/api")
async def get_download_link(url: str = Query(..., description="TeraBox share URL")):
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL — must start with http(s)://")

    logger.info(f"Request received for: {url}")

    try:
        result = await asyncio.wait_for(scrape_terabox(url), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for page to load")
    except Exception as e:
        logger.exception("Scraping error")
        raise HTTPException(status_code=502, detail=f"Scraping failed: {str(e)}")

    download_link = result.get("download_link", "")

    # Fallback: try to extract from intercepted API calls
    if not download_link:
        download_link = _extract_link_from_api_calls(result["api_calls"])

    if download_link:
        return JSONResponse(content={
            "status": "ok",
            "download_link": download_link,
            "files": result["files"],
        })

    # Debug mode: return raw API calls so you can find the right field
    return JSONResponse(
        status_code=422,
        content={
            "status": "debug",
            "message": "Download link not found. Check api_calls to identify the correct field names.",
            "files": result["files"],
            "api_calls": result["api_calls"],
        },
    )


@app.get("/health")
async def health():
    browser_ok = _browser is not None and _browser.is_connected()
    return {"status": "ok" if browser_ok else "degraded", "browser": browser_ok}
