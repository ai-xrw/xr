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
SEEN_FILE = "seen_xiuren.json"
BASE_URL = "https://www.xiurenai.com"
TARGET_PAGE = f"{BASE_URL}/jigou/xiuren"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

MAX_IMAGES_PER_ALBUM = 30      # 每套图集最多下载30张
TG_INTERVAL = 15               # 每套图集之间的冷却时间(秒)

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

async def get_rendered_html(url, scroll_times=8, extra_wait=5):
    """用 Playwright 打开页面，滚动到底部多次，等待图片加载，返回完整 HTML"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        # 多次滚动到底部，触发懒加载和无限滚动
        for _ in range(scroll_times):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)
        if extra_wait:
            await asyncio.sleep(extra_wait)
        html = await page.content()
        await browser.close()
        return html

async def get_albums_from_page(page_url):
    """从指定页面提取所有图集链接，返回一个列表"""
    print(f"🔍 正在加载并提取图集: {page_url}")
    html = await get_rendered_html(page_url, scroll_times=10, extra_wait=8)
    soup = BeautifulSoup(html, "html.parser")
    albums = []
    # 匹配常见图集链接格式：/theme/数字、/album/数字、/t/数字
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r'/(theme|album|t)/\d+', href):
            full_url = href if href.startswith("http") else BASE_URL + href
            title = a.get("title") or a.text.strip()
            if not title:
                parent = a.find_parent("div")
                if parent:
                    title = parent.get("title") or parent.text.strip()
            albums.append({"title": title or "无标题", "url": full_url})
    # 去重
    seen = set()
    unique_albums = []
    for a in albums:
        theme_id = a["url"].split("/")[-1]
        if theme_id not in seen:
            seen.add(theme_id)
            unique_albums.append(a)
    print(f"✅ 共发现 {len(unique_albums)} 个图集")
    return unique_albums

async def get_album_images(album_url):
    """从图集详情页提取前 MAX_IMAGES_PER_ALBUM 张图片"""
    print(f"  📸 抓取图集: {album_url}")
    html = await get_rendered_html(album_url, scroll_times=5, extra_wait=5)
    soup = BeautifulSoup(html, "html.parser")
    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if src and not src.startswith("data:"):
            if not src.startswith("http"):
                src = "https:" + src if src.startswith("//") else BASE_URL + "/" + src.lstrip("/")
            if src not in images:
                images.append(src)
                if len(images) >= MAX_IMAGES_PER_ALBUM:
                    break
    print(f"    提取到 {len(images)} 张图片")
    return images

def download_image(url, referer=BASE_URL):
    try:
        h = {**HEADERS, "Referer": referer}
        r = requests.get(url, headers=h, timeout=15, verify=False)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "image/jpeg")
        if not ct.startswith("image/"):
            return None
        return BytesIO(r.content), ct
    except Exception as e:
        print(f"    ❌ 下载失败: {e}")
        return None

def send_media_groups(image_data_list, target_chat_id, caption=""):
    if not image_data_list:
        return None
    first_msg_id = None
    for start in range(0, len(image_data_list), 10):
        batch = image_data_list[start:start+10]
        media, files = [], {}
        for i, (data, ctype) in enumerate(batch):
            attach = f"photo{i}"
            item = {"type": "photo", "media": f"attach://{attach}"}
            if start == 0 and i == 0 and caption:
                item["caption"] = caption
            media.append(item)
            ext = ctype.split("/")[-1].replace("jpeg", "jpg")
            data.seek(0)
            files[attach] = (f"{attach}.{ext}", data, ctype)
        files["media"] = (None, json.dumps(media), "application/json")
        success = False
        for _ in range(2):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMediaGroup",
                    data={"chat_id": target_chat_id},
                    files=files, timeout=60,
                )
                if r.status_code == 200:
                    result = r.json().get("result", [])
                    if result:
                        if start == 0 and first_msg_id is None:
                            first_msg_id = result[0]["message_id"]
                        print(f"  ✅ 媒体组发送成功 ({len(result)} 张)")
                        success = True
                        break
                elif r.status_code == 429:
                    after = r.json().get("parameters", {}).get("retry_after", 30)
                    print(f"  ⚠️ TG 限流，等待 {after}s...")
                    time.sleep(after)
                    for d, _ in batch:
                        d.seek(0)
                else:
                    print(f"  ❌ 媒体组发送失败: {r.text[:200]}")
                    break
            except Exception as e:
                print(f"  ❌ 发送异常: {e}")
                break
        if not success:
            print(f"  ⚠️ 媒体组发送失败")
            break
        time.sleep(3)
    return first_msg_id

def select_cover(downloaded):
    if not downloaded:
        return None, []
    items_with_size = []
    for i, (data, ctype) in enumerate(downloaded):
        data.seek(0, 2)
        size = data.tell()
        data.seek(0)
        items_with_size.append((i, size, data, ctype))
    items_with_size.sort(key=lambda x: x[1], reverse=True)
    best_idx = items_with_size[0][0]
    cover = downloaded[best_idx]
    rest = [item for idx, item in enumerate(downloaded) if idx != best_idx]
    return cover, rest

async def process_album(album_title, album_url, theme_id):
    print(f"\n  🖼️ 处理图集: {album_title} (ID:{theme_id})")
    image_urls = await get_album_images(album_url)
    if not image_urls:
        print("    ❌ 无图片，跳过")
        return False

    downloaded = []
    for url in image_urls:
        res = download_image(url)
        if res:
            downloaded.append(res)
        time.sleep(0.1)

    if not downloaded:
        print("    ❌ 全部下载失败")
        return False

    cover, rest = select_cover(downloaded)

    cover_data, cover_ctype = cover
    cover_data.seek(0)
    ext = cover_ctype.split("/")[-1].replace("jpeg", "jpg")
    caption = f"<b>{album_title}</b>\n\n👉 点击查看完整图集"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": (f"cover.{ext}", cover_data, cover_ctype)},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"    ❌ 频道封面发送失败: {r.text[:200]}")
            return False
        print("    ✅ 封面已发送")
    except Exception as e:
        print(f"    ❌ 频道异常: {e}")
        return False

    if rest and GROUP_ID:
        print(f"    📤 发送剩余 {len(rest)} 张到群组")
        send_media_groups(rest, GROUP_ID, caption="📎 本组合集")

    return True

async def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 秀人单页倒序抓取启动")
    if not TOKEN or not CHAT_ID:
        print("❌ 缺少 TG_TOKEN 或 TG_CHAT_ID")
        sys.exit(1)

    seen = load_seen()
    albums = await get_albums_from_page(TARGET_PAGE)

    # 倒序：从最后一个图集开始向前处理
    albums.reverse()
    print(f"🔄 已反转顺序，将从底部图集开始处理")

    total_processed = 0
    for album in albums:
        theme_id = album["url"].split("/")[-1]
        if theme_id in seen:
            print(f"  ⏭️ 已处理过: {album['title']}")
            continue
        success = await process_album(album["title"], album["url"], theme_id)
        if success:
            seen.add(theme_id)
            save_seen(seen)
            total_processed += 1
            print(f"  💾 进度已保存 (总 {total_processed})")
        time.sleep(TG_INTERVAL)

    print(f"\n🎉 完成！共处理 {total_processed} 套新图集")

if __name__ == "__main__":
    asyncio.run(main())
