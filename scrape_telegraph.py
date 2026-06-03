#!/usr/bin/env python3
"""
xchina.co 秀人网图集 → Telegra.ph + 频道封面
直接嵌入原图链接，无需上传 Telegra.ph
每天往前一页（起始 49），每图集前 30 张，末尾引导加入会员群
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
VIP_LINK   = os.getenv("VIP_LINK", "https://t.me/xiuren88bot?start=lWXnjXFzdxP").strip()
CF_COOKIE  = os.getenv("CF_COOKIE", "")

BASE_URL   = "https://xchina.co"
SERIES_URL = "https://xchina.co/photos/series-5f1476781eab4/{page}.html"
START_PAGE = 49
PAGE_FILE  = "next_page.txt"
SEEN_FILE  = "seen_xchina.json"
MAX_IMAGES = 30
TG_INTERVAL = 5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.verify = False

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

# ==================== 网络工具 ====================
def safe_get(url, retries=3, timeout=20):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            elif r.status_code == 403:
                print(f"  ❌ 403 Forbidden: {url}")
                return None
            elif r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 30))
                print(f"  ⚠️ 限流 {wait}s")
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
    url = SERIES_URL.format(page=page)
    print(f"📄 列表页: {url}")
    r = safe_get(url)
    if not r:
        return []

    if "cloudflare" in r.text.lower() and len(r.text) < 5000:
        print("❌ 被 Cloudflare 拦截，请更新 Cookie")
        with open("debug_list.html", "w", encoding="utf-8") as f:
            f.write(r.text)
        sys.exit(1)

    soup = BeautifulSoup(r.text, "html.parser")
    albums = []
    seen_ids = set()

    for item in soup.select("div.list.photo-list div.item.photo"):
        a_tag = item.find("a", href=re.compile(r'/photo/id-[a-f0-9]+\.html'))
        if not a_tag:
            continue
        detail_url = fix_url(a_tag["href"])
        album_id = re.search(r'/photo/id-([a-f0-9]+)\.html', detail_url).group(1)
        if album_id in seen_ids:
            continue
        seen_ids.add(album_id)

        albums.append({
            "album_id": album_id,
            "url": detail_url,
        })

    print(f"  找到 {len(albums)} 个图集")
    return albums

# ==================== 图集详情 + 原图链接 ====================
def parse_album_detail(album_url):
    r = safe_get(album_url)
    if not r:
        return None, None
    soup = BeautifulSoup(r.text, "html.parser")

    info = {"model": "", "series": "秀人网", "vol": "", "date": "", "title": ""}
    detail = soup.select_one(".info-card.photo-detail")
    if detail:
        items = detail.select(".item")
        for item in items:
            icon = item.select_one(".icon i")
            if not icon:
                continue
            classes = icon.get("class", [])
            text_el = item.select_one(".text")
            text = text_el.get_text(strip=True) if text_el else ""
            if "fa-address-card" in classes:
                info["model"] = text
            elif "fa-video-camera" in classes:
                a = item.find("a")
                if a:
                    info["series"] = a.get_text(strip=True)
            elif "fa-file" in classes:
                info["vol"] = text
            elif "fa-calendar-days" in classes:
                info["date"] = text

    series_clean = info["series"].strip() or "秀人网"
    if series_clean == "秀人网":
        series_clean = "Xiuren秀人网"
    else:
        series_clean = "Xiuren" + series_clean

    date_str = info["date"].strip()
    vol_str = info["vol"].strip()
    model_str = info["model"].strip()

    title_parts = [f"[{series_clean}]"]
    if date_str:
        title_parts.append(date_str)
    if vol_str:
        title_parts.append(vol_str)
    if model_str:
        title_parts.append(f"#{model_str}")
    info["title"] = " ".join(title_parts)

    return info, soup

def get_image_urls_from_album(album_url, max_images=MAX_IMAGES):
    """返回原图URL列表和info"""
    info, first_soup = parse_album_detail(album_url)
    if not info:
        info = {"title": "无标题", "model": "", "series": "秀人网", "vol": "", "date": ""}

    collected_urls = []
    page = 1
    while len(collected_urls) < max_images:
        if page == 1:
            soup = first_soup
            if soup is None:
                break
        else:
            if album_url.endswith(".html"):
                page_url = album_url[:-5] + f"/{page}.html"
            else:
                break
            print(f"  📂 分页 {page}: {page_url}")
            r = safe_get(page_url)
            if not r:
                break
            soup = BeautifulSoup(r.text, "html.parser")

        items = soup.select("div.list.photo-items div.item.photo-image")
        if not items:
            break

        for item in items:
            if len(collected_urls) >= max_images:
                break
            img_div = item.find("div", class_="img")
            if not img_div:
                continue
            style = img_div.get("style", "")
            m = re.search(r"url\(['\"]?([^'\")\s]+)['\"]?\)", style)
            if not m:
                continue
            thumb_url = m.group(1)
            # 构造原图链接
            filename = os.path.basename(thumb_url)
            name_no_dim = re.sub(r'_\d+x\d*', '', filename)
            name_jpg = os.path.splitext(name_no_dim)[0] + ".jpg"
            orig_url = thumb_url.rsplit("/", 1)[0] + "/" + name_jpg
            if orig_url not in collected_urls:
                collected_urls.append(orig_url)

        next_link = soup.select_one("div.pager a.next:not(.disabled)")
        if not next_link:
            break
        page += 1
        time.sleep(0.5)

    return collected_urls[:max_images], info

# ==================== 下载封面用 ====================
def download_cover(url, referer):
    """下载第一张图作为封面，返回 BytesIO 和 content-type"""
    for attempt in range(3):
        try:
            r = SESSION.get(url, headers={"Referer": referer}, timeout=30)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "image/jpeg")
            if not ct.startswith("image/") or len(r.content) < 2000:
                continue
            return BytesIO(r.content), ct
        except Exception as e:
            print(f"    ❌ 封面下载失败 ({attempt+1}/3): {e}")
            time.sleep(1)
    return None, None

# ==================== Telegra.ph 页面（直接嵌入原图URL） ====================
def create_telegraph_page(title, image_urls, vip_link=None):
    """直接用原图URL创建 Telegraph 页面"""
    if not image_urls:
        return None

    content = [{"tag": "img", "attrs": {"src": url}} for url in image_urls]

    if vip_link:
        vip_node = {
            "tag": "p",
            "children": [
                "查看完整版图集，点击 ",
                {
                    "tag": "a",
                    "attrs": {"href": vip_link},
                    "children": ["加入会员群"]
                }
            ]
        }
        content.append(vip_node)

    data = {
        "access_token": "anonymous",
        "title": title,
        "author_name": "XiuRen Bot",
        "content": content,
        "return_content": False,
    }
    try:
        r = requests.post("https://api.telegra.ph/createPage", json=data, timeout=30)
        if r.status_code == 200:
            result = r.json()
            if result.get("ok") and "result" in result:
                return result["result"]["url"]
        print(f"    ❌ 创建页面失败: {r.text[:200]}")
    except Exception as e:
        print(f"    ❌ 创建异常: {e}")
    return None

# ==================== Telegram 发送封面 ====================
def send_photo_to_channel(photo_data, photo_ctype, caption):
    ext = photo_ctype.split("/")[-1].replace("jpeg", "jpg")
    photo_data.seek(0)
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
        data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
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

    print(f"✅ 会员群引导链接: {VIP_LINK}")
    print(f"\n🚀 xchina 图集抓取 → Telegra.ph + 频道")
    seen = load_seen()
    current_page = load_page()
    print(f"📌 当前页码: {current_page}")

    if current_page < 1:
        print("✅ 全部页码完成")
        return

    albums = get_albums_from_list(current_page)
    if not albums:
        print("❌ 列表页无内容，退出")
        sys.exit(1)

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
        print(f"[{idx+1}/{len(new_albums)}] 开始处理 {album['url']}")

        # 获取原图链接列表
        image_urls, info = get_image_urls_from_album(album["url"], MAX_IMAGES)
        title = info.get("title", "无标题")
        print(f"  📝 标题: {title}")

        if not image_urls:
            print("  ⚠️ 无图片，跳过")
            seen.add(album["album_id"])
            continue

        if not is_first:
            image_urls = image_urls[:MAX_IMAGES]

        # 下载第一张作为封面
        print(f"  📥 下载封面...")
        cover_data, cover_type = download_cover(image_urls[0], referer=album["url"])
        if not cover_data:
            print("  ⚠️ 封面下载失败，跳过该图集")
            continue

        # 创建 Telegraph 页面（直接使用原图链接）
        print("  📝 创建 Telegraph 页面...")
        telegraph_url = create_telegraph_page(title, image_urls, vip_link=VIP_LINK)
        if not telegraph_url:
            print("  ❌ 创建页面失败，跳过")
            continue
        print(f"  ✅ 页面: {telegraph_url}")

        # 发送封面到频道
        caption = f"<b>{title}</b>\n\n<a href=\"{telegraph_url}\">📖 查看更多图集</a>"
        print("  📸 发送封面到频道...")
        send_photo_to_channel(cover_data, cover_type, caption)

        seen.add(album["album_id"])
        save_seen(seen)
        time.sleep(TG_INTERVAL)

    next_page = current_page - 1
    save_page(next_page)
    print(f"\n✅ 第{current_page}页完成，下次从第{next_page}页开始")

if __name__ == "__main__":
    main()
