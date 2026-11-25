"""
丸久小山園 抹茶補貨追蹤 → LINE 官方帳號推播

用法：
  python matcha_tracker.py            # 檢查一次（給 GitHub Actions / 排程用）
  python matcha_tracker.py --loop 600 # 每 600 秒檢查一次（自己電腦一直開著用）
  python matcha_tracker.py --test     # 只送一則測試訊息到 LINE，確認串接成功

需要的環境變數：
  LINE_CHANNEL_ACCESS_TOKEN  LINE Developers → Messaging API → Channel access token
  LINE_USER_ID               LINE Developers → Basic settings → Your user ID（U 開頭）
                             可用逗號放多個 ID；留空則改用 broadcast 發給所有好友
可選：
  WATCH_KEYWORDS             只追蹤名稱含這些字的商品，逗號分隔，例如 "Wako,Kinrin,Unkaku"
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CATALOG_URL = "https://www.marukyu-koyamaen.co.jp/english/shop/products/catalog/matcha"
STATE_FILE = Path(__file__).with_name("state.json")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
}


# ---------- 抓商品與庫存 ----------
def fetch_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse_products(html: str) -> dict:
    """回傳 {商品網址: {"name":..., "in_stock": bool}}"""
    soup = BeautifulSoup(html, "html.parser")
    products = {}

    # 官網是 WooCommerce：每個商品是 <li class="product ... instock/outofstock">
    for li in soup.select("li.product"):
        a = li.select_one("a[href]")
        if not a:
            continue
        url = a["href"].split("?")[0]
        name_el = li.select_one(
            ".woocommerce-loop-product__title, .product-name, h2, h3"
        ) or a
        name = re.sub(r"\s+", " ", name_el.get_text(" ", strip=True))

        classes = li.get("class", [])
        text = li.get_text(" ", strip=True).lower()
        if "outofstock" in classes:
            in_stock = False
        elif "instock" in classes:
            in_stock = True
        else:
            in_stock = not ("out of stock" in text or "sold out" in text)

        products[url] = {"name": name, "in_stock": in_stock}
    return products


def fetch_all_products() -> dict:
    """含分頁一起抓"""
    products, url, seen = {}, CATALOG_URL, set()
    while url and url not in seen:
        seen.add(url)
        html = fetch_html(url)
        products.update(parse_products(html))
        nxt = BeautifulSoup(html, "html.parser").select_one("a.next, a.next.page-numbers")
        url = nxt["href"] if nxt else None
        time.sleep(2)
    return products


# ---------- LINE ----------
def send_line(text: str) -> None:
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    user_ids = [u.strip() for u in os.environ.get("LINE_USER_ID", "").split(",") if u.strip()]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    messages = [{"type": "text", "text": text[:4900]}]

    if user_ids:
        endpoint = "https://api.line.me/v2/bot/message/multicast"
        body = {"to": user_ids, "messages": messages}
    else:
        endpoint = "https://api.line.me/v2/bot/message/broadcast"
        body = {"messages": messages}

    r = requests.post(endpoint, headers=headers, json=body, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"LINE 發送失敗 {r.status_code}: {r.text}")


# ---------- 主流程 ----------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def check_once() -> None:
    keywords = [k.strip().lower() for k in os.environ.get("WATCH_KEYWORDS", "").split(",") if k.strip()]
    current = fetch_all_products()
    if not current:
        raise RuntimeError("沒抓到任何商品，可能網站改版或被擋，請檢查。")

    if keywords:
        current = {u: p for u, p in current.items()
                   if any(k in p["name"].lower() or k in u.lower() for k in keywords)}

    previous = load_state()
    first_run = not previous

    restocked = [
        (p["name"], url) for url, p in current.items()
        if p["in_stock"] and (url in previous and not previous[url]["in_stock"])
    ]
    new_items = [
        (p["name"], url) for url, p in current.items()
        if p["in_stock"] and url not in previous and not first_run
    ]

    in_count = sum(p["in_stock"] for p in current.values())
    print(f"共 {len(current)} 項，有貨 {in_count} 項；補貨 {len(restocked)}，新上架 {len(new_items)}")

    if first_run:
        lines = [f"🍵 抹茶追蹤已啟動，目前有貨 {in_count}/{len(current)} 項："]
        lines += [f"・{p['name']}" for p in current.values() if p["in_stock"]] or ["（目前全部缺貨）"]
        send_line("\n".join(lines))
    elif restocked or new_items:
        lines = ["🍵 丸久小山園抹茶補貨了！"]
        lines += [f"\n✅ {n}\n{u}" for n, u in restocked]
        lines += [f"\n🆕 新上架：{n}\n{u}" for n, u in new_items]
        send_line("\n".join(lines))

    save_state(current)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, help="每 N 秒檢查一次")
    ap.add_argument("--test", action="store_true", help="送測試訊息")
    args = ap.parse_args()

    if args.test:
        send_line("✅ 測試成功！抹茶補貨通知已串接到 LINE。")
        print("已送出測試訊息")
        return

    if not args.loop:
        check_once()
        return

    while True:
        try:
            check_once()
        except Exception as e:  # 單次失敗不中斷
            print("錯誤：", e, file=sys.stderr)
        time.sleep(max(args.loop, 300))  # 最少 5 分鐘，避免對官網太頻繁


if __name__ == "__main__":
    main()
