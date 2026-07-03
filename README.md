# MACOY JAPAN 日本語版デモサイト

macoy.com の日本語版UIと、全カテゴリ巡回スクレイパー一式です。

## 構成

- `macoy-jp.html` — フロントエンド(単一HTML、レスポンシブ、Safari互換)
- `macoy_scraper.py` — 全カテゴリ巡回スクレイパー(Python 3.10+)
- `products.json` — スクレイパー実行後に生成される商品データ(フロントが自動読込)

`products.json` が無い場合、フロントは同梱のフォールバック(9点)を表示します。生成済みなら自動で LIVE DATA 表示に切り替わります。

## セットアップ

```bash
pip install requests beautifulsoup4 lxml
```

## 使い方

### 1. スクレイパー実行

まずは動作確認として少量で試すのがおすすめです。

```bash
# テスト: 各カテゴリ最大20商品まで
python macoy_scraper.py --limit 20

# 単一カテゴリのみ (フェズだけ全部)
python macoy_scraper.py --category Fezzes

# 本番: 全カテゴリ全商品 (数千件、数時間かかる想定)
python macoy_scraper.py

# 中断からの再開
python macoy_scraper.py --resume
```

進捗は 5商品ごとに `products.json` へ書き出されます。Ctrl+C で中断しても安全です。

### 2. サイトを開く

`macoy-jp.html` と `products.json` を同じフォルダに置き、ローカルサーバー経由で開いてください(fetchはfile://だとブロックされます)。

```bash
# 同フォルダで
python -m http.server 8000
# → http://localhost:8000/macoy-jp.html
```

Vercelにデプロイする場合は、両ファイルをリポジトリ直下に置いてpushするだけでOKです。

## スクレイパーの仕様

- 巡回対象: `SEED_CATEGORIES` に列挙した17カテゴリ(スクリプト冒頭で編集可能)
- リクエスト間隔: 1.0〜1.6秒のランダム遅延(礼儀正しい)
- リトライ: HTTP 429 / 5xx は指数バックオフで最大4回
- 抽出: 商品名(h1 or og:title)、価格(JSON-LD 優先、無ければ本文の $XX.XX)、画像(og:image)、在庫ステータス(本文からの推定)、カテゴリ(パンくずorURL)
- 出力形式: `{generated_at, count, items:[{n,c,p,s,img,u},...]}`
- 状態管理: `_crawl_state.json` に進捗記録、`_errors.log` に失敗URL

## 在庫ステータス値

- `2` = 在庫あり(即カート投入可能な表示)
- `1` = 受注確認(オプション選択が必要)
- `0` = 在庫切れ(sold out / out of stock を検出)

macoy.com は在庫数値を公開していないため、販売ステータスからの推定です。

## カスタマイズポイント

- 対象カテゴリを増やす: `macoy_scraper.py` の `SEED_CATEGORIES` に追加
- 遅延を変える: `DELAY_MIN`, `DELAY_MAX` を編集(サーバー負荷への配慮を優先)
- 商品名を日本語化: スクレイパーで英名収集 → 別途翻訳バッチをかます形が現実的
- 画像プロキシ: macoy.com の画像は直リンで表示していますが、CORS/リファラ制限に当たる場合はCloudflare Workersなどで中継してください

## 免責

本プロジェクトは macoy.com の商品情報を引用した日本語版デモUIです。実運用(販売代行、正規代理店化など)には Macoy Publishing 本社との契約が必要です。スクレイパーはあくまで研究・プロトタイピング目的で、robots.txt および同社利用規約を尊重してご使用ください。
