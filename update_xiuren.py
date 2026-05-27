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

# ---------- 网站配置 ----------
BASE_URL = "https://www.xiurenai.com"
API_BASE = f"{BASE_URL}/api/v2"

# 机构页面默认参数（可能需要根据实际API调整）
JIGOU_TYPE = "jigou"          # 机构类型
MODELS_PER_PAGE = 20          # 每页模特数
ALBUMS_PER_MODEL = 5          # 每个模特最多下载5套图集
MAX_IMAGES_PER_ALBUM = 30     # 每套图集最多下载30张
REQUEST_INTERVAL = 2          # 请求间隔(秒)，防封
TG_INTERVAL = 15              # 每篇文章之间的间隔(秒)

# 已下载过的图集 theme_id，避免重复
SEEN_FILE = "seen_xiuren.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE_URL,
}

GROUP_USERNAME = None

# ---------- 辅助函数 ----------
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

def get_group_username():
    global GROUP_USERNAME
    if not GROUP_ID:
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getChat?chat_id={GROUP_ID}", timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            GROUP_USERNAME = r.json()["result"].get("username")
            if GROUP_USERNAME:
                print(f"✅ 群组用户名: @{GROUP_USERNAME}")
    except Exception as e:
        print(f"⚠️ 获取群组信息异常: {e}")

def safe_api_get(url, params=None):
    """带重试的API请求"""
    for _ in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 30)
                print(f"  ⚠️ 请求太频繁，等待 {retry_after} 秒...")
                time.sleep(retry_after)
                continue
            else:
                print(f"  ❌ API 错误 {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"  ❌ 网络异常: {e}")
        time.sleep(3)
    return None

# ---------- 核心抓取逻辑 ----------
def get_star_list(page=1):
    """获取机构下的模特列表"""
    url = f"{API_BASE}/star/list"
    params = {"page": page, "size": MODELS_PER_PAGE, "type": JIGOU_TYPE}
    data = safe_api_get(url, params)
    if data and data.get("code") == 0:
        return data["data"].get("list", [])
    return []

def get_albums_by_star(star_id, page=1):
    """获取某个模特的所有图集"""
    url = f"{API_BASE}/theme/list"
    params = {"page": page, "size": 20, "star_id": star_id}
    data = safe_api_get(url, params)
    if data and data.get("code") == 0:
        return data["data"].get("list", [])
    return []

def get_album_detail(theme_id):
    """获取图集详情（图片列表）"""
    url = f"{API_BASE}/theme/detail"
    params = {"theme_id": theme_id}
    data = safe_api_get(url, params)
    if data and data.get("code") == 0:
        return data["data"]
    return None

def download_image(url, referer=BASE_URL):
    """下载单张图片并返回 BytesIO"""
    try:
        h = {**HEADERS, "Referer": referer}
        r = requests.get(url, headers=h, timeout=15, verify=False)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "image/jpeg")
        if not ct.startswith("image/"):
            return None
        return BytesIO(r.content), ct
    except Exception as e:
        print(f"    ❌ 图片下载失败: {e}")
        return None

def send_media_groups(image_data_list, target_chat_id, caption=""):
    """将图片列表分批次发送（每批10张）"""
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
            except Exception as e:
                print(f"  ❌ 发送异常: {e}")
                break
        if not success:
            print(f"  ⚠️ 媒体组发送失败")
            break
        time.sleep(3)
    return first_msg_id

def process_album(theme_id, title):
    """处理单个图集：下载图片 → 发送到 TG"""
    print(f"  📸 处理图集: {title} (ID:{theme_id})")
    detail = get_album_detail(theme_id)
    if not detail:
        print("    ❌ 获取详情失败")
        return False

    image_urls = detail.get("images", [])[:MAX_IMAGES_PER_ALBUM]
    if not image_urls:
        print("    ❌ 无图片")
        return False

    # 下载图片
    downloaded = []
    for url in image_urls:
        res = download_image(url)
        if res:
            downloaded.append(res)
        time.sleep(0.1)

    if not downloaded:
        print("    ❌ 全部下载失败")
        return False

    # 智能选封面（文件最大 + 竖图优先）
    def select_cover(items):
        items_with_size = []
        for i, (data, ctype) in enumerate(items):
            data.seek(0, 2)
            size = data.tell()
            data.seek(0)
            items_with_size.append((i, size, data, ctype))
        items_with_size.sort(key=lambda x: x[1], reverse=True)
        # 简单竖图判断：高度大于宽度（这里未实现图像尺寸解析，直接选最大的）
        best_idx = items_with_size[0][0]
        cover = items[best_idx]
        rest = [item for idx, item in enumerate(items) if idx != best_idx]
        return cover, rest

    cover, rest = select_cover(downloaded)

    # 发封面到频道
    cover_data, cover_ctype = cover
    cover_data.seek(0)
    ext = cover_ctype.split("/")[-1].replace("jpeg", "jpg")
    caption = f"<b>{title}</b>\n\n👉 点击查看完整图集"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": (f"cover.{ext}", cover_data, cover_ctype)},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"    ❌ 频道发送失败: {r.text[:200]}")
            return False
        print("    ✅ 封面已发送")
    except Exception as e:
        print(f"    ❌ 频道异常: {e}")
        return False

    # 发送剩余图片到群组
    if rest and GROUP_ID:
        print(f"    📤 发送剩余 {len(rest)} 张到群组")
        first_msg_id = send_media_groups(rest, GROUP_ID, caption="📎 本组合集")
        # 这里可以生成群组链接，但为了简化忽略

    return True

def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 秀人机构抓取启动")
    if not TOKEN or not CHAT_ID:
        print("❌ 缺少 TG_TOKEN 或 TG_CHAT_ID")
        sys.exit(1)
    get_group_username()

    seen = load_seen()
    page = 1
    total_processed = 0

    while True:
        print(f"\n===== 抓取模特列表第 {page} 页 =====")
        stars = get_star_list(page)
        if not stars:
            print("没有更多模特，或请求失败。")
            break

        for star in stars:
            star_id = star["id"]
            star_name = star.get("name", "未知模特")
            print(f"\n👤 模特: {star_name} (ID:{star_id})")

            albums = get_albums_by_star(star_id, 1)
            if not albums:
                print("  暂无图集，跳过")
                continue

            # 只处理前 ALBUMS_PER_MODEL 套图集
            for album in albums[:ALBUMS_PER_MODEL]:
                theme_id = album["id"]
                if theme_id in seen:
                    print(f"  ⏭️ 图集 {album['title']} 已处理过")
                    continue

                title = album.get("title", "无标题")
                if process_album(theme_id, title):
                    seen.add(theme_id)
                    save_seen(seen)
                    total_processed += 1
                    print(f"  💾 已保存进度 (总 {total_processed})")
                time.sleep(TG_INTERVAL)

            time.sleep(REQUEST_INTERVAL)

        page += 1
        if page > 5:          # 最多抓5页模特，防止运行时间过长
            print("已抓取5页模特，结束。")
            break

    print(f"\n✅ 共处理 {total_processed} 套新图集")
    save_seen(seen)

if __name__ == "__main__":
    main()
