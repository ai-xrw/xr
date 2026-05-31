import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import requests
import time
import json
import os
import sys
import re
from io import BytesIO
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")
GROUP_ID = os.getenv("TG_GROUP_ID")

BASE_URL = "https://xchina.co"
SERIES_URL_TEMPLATE = "https://xchina.co/photos/series-5f1476781eab4/sort-vol/{page}.html"
START_PAGE = 48
PAGE_FILE = "next_page.txt"
SEEN_FILE = "seen_xchina.json"
DEFAULT_MAX_IMAGES = 30
TG_INTERVAL = 15
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

def load_page_number():
    if not os.path.exists(PAGE_FILE):
        return START_PAGE
    try:
        with open(PAGE_FILE, "r") as f:
            num = int(f.read().strip())
            return num if num >= 1 else 1
    except:
        return START_PAGE

def save_page_number(page):
    with open(PAGE_FILE, "w") as f:
        f.write(str(page))

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

async def diagnose_page(url):
    """保存 Playwright 渲染后的 HTML，并统计关键标签数量"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        # 滚动
        for _ in range(5):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)
        html = await page.content()
        await browser.close()

        # 保存到文件（GitHub Actions 会作为 artifact 保留）
        with open("diagnose.html", "w", encoding="utf-8") as f:
            f.write(html)

        # 简单统计
        soup = BeautifulSoup(html, "html.parser")
        photo_list_div = soup.select("div.list.photo-list")
        item_divs = soup.select("div.item.photo")
        all_links = [a.get("href") for a in soup.find_all("a", href=True) if a.get("href")]
        print(f"📊 诊断统计：")
        print(f"  div.list.photo-list 数量: {len(photo_list_div)}")
        print(f"  div.item.photo 数量: {len(item_divs)}")
        print(f"  所有链接数量: {len(all_links)}")
        if all_links:
            print(f"  前10条链接: {all_links[:10]}")
        return html

async def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] xchina 终极诊断")
    page_url = SERIES_URL_TEMPLATE.replace("{page}", str(START_PAGE))
    html = await diagnose_page(page_url)
    print("✅ 诊断 HTML 已保存为 diagnose.html，请从 Artifacts 下载")
    print("如果页面中包含图集，请将 diagnose.html 的内容发给我，或告知 div.item.photo 的数量")

if __name__ == "__main__":
    asyncio.run(main())
