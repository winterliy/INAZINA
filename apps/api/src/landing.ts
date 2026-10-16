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
<meta name="theme-color" content="#f8e9ee">
<title>INAZINA API</title>
<style>
:root{color-scheme:light;--ink:#1d1d1f;--ink2:#515154;--accent:#0071e3;--glass:255,255,255;--a1:.82;--a2:.7;--line:rgba(0,0,0,.08);--base:#f8e9ee;--m1:rgba(255,178,130,.9);--m2:rgba(198,180,255,.85);--m3:rgba(140,204,255,.8);--m4:rgba(255,162,192,.8);--m5:rgba(255,233,212,.9);--ease:cubic-bezier(.22,1,.36,1);--spring:cubic-bezier(.34,1.56,.64,1)}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--ink:#f5f5f7;--ink2:#c7c7cc;--accent:#0a84ff;--glass:40,40,52;--a1:.66;--a2:.56;--line:rgba(255,255,255,.12);--base:#17121f;--m1:rgba(120,58,150,.55);--m2:rgba(42,64,150,.55);--m3:rgba(16,92,128,.5);--m4:rgba(130,40,80,.45);--m5:rgba(60,40,90,.5)}}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{min-height:100vh;display:grid;place-items:center;padding:24px 16px;color:var(--ink);background:var(--base);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;-webkit-font-smoothing:antialiased;overflow-x:clip}
@media (prefers-color-scheme:dark){body{background:var(--base)}}
.wall{position:fixed;inset:-18%;background:radial-gradient(42% 46% at 20% 26%,var(--m1),transparent 70%),radial-gradient(40% 44% at 80% 20%,var(--m2),transparent 72%),radial-gradient(46% 50% at 74% 78%,var(--m3),transparent 72%),radial-gradient(42% 46% at 24% 80%,var(--m4),transparent 70%),radial-gradient(60% 60% at 50% 50%,var(--m5),transparent 80%),var(--base);filter:blur(24px) saturate(112%);animation:mesh 70s ease-in-out infinite alternate}
.blob{position:fixed;border-radius:50%;pointer-events:none;filter:blur(60px);opacity:.5;animation:drift 48s ease-in-out infinite alternate}
.b1{left:-10%;top:4%;width:46vmax;height:46vmax;background:radial-gradient(circle,var(--m1),transparent 65%)}
.b2{right:-12%;bottom:-14%;width:52vmax;height:52vmax;background:radial-gradient(circle,var(--m3),transparent 65%);animation-delay:-16s}
main{position:relative;z-index:1;width:min(520px,100%);padding:36px 32px 28px;border-radius:28px;background:linear-gradient(135deg,rgba(var(--glass),var(--a1)),rgba(var(--glass),var(--a2)));box-shadow:inset 0 1px 0 rgba(255,255,255,.55),inset 0 0 0 1px rgba(255,255,255,.3),0 24px 60px rgba(60,30,100,.22);backdrop-filter:blur(30px) saturate(130%);-webkit-backdrop-filter:blur(30px) saturate(130%);animation:rise 800ms var(--ease) backwards}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:26px;font-size:14px;font-weight:700;letter-spacing:.02em}
.mark{display:grid;place-items:center;width:36px;height:36px;border-radius:11px;color:#fff;background:var(--accent);box-shadow:inset 0 1px 0 rgba(255,255,255,.4),0 6px 16px rgba(0,80,200,.3)}
.brand small{display:block;color:var(--ink2);font-size:12px;font-weight:500;letter-spacing:0}
h1{margin:0;font-size:30px;line-height:1.15;font-weight:700;letter-spacing:-.025em}
p{margin:10px 0 0;color:var(--ink2);font-size:15px;line-height:1.55}
.path{display:inline-block;margin-top:14px;padding:5px 11px;border-radius:999px;background:rgba(var(--glass),.7);box-shadow:inset 0 0 0 1px var(--line);font:500 12px/1.4 "SF Mono",ui-monospace,Menlo,monospace;word-break:break-all}
nav{display:grid;gap:10px;margin-top:26px}
a.card{display:flex;align-items:center;gap:14px;padding:16px 18px;border-radius:18px;color:inherit;text-decoration:none;background:rgba(var(--glass),.72);box-shadow:inset 0 0 0 1px var(--line),0 4px 14px rgba(60,30,100,.08);transition:transform .35s var(--spring),box-shadow .3s}
a.card:hover{transform:translateY(-2px);box-shadow:inset 0 0 0 1px var(--line),0 12px 26px rgba(60,30,100,.16)}
a.card:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.icon{display:grid;place-items:center;flex:none;width:42px;height:42px;border-radius:13px;color:#fff;font-size:19px}
.i1{background:linear-gradient(145deg,#0a84ff,#0060d6)}.i2{background:linear-gradient(145deg,#ff6b5a,#e0362c)}
.card b{display:block;font-size:16px}.card span{display:block;margin-top:2px;color:var(--ink2);font-size:13px}
.arrow{margin-left:auto;color:var(--ink2);font-size:20px;transition:transform .3s var(--spring)}
a.card:hover .arrow{transform:translateX(3px)}
footer{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-top:24px;padding-top:16px;border-top:1px solid var(--line);color:var(--ink2);font-size:12px}
.status{display:inline-flex;align-items:center;gap:7px}
.dot{width:8px;height:8px;border-radius:50%;background:${pythonConnected ? "#1e8e3e" : "#d93025"};box-shadow:0 0 0 3px ${pythonConnected ? "rgba(30,142,62,.18)" : "rgba(217,48,37,.18)"}}
footer a{color:var(--accent);text-decoration:none}footer a:hover{text-decoration:underline}
@keyframes mesh{0%{transform:translate3d(0,0,0) rotate(0) scale(1)}50%{transform:translate3d(-2.5%,1.5%,0) rotate(5deg) scale(1.05)}100%{transform:translate3d(2.5%,-1.5%,0) rotate(-3deg) scale(1.02)}}
@keyframes drift{to{transform:translate3d(6vmax,-4vmax,0) scale(1.12)}}
@keyframes rise{from{opacity:0;transform:translateY(18px)}}
@media (max-width:480px){main{padding:28px 20px 22px}h1{font-size:25px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="wall" aria-hidden="true"></div><div class="blob b1" aria-hidden="true"></div><div class="blob b2" aria-hidden="true"></div>
<main>
  <div class="brand"><span class="mark">I</span><span>INAZINA<small>API 서버 · 127.0.0.1:4312</small></span></div>
  <h1>${title}</h1>
  <p>${text}</p>
  ${reason === "home" ? "" : `<span class="path">${escape(path)}</span>`}
  <nav aria-label="이동할 곳">
    <a class="card" href="http://127.0.0.1:4311/"><span class="icon i2" aria-hidden="true">📈</span><span><b>HTS</b><span>시세 보기 · 주문 · 내 지갑</span></span><span class="arrow" aria-hidden="true">›</span></a>
    <a class="card" href="http://127.0.0.1:4310/"><span class="icon i1" aria-hidden="true">🛠</span><span><b>관리자 대시보드</b><span>운영 · 상품 · 사용자 관리</span></span><span class="arrow" aria-hidden="true">›</span></a>
  </nav>
  <footer><span class="status"><span class="dot"></span>${pythonConnected ? "서비스 정상" : "데이터 서비스 연결 끊김"}</span><a href="/api/health">상태 확인 (JSON)</a></footer>
</main>
</body>
</html>`;
}
