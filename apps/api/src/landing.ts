/** Friendly page for people who open the API port in a browser by mistake. */
export type LandingReason = "home" | "not_found" | "login_required";

const reasons: Record<LandingReason, { title: string; text: string }> = {
  home: { title: "여기는 API 서버예요", text: "이 주소는 앱이 데이터를 주고받는 통로라서 화면이 없어요. 아래에서 가려던 곳을 골라 주세요." },
  not_found: { title: "찾는 페이지가 없어요", text: "API 서버에는 이 주소가 없어요. 대시보드나 나루로 이동해 주세요." },
  login_required: { title: "앱에서 로그인해 주세요", text: "이 주소는 로그인한 앱이 호출하는 API예요. 브라우저로 직접 열 수는 없어요." },
};

const modalCss = "\ndialog.pw{width:min(420px,calc(100% - 32px));padding:28px;border:0;border-radius:24px;color:var(--ink);background:var(--canvas);box-shadow:0 24px 70px rgba(0,0,0,.35),inset 0 0 0 1px var(--line)}\ndialog.pw::backdrop{background:rgba(0,0,0,.42);backdrop-filter:blur(6px)}\ndialog.pw h2{margin:0 0 4px;font-size:22px;font-weight:600}\ndialog.pw p{margin:0;max-width:none;font-size:14px;text-align:left}\ndialog.pw form{display:flex;gap:8px;margin-top:20px}\ndialog.pw input{min-width:0;flex:1;padding:12px;border:1px solid var(--line);border-radius:12px;color:var(--ink);background:var(--surface);font:inherit}\ndialog.pw button{padding:12px 18px;border:0;border-radius:12px;color:#fff;background:var(--focus);font:inherit;cursor:pointer}\ndialog.pw button.ghost{color:var(--ink);background:var(--surface)}\ndialog.pw .err{min-height:20px;margin-top:10px;color:#d93025;font-size:13px;text-align:left}\ndialog.pw pre{max-height:50vh;overflow:auto;margin:16px 0 0;padding:14px;border-radius:14px;background:var(--surface);font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;text-align:left;white-space:pre-wrap;word-break:break-word}\ndialog.pw .row{display:flex;justify-content:flex-end;margin-top:16px}\ndialog.pw [hidden]{display:none!important}\n";
const modalHtml = "<dialog class=\"pw\" id=\"pw\" aria-labelledby=\"pw-title\">\n  <h2 id=\"pw-title\">API 비밀번호</h2>\n  <p>상태를 확인하려면 비밀번호를 입력하세요.</p>\n  <form id=\"pw-form\"><input type=\"password\" name=\"password\" autocomplete=\"current-password\" aria-label=\"비밀번호\" required><button type=\"submit\">확인</button><button type=\"button\" class=\"ghost\" data-close>취소</button></form>\n  <div class=\"err\" id=\"pw-err\" role=\"alert\"></div>\n  <pre id=\"pw-out\" hidden></pre>\n  <div class=\"row\" id=\"pw-done\" hidden><button type=\"button\" data-close>닫기</button></div>\n</dialog>\n<script>\n(function(){\n  var link=document.getElementById(\"health-link\"),dlg=document.getElementById(\"pw\"),form=document.getElementById(\"pw-form\"),\n      err=document.getElementById(\"pw-err\"),out=document.getElementById(\"pw-out\"),done=document.getElementById(\"pw-done\"),input=form.elements.password;\n  if(!link||!dlg||!dlg.showModal)return;\n  link.addEventListener(\"click\",function(e){e.preventDefault();form.hidden=false;out.hidden=true;done.hidden=true;err.textContent=\"\";input.value=\"\";dlg.showModal();input.focus()});\n  Array.prototype.forEach.call(dlg.querySelectorAll(\"[data-close]\"),function(b){b.addEventListener(\"click\",function(){dlg.close()})});\n  form.addEventListener(\"submit\",function(e){\n    e.preventDefault();err.textContent=\"\";\n    var body=new URLSearchParams({target:link.getAttribute(\"href\"),password:input.value});\n    fetch(\"/__browser-access\",{method:\"POST\",headers:{\"content-type\":\"application/x-www-form-urlencoded\"},body:body,redirect:\"manual\",cache:\"no-store\"}).then(function(r){\n      if(r.type===\"opaqueredirect\"||r.status===0||(r.status>=300&&r.status<400)){err.textContent=\"비밀번호가 맞지 않습니다.\";input.select();return}\n      return r.text().then(function(t){\n        var s;try{s=JSON.stringify(JSON.parse(t),null,2)}catch(x){err.textContent=\"상태를 읽지 못했습니다.\";return}\n        out.textContent=s;out.hidden=false;done.hidden=false;form.hidden=true;\n      });\n    }).catch(function(){err.textContent=\"서버에 연결하지 못했습니다.\"});\n  });\n})();\n</script>";

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
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAAXNSR0IArs4c6QAAA9xJREFUeJztW79LI0EU/nY4dUlCmvNAC2MIEq1sLARLQREthIBclT/AyiL+SCWKCIKNXGFtEQs7Ow+vEEQ0AYNiIxgQwRjlDkHB+COyu1e4CdFk1o3Ozv7yg9dkZt++79t5M7OTfQL0oQ/AAIBuAGEA3wEQndfyggzgGsAJgBSA3wD+fNbpKIBjAIpN7VjlUDO6AOxYgAAr21E56cJPAJIFgmZtksrtXfJmB2q0UUXocuiTrzYSqqaDk3L+Pdt5S37UAkHxtlerg52Xuo/acZF8nwWCMcv6iLrDcysGiLq9dSu6BQB/AfwwOxKT8E9Q10Wrvdjwgiyok4Fr4dYnX8I33jdcWFhAe3t71bbDw0PMzs7yDonfutvc3KxoQZZlxefzcd0LcE2BmZkZzXZBEDA5OckrnBK4qX17e6s5AhRFUS4uLnjvBvncqLe3913yRXR0dDhPgK2tLd0CrK2tcROAyz6grq4Oj4+PIETflHN/fw+v12t0WACvfcDY2Jhu8gDg8XgQiUQMjakchg+z09NT3cO/iFQq5Yw5IBgMUkleXV0pkiRVbZNlWfF4PIYLYHgKaK39iUQC+/v7VdsEQcD4+LhxgZXBUIXv7u6oI6CxsVGJRqPU9mw2a+8UGBwcpJLLZDIvQ5AQ5fn5mdqvra3NvgLs7u5SiU1MTJT6bW9vU/utrq7aUwBRFDUnOFEUS30jkQhVgHw+b08BpqenqaTS6XRF/6enJ2r/4eFh+wmQzWaphKLRaEX/zc1Nav9kMmkvAcLhMJVMoVBQCCEV1/T391OvkSTpVcqwNENOhObm5qhtBwcHaG1trfg9k8mgUCigvr6+oo0Qglgshvn5eeaxGvIy9PDwAFEUmfo8Pz9HIBBg6hNGvAyNjIwwJw8ALS0tCIVCzP2CdU6l02lqLn8WiUSC+RzANAX8fj9ubm4gCAIrl6+Qz+fh8/mY+mSaAvF43DDyAOD1ejE0NMTUJ9MRcHl5iaampqptyWQSGxsbuvzEYjH4/f6qbXt7e+jp6flUnG/BJJc6Ozs18zcUCun2tby8TPUjSZLS0NDAch5g42h9fZ0adC6Xq8lXIBDQFDMej1tPAK29/OLiYs3+crkc1d/Z2Zm1BNA61FDUg49afS4tLWn6DAaD1hHg6OiIGmjx4KNWe+9/xJWVFesIoHWiMzU19WG/Wm+UrI7Lvj6QMDsAs/ElgFpp4VbIRC0zcSuuiVpj41acELXAyK1IEbW6yq0ocXft5/LFZfCXac/APFRwdnXJDL6Kpl7g6rK5chGcOBJ0FU6Wp4OT5oSaSmfL4fjiab2H+I4tn/8PqM0z27Oi/8IAAAAASUVORK5CYII=">
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
${modalCss}</style>
</head>
<body>

<main>
  <div class="brand"><span class="mark">I</span><span>INAZINA<small>API 서버 · 127.0.0.1:4312</small></span></div>
  <h1>${title}</h1>
  <p>${text}</p>
  ${reason === "home" ? "" : `<span class="path">${escape(path)}</span>`}
  <nav aria-label="이동할 곳">
    <a class="card" href="http://127.0.0.1:4311/"><span class="icon i2" aria-hidden="true">📈</span><span><b>나루</b><span>시세 보기 · 주문 · 내 지갑</span></span><span class="arrow" aria-hidden="true">›</span></a>
    <a class="card" href="http://127.0.0.1:4310/"><span class="icon i1" aria-hidden="true">🛠</span><span><b>관리자 대시보드</b><span>운영 · 상품 · 사용자 관리</span></span><span class="arrow" aria-hidden="true">›</span></a>
  </nav>
  <footer><span class="status"><span class="dot"></span>${pythonConnected ? "서비스 정상" : "서비스 점검 필요"}</span><a id="health-link" href="/api/health">상태 확인 (JSON)</a></footer>
</main>
${modalHtml}
</body>
</html>`;
}
