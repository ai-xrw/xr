#!/usr/bin/env python3
"""
xchina.co 秀人网图集抓取 + Telegram 推送
从 https://xchina.co/photos/series-5f1476781eab4/{page}.html 抓取
从第48页开始，每天往前一页，每个图集只抓前30张原图
"""

import requests
from bs4 import BeautifulSoup
import os, re, time, json, sys
from io import BytesIO
import urllib3

urllib3.disable_warnings()

# ==================== 配置 ====================
TOKEN      = os.getenv("TG_TOKEN")
CHAT_ID    = os.getenv("TG_CHAT_ID")
GROUP_ID   = os.getenv("TG_GROUP_ID", "").strip()
CF_COOKIE  = os.getenv("CF_COOKIE", "")

BASE_URL          = "https://xchina.co"
SERIES_URL        = "https://xchina.co/photos/series-5f1476781eab4/{page}.html"
START_PAGE        = 48
PAGE_FILE         = "next_page.txt"
SEEN_FILE         = "seen_xchina.json"
MAX_IMAGES        = 30          # 除第一个图集外，其余限制30张
TG_INTERVAL       = 15          # Telegram 发送间隔

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.verify = False

# ==================== Cookie 注入 ====================
def inject_cookies(cookie_str):
    if not cookie_str:
        print("⚠️ 未设置 CF_COOKIE，可能触发 Cloudflare")
        return
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            SESSION.cookies.set(k.strip(), v.strip(), domain="xchina.co")
    print("✅ Cookie 已注入")

inject_cookies(CF_COOKIE)

# ==================== 状态管理 ====================
def load_page():
    if not os.path.exists(PAGE_FILE):
        return START_PAGE
    try:
        return max(int(open(PAGE_FILE).read().strip()), 1)
    except:
        return START_PAGE

def save_page(page):
    with open(PAGE_FILE, "w") as f:
        f.write(str(page))

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        return set(json.load(open(SEEN_FILE, encoding="utf-8")))
    except:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)

# ==================== 网络请求 ====================
def safe_get(url, retries=3, timeout=20):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            elif r.status_code == 403:
                print(f"  ❌ 403 Forbidden (Cookie 可能过期): {url}")
                return None
            elif r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 30))
                print(f"  ⚠️ 限流，等待 {wait}s")
                time.sleep(wait)
            else:
                print(f"  ⚠️ HTTP {r.status_code}: {url}")
                time.sleep(2)
        except Exception as e:
            print(f"  ❌ 请求异常 ({i+1}/{retries}): {e}")
            time.sleep(2)
    return None

def fix_url(src):
    if not src:
        return None
    src = src.strip()
    if src.startswith("//"):   return "https:" + src
    if src.startswith("/"):    return BASE_URL + src
    if not src.startswith("http"): return BASE_URL + "/" + src
    return src

# ==================== 列表页 → 图集链接 ====================
def get_albums_from_list(page):
    """解析系列列表页，返回图集信息列表"""
    url = SERIES_URL.format(page=page)
    print(f"📄 列表页: {url}")
    r = safe_get(url)
    if not r:
        return []

    # 检测 Cloudflare 拦截
    if "cloudflare" in r.text.lower() and len(r.text) < 5000:
        print("❌ 被 Cloudflare 拦截，请更新 Cookie")
        with open("debug_list.html", "w", encoding="utf-8") as f:
            f.write(r.text)
        sys.exit(1)

    soup = BeautifulSoup(r.text, "html.parser")

    # ---- 选择器需要根据实际列表页调整 ----
    # 常见模式1: div.list.photo-list > div.item
    # 常见模式2: div.content-box > div.list 等
    # 这里用通用方案：找所有 /photo/id- 结尾的链接，但要去重
    seen_urls = set()
    albums = []
    for a_tag in soup.find_all("a", href=re.compile(r'/photo/id-[a-f0-9]+\.html')):
        detail_url = fix_url(a_tag["href"])
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)
        album_id = re.search(r'/photo/id-([a-f0-9]+)\.html', detail_url).group(1)

        # 尝试提取封面（在列表中通常有缩略图背景）
        # 向上查找容器，找 div.img 的 style
        cover_url = ""
        container = a_tag.find_parent("div", class_=re.compile("item"))
        if container:
            img_div = container.find("div", class_="img")
            if img_div:
                style = img_div.get("style", "")
                m = re.search(r"url\(['\"]?([^'\")\s]+)['\"]?\)", style)
                if m:
                    cover_url = fix_url(m.group(1))
        # 标题（可能在 a 标签内或附近）
        title = a_tag.get("title") or a_tag.get_text(strip=True) or "无标题"

        albums.append({
            "album_id": album_id,
            "url": detail_url,
            "cover_url": cover_url,
            "title": title,
        })

    print(f"  找到 {len(albums)} 个图集")
    if not albums:
        with open("debug_list.html", "w", encoding="utf-8") as f:
            f.write(r.text)
        print("  ⚠️ 未找到图集，已保存 debug_list.html，请检查选择器")
    return albums

# ==================== 图集详情 → 原图链接 ====================
def get_images_from_album(album_url, max_images=MAX_IMAGES):
    """
    处理图集分页，提取前 max_images 张原图 URL
    图集页结构：div.list.photo-items > div.item.photo-image
    缩略图是 CSS 背景图，格式：.../00001_600x0.webp
    原图就是去掉 _数字x数字 并换 .jpg
    """
    collected = []
    page = 1
    while len(collected) < max_images:
        if page == 1:
            page_url = album_url
        else:
            # 分页格式：/photo/id-xxx/2.html
            if album_url.endswith(".html"):
                page_url = album_url[:-5] + f"/{page}.html"
            else:
                break

        print(f"  📂 分页 {page}: {page_url}")
        r = safe_get(page_url)
        if not r:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        # 只取 photo-image 项，跳过广告项 auto-height
        items = soup.select("div.list.photo-items div.item.photo-image")
        if not items:
            print("    无图片项，分页结束")
            break

        for item in items:
            if len(collected) >= max_images:
                break
            img_div = item.find("div", class_="img")
            if not img_div:
                continue
            style = img_div.get("style", "")
            m = re.search(r"url\(['\"]?([^'\")\s]+)['\"]?\)", style)
            if not m:
                continue
            thumb_url = m.group(1)
            # 构造原图：去掉 _600x0 等尺寸后缀，换 .jpg
            filename = os.path.basename(thumb_url)
            name_no_dim = re.sub(r'_\d+x\d*', '', filename)
            name_jpg = os.path.splitext(name_no_dim)[0] + ".jpg"
            orig_url = thumb_url.rsplit("/", 1)[0] + "/" + name_jpg
            if orig_url not in collected:
                collected.append(orig_url)

        # 检查是否有下一页
        next_link = soup.select_one("div.pager a.next:not(.disabled)")
        if not next_link:
            break
        page += 1
        time.sleep(0.5)

    print(f"    共获取 {len(collected)} 张原图")
    return collected[:max_images]

# ==================== 下载图片（内存） ====================
def download_image(url, referer):
    for attempt in range(3):
        try:
            r = SESSION.get(url, headers={"Referer": referer}, timeout=30)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "image/jpeg")
            if not ct.startswith("image/") or len(r.content) < 2000:
                continue
            return BytesIO(r.content), ct
        except Exception as e:
            print(f"    ❌ 下载失败 ({attempt+1}/3): {e}")
            time.sleep(1)
    return None, None

# ==================== Telegram 发送 ====================
def get_group_username():
    """获取群组公开用户名，用于构造链接"""
    if not GROUP_ID:
        return None
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getChat?chat_id={GROUP_ID}", timeout=10)
        if r.ok:
            username = r.json()["result"].get("username")
            if username:
                print(f"✅ 群组: @{username}")
                return username
    except Exception as e:
        print(f"⚠️ 获取群组信息失败: {e}")
    return None

def build_caption(title, group_link=None, is_first=False):
    """生成频道消息文本"""
    text = f"<b>{title}</b>"
    if group_link:
        text += f'\n\n👉 <a href="{group_link}">点击查看完整图集</a>'
    return text

def send_media_group(image_data_list, target_chat_id, caption=""):
    """发送一组图片到 Telegram（最多10张/次）"""
    if not image_data_list:
        return None
    first_msg_id = None

    for start in range(0, len(image_data_list), 10):
        batch = image_data_list[start:start+10]
        media = []
        files = {}

        for i, (data, ctype) in enumerate(batch):
            attach = f"photo{i}"
            item = {"type": "photo", "media": f"attach://{attach}"}
            if start == 0 and i == 0 and caption:
                item["caption"] = caption[:1024]
                item["parse_mode"] = "HTML"
            media.append(item)
            ext = ctype.split("/")[-1].replace("jpeg", "jpg")
            data.seek(0)
            files[attach] = (f"{attach}.{ext}", data, ctype)

        files["media"] = (None, json.dumps(media), "application/json")

        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMediaGroup",
                    data={"chat_id": target_chat_id},
                    files=files,
                    timeout=60,
                )
                if r.status_code == 200:
                    res = r.json().get("result", [])
                    if res and first_msg_id is None:
                        first_msg_id = res[0]["message_id"]
                    print(f"  ✅ 批次发送成功 ({len(res)} 张)")
                    break
                elif r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 30)
                    print(f"  ⚠️ 限流，等待 {wait}s")
                    time.sleep(wait)
                    for d, _ in batch:
                        d.seek(0)
                else:
                    print(f"  ❌ 发送失败 ({r.status_code}): {r.text[:200]}")
                    break
            except Exception as e:
                print(f"  ❌ 发送异常: {e}")
                time.sleep(3)
        time.sleep(TG_INTERVAL)

    return first_msg_id

def send_single_photo(photo_data, photo_ctype, chat_id, caption=""):
    """发送单张图片（封面到频道）"""
    ext = photo_ctype.split("/")[-1].replace("jpeg", "jpg")
    photo_data.seek(0)
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        files={"photo": (f"cover.{ext}", photo_data, photo_ctype)},
        timeout=30,
    )
    if r.ok:
        print("  ✅ 封面发送成功")
    else:
        print(f"  ❌ 封面发送失败: {r.text[:200]}")

# ==================== 主流程 ====================
def main():
    if not TOKEN or not CHAT_ID:
        print("❌ 缺少 TG_TOKEN / TG_CHAT_ID")
        sys.exit(1)

    print(f"\n🚀 xchina 抓取启动")
    group_username = get_group_username()
    seen = load_seen()
    current_page = load_page()
    print(f"📌 当前页码: {current_page}")

    if current_page < 1:
        print("✅ 全部页码已完成")
        return

    # 1. 获取列表页所有图集
    albums = get_albums_from_list(current_page)
    if not albums:
        print("❌ 列表页无内容，退出")
        sys.exit(1)

    # 从底部往上处理
    albums.reverse()

    new_albums = [a for a in albums if a["album_id"] not in seen]
    print(f"🆕 新图集: {len(new_albums)}/{len(albums)}")

    if not new_albums:
        print("本页已全部处理，翻到前一页")
        save_page(current_page - 1)
        return

    for idx, album in enumerate(new_albums):
        is_first = (idx == 0)
        print(f"\n{'='*55}")
        print(f"[{idx+1}/{len(new_albums)}] {album['title']}")

        # 2. 获取图集原图
        image_urls = get_images_from_album(album["url"], MAX_IMAGES)
        if not image_urls:
            print("  ⚠️ 无图片，跳过")
            seen.add(album["album_id"])
            continue

        # 第一个图集不限制数量，其余限制30张
        if not is_first:
            image_urls = image_urls[:MAX_IMAGES]

        # 3. 下载图片到内存
        print(f"  📥 下载 {len(image_urls)} 张...")
        downloaded = []
        for url in image_urls:
            data, ctype = download_image(url, referer=album["url"])
            if data:
                downloaded.append((data, ctype))
            time.sleep(0.3)

        print(f"  下载成功: {len(downloaded)}/{len(image_urls)}")
        if not downloaded:
            continue

        # 4. 发送到群组（除第一张封面外的所有图片）
        group_link = None
        if len(downloaded) > 1 and GROUP_ID:
            print(f"  📤 发送 {len(downloaded)-1} 张到群组...")
            first_id = send_media_group(downloaded[1:], GROUP_ID)
            if first_id and group_username:
                group_link = f"https://t.me/{group_username}/{first_id}"

        # 5. 发送封面到频道
        caption = build_caption(album["title"], group_link, is_first)
        print("  📸 发送封面到频道...")
        send_single_photo(downloaded[0][0], downloaded[0][1], CHAT_ID, caption)

        seen.add(album["album_id"])
        save_seen(seen)
        time.sleep(TG_INTERVAL)

    # 本页完成，页码 -1
    next_page = current_page - 1
    save_page(next_page)
    print(f"\n✅ 第{current_page}页完成，下次从第{next_page}页开始")

if __name__ == "__main__":
    main()
