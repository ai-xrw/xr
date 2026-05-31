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

# ---------- 环境变量 ----------
TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")
GROUP_ID = os.getenv("TG_GROUP_ID")

# ---------- 配置 ----------
BASE_URL = "https://xchina.co"
SERIES_URL_TEMPLATE = "https://xchina.co/photos/series-5f1476781eab4/sort-vol/{page}.html"
START_PAGE = 48
PAGE_FILE = "next_page.txt"          # 存储下一次要抓的页码
SEEN_FILE = "seen_xchina.json"       # 存储已处理的图集ID（全局去重）
DEFAULT_MAX_IMAGES = 30              # 每套图集最多下载30张
TG_INTERVAL = 15                     # 图集之间冷却(秒)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# ---------- 辅助函数 ----------
def load_page_number():
    """读取下一次要抓取的页码，不存在则返回 START_PAGE"""
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

async def get_rendered_html(url, scroll_times=8, extra_wait=5):
    """Playwright 打开页面，滚动到底多次触发懒加载，返回完整HTML"""
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

async def get_albums_from_list(page_url):
    """从列表页提取所有图集信息（封面、详情链接、标题）"""
    print(f"🔍 正在抓取列表页: {page_url}")
    html = await get_rendered_html(page_url, scroll_times=12, extra_wait=10)
    soup = BeautifulSoup(html, "html.parser")
    albums = []

    # 注意：下面选择器需要根据实际页面结构调整！
    # 常见模式：每个图集是一个 <a> 标签，内含 <img> 作为封面，href 指向详情页
    # 请根据 F12 检查修改为正确的 CSS 选择器
    for a in soup.select("a[href*='/photos/']"):  # 假设包含 /photos/ 的链接
        href = a.get("href")
        if not href or "series-" in href or "sort-vol" in href:  # 排除分页链接
            continue
        full_url = href if href.startswith("http") else BASE_URL + href
        # 提取封面图
        cover_url = ""
        img_tag = a.find("img")
        if img_tag and img_tag.get("src"):
            cover_url = img_tag["src"]
            if not cover_url.startswith("http"):
                cover_url = "https:" + cover_url if cover_url.startswith("//") else BASE_URL + "/" + cover_url.lstrip("/")
        # 提取标题
        title = a.get("title") or a.text.strip()
        if not title:
            parent = a.find_parent("div") or a.find_parent("li")
            if parent:
                title = parent.get("title") or parent.text.strip()
        if not title:
            title = "无标题"
        albums.append({
            "title": title,
            "url": full_url,
            "cover_url": cover_url,
            "album_id": href.split("/")[-1].replace(".html", "")
        })

    # 去重
    seen = set()
    unique = []
    for a in albums:
        aid = a["album_id"]
        if aid not in seen:
            seen.add(aid)
            unique.append(a)
    print(f"✅ 共发现 {len(unique)} 个图集")
    return unique

async def get_album_images(album_url, max_images=DEFAULT_MAX_IMAGES):
    """进入详情页，提取所有大图（过滤logo）"""
    print(f"  📸 抓取图集: {album_url}")
    html = await get_rendered_html(album_url, scroll_times=5, extra_wait=5)
    soup = BeautifulSoup(html, "html.parser")
    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src or src.startswith("data:"):
            continue
        if not src.startswith("http"):
            src = "https:" + src if src.startswith("//") else BASE_URL + "/" + src.lstrip("/")
        # 过滤logo
        if any(pat in src.lower() for pat in ["/logo", "/favicon", "/icon", "/avatar", "/static/logo", "/assets/logo", "logo.png", "logo.jpg"]):
            continue
        if src not in images:
            images.append(src)
            if len(images) >= max_images:
                break
    print(f"    提取到 {len(images)} 张大图")
    return images

async def get_album_images_with_retry(album_url, max_images=DEFAULT_MAX_IMAGES, retries=2):
    for attempt in range(retries):
        try:
            return await get_album_images(album_url, max_images)
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

async def process_album(album, group_username=None, is_first=False):
    """处理单个图集：下载、发送封面到频道、大图到群组，返回成功与否"""
    title = album["title"]
    album_id = album["album_id"]
    cover_url = album["cover_url"]
    album_url = album["url"]

    print(f"\n  🖼️ 处理图集: {title} (ID:{album_id})")

    # 第一套图集不限制图片数量
    max_imgs = 9999 if is_first else DEFAULT_MAX_IMAGES

    # 下载封面图
    cover_data = None
    if cover_url:
        res = download_image(cover_url)
        if res:
            cover_data = res

    # 获取大图
    image_urls = await get_album_images_with_retry(album_url, max_images=max_imgs, retries=2)

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

    # 发送剩余大图到群组，并获取群组消息ID以便生成链接
    first_group_msg_id = None
    if downloaded_rest and GROUP_ID:
        print(f"    📤 发送剩余 {len(downloaded_rest)} 张到群组")
        first_group_msg_id = send_media_groups(downloaded_rest, GROUP_ID, caption="📎 本组合集")

    # 生成群组消息跳转链接
    group_link = album_url  # 默认回退到原网页
    if first_group_msg_id and group_username:
        group_link = f"https://t.me/{group_username}/{first_group_msg_id}"

    # 构建频道封面 caption（标题 + 链接）
    cover_data_tuple = cover_data
    cover_data_tuple[0].seek(0)
    ext = cover_data_tuple[1].split("/")[-1].replace("jpeg", "jpg")
    caption = f"{title}\n\n<a href=\"{group_link}\">👉 点击查看图集</a>"
    # 非首套图集附上VIP链接
    if not is_first:
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
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] xchina 每日一页启动")

    if not TOKEN or not CHAT_ID:
        print("❌ 缺少 TG_TOKEN 或 TG_CHAT_ID")
        sys.exit(1)

    # 获取群组用户名（用于生成群组链接）
    group_username = None
    if GROUP_ID:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getChat?chat_id={GROUP_ID}", timeout=10)
            if r.status_code == 200 and r.json().get("ok"):
                group_username = r.json()["result"].get("username")
                if group_username:
                    print(f"✅ 群组用户名: @{group_username}")
                else:
                    print("⚠️ 该群组没有公开用户名，将使用原网页链接")
        except Exception as e:
            print(f"⚠️ 获取群组信息异常: {e}")

    # 确定本次要抓取的页码
    current_page = load_page_number()
    print(f"📖 本次抓取第 {current_page} 页")

    if current_page < 1:
        print("🏁 所有页面已抓取完毕，如需重新开始请删除 next_page.txt")
        return

    page_url = SERIES_URL_TEMPLATE.replace("{page}", str(current_page))
    seen = load_seen()

    # 获取该页所有图集
    albums = await get_albums_from_list(page_url)
    if not albums:
        print("⚠️ 未提取到图集，可能是选择器不匹配，请检查页面结构")
        # 即使未提取到，也继续更新页码，避免卡死
        save_page_number(current_page - 1)
        return

    # 倒序（从底部往上）
    albums.reverse()
    print(f"🔄 已反转顺序，将从页面底部开始处理")

    total_processed = 0
    for idx, album in enumerate(albums):
        if album["album_id"] in seen:
            print(f"  ⏭️ 已处理: {album['title']}")
            continue
        is_first = (idx == 0)  # 页面第一套图集（倒序后的第一个）
        success = await process_album(album, group_username=group_username, is_first=is_first)
        if success:
            seen.add(album["album_id"])
            save_seen(seen)
            total_processed += 1
            print(f"  💾 进度已保存 (本页已处理 {total_processed})")
        time.sleep(TG_INTERVAL)

    # 更新下一次要抓的页码
    next_page = current_page - 1
    save_page_number(next_page)
    print(f"📉 下次将抓取第 {next_page} 页（如果>0）")

    print(f"\n🎉 本页完成！共处理 {total_processed} 套新图集")

if __name__ == "__main__":
    asyncio.run(main())
