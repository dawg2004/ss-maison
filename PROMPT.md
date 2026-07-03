# Claude Code 引き継ぎ指示書 (ss-maison)

## ゴール
macoy.com の商品画像がブラウザで確実に表示される状態にする。
現状はClaude(web版)環境が macoy.com にネットワークアクセスできず、画像取得ができなかった。
Claude Code はローカル(Mac)で動くため macoy.com にアクセスできる。これを使って画像を実ダウンロードし、リポジトリに同梱する。

## 前提
- リポジトリ: dawg2004/ss-maison (ローカルにcloneまたはpull済み)
- 既存ファイル: index.html, macoy_scraper.py, macoy-img-proxy.js, README.md
- Python 3.10+ 利用可能

## タスク手順

### 1. スクレイパーに画像ダウンロード機能を追加
`macoy_scraper.py` を拡張し、商品ごとに画像を `images/` フォルダへ保存する。
- 画像取得時のヘッダー: Referer を `https://www.macoy.com/` に設定(ホットリンク回避)
- 保存名: 商品URL末尾のスラッグ or 画像ファイル名をそのまま使用
- 保存後、各商品の `img` フィールドを `images/<filename>` の相対パスに書き換える

参考(画像取得の必須ヘッダー):
```python
headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Referer": "https://www.macoy.com/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}
```

### 2. スクレイパー実行
まずテスト(各カテゴリ20点)、問題なければ本番。
```bash
pip install requests beautifulsoup4 lxml
python macoy_scraper.py --limit 20      # テスト
# 画像とproducts.jsonが正しく生成されるか確認
python macoy_scraper.py                  # 本番全走
```

### 3. index.html の調整
- `IMG_PROXY` は空のままでOK(ローカル画像 `images/xxx.jpg` を直接参照するため)
- `proxyImg()` は相対パス(httpで始まらないもの)はそのまま返すようにする。以下を確認:
```javascript
function proxyImg(originalUrl) {
  if (!originalUrl.startsWith('http')) return originalUrl; // ローカル画像はそのまま
  if (!IMG_PROXY) return originalUrl;
  const path = originalUrl.replace('https://www.macoy.com', '');
  return `${IMG_PROXY}/?path=${encodeURIComponent(path)}`;
}
```

### 4. ローカル動作確認
```bash
python -m http.server 8000
# http://localhost:8000/index.html を開き、画像が表示されることを確認
```

### 5. GitHubへpush
画像・products.json・更新したindex.html/scraper をすべてコミットしてpush。
```bash
git add images/ products.json index.html macoy_scraper.py
git commit -m "feat: 商品画像をローカル同梱、画像表示問題を解決"
git push origin main
```
※ 画像点数が多い場合、リポジトリ肥大に注意。数百点なら問題なし。
  数千点になる場合は Git LFS か、画像を縮小(サムネイル_t版のみ)して同梱を推奨。

### 6. Vercelデプロイ(任意)
Vercel → New Project → ss-maison を Import → Deploy。
GitHub連携済みなら push で自動再デプロイされる。

## 注意
- macoy.com の robots.txt と利用規約を尊重すること
- リクエスト間隔は既存スクレイパーの1〜1.6秒遅延を維持
- 画像の著作権は Macoy Publishing に帰属。本番運用(販売代行等)には同社との契約が必要
