import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import requests
import time
import json
import os
import sys
from io import BytesIO
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")
GROUP_ID = os.getenv("TG_GROUP_ID")
SEEN_FILE = "seen_xiuren.json"
BASE_URL = "https://www.xiurenai.com"
JIGOU_URL = f"{BASE_URL}/jigou"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

def load_seen():
    if not os.path.exists(SEEN_FILE) or os.path.getsize(SEEN_FILE) == 0:
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)

async def get_rendered_html(url):
    """用 Playwright 获取渲染后的完整 HTML"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        # 模拟滚动触发懒加载
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(1)
        html = await page.content()
        await browser.close()
        return html

async def get_sub_categories():
    """从机构首页获取所有子分类链接"""
    print("🔍 正在渲染机构首页...")
    html = await get_rendered_html(JIGOU_URL)
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.select("a[href*='/jigou/']"):
        href = a.get("href")
        if href and href != "/jigou" and href not in links:
            full_url = href if href.startswith("http") else BASE_URL + href
            links.append(full_url)
    print(f"✅ 发现 {len(links)} 个子机构")
    return links

async def get_albums_from_category(cat_url):
    """从子机构页面获取所有图集链接"""
    print(f"  📄 抓取子机构: {cat_url}")
    html = await get_rendered_html(cat_url)
    soup = BeautifulSoup(html, "html.parser")
    albums = []
    for a in soup.select("a[href*='/theme/']"):
        href = a.get("href")
        title = a.get("title") or a.text.strip()
        if href:
            full_url = href if href.startswith("http") else BASE_URL + href
            albums.append({"title": title, "url": full_url})
    print(f"    发现 {len(albums)} 个图集")
    return albums

async def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 秀人Playwright版启动")
    if not TOKEN or not CHAT_ID:
        print("❌ 缺少 TG_TOKEN 或 TG_CHAT_ID")
        sys.exit(1)

    seen = load_seen()
    sub_cats = await get_sub_categories()

    total_processed = 0
    for cat_url in sub_cats[:3]:  # 先测试前3个机构，避免运行时间过长
        albums = await get_albums_from_category(cat_url)
        for album in albums:
            theme_id = album["url"].split("/")[-1]
            if theme_id in seen:
                print(f"    ⏭️ 已处理: {album['title']}")
                continue
            print(f"    📸 待处理: {album['title']} (ID:{theme_id})")
            # 这里可以接入图片下载和发送逻辑（复用4KHD的）
            # 如果处理成功:
            # seen.add(theme_id)
            # save_seen(seen)
            total_processed += 1
        time.sleep(3)

    print(f"\n✅ 共发现 {total_processed} 个新图集")

if __name__ == "__main__":
    asyncio.run(main())
