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
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ==================== 設定 ====================
BASE_URL = "https://www.macoy.com"
DELAY_MIN = 1.0
DELAY_MAX = 1.6
TIMEOUT = 25
MAX_RETRIES = 4
MAX_SUBCATEGORY_DEPTH = 3  # サブカテゴリ一覧を辿る最大パス深度 (末端商品ページの暴走巡回を防止)
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
IMAGES_DIR = Path("images")

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

# 画像取得専用セッション。ホットリンクブロック回避のため Referer を付与する。
img_session = requests.Session()
img_session.headers.update({
    "User-Agent": USER_AGENT,
    "Referer": "https://www.macoy.com/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
})


def polite_sleep():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


# ==================== 画像ダウンロード ====================
def sanitize_filename(name: str) -> str:
    """URL由来のファイル名を安全なローカル名に整形。"""
    name = unquote(name)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_.")
    return name or "img"


def _fetch_one_image(image_url: str) -> str | None:
    """単一URLをダウンロードして images/<filename> を返す。失敗時 None。
    既に同名ファイルがあれば再取得しない (レジューム/重複対策)。
    """
    fname = sanitize_filename(Path(urlparse(image_url).path).name)
    if not fname or "." not in fname:
        return None
    dest = IMAGES_DIR / fname
    if dest.exists() and dest.stat().st_size > 0:
        return f"images/{fname}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = img_session.get(image_url, timeout=TIMEOUT)
        except requests.RequestException as e:
            log.warning(f"image download failed {image_url}: {e}")
            return None
        if r.status_code == 200 and r.content:
            IMAGES_DIR.mkdir(exist_ok=True)
            dest.write_bytes(r.content)
            return f"images/{fname}"
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt + random.random()
            log.warning(f"image HTTP {r.status_code} on {image_url} — retry {attempt}/{MAX_RETRIES} in {wait:.1f}s")
            time.sleep(wait)
            continue
        log.info(f"image HTTP {r.status_code} (次候補へ): {image_url}")
        return None
    return None


def download_image(candidates: list[str]) -> str | None:
    """候補URLを順に試し、最初に取得できた画像の images/<filename> を返す。"""
    for url in candidates:
        if not url or not url.startswith("http"):
            continue
        local = _fetch_one_image(url)
        if local:
            return local
    return None


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


# カテゴリ以外(アカウント/情報ページ等)を除外するためのパスキーワード
NON_CATEGORY_KEYWORDS = (
    "login", "cart", "checkout", "wishlist", "account", "register", "members",
    "contactus", "contact", "aboutus", "about", "default", "search", "sitemap",
    "privacy", "terms", "return-policy", "faq", "email", "cdn-cgi", "sign-up",
    "custom-embroidery", "grand-lodge-of-virginia", "learn-more", "meaning-of",
    "historical-sketch", "secret-discipline", "albert-mackey", "compare",
    "review", "tell-a-friend", "buyproduct",
)


def is_category_path(path: str) -> bool:
    """URLパスがカテゴリ/サブカテゴリ一覧ページらしいか判定 (.aspx商品や情報ページを除外)。"""
    last = path.rstrip("/").split("/")[-1]
    if not last or "." in last:  # ファイル/.aspx は対象外
        return False
    low = path.lower()
    return not any(x in low for x in NON_CATEGORY_KEYWORDS)


def looks_like_product(html: str) -> bool:
    """ページ自体が商品ページか判定 (JSON-LDにProduct型＋offers.priceがあるか)。
    macoy.comの書籍などは .aspx でないスラッグURLの商品ページのため、
    リンクパターンでは拾えず、この判定で捕捉する。
    """
    soup = BeautifulSoup(html, "lxml")
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            t = item.get("@type", "")
            is_product = t == "Product" or (isinstance(t, list) and "Product" in t)
            offers = item.get("offers")
            if is_product and isinstance(offers, dict) and offers.get("price"):
                return True
    return False


def find_subcategory_links(html: str) -> set[str]:
    """一覧ページから配下のサブカテゴリ一覧ページへのリンクを抽出。"""
    soup = BeautifulSoup(html, "lxml")
    subs = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        full = absolutize(href)
        if not full.startswith(BASE_URL):
            continue
        if is_category_path(urlparse(full).path):
            subs.add(full)
    return subs


def discover_seed_categories() -> dict[str, str]:
    """トップページのナビゲーションから実在するカテゴリURLを自動発見。
    ハードコードSEEDには404が混じるため、これを優先的に使う。
    """
    html = fetch(BASE_URL + "/")
    polite_sleep()
    if not html:
        log.warning("トップページ取得失敗 — ハードコードSEEDにフォールバック")
        return dict(SEED_CATEGORIES)
    seeds: dict[str, str] = {}
    for full in find_subcategory_links(html):
        path = urlparse(full).path
        name = path.strip("/").replace("/", "__")
        seeds[name] = path
    log.info(f"ナビから {len(seeds)} カテゴリ/サブカテゴリを自動発見")
    # ハードコードSEEDも(存在すれば)補完的にマージ
    for name, path in SEED_CATEGORIES.items():
        seeds.setdefault(name, path)
    return seeds


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

    # -- 価格・画像候補: JSON-LD (schema.org/Product) を最優先で一括抽出。
    #    画像は複数候補を集めておき、後で実際に取得できたものを採用する
    #    (サイトによって /images/product/large/ が404で /Assets/... が有効なため)。
    price = None
    img_candidates: list[str] = []

    def add_candidate(url: str | None):
        if not url:
            return
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http"):
            url = urljoin(BASE_URL + "/", url.lstrip("/"))
        if url not in img_candidates:
            img_candidates.append(url)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            if price is None and isinstance(offers, dict) and offers.get("price"):
                try:
                    price = float(offers["price"])
                except (ValueError, TypeError):
                    pass
            img_val = item.get("image")
            if isinstance(img_val, list):
                for v in img_val:
                    add_candidate(v if isinstance(v, str) else None)
            elif isinstance(img_val, str):
                add_candidate(img_val)

    if price is None:
        # フォールバック: 本文から $ を検索
        text = soup.get_text(" ", strip=True)
        m = PRICE_RX.search(text)
        if m:
            price = float(m.group(1))
    if price is None:
        return None

    # -- 画像候補を追加: GetImage.ashx?Path=... (表示画像) → og:image → 素の <img>
    gi = soup.find("img", src=re.compile(r"GetImage\.ashx", re.I))
    if gi:
        m = re.search(r"[?&]Path=([^&]+)", gi["src"])
        if m:
            add_candidate(unquote(m.group(1)).lstrip("~").lstrip("/"))
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        add_candidate(og_img["content"])
    img = soup.find("img", src=re.compile(r"/(ProductImages|Assets|images/product)/", re.I))
    if img:
        add_candidate(img.get("src"))

    if not img_candidates:
        return None

    # リポジトリ軽量化のため、Assets実体URLからサムネイル(_t)版を派生候補として追加
    for u in list(img_candidates):
        m = re.match(r"^(.*/Assets/ProductImages/.+?)(\.[A-Za-z0-9]+)$", u)
        if m and not Path(urlparse(u).path).stem.endswith("_t"):
            add_candidate(f"{m.group(1)}_t{m.group(2)}")

    # 優先度: Assetsのサムネイル(_t) → Assets実体 → その他
    def _prio(u: str) -> int:
        stem = Path(urlparse(u).path).stem
        if "/Assets/" in u and stem.endswith("_t"):
            return 0
        if "/Assets/" in u:
            return 1
        return 2
    img_candidates.sort(key=_prio)
    image = img_candidates[0]

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
        "_imgs": img_candidates,  # ダウンロード試行用の候補一覧 (保存前に除去)
    }


# ==================== 一覧クロール ====================
def crawl_category(seed_url: str, limit: int | None,
                   visited_pages: set | None = None, max_pages: int = 300) -> list[str]:
    """カテゴリ配下のすべての商品URLを収集。
    サブカテゴリ (同一セクション配下の一覧ページ) とページャーの両方を辿る。
    visited_pages を渡すとカテゴリ横断で共有し、重複巡回を避ける。
    """
    if visited_pages is None:
        visited_pages = set()
    product_urls: set[str] = set()
    queue = [seed_url]
    # サブカテゴリ探索を同一セクション(先頭パスセグメント)に限定して暴走を防ぐ
    seed_section = urlparse(seed_url).path.strip("/").split("/")[0].lower()
    pages_done = 0

    while queue:
        page = queue.pop(0)
        if page in visited_pages:
            continue
        visited_pages.add(page)
        pages_done += 1

        log.info(f"  [list] {page}")
        html = fetch(page)
        polite_sleep()
        if not html:
            continue

        found = find_product_links(html)
        # このページ自体が商品ページ (スラッグURLの書籍等) なら商品として捕捉
        if looks_like_product(html):
            found.add(page)
        new = found - product_urls
        product_urls.update(new)
        if new:
            log.info(f"        + {len(new)} products (total {len(product_urls)})")

        if limit and len(product_urls) >= limit:
            break
        if pages_done >= max_pages:
            log.info(f"        max_pages={max_pages} 到達、このカテゴリの探索を打ち切り")
            break

        # ページャー ("Next")
        nxt = find_next_page(html, page)
        if nxt and nxt not in visited_pages:
            queue.append(nxt)

        # サブカテゴリ (同一セクション配下の一覧ページのみ)。
        # パス深度 <= MAX_SUBCATEGORY_DEPTH に制限し、末端の個別商品ページ
        # (例: /Masonic-Store/.../Generic-LodgeChapter-Shirts/<Lodge名>-Shirt) を
        # サブカテゴリ扱いで無限に辿るのを防ぐ。
        for sub in find_subcategory_links(html):
            sub_parts = urlparse(sub).path.strip("/").split("/")
            if sub in visited_pages or sub in queue:
                continue
            if sub_parts[0].lower() == seed_section and len(sub_parts) <= MAX_SUBCATEGORY_DEPTH:
                queue.append(sub)

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
    ap.add_argument("--no-discover", action="store_true",
                    help="ナビ自動発見を使わずハードコードSEEDのみ使用")
    args = ap.parse_args()

    state = load_state() if args.resume else {"done_categories": [], "products": []}
    log.info(f"開始: 既存 {len(state['products'])}件 / 完了カテゴリ {len(state['done_categories'])}件")

    if args.category:
        categories = [(args.category, SEED_CATEGORIES.get(args.category, f"/{args.category}"))]
    elif args.no_discover:
        categories = list(SEED_CATEGORIES.items())
    else:
        categories = list(discover_seed_categories().items())

    # カテゴリ横断で共有する巡回済み一覧ページ / 収集済み商品URL
    visited_pages: set[str] = set()
    seen_products: set[str] = {p["u"] for p in state["products"]}

    for cat_name, cat_path in categories:
        if cat_name in state["done_categories"] and not args.category:
            log.info(f"⏭  {cat_name} は完了済み。スキップ")
            continue

        log.info(f"\n=== カテゴリ: {cat_name} ({cat_path}) ===")
        seed = absolutize(cat_path)

        try:
            product_urls = crawl_category(seed, args.limit, visited_pages=visited_pages)
        except KeyboardInterrupt:
            log.warning("Ctrl+C で中断。現状を保存します")
            save_state(state)
            save_products(state["products"])
            return

        # 既に他カテゴリで取得済みの商品を除外
        product_urls = [u for u in product_urls if u not in seen_products]
        log.info(f"→ {cat_name}: 新規商品URL {len(product_urls)}件を取得。詳細フェッチ開始")

        for i, url in enumerate(product_urls, 1):
            if url in seen_products:
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
                # 画像をローカルに保存し、imgフィールドを相対パスへ書き換える
                candidates = item.pop("_imgs", [item["img"]])
                if item["img"].startswith("http"):
                    local = download_image(candidates)
                    polite_sleep()
                    if local:
                        item["img"] = local
                state["products"].append(item)
                seen_products.add(url)
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
