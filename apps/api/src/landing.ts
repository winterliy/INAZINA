/** Friendly page for people who open the API port in a browser by mistake. */
export type LandingReason = "home" | "not_found" | "login_required";

const reasons: Record<LandingReason, { title: string; text: string }> = {
  home: { title: "여기는 API 서버예요", text: "이 주소는 앱이 데이터를 주고받는 통로라서 화면이 없어요. 아래에서 가려던 곳을 골라 주세요." },
  not_found: { title: "찾는 페이지가 없어요", text: "API 서버에는 이 주소가 없어요. 대시보드나 HTS로 이동해 주세요." },
  login_required: { title: "앱에서 로그인해 주세요", text: "이 주소는 로그인한 앱이 호출하는 API예요. 브라우저로 직접 열 수는 없어요." },
};

const escape = (value: string) => value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);

export function landingPage(reason: LandingReason, path: string, pythonConnected: boolean): string {
  const { title, text } = reasons[reason];
  return `<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#ffffff">
<title>INAZINA API</title>
<style>
:root{color-scheme:light;--ink:#1d1d1f;--ink2:#6e6e73;--accent:#0066cc;--focus:#0071e3;--canvas:#fff;--surface:#f5f5f7;--line:rgba(0,0,0,.08);--ease:cubic-bezier(.22,1,.36,1)}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--ink:#f5f5f7;--ink2:#a1a1a6;--accent:#2997ff;--focus:#2997ff;--canvas:#000;--surface:#161617;--line:rgba(255,255,255,.1)}}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{min-height:100vh;display:grid;place-items:center;padding:64px 20px;color:var(--ink);background:var(--canvas);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;font-size:17px;letter-spacing:-.022em;-webkit-font-smoothing:antialiased;overflow-x:clip}
main{width:min(760px,100%);text-align:center;animation:rise 700ms var(--ease) backwards}
.brand{display:inline-flex;align-items:center;gap:10px;margin-bottom:40px;font-size:14px;font-weight:600;letter-spacing:-.224px;text-align:left}
.mark{display:grid;place-items:center;width:28px;height:28px;border-radius:7px;color:var(--canvas);background:var(--ink);font-size:14px}
.brand small{display:block;color:var(--ink2);font-size:12px;font-weight:400;letter-spacing:0}
h1{margin:0;font-size:56px;line-height:1.07;font-weight:600;letter-spacing:-.015em}
p{max-width:560px;margin:16px auto 0;color:var(--ink2);font-size:21px;line-height:1.38;letter-spacing:-.01em}
.path{display:inline-block;margin-top:20px;padding:6px 14px;border-radius:9999px;background:var(--surface);font:400 12px/1.4 "SF Mono",ui-monospace,Menlo,monospace;word-break:break-all}
nav{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:56px;text-align:left}
a.card{display:flex;flex-direction:column;align-items:flex-start;min-height:240px;padding:28px 28px 26px;border-radius:28px;color:inherit;text-decoration:none;background:var(--surface);transition:transform .16s var(--ease)}
a.card:active{transform:scale(.98)}
a.card:focus-visible{outline:2px solid var(--focus);outline-offset:3px}
.icon{display:grid;place-items:center;flex:none;width:48px;height:48px;border-radius:12px;color:#fff;font-size:22px}
.i1{background:var(--accent)}.i2{background:var(--ink);color:var(--canvas)}
.card b{display:block;margin-top:40px;color:var(--ink);font-size:24px;line-height:1.17;font-weight:600;letter-spacing:-.01em}.card .icon{display:grid;margin:0}.card span{display:block;margin-top:6px;color:var(--ink2);font-size:15px;line-height:1.4}
.card .arrow{margin-top:auto;padding-top:20px;color:var(--accent);font-size:28px;line-height:1;transition:transform .2s var(--ease)}
a.card:hover .arrow{transform:translateX(4px)}
footer{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:8px 24px;margin-top:40px;color:var(--ink2);font-size:12px}
.status{display:inline-flex;align-items:center;gap:7px}
.dot{width:8px;height:8px;border-radius:50%;background:${pythonConnected ? "#30d158" : "#ff453a"}}
footer a{color:var(--accent);text-decoration:none}footer a:hover{text-decoration:underline}
@keyframes rise{from{opacity:0;transform:translateY(18px)}}
@media (max-width:600px){body{padding:48px 16px}h1{font-size:40px}p{font-size:19px}nav{grid-template-columns:1fr;margin-top:40px}a.card{min-height:0}.card b{margin-top:28px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>

<main>
  <div class="brand"><span class="mark">I</span><span>INAZINA<small>API 서버 · 127.0.0.1:4312</small></span></div>
  <h1>${title}</h1>
  <p>${text}</p>
  ${reason === "home" ? "" : `<span class="path">${escape(path)}</span>`}
  <nav aria-label="이동할 곳">
    <a class="card" href="http://127.0.0.1:4311/"><span class="icon i2" aria-hidden="true">📈</span><span><b>HTS</b><span>시세 보기 · 주문 · 내 지갑</span></span><span class="arrow" aria-hidden="true">›</span></a>
    <a class="card" href="http://127.0.0.1:4310/"><span class="icon i1" aria-hidden="true">🛠</span><span><b>관리자 대시보드</b><span>운영 · 상품 · 사용자 관리</span></span><span class="arrow" aria-hidden="true">›</span></a>
  </nav>
  <footer><span class="status"><span class="dot"></span>${pythonConnected ? "서비스 정상" : "서비스 점검 필요"}</span><a href="/api/health">상태 확인 (JSON)</a></footer>
</main>
</body>
</html>`;
}
