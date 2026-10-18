# Wide-Learning 사용 안내

Laplace plan detail no. 2. Claude(CThread)와 Codex(GThread)가 경제·금융 자료를 나눠 읽고, **서로 출처를 확인해 검증된 내용만** LAPLACE 시뮬레이터에 반영하는 작업장입니다. 이 문서는 사용자용 안내이고, CLI가 지키는 세부 규칙은 [RULES.md](RULES.md)에 있습니다.

## 한눈에 보기

```
① 주제 승인 (사용자)          topics.md          draft → approved
        ↓
② 자료 읽기·기록 (두 CLI)     threads/*.md       claim  + 출처(registry.md)
        ↓
③ 교차 검토 (두 CLI)          threads/*.md       review: agree / partial / needs-source / dispute
        ↓
④ 확정 (원 작성자)            threads/*.md       verified · simulator-assumption · rejected · disputed
        ↓                                         └ disputed → 사용자 판단
⑤ 정리 (지정한 CLI 하나)      wide-learning.md   확정된 내용만 주제별 요약
        ↓
⑥ 적용 승인 (사용자)          apply-queue.md     proposed → approved
        ↓
⑦ 구현 (CLI)                  코드·문서          applied + 커밋 기록
```

**사용자가 결정할 곳은 세 군데**입니다. ① 어떤 주제를 다룰지, ④ 합의가 안 된(`disputed`) 쟁점, ⑥ 무엇을 시스템에 반영할지.

## 폴더 구조

| 파일 | 내용 | 누가 쓰나 |
|---|---|---|
| `README.md` | 이 안내 | — |
| [RULES.md](RULES.md) | 운영 규칙: 파일 소유권, 진행 단계, 종료 조건, 안전 규칙 | 사용자 |
| [WLMD.md](WLMD.md) | 기록 형식(WLMD) 명세와 예시 | 사용자 |
| [SOURCES.md](SOURCES.md) | 자료 등급(S1·S2·S3·X), 확인 절차, 권장 출처 목록 | 사용자 |
| [REVIEW.md](REVIEW.md) | 검토 체크리스트, 판정·상태 정의, 시스템 적용 기준 | 사용자 |
| [topics.md](topics.md) | 다룰 주제와 질문 (Laplace 1~12장에 대응) | 사용자 |
| [registry.md](registry.md) | 출처 목록 (덧붙이기만) | 두 CLI |
| [threads/cthread-claude.md](threads/cthread-claude.md) | CLAUDE의 기록 `C-NNN` (덧붙이기만) | CLAUDE만 |
| [threads/gthread-codex.md](threads/gthread-codex.md) | CODEX의 기록 `G-NNN` (덧붙이기만) | CODEX만 |
| [wide-learning.md](wide-learning.md) | 확정된 내용의 주제별 통합본 | 지정한 CLI 하나 |
| [apply-queue.md](apply-queue.md) | 시스템 반영 후보 `A-NNN` | CLI가 제안, 사용자가 승인 |
| `prompts/` | 각 스레드에 붙여 넣을 시작 프롬프트 | — |
| `AGENTS.md`, `CLAUDE.md` | 이 폴더에서 일하는 CLI가 자동으로 읽는 요약 지침 | — |
| `../../tools/wlmd_check.py` | 규칙 검사기 | — |

두 CLI가 같은 파일을 동시에 고쳐 내용이 사라지는 일을 막으려고, 기록 파일을 스레드별로 나눴습니다. 원래 계획의 `wide-learning.md`는 검토가 끝난 내용을 모으는 통합본 역할을 합니다.

## 시작하기

1. **주제 고르기**: [topics.md](topics.md)에서 다룰 주제의 `status`를 `approved`로 바꿉니다. 기본으로 다음 두 개를 승인해 두었습니다.
   - `T-00` 경제학 개념 지도: 학습자료1·2에서 개념 목록을 뽑는 출발점
   - `T-04` 개인 신용평가와 연체정보: 방금 만든 신용점수 모델([credit_score.md](../credit_score.md))의 가정을 실제 자료와 대조
2. **스레드 시작**: CThread에는 [prompts/cthread-claude.md](prompts/cthread-claude.md), GThread에는 [prompts/gthread-codex.md](prompts/gthread-codex.md)의 코드 블록을 붙여 넣습니다.
3. **교차 검토 요청**: 한쪽이 기록을 마치면 다른 쪽에 "상대 스레드의 새 claim을 검토해줘"라고 말합니다. 각 CLI는 상대 기록을 읽기만 하고, 의견은 자기 파일에 `review`로 씁니다.
4. **진행 확인**: 아무 때나 아래 명령으로 주제별 진행 상황과 규칙 위반을 봅니다.
   ```bash
   python3 tools/wlmd_check.py
   ```
   출력 예: `T-04 [approved] 개인 신용평가와 연체정보 — proposed 3, verified 2`
5. **쟁점 판단**: `disputed`가 생기면 두 스레드의 해당 항목을 보고 결정을 알려 줍니다.
6. **정리**: 주제가 마무리되면 한 스레드에 "T-04를 wide-learning.md에 정리하고 apply-queue.md에 반영 후보를 올려줘"라고 지시합니다.
7. **적용 승인**: [apply-queue.md](apply-queue.md)에서 반영할 항목의 `status`를 `approved`로 바꾸고 구현을 지시합니다.

## 자료 등급 요약

| 등급 | 예 | 의미 |
|---|---|---|
| S1 | 한국은행·통계청 통계, 법령 원문, BIS·IFRS 기준서, 감독기관 규정 | `verified`의 필수 근거 |
| S2 | 연구기관 보고서, 논문, 교과서, 각주 있는 위키백과 | 보충·해석 |
| S3 | 나무위키(학습자료1·2), 블로그 | 개념 지도용. 단독 근거 불가 |
| X | AI가 만든 요약, 출처 불명 자료 | 등록 금지 |

## 확정 조건

어떤 내용이 `verified`가 되려면 **S1 출처**와 **상대 CLI가 그 출처를 직접 열어 확인한 `agree` 검토**가 모두 필요합니다. 두 AI가 같은 의견이라는 사실만으로는 검증되지 않습니다. 검사기가 이 조건을 강제합니다.

게임 설계 선택(예: "연체 반감기 14일")은 사실이 아니라 `simulator-assumption`으로 따로 표시합니다. 그래야 문서와 코드에서 "현실이 이렇다"와 "게임에서 이렇게 정했다"가 섞이지 않습니다.

## 동시 진행할 때

- 두 CLI가 같은 작업 폴더를 쓰면, 각자 자기 스레드 파일과 `registry.md`만 고칩니다.
- 커밋할 때는 자기 파일만 지정해서 커밋하라고 지시하는 것이 안전합니다(예: `git add docs/wide-learning/threads/cthread-claude.md docs/wide-learning/registry.md`).
- `python3 tools/wlmd_check.py --against HEAD`는 마지막 커밋 이후 스레드 파일과 출처 목록이 덧붙이기만 됐는지도 확인합니다.

## 참고

- 스레드 파일 이름을 `claude.md`로 하지 않은 이유: macOS는 대소문자를 구분하지 않아 Claude Code가 이 파일을 지침 파일(`CLAUDE.md`)로 읽게 됩니다. 웹 자료가 섞인 기록이 지침으로 읽히지 않도록 이름을 `cthread-claude.md`로 했습니다.
- 나무위키 내용은 CC BY-NC-SA 2.0 KR입니다. 기록에는 요약과 출처만 남기고 원문을 길게 옮기지 않습니다.
- 상위 계획: [Laplace 금융 시뮬레이터 현실화 계획](../laplace_finance_realism_plan.md)
