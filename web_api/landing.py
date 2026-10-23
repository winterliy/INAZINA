"""Friendly page for people who open the private data-service port in a browser.

Same look as the API server's page (apps/api/src/landing.ts). The service only
answers the API server, which authenticates with an internal token and asks for
JSON; a browser asking for HTML gets this page instead of a raw JSON error.
"""
import html

REASONS = {
    "home": ("여기는 API 서버예요",
             "이 주소는 앱이 데이터를 주고받는 통로라서 화면이 없어요. 아래에서 가려던 곳을 골라 주세요."),
    "not_found": ("찾는 페이지가 없어요",
                  "API 서버에는 이 주소가 없어요. 대시보드나 HTS로 이동해 주세요."),
}

STYLE = """
:root{{color-scheme:light;--ink:#1d1d1f;--ink2:#6e6e73;--accent:#0066cc;--focus:#0071e3;--surface:#fff;--pearl:#fafafc;--line:rgba(0,0,0,.08);--base:#f5f5f7;--ease:cubic-bezier(.22,1,.36,1)}}
@media (prefers-color-scheme:dark){{:root{{color-scheme:dark;--ink:#f5f5f7;--ink2:#a1a1a6;--accent:#2997ff;--focus:#2997ff;--surface:#1d1d1f;--pearl:#2a2a2c;--line:rgba(255,255,255,.1);--base:#000}}}}
*{{box-sizing:border-box}}
html,body{{margin:0;min-height:100%}}
body{{min-height:100vh;display:grid;place-items:center;padding:24px 16px;color:var(--ink);background:var(--base);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;font-size:17px;letter-spacing:-.022em;-webkit-font-smoothing:antialiased;overflow-x:clip}}
@media (prefers-color-scheme:dark){{body{{background:var(--base)}}}}
main{{position:relative;z-index:1;width:min(520px,100%);padding:40px 32px 28px;border:1px solid var(--line);border-radius:18px;background:var(--surface);animation:rise 600ms var(--ease) backwards}}
.brand{{display:flex;align-items:center;gap:10px;margin-bottom:28px;font-size:14px;font-weight:600;letter-spacing:-.224px}}
.mark{{display:grid;place-items:center;width:36px;height:36px;border-radius:8px;color:var(--surface);background:var(--ink)}}
.brand small{{display:block;color:var(--ink2);font-size:12px;font-weight:500;letter-spacing:0}}
h1{{margin:0;font-size:34px;line-height:1.12;font-weight:600;letter-spacing:-.374px}}
p{{margin:12px 0 0;color:var(--ink2);font-size:17px;line-height:1.47}}
.path{{display:inline-block;margin-top:14px;padding:5px 11px;border-radius:9999px;background:var(--pearl);border:1px solid var(--line);font:500 12px/1.4 "SF Mono",ui-monospace,Menlo,monospace;word-break:break-all}}
nav{{display:grid;gap:10px;margin-top:26px}}
a.card{{display:flex;align-items:center;gap:14px;padding:16px 18px;border:1px solid var(--line);border-radius:18px;color:inherit;text-decoration:none;background:var(--pearl);transition:transform .2s var(--ease)}}
a.card:active{{transform:scale(.98)}}
a.card:focus-visible{{outline:2px solid var(--accent);outline-offset:3px}}
.icon{{display:grid;place-items:center;flex:none;width:42px;height:42px;border-radius:13px;color:#fff;font-size:19px}}
.i1{{background:var(--accent)}}.i2{{background:var(--ink);color:var(--surface)}}
.card b{{display:block;color:var(--ink);font-size:17px;font-weight:600}}.card .icon{{display:grid;margin:0}}.card span{{display:block;margin-top:2px;color:var(--ink2);font-size:14px}}
.arrow{{margin-left:auto;color:var(--accent);font-size:20px}}
footer{{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-top:24px;padding-top:16px;border-top:1px solid var(--line);color:var(--ink2);font-size:12px}}
.status{{display:inline-flex;align-items:center;gap:7px}}
.dot{{width:8px;height:8px;border-radius:50%;background:{dot};box-shadow:0 0 0 3px {halo}}}
footer a{{color:var(--accent);text-decoration:none}}footer a:hover{{text-decoration:underline}}
@keyframes rise{{from{{opacity:0;transform:translateY(18px)}}}}
@media (max-width:480px){{main{{padding:28px 20px 22px}}h1{{font-size:25px}}}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important;transition:none!important}}}}
"""


def landing_page(reason, path, database_ok):
    title, text = REASONS[reason]
    style = STYLE.format(dot="#1e8e3e" if database_ok else "#d93025",
                         halo="rgba(30,142,62,.18)" if database_ok else "rgba(217,48,37,.18)")
    path_chip = "" if reason == "home" else f'<span class="path">{html.escape(path)}</span>'
    status = "서비스 정상" if database_ok else "서비스 점검 필요"
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#f5f5f7">
<title>INAZINA API</title>
<style>{style}</style>
</head>
<body>

<main>
  <div class="brand"><span class="mark">I</span><span>INAZINA<small>API 서버 · 127.0.0.1:4313</small></span></div>
  <h1>{title}</h1>
  <p>{text}</p>
  {path_chip}
  <nav aria-label="이동할 곳">
    <a class="card" href="http://127.0.0.1:4311/"><span class="icon i2" aria-hidden="true">📈</span><span><b>HTS</b><span>시세 보기 · 주문 · 내 지갑</span></span><span class="arrow" aria-hidden="true">›</span></a>
    <a class="card" href="http://127.0.0.1:4310/"><span class="icon i1" aria-hidden="true">🛠</span><span><b>관리자 대시보드</b><span>운영 · 상품 · 사용자 관리</span></span><span class="arrow" aria-hidden="true">›</span></a>
  </nav>
  <footer><span class="status"><span class="dot"></span>{status}</span><a href="/internal/health">상태 확인 (JSON)</a></footer>
</main>
</body>
</html>"""
