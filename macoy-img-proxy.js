// ================================================================
// macoy-img-proxy — Cloudflare Worker
// ================================================================
// デプロイ手順:
//   1. https://dash.cloudflare.com/ にログイン(無料アカウントでOK)
//   2. Workers & Pages → Create Application → Create Worker
//   3. このコードを丸ごと貼り付けて「Deploy」
//   4. Worker URL が発行される:
//      例) https://macoy-img-proxy.YOUR_NAME.workers.dev
//
// HTMLのimg srcをこの形式に書き換える:
//   変更前: https://www.macoy.com/Assets/ProductImages/1603_t.jpg
//   変更後: https://macoy-img-proxy.YOUR_NAME.workers.dev/?path=/Assets/ProductImages/1603_t.jpg
//
// 無料プランで 1日10万リクエストまで無料。商品サイト規模なら十分。
// ================================================================

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // CORS プリフライト
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, HEAD',
        }
      });
    }

    // path パラメータ取得
    // ?path=/Assets/ProductImages/foo.jpg
    const path = url.searchParams.get('path') || url.pathname;
    if (!path || path === '/') {
      return new Response('Usage: ?path=/Assets/ProductImages/filename.jpg', { status: 400 });
    }

    const target = `https://www.macoy.com${path}`;

    try {
      const res = await fetch(target, {
        headers: {
          'User-Agent':      'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
          'Referer':         'https://www.macoy.com/',
          'Accept':          'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
          'Accept-Language': 'en-US,en;q=0.9',
          'Sec-Fetch-Dest':  'image',
          'Sec-Fetch-Mode':  'no-cors',
          'Sec-Fetch-Site':  'same-origin',
        }
      });

      const ct = res.headers.get('Content-Type') || 'image/jpeg';

      return new Response(res.body, {
        status: res.status,
        headers: {
          'Content-Type':                ct,
          'Cache-Control':               'public, max-age=86400, stale-while-revalidate=3600',
          'Access-Control-Allow-Origin': '*',
          'X-Proxied-From':              target,
        }
      });
    } catch (err) {
      return new Response(`Proxy error: ${err.message}`, { status: 502 });
    }
  }
};
