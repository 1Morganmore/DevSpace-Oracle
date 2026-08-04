# 기술 변경 기록

README는 현재 제품의 목적과 사용법만 설명합니다. 구현 변경, 호환 패치,
레거시 이전 기록은 이 문서에서 관리합니다.

## 현재 릴리스

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
- CodexPro와 agbrowse 신규 제출 경로는 동결했습니다.

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
- 기본 설치는 동결된 agbrowse/CodexPro 의존성을 설치하거나 갱신하지 않습니다.
- portability, fast gate, golden-path, v3/v4 계약 테스트를 CI에서 실행합니다.

## 레거시 기록

과거 CodexPro·agbrowse 기반 v1~v4 실행기와 goal supervisor는 새 작업을
만들 수 없습니다. 이미 저장된 실행을 원래 신원으로 복구할 때만 사용합니다.
자세한 목록은 [FROZEN_LEGACY.md](FROZEN_LEGACY.md)에 있습니다.

세부 커밋 단위 변경은 Git 로그와 GitHub Releases/Actions를 권위 기록으로
사용합니다.
