# 공식 사이트 공개 (GitHub Pages)

- 주소: `https://winterliy.github.io/INAZINA/` (저장소 `winterliy/INAZINA`, Pages 소스는 GitHub Actions).
- 배포: `master`에 `apps/site/**` 또는 `.github/workflows/pages.yml` 변경이 푸시되면 `.github/workflows/pages.yml`이 표시 전용으로 빌드해 올립니다. 수동 실행은 Actions 탭의 "Pages" → Run workflow.
- 표시 전용 빌드: `VITE_DISPLAY_ONLY=1`이면 `apps/site/index.html`의 `<!--display:hide-->…<!--/display:hide-->` 구간(Contact/Report 양식과 그것을 언급하는 문구)을 지우고 `<!--display:show-->` 구간만 남깁니다. 내부 사이트(4314)는 일반 빌드라 양식이 그대로 있습니다. 문서는 `public/links/*.txt`를 그대로 보여 줍니다.
- 로컬 확인: `cd apps/site && VITE_DISPLAY_ONLY=1 VITE_BASE=/INAZINA/ npx vite build --outDir /tmp/pages-check`
- 개인정보 처리방침과 약관은 이 사이트가 GitHub(미국)에서 제공된다는 점, 문의가 이메일로만 된다는 점을 적고 있습니다. 호스팅을 바꾸거나 양식·분석 도구를 넣으면 방침도 함께 고쳐야 합니다.
- 사이트는 `robots.txt`로 검색 색인을 막아 두었습니다(필요하면 `apps/site/public/robots.txt`를 바꾸세요).
