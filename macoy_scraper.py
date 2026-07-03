#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macoy_scraper.py
================
macoy.com 全商品を巡回してJSONに書き出す実運用スクレイパー。

使い方:
    pip install requests beautifulsoup4 lxml
    python macoy_scraper.py               # 通常実行
    python macoy_scraper.py --resume      # 中断からの再開
    python macoy_scraper.py --limit 50    # 各カテゴリ最大50商品でテスト
    python macoy_scraper.py --category Fezzes  # 単一カテゴリのみ

出力:
    products.json       - フロント (macoy-jp.html) が読む本体
    _crawl_state.json   - レジューム用の進捗ファイル
    _errors.log         - 失敗URLのログ

設計方針:
    1. 各カテゴリ一覧ページを取得 → 商品リンクを抽出
    2. ページネーションが検出できたら次ページも辿る
    3. 各商品詳細ページで name / price / image / stock を抽出
    4. カテゴリ1つ処理するたびに products.json を上書き保存 (中断安全)
    5. 429/5xxは指数バックオフでリトライ
    6. 礼儀正しい遅延 (デフォルト 1.2秒 / リクエスト)
"""

import argparse
import json
import logging
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ==================== 設定 ====================
BASE_URL = "https://www.macoy.com"
DELAY_MIN = 1.0
DELAY_MAX = 1.6
TIMEOUT = 25
MAX_RETRIES = 4
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15 MacoyScraperBot/1.0"
)

# 巡回する起点カテゴリ (macoy.comのメインナビ由来)
# ここに増やせば対象拡大。相対パスでOK。
SEED_CATEGORIES = {
    "Fezzes":              "/Fezzes",
    "Masonic-Aprons":      "/Masonic-Aprons",
    "Masonic-Store":       "/Masonic-Store",
    "OES-Sashes":          "/OES-Sashes",
    "OES-Supplies":        "/OES-Supplies",
    "Masonic-Rings":       "/Masonic-Rings-Jewelry",
    "Books":               "/Books",
    "Best-Sellers":        "/Books/Best-Sellers",
    "Sales-Clearance":     "/Great-Masonic-Deals-",
    "Masonic-Gifts":       "/Masonic-Gifts",
    "Car-Emblems":         "/Car-Emblems",
    "Gloves-Hats":         "/Gloves-Hats",
    "Accessories":         "/Accessories",
    "Masonic-Shirts":      "/Masonic-Shirts",
    "Macoy-Prints":        "/Macoy-Prints",
    "Ritual-Books":        "/Ritual-Books",
    "Bibles-Accessories":  "/Bibles-Accessories",
}

OUT_JSON  = Path("products.json")
STATE_JSON = Path("_crawl_state.json")
ERROR_LOG = Path("_errors.log")

# ==================== ロガー ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("macoy")

# ==================== HTTPセッション ====================
session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
})


def polite_sleep():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


def fetch(url: str) -> str | None:
    """GETしてHTML本文を返す。429/5xxはバックオフでリトライ。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt + random.random()
                log.warning(f"HTTP {r.status_code} on {url} — retry {attempt}/{MAX_RETRIES} in {wait:.1f}s")
                time.sleep(wait)
                continue
            log.error(f"HTTP {r.status_code} on {url}")
            return None
        except requests.RequestException as e:
            wait = 2 ** attempt
            log.warning(f"Exception on {url}: {e} — retry {attempt}/{MAX_RETRIES} in {wait}s")
            time.sleep(wait)
    log_error(url, "max retries exceeded")
    return None


def log_error(url: str, msg: str):
    with ERROR_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%F %T')} | {url} | {msg}\n")


# ==================== 抽出ロジック ====================
PRICE_RX = re.compile(r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)")

def absolutize(href: str) -> str:
    return urljoin(BASE_URL + "/", href.lstrip("/"))


def find_product_links(html: str) -> set[str]:
    """カテゴリ一覧ページから商品詳細URLを抽出。
    macoy.comの商品リンクは末尾が .aspx か /Product-Name-数字 のパターン。
    """
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        # 商品ページの典型パターン
        # 例: /Masonic-Aprons/Masonic-Hats-Fezzes-Fez-Cases/Plain-Fez-with-sweatband-5006.aspx
        # 例: /OES-Supplies/.../OES-Ceramic-Car-Coaster-6623.aspx
        if href.endswith(".aspx") and "/" in href and href.count("/") >= 2:
            # ナビや静的ページを除外
            if any(x in href.lower() for x in ["login", "cart", "contactus", "aboutus", "search", "wishlist"]):
                continue
            links.add(absolutize(href))
        # ID末尾なしのショートURLも一部あり
        elif re.search(r"/[A-Za-z][A-Za-z0-9\-]+-\d{3,6}$", href):
            links.add(absolutize(href))
    return links


def find_next_page(html: str, current_url: str) -> str | None:
    """一覧のページャーから "Next" リンクを探す。"""
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        text = (a.get_text() or "").strip().lower()
        if text in ("next", "next »", "next >", "»", ">"):
            return absolutize(a["href"])
    return None


def extract_product(html: str, url: str) -> dict | None:
    """商品詳細ページから構造化データを抽出。"""
    soup = BeautifulSoup(html, "lxml")

    # -- 商品名: h1 か og:title
    name = None
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        name = h1.get_text(strip=True)
    else:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            name = og["content"].strip()
    if not name or len(name) < 3:
        return None

    # -- 価格: 本文中の $XX.XX を抽出
    #    JSON-LDがあれば最優先
    price = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            offers = item.get("offers") if isinstance(item, dict) else None
            if isinstance(offers, dict) and offers.get("price"):
                try:
                    price = float(offers["price"])
                    break
                except (ValueError, TypeError):
                    pass
        if price:
            break

    if price is None:
        # フォールバック: 本文から $ を検索
        text = soup.get_text(" ", strip=True)
        m = PRICE_RX.search(text)
        if m:
            price = float(m.group(1))
    if price is None:
        return None

    # -- 画像: og:image か メイン画像
    image = None
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        image = og_img["content"].strip()
    else:
        img = soup.find("img", src=re.compile(r"/(ProductImages|Assets)/", re.I))
        if img:
            image = urljoin(BASE_URL + "/", img["src"])
    if image and image.startswith("//"):
        image = "https:" + image
    if not image:
        return None

    # -- 在庫ステータス (テキスト推定)
    body_text = soup.get_text(" ", strip=True).lower()
    if "out of stock" in body_text or "sold out" in body_text:
        stock = 0
    elif "add to cart" in body_text:
        stock = 2  # 即カート投入可 = 在庫あり
    elif "view details" in body_text or "add to wish" in body_text:
        stock = 1  # オプション選択必要
    else:
        stock = 1

    # -- カテゴリ推定: パンくずかURLパスから
    category = None
    crumbs = soup.select(".breadcrumb a, nav.breadcrumb a, [class*=breadcrumb] a")
    if crumbs:
        parts = [c.get_text(strip=True) for c in crumbs if c.get_text(strip=True)]
        if len(parts) >= 2:
            category = parts[-1] if parts[-1].lower() != "home" else parts[-2]
    if not category:
        # URLパスから第1セグメントを流用
        path_parts = urlparse(url).path.strip("/").split("/")
        if path_parts:
            category = path_parts[0].replace("-", " ")

    return {
        "n": name,
        "c": category or "その他",
        "p": round(price, 2),
        "s": stock,
        "img": image,
        "u": url,
    }


# ==================== 一覧クロール ====================
def crawl_category(seed_url: str, limit: int | None) -> list[str]:
    """カテゴリ配下のすべての商品URLを収集。"""
    visited_pages = set()
    product_urls: set[str] = set()
    queue = [seed_url]

    while queue:
        page = queue.pop(0)
        if page in visited_pages:
            continue
        visited_pages.add(page)

        log.info(f"  [list] {page}")
        html = fetch(page)
        polite_sleep()
        if not html:
            continue

        found = find_product_links(html)
        new = found - product_urls
        product_urls.update(new)
        log.info(f"        + {len(new)} products (total {len(product_urls)})")

        if limit and len(product_urls) >= limit:
            break

        # サブカテゴリ / ページャーを探す (単純な "Next" のみ、深追いしすぎない)
        nxt = find_next_page(html, page)
        if nxt and nxt not in visited_pages:
            queue.append(nxt)

    return sorted(product_urls)[:limit] if limit else sorted(product_urls)


# ==================== メイン ====================
def load_state() -> dict:
    if STATE_JSON.exists():
        return json.loads(STATE_JSON.read_text(encoding="utf-8"))
    return {"done_categories": [], "products": []}


def save_state(state: dict):
    STATE_JSON.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def save_products(products: list[dict]):
    """フロントが読むJSONを整形して保存。"""
    # 重複除去 (URL基準)
    seen = set()
    unique = []
    for p in products:
        if p["u"] in seen:
            continue
        seen.add(p["u"])
        unique.append(p)
    OUT_JSON.write_text(
        json.dumps({"generated_at": time.strftime("%F %T"), "count": len(unique), "items": unique},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log.info(f"💾 products.json 保存: {len(unique)}点")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", action="store_true", help="中断状態から再開")
    ap.add_argument("--limit", type=int, help="各カテゴリで最大何商品まで取るか (テスト用)")
    ap.add_argument("--category", help="特定カテゴリ名のみ実行 (例: Fezzes)")
    args = ap.parse_args()

    state = load_state() if args.resume else {"done_categories": [], "products": []}
    log.info(f"開始: 既存 {len(state['products'])}件 / 完了カテゴリ {len(state['done_categories'])}件")

    categories = SEED_CATEGORIES.items()
    if args.category:
        categories = [(args.category, SEED_CATEGORIES.get(args.category, f"/{args.category}"))]

    for cat_name, cat_path in categories:
        if cat_name in state["done_categories"] and not args.category:
            log.info(f"⏭  {cat_name} は完了済み。スキップ")
            continue

        log.info(f"\n=== カテゴリ: {cat_name} ({cat_path}) ===")
        seed = absolutize(cat_path)

        try:
            product_urls = crawl_category(seed, args.limit)
        except KeyboardInterrupt:
            log.warning("Ctrl+C で中断。現状を保存します")
            save_state(state)
            save_products(state["products"])
            return

        log.info(f"→ {cat_name}: 商品URL {len(product_urls)}件を取得。詳細フェッチ開始")

        for i, url in enumerate(product_urls, 1):
            if any(p["u"] == url for p in state["products"]):
                continue
            html = fetch(url)
            polite_sleep()
            if not html:
                continue
            try:
                item = extract_product(html, url)
            except Exception as e:
                log_error(url, f"parse error: {e}")
                continue
            if item:
                state["products"].append(item)
                if i % 5 == 0:
                    log.info(f"    {i}/{len(product_urls)} … 現在 {len(state['products'])}点")
                    save_products(state["products"])
                    save_state(state)

        state["done_categories"].append(cat_name)
        save_products(state["products"])
        save_state(state)
        log.info(f"✔ {cat_name} 完了 (累計 {len(state['products'])}点)")

    log.info(f"\n🎉 全カテゴリ完了。products.json に {len(state['products'])}点書き出し")


if __name__ == "__main__":
    main()
