/**
 * What a browser sees on the API port. The first screen is the friendly "this is an API server" page; the
 * administrator sign-in (the shared layout, apps/shared/gate.html) opens only from "상태 확인" or when a protected
 * address is opened directly.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export type LandingReason = "home" | "not_found" | "login_required" | "login";

const sharedDir = join(dirname(fileURLToPath(import.meta.url)), "../../shared");
const template = readFileSync(join(sharedDir, "gate.html"), "utf8").replace("{{SCENE}}", readFileSync(join(sharedDir, "scene.svg"), "utf8"));
export const ICON = '<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAAXNSR0IArs4c6QAAAjdJREFUeJztm0FrE0EYhp8dGpoeitIgFq0SwaqH6qXSXsRLCbS33ES8C/070v/gYS85CuJFcmmhh2IhtAhKxECUhYZeIjSpB6chNpFutc673ZkHvkMGkrzfuzs7s5u8EemoAKvAMnAPKAEm5Xtd0QcSYB/YBN4Ab//1Q9eBBnB8Sathezg3i0A9Aw1cVNVtT6l4BvQyIPqiq2d7O7N5tdD/XX80YTGnR37cmTB2OuRpzp9V9dPNr2dAlOv6bXW4zEvd31bjpPlKBsSoqmLsDs9XVo3d3vrKcgR8A66plYj4Htl1MWs3Nq7oR/ZiIGGyeAWAH92OSgITii+9WX7K/YfPuXH7CQCtZp29D6/5+vm9cy0SAxYev+TBoxeD16XrCxQmpyUGOJ/7s3NLlOfXRsbL82vMzi25luPeAGMKFKdmRsaLUzMYU3Atx70B7dY2SXt3ZDxp79JubbuW496A3lGXna0NDjvNwdhhp8nO1ga9o65rOciWwaulu9y6swLAl0/vOEg+KmToDMgKvu4AB3hvgGQjVK1Wx47XajXnWlA8iIjj+Pg0cRxLHop4PwWCAWoBaoIBagFqggFqAWqCAWoBaoIBagFqggFqAWqCAWoBaoIBagFqggFqAWqCAWoBarw3IPw0ZpMWvtI3NmbiK4mxGRtf2Tc2YOQrm8amq3xl0Lu3f5c/2Qe8kh0DHSM9ex2ZIYSmfuF1bG7YhDyeCamCk8PTIU/XhHNFZ4fJfXg6SmlEbuPzPwHcXCuP81/MBwAAAABJRU5ErkJggg==">';
const escape = (value: string) => value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);
const jsString = (value: string) => JSON.stringify(value).slice(1, -1).replace(/</g, "\\u003c");

const reasons: Record<Exclude<LandingReason, "login">, { title: string; text: string }> = {
  home: { title: "여기는 API 서버예요", text: "이 주소는 앱이 데이터를 주고받는 통로라서 화면이 없어요. 아래에서 가려던 곳을 골라 주세요." },
  not_found: { title: "찾는 페이지가 없어요", text: "API 서버에는 이 주소가 없어요. 대시보드나 나루로 이동해 주세요." },
  login_required: { title: "앱에서 로그인해 주세요", text: "이 주소는 로그인한 앱이 호출하는 API예요. 브라우저로 직접 열 수는 없어요." },
};



function homePage(reason: Exclude<LandingReason, "login" | "login_required">, path: string, pythonConnected: boolean): string {
  const { title, text } = reasons[reason];
  return `<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#ffffff">
<title>INAZINA API</title>
${ICON}
<style>
:root{color-scheme:light;--ink:#1d1d1f;--ink2:#6e6e73;--accent:#0066cc;--focus:#0071e3;--canvas:#fff;--surface:#f5f5f7;--line:rgba(0,0,0,.08);--ease:cubic-bezier(.22,1,.36,1)}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--ink:#f5f5f7;--ink2:#a1a1a6;--accent:#2997ff;--focus:#2997ff;--canvas:#000;--surface:#161617;--line:rgba(255,255,255,.1)}}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{min-height:100vh;display:grid;place-items:center;padding:64px 20px;color:var(--ink);background:var(--canvas);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;font-size:17px;letter-spacing:-.022em;-webkit-font-smoothing:antialiased;overflow-x:clip}
main{width:min(760px,100%);text-align:center;animation:rise 700ms var(--ease) backwards}
.brand{display:inline-flex;align-items:center;gap:10px;margin-bottom:40px;font-size:14px;font-weight:600;letter-spacing:-.224px;text-align:left}
.wordmark{display:block;height:20px;width:auto;color:var(--ink)}
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
  <div class="brand"><span><svg xmlns="http://www.w3.org/2000/svg" viewBox="-6 -714 3273 728" class="wordmark" role="img" aria-label="inazina." height="20"><g fill="currentColor"><path d="M62 0V-480H172V0Z"/><path d="M594 0V-277Q594 -325 563.5 -356.0Q533 -387 485 -387Q453 -387 428.0 -373.0Q403 -359 389.0 -334.0Q375 -309 375 -277L332 -301Q332 -356 356.0 -398.5Q380 -441 422.5 -465.5Q465 -490 518 -490Q572 -490 614.0 -462.5Q656 -435 680.0 -391.5Q704 -348 704 -301V0ZM265 0V-480H375V0Z"/><path d="M987 10Q922 10 869.5 -23.0Q817 -56 786.5 -112.5Q756 -169 756 -239Q756 -310 786.5 -366.5Q817 -423 869.5 -456.5Q922 -490 987 -490Q1042 -490 1084.5 -466.5Q1127 -443 1152.0 -402.0Q1177 -361 1177 -309V-171Q1177 -119 1152.5 -78.0Q1128 -37 1085.0 -13.5Q1042 10 987 10ZM1005 -94Q1066 -94 1103.5 -135.0Q1141 -176 1141 -240Q1141 -283 1124.0 -316.0Q1107 -349 1076.5 -367.5Q1046 -386 1005 -386Q965 -386 934.5 -367.5Q904 -349 886.5 -316.0Q869 -283 869 -240Q869 -197 886.5 -164.0Q904 -131 934.5 -112.5Q965 -94 1005 -94ZM1134 0V-129L1153 -246L1134 -362V-480H1244V0Z"/><path d="M1300 -60 1568 -420H1697L1429 -60ZM1300 0V-60L1400 -100H1691V0ZM1318 -380V-480H1697V-420L1597 -380Z"/><path d="M1758 0V-480H1868V0Z"/><path d="M2290 0V-277Q2290 -325 2259.5 -356.0Q2229 -387 2181 -387Q2149 -387 2124.0 -373.0Q2099 -359 2085.0 -334.0Q2071 -309 2071 -277L2028 -301Q2028 -356 2052.0 -398.5Q2076 -441 2118.5 -465.5Q2161 -490 2214 -490Q2268 -490 2310.0 -462.5Q2352 -435 2376.0 -391.5Q2400 -348 2400 -301V0ZM1961 0V-480H2071V0Z"/><path d="M2683 10Q2618 10 2565.5 -23.0Q2513 -56 2482.5 -112.5Q2452 -169 2452 -239Q2452 -310 2482.5 -366.5Q2513 -423 2565.5 -456.5Q2618 -490 2683 -490Q2738 -490 2780.5 -466.5Q2823 -443 2848.0 -402.0Q2873 -361 2873 -309V-171Q2873 -119 2848.5 -78.0Q2824 -37 2781.0 -13.5Q2738 10 2683 10ZM2701 -94Q2762 -94 2799.5 -135.0Q2837 -176 2837 -240Q2837 -283 2820.0 -316.0Q2803 -349 2772.5 -367.5Q2742 -386 2701 -386Q2661 -386 2630.5 -367.5Q2600 -349 2582.5 -316.0Q2565 -283 2565 -240Q2565 -197 2582.5 -164.0Q2600 -131 2630.5 -112.5Q2661 -94 2701 -94ZM2830 0V-129L2849 -246L2830 -362V-480H2940V0Z"/></g><path d="M3116 10Q3084 10 3063.5 -11.5Q3043 -33 3043 -64Q3043 -96 3063.5 -117.0Q3084 -138 3116 -138Q3148 -138 3168.5 -117.0Q3189 -96 3189 -64Q3189 -33 3168.5 -11.5Q3148 10 3116 10Z" fill="#1f3fff"/><circle cx="117.0" cy="-634.0" r="66.0" fill="#1f3fff"/><circle cx="1813.0" cy="-634.0" r="66.0" fill="#1f3fff"/></svg><small>API 서버 · 127.0.0.1:4312</small></span></div>
  <h1>${title}</h1>
  <p>${text}</p>
  ${reason === "home" ? "" : `<span class="path">${escape(path)}</span>`}
  <nav aria-label="이동할 곳">
    <a class="card" href="http://127.0.0.1:4311/"><span class="icon i2" aria-hidden="true">📈</span><span><b>나루</b><span>시세 보기 · 주문 · 내 지갑</span></span><span class="arrow" aria-hidden="true">›</span></a>
    <a class="card" href="http://127.0.0.1:4310/"><span class="icon i1" aria-hidden="true">🛠</span><span><b>관리자 대시보드</b><span>운영 · 상품 · 사용자 관리</span></span><span class="arrow" aria-hidden="true">›</span></a>
  </nav>
  <footer><span class="status"><span class="dot"></span>${pythonConnected ? "서비스 정상" : "서비스 점검 필요"}</span><a href="/?login">상태 확인</a></footer>
</main>
</body>
</html>`;
}

const copy: Record<"home" | "not_found" | "login_required" | "login", { heading: string; lead: string }> = {
  login: { heading: "관리자 로그인", lead: "API 서버의 상태를 보려면 관리자 계정으로 로그인하세요." },
  home: { heading: "", lead: "" },
  not_found: { heading: "찾는 페이지가 없어요", lead: "API 서버에는 이 주소가 없어요. 로그인하면 서비스 상태를 확인할 수 있어요." },
  login_required: { heading: "로그인이 필요합니다", lead: "이 주소는 관리자 로그인이 필요해요. 로그인하면 결과를 바로 보여드려요." },
};


function gatePage(reason: "login_required" | "login", path: string, pythonConnected: boolean): string {
  const { heading, lead } = copy[reason];
  const target = reason === "login_required" && path.startsWith("/api/") ? path : "/api/health";
  const values: Record<string, string> = {
    TITLE: escape(heading), ICON, SERVICE: "API 서버", PORT: "4312", HEADING: escape(heading), LEAD: escape(lead),
    PATH_CHIP: reason === "login" ? "" : `<span class="chip">${escape(path)}</span>`,
    LINKS: '<a class="back" href="/">돌아가기</a>',
    STATUS_CLASS: pythonConnected ? "ok" : "", STATUS_TEXT: pythonConnected ? "서비스 정상" : "서비스 점검 필요", TARGET_JS: jsString(target),
  };
  return template.replace(/\{\{(\w+)\}\}/g, (_match, key: string) => values[key] ?? "");
}

export function landingPage(reason: LandingReason, path: string, pythonConnected: boolean): string {
  return reason === "login" || reason === "login_required" ? gatePage(reason, path, pythonConnected) : homePage(reason, path, pythonConnected);
}

/** The tab icon as a file, for browsers that ask for /favicon.ico whatever the page says. */
export const FAVICON_PNG = Buffer.from(/base64,([A-Za-z0-9+/=]+)/.exec(ICON)![1], "base64");
