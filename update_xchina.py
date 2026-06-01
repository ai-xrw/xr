"""
xchina.co 秀人网 每日一页图集抓取 + Telegram 推送
绕过 Cloudflare：用浏览器 Cookie 直接注入，不再依赖 Playwright
"""

import requests
from bs4 import BeautifulSoup
from io import BytesIO
import os, re, time, json, sys
import urllib3

urllib3.disable_warnings()

# ===================== 配置 =====================
TOKEN    = os.getenv("TG_TOKEN")
CHAT_ID  = os.getenv("TG_CHAT_ID")
GROUP_ID = os.getenv("TG_GROUP_ID", "")
VIP_LINK = os.getenv("VIP_LINK", "")          # VIP 群链接，填入 Secret 或直接写死
CF_COOKIE = os.getenv("CF_COOKIE", "")        # 从浏览器复制的完整 Cookie 字符串

BASE_URL       = "https://xchina.co"
SERIES_URL     = "https://xchina.co/photos/series-5f1476781eab4/sort-vol/{page}.html"
START_PAGE     = 48
PAGE_FILE      = "next_page.txt"
SEEN_FILE      = "seen_xchina.json"
MAX_IMAGES     = 30    # 第一套不限，后续限制
TG_INTERVAL    = 15    # 发送间隔秒

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Upgrade-Insecure-Requests": "1",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.verify = False

def _inject_cookies(cookie_str):
    """解析 'k=v; k2=v2' 格式并注入到 Session"""
    if not cookie_str:
        print("⚠️  CF_COOKIE 未设置，可能被 Cloudflare 拦截")
        return
    count = 0
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            SESSION.cookies.set(k.strip(), v.strip(), domain="xchina.co")
            count += 1
    print(f"✅  注入 {count} 个 Cookie")

_inject_cookies(CF_COOKIE)
GROUP_USERNAME = None

# ===================== 状态管理 =====================

def load_page():
    if not os.path.exists(PAGE_FILE):
        return START_PAGE
    try:
        n = int(open(PAGE_FILE).read().strip())
        return max(n, 1)
    except:
        return START_PAGE

def save_page(page):
    with open(PAGE_FILE, "w") as f:
        f.write(str(page))

def load_seen():
    if not os.path.exists(SEEN_FILE) or os.path.getsize(SEEN_FILE) == 0:
        return set()
    try:
        return set(json.load(open(SEEN_FILE, encoding="utf-8")))
    except:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)

# ===================== 工具 =====================

def safe_get(url, retries=3, timeout=20):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            elif r.status_code == 403:
                print(f"  ❌  403 Forbidden（Cookie 可能过期）: {url}")
                return None
            elif r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 30))
                print(f"  ⚠️  限流，等待 {wait}s")
                time.sleep(wait)
            else:
                print(f"  ⚠️  HTTP {r.status_code}: {url}")
                time.sleep(2)
        except Exception as e:
            print(f"  ❌  请求异常 ({i+1}/{retries}): {e}")
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

# ===================== Step 1: 列表页 → 图集列表 =====================
# 结构（来自文档）:
#   div.list.photo-list > div.item.photo
#     a[href="/photo/id-xxx.html"]   ← 详情链接
#     div.img[style="background-image:url('...')"]  ← 封面
#     div.title > a  ← 标题

def get_albums_from_list(page_url):
    print(f"📄  列表页: {page_url}")
    r = safe_get(page_url)
    if not r:
        return []

    # 检测是否被 CF 拦截
    if "cloudflare" in r.text.lower() and len(r.text) < 5000:
        print("❌  被 Cloudflare 拦截，Cookie 可能已过期，请重新复制")
        print(f"   响应内容前200字: {r.text[:200]}")
        sys.exit(1)

    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select("div.list.photo-list div.item.photo")
    print(f"  找到 {len(items)} 个图集")

    if len(items) == 0:
        # 保存 HTML 便于调试
        with open("debug_list.html", "w", encoding="utf-8") as f:
            f.write(r.text)
        print("  ⚠️  未找到图集，已保存 debug_list.html，请检查页面结构")
        return []

    albums = []
    seen_ids = set()
    for item in items:
        # 详情链接
        a_tag = item.find("a", href=re.compile(r'/photo/id-[a-f0-9]+\.html'))
        if not a_tag:
            continue
        detail_url = fix_url(a_tag.get("href"))
        album_id = a_tag["href"].split("/")[-1].replace(".html", "")
        if album_id in seen_ids:
            continue
        seen_ids.add(album_id)

        # 封面（CSS background-image）
        cover_url = ""
        img_div = item.find("div", class_="img")
        if img_div:
            style = img_div.get("style", "")
            m = re.search(r"url\(['\"]?([^'\")\s]+)['\"]?\)", style)
            if m:
                cover_url = fix_url(m.group(1))

        # 标题
        title_div = item.find("div", class_="title")
        title = title_div.a.get_text(strip=True) if title_div and title_div.a else "无标题"

        albums.append({
            "album_id": album_id,
            "url": detail_url,
            "cover_url": cover_url,
            "title": title,
        })

    return albums


# ===================== Step 2: 详情页 → 图片列表 =====================

def get_images_from_detail(detail_url):
    """进入图集详情页，提取所有大图 URL"""
    print(f"  📂  详情页: {detail_url}")
    r = safe_get(detail_url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    images = []
    seen = set()

    # 内容区
    content = None
    for sel in [".photo-content", ".content", "article", "main", ".pics"]:
        content = soup.select_one(sel)
        if content:
            break

    for img in (content or soup).find_all("img"):
        src = (img.get("data-src") or img.get("data-original")
               or img.get("data-lazy-src") or img.get("src") or "")
        src = fix_url(src)
        if not src:
            continue
        if re.search(r"(logo|icon|avatar|banner|watermark)", src, re.I):
            continue
        if not re.search(r"\.(jpg|jpeg|png|webp)", src, re.I):
            continue
        if src not in seen:
            images.append(src)
            seen.add(src)

    # 检查详情页内分页
    inner_pages = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r'/photo/id-[a-f0-9]+\.html\?p=(\d+)', a["href"])
        if m:
            inner_pages.add(fix_url(a["href"]))

    for sub_url in sorted(inner_pages):
        sub_r = safe_get(sub_url)
        if sub_r:
            sub_soup = BeautifulSoup(sub_r.text, "html.parser")
            for img in sub_soup.find_all("img"):
                src = (img.get("data-src") or img.get("data-original") or img.get("src") or "")
                src = fix_url(src)
                if src and src not in seen:
                    if re.search(r"\.(jpg|jpeg|png|webp)", src, re.I):
                        if not re.search(r"(logo|icon|avatar|banner)", src, re.I):
                            images.append(src)
                            seen.add(src)
        time.sleep(1)

    print(f"    共 {len(images)} 张图片")
    return images


# ===================== Step 3: 下载图片 =====================

def download_image(url, referer):
    for attempt in range(3):
        try:
            r = SESSION.get(url, headers={"Referer": referer}, timeout=30)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "image/jpeg")
            if not ct.startswith("image/") or len(r.content) < 2000:
                return None, None
            return BytesIO(r.content), ct
        except Exception as e:
            print(f"    ❌  下载失败 ({attempt+1}/3): {e}")
            time.sleep(1)
    return None, None


# ===================== Step 4: Telegram 发送 =====================

def get_group_username():
    global GROUP_USERNAME
    if not GROUP_ID:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getChat?chat_id={GROUP_ID}",
            timeout=10
        )
        if r.ok:
            GROUP_USERNAME = r.json()["result"].get("username")
            if GROUP_USERNAME:
                print(f"✅  群组: @{GROUP_USERNAME}")
    except Exception as e:
        print(f"⚠️  获取群组信息失败: {e}")


def build_tags(title):
    """从标题提取标签，如 'Vol. 10802' → '#Vol10802'"""
    tags = []
    for word in re.findall(r'[\w\u4e00-\u9fff]+', title):
        if re.match(r'^\d+$', word):
            continue  # 跳过纯数字
        tags.append(f"#{word}")
    # 保留前 5 个
    return " ".join(tags[:5])


def build_caption(title, group_link=None, vip_link=None, is_first=False):
    tags = build_tags(title)
    text = f"<b>{title}</b>\n{tags}"
    if group_link:
        text += f'\n\n👉 <a href="{group_link}">点击查看图集</a>'
    if vip_link and not is_first:
        text += f'\n🌟 <a href="{vip_link}">点我进VIP群查看完整版</a>'
    return text


def send_media_group(image_data_list, target_chat_id, caption=""):
    """每批最多 10 张"""
    if not image_data_list:
        return None
    first_msg_id = None

    for start in range(0, len(image_data_list), 10):
        batch = image_data_list[start:start + 10]
        media, files = [], {}

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
                    print(f"  ✅  批次 {start//10+1} 发送成功 ({len(res)} 张)")
                    break
                elif r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 30)
                    print(f"  ⚠️  限流，等待 {wait}s")
                    time.sleep(wait)
                    for d, _ in batch:
                        d.seek(0)
                else:
                    print(f"  ❌  发送失败 ({r.status_code}): {r.text[:300]}")
                    break
            except Exception as e:
                print(f"  ❌  发送异常: {e}")
                time.sleep(3)

        time.sleep(TG_INTERVAL)

    return first_msg_id


def send_channel_cover(cover_data, cover_ctype, caption):
    """发封面到频道"""
    ext = cover_ctype.split("/")[-1].replace("jpeg", "jpg")
    cover_data.seek(0)
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
        data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
        files={"photo": (f"cover.{ext}", cover_data, cover_ctype)},
        timeout=30,
    )
    if r.ok:
        print(f"  ✅  频道封面发送成功")
    else:
        print(f"  ❌  频道发送失败: {r.text[:300]}")


# ===================== 主流程 =====================

def main():
    if not TOKEN or not CHAT_ID:
        print("❌  缺少 TG_TOKEN / TG_CHAT_ID")
        sys.exit(1)

    print(f"\n🚀  xchina 秀人网抓取启动")
    get_group_username()

    seen = load_seen()
    current_page = load_page()
    print(f"📌  当前页码: {current_page}")

    if current_page < 1:
        print("✅  所有页码已处理完毕")
        sys.exit(0)

    # Step 1: 获取本页图集列表
    page_url = SERIES_URL.format(page=current_page)
    albums = get_albums_from_list(page_url)

    if not albums:
        print("❌  列表页无图集（可能被拦截或页面结构变化）")
        sys.exit(1)

    # 从底部往上处理（倒序）
    albums.reverse()

    # 过滤已处理
    new_albums = [a for a in albums if a["album_id"] not in seen]
    print(f"🆕  新图集: {len(new_albums)}/{len(albums)}")

    if not new_albums:
        print("本页已全部处理过，翻到下一页")
        save_page(current_page - 1)
        sys.exit(0)

    # Step 2-4: 逐个处理
    for idx, album in enumerate(new_albums):
        is_first = (idx == 0)
        print(f"\n{'='*55}")
        print(f"[{idx+1}/{len(new_albums)}] {album['title']}")

        # 获取图片列表
        image_urls = get_images_from_detail(album["url"])
        if not image_urls:
            print("  ⚠️  没有图片，跳过")
            seen.add(album["album_id"])
            continue

        # 限制图片数量
        if not is_first:
            image_urls = image_urls[:MAX_IMAGES]

        # 下载
        print(f"  📥  下载 {len(image_urls)} 张 ...")
        downloaded = []
        for url in image_urls:
            data, ctype = download_image(url, referer=album["url"])
            if data:
                downloaded.append((data, ctype))
            time.sleep(0.3)

        print(f"  下载成功: {len(downloaded)}/{len(image_urls)}")
        if not downloaded:
            continue

        # 发送到群组（除封面外）
        group_link = None
        rest = downloaded[1:]
        if rest and GROUP_ID:
            print(f"  📤  发送 {len(rest)} 张到群组 ...")
            first_id = send_media_group(rest, GROUP_ID)
            if first_id and GROUP_USERNAME:
                group_link = f"https://t.me/{GROUP_USERNAME}/{first_id}"

        # 发封面到频道
        cover_data, cover_ctype = downloaded[0]
        caption = build_caption(
            album["title"],
            group_link=group_link,
            vip_link=VIP_LINK,
            is_first=is_first,
        )
        print("  📸  发送封面到频道 ...")
        send_channel_cover(cover_data, cover_ctype, caption)

        seen.add(album["album_id"])
        save_seen(seen)
        time.sleep(TG_INTERVAL)

    # 本页处理完毕，页码 -1
    next_page = current_page - 1
    save_page(next_page)
    print(f"\n✅  本页完成，下次从第 {next_page} 页开始")


if __name__ == "__main__":
    main()
