# 공식 사이트 공개 (GitHub Pages)

- 주소: `https://winterliy.github.io/INAZINA/` (저장소 `winterliy/INAZINA`, Pages 소스는 GitHub Actions).
- 문서 주소(디스코드 포털 등에 붙일 수 있음): `…/INAZINA/terms/`(이용약관), `/privacy/`(개인정보 처리방침), `/operations/`(운영규칙), `/internal/`(내부규칙), `/help/`(명령어 도움말). 각 페이지에 본문이 빌드 때 들어가므로 로그인이나 스크립트 없이 읽힙니다. 홈의 "LINK to"는 이 페이지를 새 창으로 열고, 문서 페이지 우측 상단 "돌아가기"는 홈으로 돌아갑니다.
- 문서 내용은 `apps/site/public/links/*.txt`(콘솔 "문서 · 약관"에서 편집하는 파일)입니다. 콘솔은 이 컴퓨터의 파일만 바꾸므로, 저장·개정안 취소·시행일 교체가 일어나면 `tools/publish_documents.py`가 그 파일만 골라 커밋·푸시하고(`documents.publish_async`, `start_all`이 `INAZINA_PUBLISH_DOCUMENTS=1`로 켬, 기록은 `logs/publish.log`), 푸시되면 `.github/workflows/pages.yml`이 사이트를 다시 배포합니다(수동 실행은 Actions 탭). 끄려면 `INAZINA_PUBLISH_DOCUMENTS=0`. 배포는 푸시 후 몇 분 걸립니다.
- 정적 사이트입니다. Contact/Report 양식과 서버 API는 없습니다(문의는 이메일). 내부 4314 사이트는 더 이상 실행하지 않습니다.
- 검색: `robots.txt`가 색인을 허용하고 `sitemap.xml`을 알려 줍니다. 검색 결과에 나오려면 시간이 걸리며, 빨리 하려면 Google Search Console, 네이버 서치어드바이저에 사이트를 등록하고 sitemap을 제출하세요(소유 확인이 필요해 직접 해야 합니다).
- 로컬 확인: `cd apps/site && VITE_BASE=/INAZINA/ VITE_SITE_URL=https://winterliy.github.io/INAZINA/ npx vite build --outDir /tmp/pages-check`, 개발 서버는 `npm run dev --workspace @inazina/site`(4314, `start_all`은 켜지 않음).
- 개인정보 처리방침과 약관은 이 사이트가 GitHub(미국)에서 제공된다는 점, 문의가 이메일로만 된다는 점을 적고 있습니다. 호스팅을 바꾸거나 양식·분석 도구를 넣으면 방침도 함께 고쳐야 합니다.
