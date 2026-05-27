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
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# 每个图集最多下载的图片数量
MAX_IMAGES_PER_ALBUM = 30
# 每篇文章之间的冷却时间（秒）
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

async def get_rendered_html(url, scroll_times=3):
    """用 Playwright 打开页面并返回完整 HTML，自动滚动加载更多内容"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        for _ in range(scroll_times):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(1.5)
        html = await page.content()
        await browser.close()
        return html

async def get_organization_pages():
    """抓取前两页机构列表，返回所有机构链接"""
    org_urls = []
    for page in [1, 2]:
        url = f"{BASE_URL}/jigou" if page == 1 else f"{BASE_URL}/jigou/page/{page}"
        print(f"🔍 正在渲染机构列表第 {page} 页: {url}")
        html = await get_rendered_html(url, scroll_times=2)
        soup = BeautifulSoup(html, "html.parser")
        # 提取所有指向 /jigou/xxx 的链接（排除分页自身）
        for a in soup.select("a[href*='/jigou/']"):
            href = a.get("href")
            if href and href != "/jigou" and "/page/" not in href:
                full_url = href if href.startswith("http") else BASE_URL + href
                if full_url not in org_urls:
                    org_urls.append(full_url)
        print(f"  第 {page} 页提取到 {len(org_urls)} 个机构")
        await asyncio.sleep(2)
    return org_urls

async def get_albums_from_org(org_url):
    """从机构页面提取所有图集链接，滚动加载多次确保内容出现"""
    print(f"  📄 抓取机构页面: {org_url}")
    html = await get_rendered_html(org_url, scroll_times=5)
    soup = BeautifulSoup(html, "html.parser")
    albums = []
    # 常见图集链接格式：/theme/123、/album/123、/t/123 等，这里先匹配几种
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # 提取可能包含图集 ID 的链接
        if re.search(r'/(theme|album|t)/\d+', href):
            full_url = href if href.startswith("http") else BASE_URL + href
            title = a.get("title") or a.text.strip()
            if not title:
                # 尝试从父元素找标题
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
    print(f"    发现 {len(unique_albums)} 个图集")
    return unique_albums

async def get_album_images(album_url):
    """进入图集详情页，提取图片 URL（前 MAX_IMAGES_PER_ALBUM 张）"""
    print(f"    📸 抓取图集: {album_url}")
    html = await get_rendered_html(album_url, scroll_times=5)
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
    print(f"      提取到 {len(images)} 张图片")
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
        print(f"      ❌ 下载失败: {e}")
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
    """从已下载图片中选一张最大的作为封面"""
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
    """处理单个图集：下载 → 发送 TG"""
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

    # 智能选封面（选文件最大的一张）
    cover, rest = select_cover(downloaded)

    # 发送封面到频道
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

    # 发送剩余图片到群组（如果有群组 ID）
    if rest and GROUP_ID:
        print(f"    📤 发送剩余 {len(rest)} 张到群组")
        send_media_groups(rest, GROUP_ID, caption="📎 本组合集")

    return True

async def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 秀人完整抓取启动")
    if not TOKEN or not CHAT_ID:
        print("❌ 缺少 TG_TOKEN 或 TG_CHAT_ID")
        sys.exit(1)

    seen = load_seen()
    org_urls = await get_organization_pages()
    print(f"\n✅ 共发现 {len(org_urls)} 个机构（前两页）")

    total_processed = 0
    for org_url in org_urls:
        albums = await get_albums_from_org(org_url)
        for album in albums:
            theme_id = album["url"].split("/")[-1]
            if theme_id in seen:
                print(f"    ⏭️ 已处理过: {album['title']}")
                continue
            success = await process_album(album["title"], album["url"], theme_id)
            if success:
                seen.add(theme_id)
                save_seen(seen)
                total_processed += 1
                print(f"    💾 进度已保存 (总 {total_processed})")
            time.sleep(TG_INTERVAL)
        time.sleep(3)

    print(f"\n🎉 完成！共处理 {total_processed} 套新图集")

if __name__ == "__main__":
    asyncio.run(main())
