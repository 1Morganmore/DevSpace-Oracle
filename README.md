# CodexPro Automation

Codex에서 웹 ChatGPT를 **답변 도구가 아니라 실제 작업자·검토자·조사팀처럼 안전하게 활용**하기 위한 Windows용 자동화 계층입니다.

브라우저 자동화 엔진을 새로 만들지 않습니다. 공개 패키지인 `agbrowse`를 그대로 사용하고, 이 저장소는 그 위에 다음 기능만 얇게 올립니다.

- 일반 GPT, 계획, 검토, 수정, 지휘, 종합, 웹 멀티 GPT, 심층 리서치, Pro 모드 라우팅
- CodexPro 앱 등록·연결·권한 확인
- 같은 프로젝트의 중복 질문 방지와 다른 프로젝트의 병렬 실행
- 중단·브라우저 재시작·응답 지연 뒤 정확한 작업 복구
- 작업이 끝난 탭만 자동 정리하고 다른 탭은 보호
- 프롬프트·응답·실행 대상을 해시와 URL로 구분

> 현재 공개판은 **Windows + Codex 앱 + ChatGPT 웹 로그인 환경**을 기준으로 만들었습니다. ChatGPT UI나 외부 패키지 변경에 따라 추가 대응이 필요할 수 있습니다.

> **중요:** 앱을 사용하는 모든 모드는 ChatGPT의 **개발자 모드(Developer Mode)**가 켜져 있어야 합니다. ChatGPT 웹에서 `설정 → 앱 → 고급 설정 → 개발자 모드`를 켜세요. 워크스페이스 정책에 따라 관리자·소유자가 `워크스페이스 설정 → 앱 → 만들기`에서 권한을 먼저 허용해야 할 수 있습니다. 토글이나 `앱 만들기`가 보이지 않으면 현재 계정/워크스페이스에서 커스텀 MCP 앱 사용 권한을 제공하지 않는 상태입니다.

## 왜 만들었나

ChatGPT 웹 자동화를 단순 매크로로 만들면 다음 문제가 반복됩니다.

- 질문을 보냈는지 불확실한데 같은 질문을 다시 보내 버림
- 다른 프로젝트에서 실행 중인 탭을 현재 작업으로 착각함
- 앱 설정을 확인하다가 작업 중인 대화 탭을 건드림
- 끝난 탭은 쌓이고, 반대로 진행 중인 탭은 닫힘
- 계획·검토·수정 프롬프트가 전부 반례 찾기 형태로 좁아짐
- Pro, 일반 GPT, 심층 리서치의 앱 사용 규칙이 섞임

이 프로젝트는 화면 모양이나 “아마 끝났을 것” 같은 추측 대신, 프로젝트·실행 ID·브라우저 세션·탭 ID·대화 URL을 함께 기록해 작업을 추적합니다.

## 모드 한눈에 보기

| 모드 | 하는 일 | CodexPro 앱 | 권장 상황 |
|---|---|---:|---|
| 일반 GPT | 질문에 직접 답하고 분석 | 필수 | 설명, 비교, 아이디어, 일반 문제 해결 |
| 계획 모드 | 여러 접근을 비교하고 실행 가능한 계획 작성 | 필수 | 구현 전 설계, 마이그레이션 계획 |
| 검토 모드 | 이미 있는 계획·코드·결과에서 근거 있는 결함 탐색 | 필수 | PR 검토, 설계 검증, 회귀 위험 점검 |
| 수정 모드 | 작업공간을 읽고 직접 수정·테스트·재수정 | 필수 | 버그 수정, 기능 구현, 문서·설정 변경 |
| 지휘 모드 | 웹 GPT가 구현 주도권을 갖고 탐색부터 수정·테스트까지 수행 | 필수 | 로컬 Codex 토큰 사용을 최대한 줄이고 싶은 큰 작업 |
| GPT 종합모드 | 조사 여부 판단 → 계획 → 필요 시 멀티 GPT → 검토 → 수정/구현 → 로컬 검증 | 필수 | 복잡한 기능, 공유 인프라, 공개 배포, 고위험 변경 |
| 병렬 구현 v3 | 독립 컴포넌트를 격리된 exact-unit 작업공간에서 병렬 구현하고 호스트가 결정론적으로 통합 | exact-unit 전용 | 파일 소유권이 분리되는 대규모 구현 |
| 웹 멀티 GPT | 여러 새 대화를 병렬로 돌려 독립 제안·정제·병합·판정 | 필수 | 해법 공간이 넓거나 한 GPT의 관점 편향을 줄이고 싶을 때 |
| 심층 리서치 | 최신 외부 자료를 폭넓게 조사하고 출처 기반 근거 구성 | 필수 | 최신 기술·시장·법률·표준·제품 조사 |
| Pro | 폭넓은 고난도 판단·설계·리서치 | **금지** | 중요한 전략 판단, 넓은 설계, 어려운 연구 질문 |

핵심 규칙은 간단합니다.

- **Pro만 첨부 전용**입니다. Pro에서는 CodexPro 앱을 선택하지 않습니다.
- **Pro가 아닌 모든 모드에서는 앱이 필수**입니다. 앱이 없거나 URL·권한이 맞지 않으면 질문을 보내지 않습니다.
- 일반 GPT는 명시적인 `High` 또는 `Very High`를 지원합니다. 심층 리서치는 기존대로 `High`만 사용합니다.
- 병렬 구현은 workflow v3의 `features.parallel_implementation_v1=true`와 `CODEX_CHATGPT_PARALLEL_IMPLEMENTATION_V1=1`이 모두 있어야 하며, 하나라도 없으면 아무 부작용 없이 차단됩니다.
- 모드 실패를 이유로 Pro를 일반 GPT로, 또는 일반 GPT를 다른 브라우저 경로로 몰래 바꾸지 않습니다.

## 각 모드 자세히

### 일반 GPT

원래 질문에 직접 답하는 읽기 중심 모드입니다. 명시적으로 검토를 요청하지 않았다면 무조건 반례부터 찾는 검토 프롬프트로 바꾸지 않습니다.

예:

```text
GPT로 이 저장소의 세션 복구 구조를 초보자도 이해하게 설명해줘
```

### 계획 모드

처음부터 하나의 해법을 정답으로 박지 않고, 가능한 설계군을 비교한 뒤 일관된 실행 계획을 선택합니다. 위험과 반론은 계획을 만든 뒤 점검합니다.

예:

```text
GPT 계획 모드로 인증 구조 개편 계획을 작성해줘
```

### 검토 모드

후보 계획이나 구현을 공격적으로 검증하는 모드입니다. 단순한 취향이 아니라 **판정 기준, 증거, 실제 영향**이 있어야 차단 사유로 인정합니다.

예:

```text
GPT 검토 모드로 이 마이그레이션 계획의 데이터 손실 위험을 검토해줘
```

### 수정 모드

앱으로 실제 작업공간을 보고 `조사 → 수정 → 테스트 → 결과 재확인 → 필요 시 보완`을 수행합니다. 앱 호출 없이 첨부만 보고 “수정했다”고 말하는 경로는 허용하지 않습니다.

예:

```text
GPT 수정 모드로 실패한 테스트 원인을 찾고 코드를 고친 뒤 다시 테스트해줘
```

### 지휘 모드

로컬 Codex 모델의 토큰 사용을 최소화하기 위한 최상위 실행 모드입니다.

- 웹 GPT: 작업공간 탐색, 설계 판단, 파일 수정, 테스트, 결과에 따른 재수정
- 로컬 Codex: 프로젝트 잠금, 프롬프트·결과 해시, 정확한 탭/URL 추적, 결정론적 최종 검증, Git 반영
- 같은 프로젝트의 웹 제출은 1개로 직렬화하지만, 그 한 `ExecutionMission` 안의 독립 탐색·수정·테스트 lanes는 웹 GPT가 병렬 도구 호출로 운영하고 직접 통합

즉 웹 GPT가 단순 조언만 하고 구현을 Codex에 떠넘기면 지휘 모드가 끝난 것이 아닙니다.
전역의 “독립 도구 호출을 병렬화” 규칙도 작업 소유권을 넓히지 않습니다. 지휘모드에서 로컬 Codex가 이를 이유로 전략 검색, 코드 작성, 대안 구현을 병렬 실행하면 계약 위반입니다.

예:

```text
GPT 지휘 모드로 이 기능을 구현하고 테스트까지 완료해줘
```

### GPT 종합모드

복잡한 작업을 단계별로 나누되, 모든 단계를 무조건 실행해 시간을 낭비하지는 않습니다.

1. 최신 외부 근거가 필요한지 판단
2. 필요하면 심층 리서치
3. 새 일반 GPT가 계획 작성
4. 위험도와 해법 폭을 보고 웹 멀티 GPT 필요 여부 판단
5. 새 일반 GPT가 계획을 검토
6. `REVISE`면 필요한 차이만 보완
7. 실행 임무를 만들고 새 지휘 GPT가 실제 구현
8. 로컬에서 결정론적 테스트와 상태 확인

새 v4 종합모드에서는 다음 단계의 실제 프롬프트도 웹 GPT가 작성합니다. 계획 GPT가 검토 또는 멀티 GPT 질문을 만들고, 검토 GPT가 다음 계획 또는 구현 프롬프트와 임무를 만듭니다. 로컬 Codex는 그 문장을 다시 작성하지 않고 UTF-8·해시·단계 바인딩만 검증해 전달하므로 단계가 길어져도 로컬 토큰 소비가 크게 늘지 않습니다. v1/v2는 기존 실행 복구 전용이고, v3는 별도의 병렬 구현 계약입니다.

공개 배포, 보안·권한, 여러 컴포넌트가 얽힌 구조, 되돌리기 어려운 변경은 멀티 GPT가 자동으로 선택될 가능성이 높습니다.

예:

```text
GPT 종합모드로 이 프로젝트를 공개 배포 가능한 수준까지 정리해줘
```

### 병렬 구현 v3

승인된 구현 그래프를 실제 소스에 적용하는 선택적 실행 모드입니다. 하나의 `parallel-implementation` parent만 canonical 프로젝트 잠금과 staging/finalizer 권한을 가지며, 독립 컴포넌트는 `parallel-runtime-v1/worktrees/u-<24hex>` 아래 exact-unit 작업공간에서만 병렬 실행됩니다. 의존 또는 파일 경로 충돌이 있는 unit은 같은 컴포넌트로 합쳐져 결정론적 순서로 직렬화됩니다.

worker는 Git metadata에 접근하거나 Git 명령을 수행하지 않습니다. 호스트가 실제 diff와 파일 소유권, 등록된 테스트, staging metadata, listener/tunnel/app identity를 검증하고 deterministic commit을 만듭니다. 모든 required unit, component integration, full tests, canonical identity revalidation을 통과한 경우에만 ff-only로 canonical branch를 갱신합니다. 자세한 계약과 복구 규칙은 `docs/ARCHITECTURE_V3.md`를 참조하세요.

### 웹 멀티 GPT

한 대화에 여러 역할을 흉내 내는 방식이 아니라 **독립된 ChatGPT 대화 여러 개를 실제 병렬 실행**합니다.

대략 다음 역할을 거칩니다.

- Branch Designer: 서로 다른 접근 방향 분리
- Proposal Builders: 각 방향을 독립적으로 구체화
- Feasibility Engineers: 실행 가능성 보강
- Synthesis Architects: 단순 투표가 아닌 새 통합안 작성
- Gap Closers: 남은 핵심 빈틈 보완
- Rubric Judge: 기준에 따라 판정
- Decision Author / Final Responder: 최종안 정리

각 제안 GPT에는 원래 문제와 자기 분기만 전달해, 첫 계획의 서술에 모두가 끌려가는 현상을 줄였습니다. 병렬 수는 고정값이 아니라 계획 결과에 따라 정해집니다.

실제로는 서로 다른 세션·탭·canonical URL을 가진 GPT들이 동시에 실행됩니다. 공급자 동시 생성 상한은 최대 5개이므로 6~10개 lane은 `5 + 나머지` capacity wave로 실행합니다. 각 wave의 barrier는 그 wave에 실제 제출되는 자식만 기다리며, 논리적 lane 수와 결과 순서는 줄이지 않습니다.

### 심층 리서치

`@심층 리서치` 기능을 선택해 최신 외부 근거를 조사합니다. 종합모드에서는 최신성·출처 다양성·법률·시장·표준·추천 불확실성이 중요할 때 자동 게이트가 선택할 수 있습니다.

심층 리서치도 일반 GPT 계열이므로 CodexPro 앱 연결이 필수입니다.

### Pro

Pro는 다른 모드와 반대로 **앱을 절대 사용하지 않는 첨부 전용 모드**입니다. 전체 작업 지침은 UTF-8 프롬프트 파일로 만들고 필요한 자료와 함께 첨부합니다. 질문을 지나치게 반례 중심 폼에 가두지 않고, Pro가 문제 자체를 넓게 재구성하고 설계할 여지를 남깁니다.

## 프롬프트 구조

모드 이름만 바꾸고 같은 프롬프트를 재사용하지 않습니다. 프롬프트 구조 v3는 다음 요소를 분리합니다.

- 작업 종류
- 사고 프레임
- 읽기/쓰기 권한
- 제공할 맥락
- 반론·검토 강도
- 결과 형식
- 사고 예산
- 최종 결정 권한

따라서 검토 모드만 적대적 검증을 강하게 수행하고, 계획·수정·지휘 모드는 먼저 해법을 만들고 실행하는 데 집중합니다.

긴 실제 작업 지침은 채팅 입력창에 그대로 붙이지 않습니다. 엄격한 UTF-8 파일로 저장하고 SHA-256을 기록한 뒤, ChatGPT에는 그 파일을 읽으라는 짧은 고정 문장만 보냅니다. 프롬프트 잘림과 인코딩 손상을 줄이기 위한 장치입니다.

## 병렬 실행과 중복 방지

- 같은 정규화 프로젝트 루트: 활성 또는 전송 여부가 불확실한 작업 **1개만 허용**
- 다른 프로젝트 루트: 서로 다른 `agbrowse --parallel` 세션과 탭으로 병렬 실행 가능
- 출력 폴더 이름은 프로젝트 구분 기준으로 사용하지 않음
- 전송 이후 상태가 불확실하면 새 질문을 보내지 않고 기존 실행부터 판정

브라우저가 꺼졌거나 로컬 실행기가 죽어도 저장된 정확한 대화 URL을 가장 먼저 확인합니다. URL이 있으면 그 대화만 읽고 절대 세션의 오래된 루트 주소로 다시 이동시키지 않습니다. URL을 저장하지 못한 경우에만 세션 기록과 전용 읽기 전용 탭에서 실행 소유 프롬프트 파일명을 대조합니다. PID·heartbeat·잠금은 진단 정보일 뿐, 실제 작업 완료 여부보다 우선하지 않습니다.

작업의 최종 식별자는 `정확한 canonical 대화 URL + prompt-<run_id>.txt 파일명`입니다. 다른 URL·run·과거 poll을 현재 작업 증거로 섞지 않으며, 정확한 URL에서 `streaming=false`와 비어 있지 않은 현재 답변이 확인되면 stale PID·heartbeat·lock을 기다리지 않고 즉시 완료로 정산합니다. 자동화 수리 작업과 사용자 프로젝트 실행도 별도 소유 작업으로 유지합니다.

## 탭 관리

자동 종료 대상:

- 결과가 비어 있지 않음
- 공급자 응답이 실제 종료 상태
- 결과와 증거가 디스크에 안전하게 저장됨
- 그 탭이 정확히 현재 실행 소유임
- 같은 대화 URL의 살아 있는 탭이 하나뿐임

이 자동 정리 규칙은 새 실행뿐 아니라 복구된 과거 실행까지 포함하며(`including recovered legacy runs`), 별도의 두 번째 정리 요청을 요구하지 않습니다(`does not require a separate cleanup request`).

보호 대상:

- 응답 생성 중
- 전송 여부 불확실
- 사용자가 수동으로 연 탭
- 다른 프로젝트·다른 실행 소유 탭
- 소유권이 애매한 탭

앱 등록·삭제·권한 설정은 반드시 새로 만든 전용 유틸리티 탭에서만 수행합니다. 기존 대화 탭을 설정 화면으로 바꾸거나 그 탭에 입력하지 않습니다.

## 의존성

### 필수

| 항목 | 용도 |
|---|---|
| Windows 10/11 | 현재 지원 운영체제 |
| PowerShell | 설치·업데이트·복구 스크립트 |
| Python 3 | 브리지, 상태 관리, 검증 |
| Node.js 20 이상 + npm/npx | `agbrowse`, `codexpro@latest` 실행 |
| Chrome 또는 Chromium | ChatGPT 웹 작업 |
| Codex 앱 | 스킬 실행과 로컬 작업 관리 |
| ChatGPT 로그인 | 웹 GPT 사용 |
| ChatGPT 개발자 모드 | 커스텀 CodexPro 앱 등록·호출 |

### 외부 프로젝트

- [`agbrowse`](https://github.com/lidge-jun/agbrowse): 실제 브라우저 자동화 엔진. 이 저장소가 소스를 복사하거나 포크하지 않습니다.
- `codexpro`: 작업공간을 ChatGPT Developer App으로 연결하는 외부 런타임. 공개 주소를 만들 때 `codexpro@latest`를 가져옵니다.
- [`hehee9/multi-gpt`](https://github.com/hehee9/multi-gpt): 웹 멀티 GPT의 역할 토폴로지와 병합 아이디어의 기준이 된 MIT 프로젝트입니다.
- Cloudflare Quick Tunnel: 공개 기본 터널. 별도 고정 도메인 설정이 없을 때 사용합니다.

테스트 기준 `agbrowse` 버전은 `0.1.18`입니다. 영구 고정이나 백그라운드 자동 업데이트가 아니라, 설치 시 버전·npm 무결성·실행 파일 해시·공개 명령 계약을 기록하고 검증합니다. 다른 버전을 쓰려면 에이전트가 정확한 버전을 선택해 같은 검증을 통과시켜야 합니다.

## 설치

### 1. 저장소 받기

```powershell
git clone https://github.com/ventianima-lab/codexpro-automation.git
cd codexpro-automation
```

### 2. 변경 내용 미리 보기

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -WhatIf
```

### 3. ChatGPT 개발자 모드 켜기

ChatGPT 웹에서 다음 순서로 확인합니다.

1. `설정`
2. `앱`
3. `고급 설정`
4. `개발자 모드` 켜기

Business·Enterprise·Edu 같은 워크스페이스 계정은 관리자 또는 소유자의 허용이 먼저 필요할 수 있습니다. 토글 또는 `앱 만들기` 메뉴가 없다면 반복해서 앱 등록을 시도하지 말고 워크스페이스 관리자에게 권한을 요청하세요. 계정별 제공 범위와 UI는 바뀔 수 있으므로 [OpenAI 공식 개발자 모드 안내](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta)를 확인하세요.

Pro는 예외입니다. Pro 모드는 앱을 사용하지 않는 첨부 전용이므로 개발자 모드 사전조건이 적용되지 않습니다.

### 4. 설치

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

설치기는 다음 작업을 수행합니다.

1. 선택된 `agbrowse` 버전의 npm 무결성과 실행 계약을 확인
2. 필요한 브리지와 스킬만 `%USERPROFILE%\.codex`에 복사
3. 덮어쓰는 기존 파일을 날짜별 백업
4. 설치 파일 해시와 복구 영수증 저장
5. 토큰, 앱 등록 상태, 브라우저 프로필, 대화 기록은 복사하지 않음

설치 후 Codex 앱을 다시 시작하거나 새 작업을 열어야 새 스킬 목록이 반영될 수 있습니다.

### 오프라인 파일 설치

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -SkipDependencyInstall
```

이 방식은 파일만 설치합니다. 실제 GPT 작업 전에 정확한 `agbrowse` 버전을 별도로 검증해야 합니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\update.ps1 -AgbrowseVersion 0.1.18
```

## 설치 확인

```powershell
powershell -ExecutionPolicy Bypass -File .\doctor.ps1
```

`doctor.ps1`은 설치 영수증, 파일 해시, `agbrowse` 실행 파일, 버전 계약을 확인하고 문제가 있으면 필요한 명령을 알려 줍니다.

개발자용 전체 검증:

```powershell
python .\scripts\check_portability.py --root .
python -m pytest -q tests skills\chatgpt-thinking-browser\tests skills\chatgpt-pro-plan-handoff\tests skills\chatgpt-pro-browser\tests\test_codexpro_agbrowse_app.py
```

## 앱과 터널

공개판 기본값은 **드라이브별 앱 1개 + Cloudflare 동적 주소**입니다.

- 같은 드라이브의 여러 프로젝트가 하나의 드라이브 앱을 공유
- 프롬프트에서 실제 작업 경로와 허용 경로를 좁힘
- 다른 드라이브는 별도 앱·포트·공개 주소 사용
- 등록 주소가 현재 서버 주소와 같고 권한 증거가 유효하면 매 질문마다 설정 화면을 열지 않음
- 주소가 바뀌면 새 앱 후보를 먼저 연결·검증한 뒤 기존 앱 정리

고정 ngrok 주소는 선택 기능입니다. 예약 호스트, ngrok 설치·인증, 드라이브 정책을 사용자가 명시적으로 준비한 경우에만 재사용합니다. 설정이 불완전한 고정 주소를 동적 주소로 조용히 바꾸지는 않습니다.

고정 ngrok을 재사용하는 동안 로컬 CodexPro HTTP 자식만 종료되면 경량 watchdog이 같은 드라이브 루트·포트에서 로컬 서버만 다시 시작합니다. 기존 ngrok 터널과 ChatGPT 앱 등록은 교체하거나 중복 생성하지 않습니다.

## 업데이트·되돌리기·삭제

정확한 `agbrowse` 버전으로 업데이트:

```powershell
powershell -ExecutionPolicy Bypass -File .\update.ps1 -AgbrowseVersion <정확한-semver>
```

설치 전 상태로 되돌리기:

```powershell
powershell -ExecutionPolicy Bypass -File .\rollback.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File .\rollback.ps1
```

설치 파일 제거:

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```

활성 또는 상태 불확실 GPT 작업이 있으면 업데이트와 복구가 해당 작업을 죽이지 않고 보류됩니다.

## 자주 생기는 문제

### 앱을 매번 다시 확인한다

저장된 전체 MCP URL, 현재 살아 있는 서버 URL, 드라이브 루트, 권한 증거가 일치해야 확인을 생략합니다. 하나라도 바뀌었으면 설정 화면을 다시 확인하는 것이 정상입니다.

### `CHATGPT_DEVELOPER_MODE_REQUIRED`가 나온다

ChatGPT 웹에서 `설정 → 앱 → 고급 설정 → 개발자 모드`를 켜세요. `개발자 모드`나 `앱 만들기`가 보이지 않으면 현재 계정 또는 워크스페이스에 커스텀 앱 권한이 없는 것입니다. 관리자·소유자에게 권한을 요청하거나 기능이 제공되는 계정을 사용해야 합니다. 실행기는 이 상태에서 앱 등록이나 GPT 질문을 반복 전송하지 않습니다.

### 같은 프로젝트가 잠겼다고 나온다

바로 잠금 파일을 지우지 마세요. 이전 작업이 실제로 전송됐을 수 있습니다. 실행기는 기록된 세션·탭·URL과 ChatGPT 기록을 자동 대조하며, 정확히 완료 또는 미전송이 확인돼야 다음 질문을 허용합니다.

### `agbrowse` 세션이 0개라고 나온다

새 작업 전이라면 검증된 headed 런타임을 자동으로 시작할 수 있습니다. 이미 보낸 작업이라면 새 질문을 만들지 않고 저장된 세션과 URL 복구를 먼저 시도합니다.

### 작업 탭이 남았다

결과 캡처나 소유권 확인이 끝나지 않았을 수 있습니다. 완료된 정확한 소유 탭은 자동 정리되며, 불확실하거나 사용자가 연 탭은 보호됩니다. `cleanup_pending` 상태라면 같은 탭만 다시 정리합니다.

### Pro에서 앱이 안 보인다

정상입니다. Pro는 첨부 전용이며 앱 사용이 금지됩니다.

## 보안과 공개 저장소 주의사항

다음 파일과 값은 커밋하지 마세요.

- `codexpro_token` 및 기타 접근 토큰
- `.env`, 자격증명, 앱 레지스트리와 진행 중 트랜잭션
- Chrome 프로필과 ChatGPT 로그인 상태
- 실행 상태 폴더, 로그, 스크린샷, 프롬프트와 응답 원문
- 개인 PC 절대경로와 개인 고정 터널 주소
- `node_modules`

보안 문제는 공개 Issue에 비밀 값을 붙이지 말고 GitHub Security의 비공개 취약점 신고를 사용해 주세요.

## 라이선스와 고지

이 저장소의 자체 코드는 [MIT License](LICENSE)로 배포합니다. 외부 프로젝트의 라이선스와 출처는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 정리했습니다.

이 프로젝트는 개인·커뮤니티 프로젝트이며 OpenAI의 공식 제품이 아니고 OpenAI의 보증이나 후원을 받지 않습니다. OpenAI, ChatGPT, Codex 관련 이름과 상표는 각 권리자에게 있습니다.

## 더 읽기

- [설계와 상태 복구 구조](docs/ARCHITECTURE_V2.md)
- [종합모드 v4 웹 네이티브 릴레이](docs/ARCHITECTURE_V4.md)
- [모드별 프롬프트 구조](docs/gpt55-operation-mode-prompts.md)
- [지휘 모드 운영 규칙](docs/codexpro-gpt55-orchestrator-runbook.md)
- [디시인사이드 게시글용 소개문](docs/DCINSIDE_POST_KO.md)
