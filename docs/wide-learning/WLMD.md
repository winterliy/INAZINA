# WLMD (Wide-Learning Markdown) 기록 형식

스레드 파일(`threads/*.md`)과 출처 목록(`registry/*.md`)은 이 형식을 따릅니다. 사람이 읽기 쉬운 마크다운이면서 `tools/wlmd_check.py`가 기계적으로 검사할 수 있게 만든 규칙입니다. 채팅처럼 `CLI명: 내용` 순서로 읽히도록 머리줄에 작성자를 둡니다.

## 1. 스레드 항목

```markdown
### [C-003] CLAUDE · 2026-10-18 · T-04
- type: claim
- claim: 국내 개인신용평가에서 상환이력 정보의 반영 비중이 가장 크다.
- detail: 신용평가사가 공개한 평가요소별 비중을 확인했다. 부채수준, 신용거래기간,
  신용형태가 뒤를 잇는다.
- sources: S-101; S-102
- locator: S-101 "평가요소별 반영비중" 표
- tier: S2
- confidence: medium
- status: proposed
- laplace: 4
```

머리줄: `### [ID] 작성자 · YYYY-MM-DD · 주제ID`

| 필드 | 필수 | 값 |
|---|---|---|
| `type` | 모두 | `claim`, `question`, `review`, `correction`, `summary` |
| `claim` | claim·correction | 검증 가능한 한 문장 |
| `detail` | 선택 | 설명. 이어지는 줄은 두 칸 들여쓰기 |
| `re` | review·correction 필수 | 대상 항목 ID (상대 또는 자기 항목) |
| `sources` | claim·correction | `registry/`에 등록된 출처 ID, `;`로 구분 |
| `locator` | 수치·법령·기준서 인용 시 | 조항·표·페이지 |
| `tier` | claim·correction | 인용 출처 중 가장 높은 등급(`S1`·`S2`·`S3`). 실제 출처 등급과 맞아야 함 |
| `confidence` | claim·correction | `high`, `medium`, `low` |
| `status` | claim·correction | `proposed`, `verified`, `disputed`, `simulator-assumption`, `rejected` |
| `laplace` | claim | 관련 Laplace 장 번호(1~12), 여러 개면 `,` |
| `verdict` | review | `agree`, `dispute`, `needs-source`, `partial` |
| `checked` | review | 직접 열어 확인한 출처 ID, 없으면 `none` |
| `answers` | correction 선택 | 이 correction으로 답한 상대 review ID |
| `note` | 선택 | 자료 속 지시문 발견, 라이선스 메모 등 |

### 상태 변경

이미 쓴 항목은 고치지 않으므로, 상태가 바뀌면 **원 작성자가** `correction` 항목으로 새 상태를 선언합니다.

```markdown
### [C-009] CLAUDE · 2026-10-19 · T-04
- type: correction
- re: C-003
- claim: 국내 개인신용평가에서 상환이력 정보의 반영 비중이 가장 크다.
- sources: S-101; S-005
- tier: S1
- confidence: high
- status: verified
- answers: G-004
```

상대 검토에 답하는 correction에는 `answers: G-004`처럼 답한 review ID를 적습니다. 답하지 않은 검토는 `--todo` 목록에 계속 나타납니다. 반박을 받아들이지 않을 때도 이유를 적은 correction(상태 유지)으로 답합니다.

`verified`를 선언하려면 다음이 모두 있어야 합니다(검사기가 확인).
- 인용 출처 중 S1이 하나 이상
- 상대 CLI가 대상 주장(`re`가 원 항목 또는 이 correction)에 남긴 `verdict: agree` 검토
- 그 검토의 `checked`가 `none`이 아님

### 검토 항목

```markdown
### [G-004] CODEX · 2026-10-18 · T-04
- type: review
- re: C-003
- verdict: agree
- checked: S-101; S-005
- detail: S-101 표에서 비중 확인. 1차 근거로 S-005(감독기관 자료)를 추가 등록했다.
```

## 2. 출처 항목 (`registry/user.md`, `registry/cthread-claude.md`, `registry/gthread-codex.md`)

```markdown
### [S-101] 개인신용평가 평가요소 안내
- url: https://example.org/credit-factors
- publisher: 발행 기관명
- tier: S2
- accessed: 2026-10-18
- added-by: CLAUDE
- license: 확인 필요
- note: 기관이 직접 공개한 설명 페이지
```

필수: `url`, `publisher`, `tier`, `accessed`, `added-by`. `added-by`는 파일 주인(USER·CLAUDE·CODEX)과 같아야 하고, ID는 그 주인의 범위(S-0xx·S-1xx·S-2xx)여야 합니다. 같은 URL은 세 파일을 통틀어 한 번만 등록합니다. 선택: `archive`(웹 아카이브 사본 주소). 등급 판정은 [SOURCES.md](SOURCES.md)를 따릅니다.

## 3. 주제 항목 (`topics.md`)

```markdown
### [T-04] 개인 신용평가 요소와 연체정보 관리
- question: 실제 신용평가는 어떤 요소를 어떤 비중으로 쓰며, 연체정보는 얼마나 오래 남는가?
- laplace: 4
- status: approved
- owner: both
```

`status`: `draft`(사용자 승인 전, 진행 금지), `approved`, `in-review`, `done`, `parked`.

## 4. 작성 요령

- 주장 하나에 항목 하나. "그리고"로 이어지는 두 주장은 나눕니다.
- 추측은 `confidence: low`로 쓰고, 근거 없이 단정하지 않습니다.
- 숫자는 단위와 기준 시점을 함께 씁니다(예: "2025년 말 기준, 연 3.00%").
- 시뮬레이터 설계 제안은 `status: simulator-assumption`으로 쓰고 근거가 된 사실 항목을 `re`나 `detail`에 연결합니다.
