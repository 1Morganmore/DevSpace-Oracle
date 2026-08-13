# 기술 변경 기록

README는 현재 제품의 목적과 사용법만 설명합니다. 구현 변경, 호환 패치,
레거시 이전 기록은 이 문서에서 관리합니다.

## 현재 릴리스

### 1.8.1 DevSpace 등록 후 연결 안정화

- 수동 ChatGPT 앱 등록·재연결 직후 기존 DevSpace 설정, Owner 자격, OAuth DB,
  허용 루트와 Funnel 주소를 보존하면서 관리 서비스를 한 번 재순환하는 명시적
  `post-register` CLI 단계를 추가했습니다. 등록 앱 검증은 일반(non-Pro) Oracle
  `@codex` 읽기 검사로 분리하며, 자동 등록·재등록·반복 refresh는 하지
  않습니다. doctor의 실패 분기는 일회 `post-register` 안내로 변경했습니다.
- `post-register`는 먼저 로컬 `/healthz`의 정확한 DevSpace 신원을 확인한 뒤,
  status가 정확한 host+HTTPS port의 단일 `/` handler이고 정확한
  `http://127.0.0.1:<local_port>` proxy일 때만 scoped
  `tailscale funnel --bg --https=<port> off`로 exclusive HTTPS 슬롯을
  재순환하고 같은 target으로 재수립합니다. 전체 `tailscale funnel reset`은
  사용하지 않으며, 같은 port의 추가 path handler·충돌 매핑·다른 port는 절대
  제거하지 않고 충돌은 mutation 전에 fail-closed합니다. 재수립 후 공개
  `/healthz`의 정확한 신원을 다시 확인합니다.
- 기존 `%USERPROFILE%\.devspace\config.json`이 있으면 `setup --apply`가
  interactive `devspace init`을 건너뛰고, 설정을 백업한 뒤 전체
  `allowedRoots`를 atomic replace+readback으로 병합합니다. Owner/OAuth/기타
  키는 보존하며, symlink나 invalid(JSON/단계) 설정은 mutation 없이
  fail-closed합니다.

### 1.8.0 상위 런타임 갱신

- 지원 Node.js 범위를 `>=24 <27`로 올리고 프로젝트 릴리스를 1.8.0으로
  갱신했습니다.
- 신규 실행은 해시 검증한 Oracle `0.17.2`와 DevSpace `1.0.7`을 사용합니다.
  DevSpace 승격은 workspace 재사용 안내와 unknown-workspace 오류 문구를
  수용하며, 기존 OAuth discovery와 bounded traversal 패치를 새 npm dist
  바이트에 다시 해시 결합합니다. 자동시작과 명시적 재시작은 호환 패치 전에
  정확한 1.0.7 패키지를 npm 캐시에 준비하므로 버전 승격 직후에도 fail-closed
  패치 검증이 새 패키지를 찾을 수 있습니다. Oracle `0.16.1`, `0.17.0`,
  `0.17.1`은 이미 저장된 해당 버전 실행의 정확한 복구에만 남깁니다.
- 일반 작업은 `GPT-5.6 Sol` + 보이는 `Extra High`(`Power 4 of 5`)만 지원하고
  `Medium`/`High` 별칭은 거부하며, Pro는 첨부 전용 `gpt-5.6-sol` +
  `heavy`(보이는 `Power 5 of 5`/`Pro` 증명)를 유지합니다. Web Multi는 명시적으로
  선택한 경우에만 실행하며 자동 fallback으로 사용하지 않습니다.
- Oracle `0.17.2`가 보고하는 `Power 4 of 5 (Extra High)`와
  `Power 5 of 5 (Pro)` 사전제출 거부를 정확한 실행 프로필에 결합해
  `not_executed`로 정산합니다. 다른 Power 등급은 exact-session 잠금을 유지합니다.
  업그레이드 전에 시작된 Oracle `0.17.1` 실행도 해당 버전의 레거시
  Extra High/Pro Heavy 거부 증거가 완전하면 같은 `not_executed` 정산 경로로
  복구해 잘못 유지된 프로젝트 잠금을 해제합니다.
- ChatGPT의 현재 Advanced picker가 `aria-controls` 대상 subtree 밖이지만 같은
  visible menu 안에 Power slider를 배치하는 구조를 수용합니다. 메뉴 범위 확장은
  effort pill의 가장 가까운 visible menu가 해당 `aria-controls` root를 실제로
  포함할 때만 허용합니다. 교차 menu root, 다른 menu의 slider, 숨은 조상,
  불일치 모델·Effort는 계속 거부합니다. 이전 1.8.0 패치 해시는 exact reverse
  patch로 pristine에 복원한 뒤 새 해시 결합 패치로 승격합니다.
- 현재 ChatGPT가 크기를 유지한 simple Power readout 자체는 `opacity: 0`으로,
  내부 ARIA slider는 `0..4` 범위로 렌더링하는 구조를 수용합니다. 완전한
  zero-based 범위만 표시 Power `1..5`로 변환하고, 부분·비지원 범위나 유효하지
  않은 control 값과 control/text 불일치는 모두 거부합니다.
  opacity 예외는 정확한 slider test-id 노드에만 적용하며 조상 picker·effort
  pill·coherent Advanced view의 표시와 양의 opacity는 계속 요구합니다.
- 같은 정규화 `CODEX_HOME`을 대상으로 한 설치는 Windows named mutex로
  단일 writer만 허용합니다. 경쟁 설치는 파일 변경 전에 거부하고, 비정상 종료로
  버려진 mutex 소유권은 기존 WAL crash recovery를 계속 수행할 수 있게 인수합니다.
- 업그레이드 영수증 선택은 각 유효 chain head에서 현재 파일을 소유하는 가장
  가까운 ancestor를 제거 기준으로 사용하되, 새 `previous_receipt`는 그 branch의
  실제 head를 가리킵니다. rollback 뒤 재설치도 선형 이력을 유지하고, fork,
  cycle, 다중 영수증인데 현재 owner가 없는 상태는 mutation 전에 거부합니다.

## 이전 릴리스

### 1.7.0 신뢰성·보안 보강

- 설치 트랜잭션은 기존 WAL v1을 읽을 수 있는 상태로 보존하면서 새 WAL v2에
  영수증 결합과 오류·충돌 복구 상태를 기록합니다.
- 빠른 게이트가 하위 프로세스 트리까지 제한 시간 안에 종료하고 시간 초과를
  종료 코드 124로 보고합니다.
- Local Multi-GPT 작업 상태를 원자적으로 저장하고 재시작 소유권을 보수적으로
  판정하며, 출력·실행 시간·동시 자식 수를 제한합니다.
- 파일 기반 작업은 호스트가 지정한 좁은 root 안의 UTF-8 비민감 자료만 받아
  상대 경로와 SHA-256 증거로 전달합니다.

### Oracle + DevSpace 단일 실행 경로

- 일반 GPT, 계획, 검토, 수정, 지휘, 심층 리서치, 종합모드와 Web
  Multi-GPT를 Oracle + DevSpace로 통일했습니다.
- Pro는 Oracle 첨부 전용이며 DevSpace를 사용하지 않습니다.
- 신규 제출은 Oracle과 DevSpace 경로만 사용합니다.

### Windows 브라우저 실행 격리

- 실행마다 로그인 프로필의 throwaway 복사본을 사용합니다.
- Windows에서는 Node 내장 복사로 프로필을 만들며 rsync를 요구하지 않습니다.
- 각 Oracle 실행이 소유한 숨김 Chrome만 정리합니다.

### 장기 작업과 복구

- 비Pro 작업은 기본 `--browser-timeout 90m`을 사용합니다.
- Oracle의 재로드·fallback은 같은 90분 예산의 남은 시간만 사용합니다.
- CDP 호출이 멈춰도 host watchdog이 30초 grace 뒤 동일 세션을 보존한 채
  `attention_required`로 반환합니다.
- 제출 후 로컬 종료·브라우저 연결 끊김은 `attention_required`로 보존합니다.
- 복구는 저장된 정확한 slug와 대화 URL만 사용하고 새 질문을 보내지 않습니다.
- terminal 상태는 이후 관찰에서 live로 되돌아가지 않습니다.

### 종합모드

- plan → optional Pro/Web Multi → review → implementation → final web gate
  → local deterministic gate 순서를 사용합니다.
- 각 단계는 다음 미션과 workflow/stage/attempt/input-SHA 결합 영수증을
  직접 작성합니다.
- review 단계가 수정 가능한 계획 결함을 직접 고치고 구현 미션을 확정합니다.
- Pro 증거 파일은 `[PRO_ATTACHMENT_CONTRACT]`에 선언된 파일만 첨부합니다.
- 손상된 Pro JSON은 신원이 정확히 일치하는 제한된 경우에만 감사 기록과
  함께 복구합니다.

### Web Multi-GPT

- 독립 Oracle solver 2~25개를 최대 5개씩 wave로 실행합니다.
- Windows lane마다 별도 프로필을 사용합니다.
- 각 solver는 짧은 handoff 파일을 만들고 merger 하나가 안정된 순서로
  결과를 병합합니다.

### 설치와 릴리스

- 설치 전 파일을 백업하고 durable 영수증을 남깁니다.
- 기본 설치는 외부 legacy browser 의존성을 설치하거나 갱신하지 않습니다.
- portability, fast gate, golden-path, focused/full release 계약 테스트를 CI에서 실행합니다.

## 제거 기록

과거 browser runtime과 goal supervisor는 현재 패키지에서 제거되었습니다.
사용자 상태와 출력은 설치·업그레이드 과정에서 보존합니다.
현재 upstream 차이는 [VS_UPSTREAM.md](VS_UPSTREAM.md)에 있습니다.

세부 커밋 단위 변경은 Git 로그와 GitHub Releases/Actions를 권위 기록으로
사용합니다.
