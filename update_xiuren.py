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

MAX_IMAGES_PER_ALBUM = 30
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
    """提取图集列表，包含封面图"""
    print(f"🔍 正在加载并提取图集: {page_url}")
    html = await get_rendered_html(page_url, scroll_times=12, extra_wait=10)
    soup = BeautifulSoup(html, "html.parser")
    albums = []
    pattern = re.compile(r'/jigou/xiuren/(\d+)\.html')

    # 遍历所有图集链接（a标签）
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

        # 提取封面图：优先从a标签内部的img获取，否则从父元素中找第一个img
        cover_url = ""
        img_tag = a.find("img")
        if img_tag and img_tag.get("src"):
            cover_url = img_tag["src"]
        else:
            # 尝试从父级元素中找
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

    # 去重
    seen = set()
    unique = []
    for a in albums:
        tid = a["theme_id"]
        if tid not in seen:
            seen.add(tid)
            unique.append(a)
    print(f"✅ 共发现 {len(unique)} 个图集")
    return unique

async def _get_album_images(album_url):
    """获取详情页大图，过滤logo"""
    print(f"  📸 抓取图集: {album_url}")
    html = await get_rendered_html(album_url, scroll_times=5, extra_wait=5)
    soup = BeautifulSoup(html, "html.parser")
    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src or src.startswith("data:"):
            continue
        # 补全URL
        if not src.startswith("http"):
            src = "https:" + src if src.startswith("//") else BASE_URL + "/" + src.lstrip("/")
        # 过滤logo/图标
        if any(pat in src.lower() for pat in ["/logo", "/favicon", "/icon", "/avatar", "/static/logo", "/assets/logo", "logo.png", "logo.jpg"]):
            continue
        # 去重
        if src not in images:
            images.append(src)
            if len(images) >= MAX_IMAGES_PER_ALBUM:
                break
    print(f"    提取到 {len(images)} 张大图（已过滤logo）")
    return images

async def get_album_images_with_retry(album_url, retries=2):
    for attempt in range(retries):
        try:
            return await _get_album_images(album_url)
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

def generate_tags(title):
    """从标题提取模特名标签，例如 '[秀人XiuRen] 2025.12.17 No.11120 Twins-夭夭' -> ['#Twins-夭夭']"""
    tags = []
    # 尝试匹配 No.数字 后面的部分（可能是模特名）
    match = re.search(r'No\.\d+\s+(.*)', title)
    if match:
        names = match.group(1).strip()
        # 可能多个模特名用逗号、空格等分隔，这里简单以空格分割，并过滤短词
        for name in names.split():
            name = name.strip().rstrip(',;')
            if len(name) > 1:  # 至少两个字符
                tags.append(f"#{name}")
    # 如果没有 No.，可以尝试匹配 ][空格]后面的内容？
    if not tags:
        # 简单备用：去掉方括号内容，取剩余部分作为模特名
        clean = re.sub(r'\[.*?\]', '', title).strip()
        if clean:
            tags.append(f"#{clean}")
    return tags[:5]  # 最多5个标签

async def process_album(album):
    title = album["title"]
    theme_id = album["theme_id"]
    cover_url = album["cover_url"]
    album_url = album["url"]

    print(f"\n  🖼️ 处理图集: {title} (ID:{theme_id})")
    # 下载封面图
    cover_data = None
    if cover_url:
        res = download_image(cover_url)
        if res:
            cover_data = res
    if not cover_data:
        # 如果没有封面图，则从大图中选第一张作为封面
        print("    ⚠️ 未找到封面图，将使用第一张大图作为封面")
        image_urls = await get_album_images_with_retry(album_url, retries=2)
        if not image_urls:
            print("    ❌ 无图片，跳过")
            return False
        # 取第一张作为封面，剩余作为群组图
        cover_data = download_image(image_urls[0])
        rest_urls = image_urls[1:]
    else:
        # 下载大图（群组用）
        image_urls = await get_album_images_with_retry(album_url, retries=2)
        if not image_urls:
            # 没有大图，只有封面图
            image_urls = []
        # 大图不包含封面图，直接发送
        rest_urls = image_urls

    # 下载剩余大图
    downloaded_rest = []
    for url in rest_urls:
        res = download_image(url)
        if res:
            downloaded_rest.append(res)
        time.sleep(0.1)

    # 标签
    tags = generate_tags(title)
    tag_str = " ".join(tags) if tags else ""

    # 发送封面到频道
    cover_data_tuple = cover_data  # (BytesIO, ctype)
    cover_data_tuple[0].seek(0)
    ext = cover_data_tuple[1].split("/")[-1].replace("jpeg", "jpg")
    caption = f"<b>{title}</b>"
    if tag_str:
        caption += f"\n{tag_str}"
    caption += f"\n\n👉 点击查看完整图集"
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

    # 发送剩余大图到群组
    if downloaded_rest and GROUP_ID:
        print(f"    📤 发送剩余 {len(downloaded_rest)} 张到群组")
        send_media_groups(downloaded_rest, GROUP_ID, caption="📎 本组合集")

    return True

async def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 秀人最终版启动")
    if not TOKEN or not CHAT_ID:
        print("❌ 缺少 TG_TOKEN 或 TG_CHAT_ID")
        sys.exit(1)

    seen = load_seen()
    albums = await get_albums_from_page(TARGET_PAGE)
    albums.reverse()
    print(f"🔄 已反转顺序，从底部图集开始处理")

    total_processed = 0
    for album in albums:
        if album["theme_id"] in seen:
            print(f"  ⏭️ 已处理: {album['title']}")
            continue
        success = await process_album(album)
        if success:
            seen.add(album["theme_id"])
            save_seen(seen)
            total_processed += 1
            print(f"  💾 进度已保存 (总 {total_processed})")
        time.sleep(TG_INTERVAL)

    print(f"\n🎉 完成！共处理 {total_processed} 套新图集")

if __name__ == "__main__":
    asyncio.run(main())
