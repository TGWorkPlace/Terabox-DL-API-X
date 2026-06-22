# main.py
import asyncio
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

app = FastAPI(title="TeraBox Downloader API")


async def scrape_via_playwright(terabox_url: str) -> dict:
    api_calls = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        )
        page = await context.new_page()

        # Intercept all fetch/XHR responses
        async def handle_response(response):
            url = response.url
            if any(x in url for x in ["/api", "terabox", "download", "file", "fetch"]):
                try:
                    body = await response.json()
                    api_calls.append({
                        "url": url,
                        "status": response.status,
                        "body": body,
                    })
                except Exception:
                    pass

        page.on("response", handle_response)

        await page.goto("https://tboxdownloader.in/", wait_until="networkidle")

        # Paste URL and submit
        await page.fill("#urlInput", terabox_url)
        await page.click("#submitBtn")

        # Wait for file list
        try:
            await page.wait_for_selector(".file-item", timeout=20000)
        except Exception:
            pass

        # Click first file item to open modal
        try:
            await page.click(".file-item", timeout=5000)
            await page.wait_for_selector("#fileModal:not(.hidden)", timeout=5000)
        except Exception:
            pass

        # Click get download link button
        try:
            await page.click("#genDownloadBtn", timeout=5000)
            await page.wait_for_selector("#downloadResult:not(.hidden)", timeout=15000)
        except Exception:
            pass

        # Extra wait for all API responses to settle
        await asyncio.sleep(3)

        # Safely grab download link from DOM
        download_link = ""
        try:
            download_link = await page.locator("#finalDownloadLink").input_value(timeout=3000)
        except Exception:
            pass

        # Grab file list from DOM
        files_data = await page.evaluate("""() => {
            const items = document.querySelectorAll('.file-item');
            return Array.from(items).map(item => ({
                name: item.querySelector('.file-name')?.textContent?.trim() || '',
                meta: item.querySelector('.file-meta')?.textContent?.trim() || '',
            }));
        }""")

        await browser.close()

    return {
        "api_calls": api_calls,
        "download_link": download_link,
        "files": files_data,
    }


@app.get("/api")
async def get_download_link(url: str = Query(..., description="TeraBox share URL")):
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL")

    try:
        result = await scrape_via_playwright(url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scraping failed: {str(e)}")

    download_link = result.get("download_link", "")

    # Try to find download link from intercepted API calls
    if not download_link:
        for call in result["api_calls"]:
            body = call["body"]
            if isinstance(body, dict):
                for key in ["download_link", "dlink", "url", "link", "direct_link", "downloadUrl"]:
                    if body.get(key):
                        download_link = body[key]
                        break
            if download_link:
                break

    if not download_link:
        return JSONResponse(content={
            "status": "debug",
            "message": "Could not extract download link. Check api_calls to find real field names.",
            "files": result["files"],
            "api_calls": result["api_calls"],
        })

    return JSONResponse(content={
        "status": "ok",
        "download_link": download_link,
        "files": result["files"],
    })


@app.get("/health")
async def health():
    return {"status": "ok"}
