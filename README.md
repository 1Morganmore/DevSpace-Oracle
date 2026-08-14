# Codex Web GPT Orchestrator

한국어 | [English](README.en.md)

Codex가 웹 ChatGPT에 계획·리서치·검토·코드 구현을 맡기고, 로컬 Codex는
제출·복구·해시·최종 테스트만 담당하도록 만드는 Windows용 자동화 도구입니다.

현재 릴리스는 `1.8.1`입니다.

이 프로젝트는 다음 두 도구를 연결합니다.

- [Oracle](https://github.com/steipete/oracle): 로그인된 ChatGPT 브라우저
  세션 생성, 모델 선택, 응답 대기와 결과 회수
- [DevSpace](https://github.com/Waishnav/devspace): 사용자가 허용한 로컬
  프로젝트의 파일 읽기·쓰기와 명령 실행

일반 GPT 작업은 Oracle이 `GPT-5.6 Sol`과 보이는 `Extra High` 등급
(`Power 4 of 5`)을 확인한 뒤 정확히 `@DevSpace`와 절대 UTF-8 미션 파일
경로만 ChatGPT에 전달합니다. Pro 작업은 `gpt-5.6-sol`과 증명된
`Power 5 of 5`(`Pro`) 등급, 정확한 첨부 파일만 사용하며 DevSpace를
사용하지 않습니다.

## 이 도구로 할 수 있는 일

- 웹 GPT가 로컬 프로젝트를 읽고 직접 수정·테스트
- 계획, 검토, 수정, 지휘, 심층 리서치 모드
- 여러 독립 ChatGPT 세션을 동시에 실행하는 Web Multi-GPT
- PC 로컬 Codex 레인을 병렬 실행하는 읽기 전용 Local Multi-GPT
- 계획 → 검토 → 구현 → 최종 검증을 연결하는 종합모드
- 프로젝트별 실행 잠금, 미션·첨부 해시, 정확한 세션 복구
- 다른 프로젝트의 ChatGPT 작업과 분리된 브라우저 프로필
- 작업 완료 후 Oracle 소유 대화 자동 보관
- 설치 파일 백업, 설치 영수증, 롤백

## 동작 구조

```text
사용자 요청
    ↓
Codex가 UTF-8 미션 파일과 실행 manifest 작성
    ↓
Oracle이 로그인된 ChatGPT 세션 실행
    ├─ 일반 GPT: @DevSpace + 미션 경로
    └─ Pro: 미션 + 고정 해시 첨부 파일
    ↓
웹 GPT가 프로젝트 탐색·계획·구현·테스트
    ↓
Oracle이 결과를 로컬 파일로 회수
    ↓
Codex가 해시·상태·최종 결정론적 테스트만 확인
```

호스트 상태와 ChatGPT 출력은 DevSpace 프로젝트 밖의
`%USERPROFILE%\.codex\state\chatgpt-oracle`에 저장됩니다.

## 모드

| 모드 | CLI/영어 이름 | 용도 | 실행 방식 |
|---|---|---|---|
| 일반 GPT | `direct` / GPT | 질문·분석·작은 작업 | Oracle + DevSpace, 단일 세션 |
| 계획 | `plan` / plan | 구현 전 설계 | Oracle + DevSpace, 읽기 전용 |
| 검토 | `review` / review | 코드·계획의 독립 검토 | Oracle + DevSpace, 읽기 전용 |
| 수정 | `edit` / edit | 정해진 범위의 수정·테스트 | Oracle + DevSpace |
| 지휘 | `orchestrator` / orchestrator | 계획이 확정된 작업을 한 GPT가 끝까지 수행 | Oracle + DevSpace, 단일 세션 |
| 심층 리서치 | `deep-research` / deep research | 공개 자료와 프로젝트 증거 조사 | Oracle Deep Research + DevSpace |
| Web Multi-GPT | Web Multi-GPT | 여러 관점의 독립 탐색·검증 | 독립 Oracle 세션 2~25개 + merger |
| Local Multi-GPT | Local Multi-GPT | 로컬 병렬 자문·반례 탐색 | `gpt-5.6-luna` + `max` 고정, 읽기 전용 |
| 종합모드 | comprehensive mode | 계획부터 구현·최종 게이트까지 자동 연결 | plan → 명시적으로 선택한 Pro/Web Multi → review → implementation → gate |
| Pro | `pro` / Pro | 독립적인 최종 판단·설계 검토 후 결과만 반환 | Oracle `gpt-5.6-sol` + `Power 5 of 5`, 첨부 전용, DevSpace 없음 |

지휘는 웹 제출 한 번으로 끝나는 실행 모드입니다. 종합모드는 지휘와 같은
구현 단계를 포함하면서 계획·독립 검토·선택적 Pro/Web Multi·최종 게이트를
추가한 다단계 워크플로입니다. Web Multi는 명시적으로 선택한 경우에만
실행하며 일반 작업이나 실패한 작업에서 자동으로 전환하지 않습니다.

단순 Pro는 종합모드와 별개인 한 번짜리 검토 경로입니다. 첨부된 계획·코드·문서를
검토하고 결과 파일을 반환하면 끝나며, 자동으로 구현이나 다음 단계로 넘어가지
않습니다. 계획부터 구현까지 이어야 할 때만 종합모드를 사용합니다.

Local Multi-GPT와 Web Multi-GPT는 서로 다른 경로입니다. Local Multi-GPT는
PC의 Codex 하위 레인을 사용하는 선택적 자문 도구이며, 모든 단계가
`gpt-5.6-luna`와 `max` 사고 레벨로 고정됩니다. 다른 모델이나 사고
레벨을 요청하면 하위 프로세스를 시작하기 전에 거부합니다. Web Multi-GPT는
Oracle이 여러 독립 ChatGPT 웹 세션을 실행한 뒤 결과를 병합합니다.

파일을 사용하는 Local Multi-GPT 작업은 MCP 호스트가 좁은 절대 경로 목록을
JSON 배열로 `MULTI_GPT_ALLOWED_ROOTS_JSON`에 설정한 경우에만 실행됩니다.
파일시스템 루트와 홈 전체는 거부하며, 정규 경로·링크·민감 파일·UTF-8·크기와
SHA-256을 하위 프로세스 시작 전에 검사합니다. 작업 상태는 원자적으로 저장되고,
재시작 후 소유 프로세스가 확실히 종료된 작업은
`failed / ORPHANED_AFTER_RESTART`가 됩니다. 살아 있거나 소유권이 불명확한
외부 작업은 보존하며 새 서버에서 취소하지 않습니다.

## 요구사항

- Windows 11
- Python
- Node.js 24 이상, 27 미만
- Git for Windows / Git Bash
- Tailscale
- 브라우저에서 ChatGPT에 로그인된 Oracle 프로필
- ChatGPT Developer Mode에 최초 한 번 수동 등록한 DevSpace 앱

현재 검증된 조합은 Oracle `0.17.3`와 DevSpace `1.0.7`입니다. 설치기는
정확한 파일 해시가 일치할 때만 Windows 호환 패치를 적용합니다. Oracle
0.17.3의 상위 변경(답변 placeholder 제한, manual-login 재연결의 명시적
쿠키 동기화 opt-in, 일본어 picker 라벨, 명시적 headless 처리)은 로컬
hash-gated 패치 아래 그대로 보존하며 아직 라이브 브라우저 검증은 하지
않았습니다. Oracle `0.16.1`, `0.17.0`, `0.17.1`, `0.17.2`는 이미 저장된
해당 버전 실행의 정확한 복구에만 사용할 수 있습니다.

## 설치

```powershell
git clone https://github.com/1Morganmore/DevSpace-Oracle.git
cd DevSpace-Oracle
.\install.ps1 -WhatIf
.\install.ps1
```

설치기는 기존 파일을 백업하고
`%USERPROFILE%\.codex\receipts`에 설치 영수증을 남깁니다.

## DevSpace 최초 연결

DevSpace 앱은 프로젝트마다 설치하는 것이 아닙니다. 앱 하나에 허용할
프로젝트 루트를 여러 번 `--root`로 지정합니다.

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py setup `
  --root C:\projects\alpha `
  --root C:\projects\beta `
  --hostname your-device.your-tailnet.ts.net `
  --public-port 8443 `
  --dry-run
```

내용을 확인한 뒤 `--dry-run`을 `--apply`로 바꿉니다. ChatGPT Developer
Mode에는 다음 앱 하나만 수동으로 등록합니다.

- 이름: `DevSpace`
- URL: `https://your-device.your-tailnet.ts.net:8443/mcp`

Owner 승인을 완료한 뒤에는 매 작업마다 앱 목록·권한·URL을 다시 확인하거나
앱을 재등록하지 않습니다. 새 프로젝트는 DevSpace 허용 루트에만 추가합니다.
ChatGPT 설정·앱 목록·권한·삭제·선택 UI를 자동화하지 않습니다.

자세한 과정은
[DevSpace + Tailscale 설정](docs/DEVSPACE_TAILSCALE_SETUP.md)을
참고하세요.

허용 경로만 바꿀 때는 Owner 인증을 다시 만들지 않습니다. 현재 값을 확인하고,
교체할 전체 목록을 미리 본 뒤 적용합니다.

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py roots
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py roots `
  --root C:\projects\alpha --root D:\work\beta --dry-run
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py roots `
  --root C:\projects\alpha --root D:\work\beta --apply --restart
```

`roots`는 `config.json`의 `allowedRoots`만 원자적으로 교체하고 다른 설정과
`auth.json`은 보존합니다. `--restart`를 생략하면 다음 DevSpace 재시작 때 반영됩니다.

## 일반 GPT 실행 예시

프로젝트 안에 UTF-8 미션 파일을 만든 뒤 먼저 미리보기 합니다.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" `
  --mode orchestrator `
  --project-root C:\project `
  --mission-path C:\project\mission.md `
  --manifest-output C:\project\.ai-bridge\oracle.json `
  --reasoning-level "Very High" `
  --chatgpt-project-url https://chatgpt.com/g/g-p-example/project `
  --dry-run
```

`--chatgpt-project-url`은 선택 사항입니다. 지정하면 Oracle이 그 ChatGPT Project에서
새 채팅을 만듭니다. 이름을 퍼지 검색하지 않고 정확한
`https://chatgpt.com/g/<id>/project` URL만 받습니다. 생략하면 기존처럼 일반 새
채팅을 사용합니다. 같은 필드는 Pro, 종합모드, Web Multi-GPT 매니페스트에도
사용할 수 있습니다.

실제 실행 승인이 있을 때만 `--dry-run`을 제거하고, 같은 명령에 미리보기의
최상위 `oracle_manifest_sha256`을
`--expected-manifest-sha256 <oracle_manifest_sha256>`로 전달합니다.

## Pro 실행 예시

Pro는 프로젝트 앱을 사용하지 않습니다. 미션과 필요한 증거 파일을 정확한
해시로 고정해 첨부합니다.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" `
  --mode pro `
  --project-root C:\project `
  --mission-path C:\project\pro.md `
  --context-manifest C:\project\.ai-bridge\pro-context-manifest.json `
  --attachment C:\project\.ai-bridge\packet.zip `
  --manifest-output C:\project\.ai-bridge\pro.json `
  --chatgpt-project-url https://chatgpt.com/g/g-p-example/project `
  --dry-run
```

## 실행과 복구 원칙

- 같은 프로젝트에는 활성 또는 불확실한 Oracle 작업 하나만 허용합니다.
- 다른 프로젝트는 서로 분리된 프로필로 병렬 실행할 수 있습니다.
- Web Multi-GPT는 하나의 부모 작업 안에서 최대 5개 세션씩 wave로 실행합니다.
- 비Pro의 무거운 작업은 1차 90분과 복구 90분, 실효 약 180분까지 기다립니다.
- 브라우저나 로컬 프로세스 종료는 웹 작업 실패의 증거가 아닙니다.
- 복구는 저장된 정확한 Oracle slug와 대화 URL만 사용하며 재제출하지 않습니다.
- 완료에는 Oracle 종료 코드 0과 비어 있지 않은 새 결과 파일이 모두 필요합니다.

정확한 실행을 회수하려면:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" recover `
  --run-dir C:\exact\oracle-run `
  --action harvest
```

ChatGPT를 열거나 실행 상태를 만들지 않고 로컬 런타임 준비 상태를 확인하려면:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" preflight `
  --manifest C:\project\.ai-bridge\oracle.json `
  --expected-manifest-sha256 <oracle_manifest_sha256>
```

DevSpace 실행의 preflight는 정확한 Oracle/DevSpace 패키지 해시, 로그인 프로필
seed, 프로젝트 소유권, 실행 중인 DevSpace 프로세스, Tailscale Funnel, 로컬·공개
`/healthz` identity를 확인합니다. 브라우저 실행, 프롬프트 제출, 호환성 패치 적용,
ChatGPT 로그인·앱 UI 검사는 하지 않습니다. Tailscale 자체 호스트명은 자동 감지하며
필요할 때만 `--devspace-hostname`으로 지정합니다.

Preflight 출력은 현재 상태에 대한 읽기 전용 참고 증거이며 제출 권한을 저장하지
않습니다. 실제 DevSpace `run`은 기존 프로젝트 submit mutex 안에서 Tailscale
호스트명, Funnel 매핑, 로컬·공개 `/healthz` identity를 다시 확인합니다. 실패하면
Oracle 브라우저를 시작하지 않고 구조화된 `SUBMISSION_NOT_READY` 증거를 exact run
state에 남긴 뒤 안전한 pre-submit 실패로 종료합니다.

저장된 정확한 실행을 변경 없이 분류하거나 감시하려면:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_diagnose.py" triage --run-dir C:\exact\oracle-run
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_diagnose.py" watch --run-dir C:\exact\oracle-run
```

정상 완료 데스크톱 알림은 기존 Oracle이 담당합니다. `watch`는
`attention_required`를 포함한 host 상태 변화를 NDJSON으로 알리며 복구하거나
재제출하지 않습니다.

## 업데이트와 제거

```powershell
.\install.ps1 -WhatIf
.\install.ps1
.\rollback.ps1
.\uninstall.ps1
```

## 문서

- [전역 ChatGPT 라우팅과 모드 선택](docs/GLOBAL_CHATGPT_ROUTING.md)
- [DevSpace + Tailscale 최초 설정](docs/DEVSPACE_TAILSCALE_SETUP.md)
- [기술 변경 기록](docs/CHANGELOG.md)
- [Upstream 대비 변경점](docs/VS_UPSTREAM.md)
- [릴리스 검증 절차](docs/RELEASE_CHECKLIST.md)
- [보안 정책](SECURITY.md)
- [제3자 라이선스](THIRD_PARTY_NOTICES.md)

## 라이선스

MIT License. Oracle·DevSpace 등 제3자 구성요소의 저작권과 라이선스는
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 정리되어 있습니다.
