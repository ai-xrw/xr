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

DEFAULT_MAX_IMAGES = 30           # 后续图集限制30张
TG_INTERVAL = 15

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

async def get_rendered_html(url, scroll_times=5, extra_wait=3):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        for _ in range(scroll_times):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)
        if extra_wait:
            await asyncio.sleep(extra_wait)
        html = await page.content()
        await browser.close()
        return html

async def get_albums_from_page(page_url):
    print(f"🔍 正在加载并提取图集: {page_url}")
    html = await get_rendered_html(page_url, scroll_times=12, extra_wait=10)
    soup = BeautifulSoup(html, "html.parser")
    albums = []
    pattern = re.compile(r'/jigou/xiuren/(\d+)\.html')

    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = pattern.search(href)
        if not match:
            continue

        full_url = href if href.startswith("http") else BASE_URL + href
        title = a.get("title") or a.text.strip()
        if not title:
            parent = a.find_parent("div")
            if parent:
                title = parent.get("title") or parent.text.strip()

        cover_url = ""
        img_tag = a.find("img")
        if img_tag and img_tag.get("src"):
            cover_url = img_tag["src"]
        else:
            parent = a.find_parent()
            if parent:
                img_tag = parent.find("img")
                if img_tag and img_tag.get("src"):
                    cover_url = img_tag["src"]

        if cover_url and not cover_url.startswith("http"):
            cover_url = "https:" + cover_url if cover_url.startswith("//") else BASE_URL + "/" + cover_url.lstrip("/")

        albums.append({
            "title": title or "无标题",
            "url": full_url,
            "cover_url": cover_url,
            "theme_id": match.group(1)
        })

    seen = set()
    unique = []
    for a in albums:
        tid = a["theme_id"]
        if tid not in seen:
            seen.add(tid)
            unique.append(a)
    print(f"✅ 共发现 {len(unique)} 个图集")
    return unique

async def _get_album_images(album_url, max_images=DEFAULT_MAX_IMAGES):
    print(f"  📸 抓取图集: {album_url} (最多{max_images}张)")
    html = await get_rendered_html(album_url, scroll_times=5, extra_wait=5)
    soup = BeautifulSoup(html, "html.parser")
    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src or src.startswith("data:"):
            continue
        if not src.startswith("http"):
            src = "https:" + src if src.startswith("//") else BASE_URL + "/" + src.lstrip("/")
        if any(pat in src.lower() for pat in ["/logo", "/favicon", "/icon", "/avatar", "/static/logo", "/assets/logo", "logo.png", "logo.jpg"]):
            continue
        if src not in images:
            images.append(src)
            if len(images) >= max_images:
                break
    print(f"    提取到 {len(images)} 张图片")
    return images

async def get_album_images_with_retry(album_url, max_images=DEFAULT_MAX_IMAGES, retries=2):
    for attempt in range(retries):
        try:
            return await _get_album_images(album_url, max_images)
        except Exception as e:
            print(f"      ⚠️ 第{attempt+1}次失败: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(10)
    return []

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

def make_title_with_tag(original_title):
    """将标题中的模特名前面加上#，如 'No.11069 心上可Flora' -> 'No.11069 #心上可Flora'"""
    return re.sub(r'(No\.\d+)\s+(.*)', r'\1 #\2', original_title)

async def process_album(album, first=False):
    title = album["title"]
    theme_id = album["theme_id"]
    cover_url = album["cover_url"]
    album_url = album["url"]

    # 生成带#的标题行
    title_line = make_title_with_tag(title)
    print(f"\n  🖼️ 处理图集: {title_line} (ID:{theme_id})")

    # 确定大图数量
    max_images = 9999 if first else DEFAULT_MAX_IMAGES

    # 下载封面图
    cover_data = None
    if cover_url:
        res = download_image(cover_url)
        if res:
            cover_data = res

    # 获取大图列表（用于群组）
    image_urls = await get_album_images_with_retry(album_url, max_images=max_images, retries=2)

    # 如果没有封面图，则用第一张大图作为封面
    if not cover_data:
        if not image_urls:
            print("    ❌ 无图片，跳过")
            return False
        cover_data = download_image(image_urls[0])
        rest_urls = image_urls[1:]
    else:
        rest_urls = image_urls if image_urls else []

    # 下载剩余大图
    downloaded_rest = []
    for url in rest_urls:
        res = download_image(url)
        if res:
            downloaded_rest.append(res)
        time.sleep(0.1)

    # 发送剩余图片到群组，并获取第一个媒体组的消息ID
    first_group_msg_id = None
    if downloaded_rest and GROUP_ID:
        print(f"    📤 发送剩余 {len(downloaded_rest)} 张到群组")
        first_group_msg_id = send_media_groups(downloaded_rest, GROUP_ID, caption="📎 本组合集")

    # 生成群组消息链接
    group_link = album_url  # 默认回退到原网页
    if first_group_msg_id and GROUP_USERNAME:
        group_link = f"https://t.me/{GROUP_USERNAME}/{first_group_msg_id}"

    # 构建频道封面标题
    cover_data_tuple = cover_data
    cover_data_tuple[0].seek(0)
    ext = cover_data_tuple[1].split("/")[-1].replace("jpeg", "jpg")

    caption = title_line
    caption += f"\n\n<a href=\"{group_link}\">👉 点击查看图集</a>"

    # 第一个图集不加VIP链接，后续图集加上
    if not first:
        caption += f"\n\n🌟 <a href=\"https://t.me/xiuren88bot?start=lWAXnjXFzdxP\">点我进vip群查看完整版</a>"

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": (f"cover.{ext}", cover_data_tuple[0], cover_data_tuple[1])},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"    ❌ 频道封面发送失败: {r.text[:200]}")
            return False
        print("    ✅ 封面已发送")
    except Exception as e:
        print(f"    ❌ 频道异常: {e}")
        return False

    return True

async def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 秀人最终版启动")
    if not TOKEN or not CHAT_ID:
        print("❌ 缺少 TG_TOKEN 或 TG_CHAT_ID")
        sys.exit(1)

    # 获取群组用户名
    global GROUP_USERNAME
    if GROUP_ID:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getChat?chat_id={GROUP_ID}", timeout=10)
            if r.status_code == 200 and r.json().get("ok"):
                GROUP_USERNAME = r.json()["result"].get("username")
                if GROUP_USERNAME:
                    print(f"✅ 群组用户名: @{GROUP_USERNAME}")
                else:
                    print("⚠️ 该群组没有公开用户名，将无法生成群组链接")
        except Exception as e:
            print(f"⚠️ 获取群组信息异常: {e}")

    seen = load_seen()
    albums = await get_albums_from_page(TARGET_PAGE)
    albums.reverse()
    print(f"🔄 已反转顺序，从底部图集开始处理")

    total_processed = 0
    for idx, album in enumerate(albums):
        theme_id = album["theme_id"]
        if theme_id in seen:
            print(f"  ⏭️ 已处理: {album['title']}")
            continue
        # 第一套图集特殊处理：全量图片，不加VIP链接
        is_first = (idx == 0)
        success = await process_album(album, first=is_first)
        if success:
            seen.add(theme_id)
            save_seen(seen)
            total_processed += 1
            print(f"  💾 进度已保存 (总 {total_processed})")
        time.sleep(TG_INTERVAL)

    print(f"\n🎉 完成！共处理 {total_processed} 套新图集")

if __name__ == "__main__":
    asyncio.run(main())
