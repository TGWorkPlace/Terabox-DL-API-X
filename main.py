# main.py
import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="TeraBox Downloader API")

BASE = "https://tboxdownloader.in"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Referer": BASE + "/",
    "Origin": BASE,
    "Content-Type": "application/json",
}


@app.get("/api")
async def get_download_link(url: str = Query(..., description="TeraBox share URL")):
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL")

    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: Get file info
        try:
            r1 = await client.post(
                f"{BASE}/api/get",  # <-- update this if different
                json={"url": url},
                headers=HEADERS,
            )
            r1.raise_for_status()
            info = r1.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Info fetch failed: {str(e)}")

        files = info.get("list") or info.get("files") or []
        if not files:
            raise HTTPException(status_code=404, detail="No files found in response")

        results = []
        for f in files:
            fid = f.get("fs_id") or f.get("fid") or f.get("id")
            name = f.get("server_filename") or f.get("filename") or "unknown"
            size = f.get("size", 0)

            # Step 2: Get download link for each file
            try:
                r2 = await client.post(
                    f"{BASE}/api/download",  # <-- update this if different
                    json={"fid": fid},
                    headers=HEADERS,
                )
                r2.raise_for_status()
                dl = r2.json()
            except Exception as e:
                results.append({
                    "filename": name,
                    "size_mb": round(size / 1e6, 2),
                    "error": str(e),
                })
                continue

            link = dl.get("download_link") or dl.get("url") or dl.get("dlink") or dl.get("link")
            results.append({
                "filename": name,
                "size_mb": round(size / 1e6, 2),
                "download_link": link,
            })

    return JSONResponse(content={"status": "ok", "results": results})
