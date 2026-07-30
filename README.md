# Oracle GPT Automation for Codex

Codex가 웹 ChatGPT에 계획·검토·수정·구현을 맡기고 로컬 토큰 사용을
줄이기 위한 Windows 자동화입니다.

새 기본 경로는 두 공개 프로젝트를 수정하지 않고 조합합니다.

- [Oracle](https://github.com/steipete/oracle): ChatGPT 브라우저 세션,
  응답 대기·회수, 완료 대화 보관
- [DevSpace](https://github.com/Waishnav/devspace): 허용한 로컬
  작업공간의 읽기·쓰기·명령 실행 MCP

일반 GPT부터 Pro까지 **모든 신규 브라우저 실행은 Oracle 하나로 통일**합니다.
과거 `agbrowse`·CodexPro 기반 파일은 삭제하지 않았지만 새 질문에는 절대
쓰지 않습니다. 기존 실행의 정확한 복구와 롤백 증거로만 남아 있는
**동결(frozen) 자산**입니다. 어떤 실패에서도 Oracle이
agbrowse·CodexPro·in-app Browser·`@chrome`·Playwright/CDP로 자동
전환하지 않습니다. 자세한 경계는
[동결 자산 안내](docs/FROZEN_LEGACY.md)를 참고하세요.

## 왜 단순해졌나

이전 경로는 매 질문마다 앱 등록·URL·권한을 확인하고, 설정 탭에서 앱을
선택하며, 별도의 탭/락/heartbeat 장부로 완료를 추정했습니다. 새 경로는:

1. DevSpace를 최초 한 번 수동 등록
2. ChatGPT에는 `@DevSpace`와 절대 미션 파일 경로만 전달
3. Oracle이 자기 세션과 완료 회수·archive를 담당
4. 로컬은 host-only 상태, 해시, 마지막 결정론적 검사만 담당

ChatGPT 설정·앱 목록·권한·삭제·선택 UI를 자동화하지 않습니다.
기존 전역 규칙에서 agbrowse·CodexPro를 일반 GPT 기본값으로 지정했다면
[전역 라우팅 안내](docs/GLOBAL_CHATGPT_ROUTING.md)에 맞춰 제거하세요.

## 모드

아래 9개가 현행 모드 전부입니다. 표에 없는 실행 경로는 동결 자산입니다.

| 모드 | 호출 키워드 | 새 실행 경로 | 담당 스킬 |
|---|---|---|---|
| 일반 GPT | `GPT`, `지피티` | Oracle direct + DevSpace | `chatgpt-thinking-browser` |
| 계획 | `계획` | Oracle plan + DevSpace | `chatgpt-thinking-browser` |
| 검토 | `검토` | Oracle review + DevSpace | `chatgpt-thinking-browser` |
| 수정 | `수정` | Oracle edit + DevSpace | `chatgpt-thinking-browser` |
| 지휘 | `지휘모드` | Oracle orchestrator + DevSpace | `chatgpt-thinking-browser` |
| 심층 리서치 | `심층 리서치` | Oracle browser research deep + DevSpace | `chatgpt-deep-research-browser` |
| 종합모드 | `종합모드` | Oracle plan → optional Pro/Multi → review → implementation → final web gate → local gate | `chatgpt-pro-plan-handoff` |
| Web Multi-GPT | `웹 멀티 GPT` | 독립 Oracle solver 2~25개, 최대 5개씩 wave, 별도 merger | `web-multi-gpt` |
| Pro | `프로` | Oracle attachment-only, DevSpace 금지 | `chatgpt-pro-browser` |

공통 실행기는 `chatgpt-oracle-runtime`이고, 질문 설계는
`chatgpt-question-designer`, 최초 1회 연결 설정은
`chatgpt-workspace-setup`이 담당합니다.

Oracle 0.16.1의 통합 모델 선택 UI에 맞춘 해시 검증형 호환 패치를
적용합니다. 일반·계획·검토·수정·지휘·종합·Web Multi는
`GPT-5.6 Sol`과 `heavy`를 선택하고 화면의 `Extra High(매우 높음)`을
검증합니다. 알려지지 않은 Oracle 버전이나 파일 해시는 수정하지 않고
차단하며, xhigh를 만들거나 자동 하향하지 않습니다.

## 설치

Node.js `22.19 이상, 27 미만`, Git Bash, Python, 로그인된 ChatGPT
브라우저 환경이 필요합니다.

```powershell
git clone https://github.com/ventianima-lab/codexpro-automation.git
cd codexpro-automation
.\install.ps1
```

설치기는 현재 파일을 백업하고 영수증을 남깁니다. 미리보기:

```powershell
.\install.ps1 -WhatIf
```

기본 설치는 동결된 agbrowse/CodexPro 의존성을 설치하거나 갱신하지
않습니다. 이미 저장된 구형 실행을 복구해야 하는 컴퓨터에서만
`-InstallLegacyRecoveryDependency`를 명시하세요.

## DevSpace 최초 연결

DevSpace는 로컬 사용자 권한으로 파일과 명령을 다루므로 드라이브 전체가
아닌 필요한 프로젝트만 허용하세요.

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py setup `
  --root C:\projects\one `
  --hostname your-device.your-tailnet.ts.net `
  --dry-run
```

검토 후에만 `--apply`를 사용합니다. ChatGPT Developer Mode에서 다음을
한 번 수동 등록하고 Owner 승인을 완료합니다.

- 앱 이름: `DevSpace`
- MCP URL: `https://your-device.your-tailnet.ts.net/mcp`

기존 Funnel 443 매핑이 있으면 덮어쓰지 않고 차단합니다. 이때
`--public-port 8443`처럼 사용 가능한 Tailscale Funnel 포트를 명시하고
등록 URL에도 같은 포트를 넣습니다.

자동화는 이 등록을 확인·수정·삭제하지 않습니다. 자세한 내용은
[DevSpace + Tailscale 설정](docs/DEVSPACE_TAILSCALE_SETUP.md)을
참고하세요.

## 일반 모드 실행

프로젝트 안에 UTF-8 미션 파일을 만든 뒤 먼저 dry-run 합니다.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" `
  --mode orchestrator `
  --project-root C:\project `
  --mission-path C:\project\mission.md `
  --manifest-output C:\project\.ai-bridge\oracle.json `
  --reasoning-level "Very High" `
  --dry-run
```

실제 웹 실행 승인이 있을 때만 `--dry-run`을 뺍니다. 일반 모드에는
첨부를 사용하지 않습니다.

## Pro 첨부 전용 실행

Pro도 Oracle을 사용하지만 DevSpace를 호출하지 않습니다. 짧은 UTF-8
지시 파일과 정확한 첨부 파일을 지정하고 먼저 미리봅니다.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" `
  --mode pro `
  --project-root C:\project `
  --mission-path C:\project\pro.md `
  --attachment C:\project\packet.zip `
  --manifest-output C:\project\.ai-bridge\pro.json `
  --dry-run
```

첨부 파일과 해시, Pro 모델 선택, 출력 위치가 정확할 때만 실제 실행합니다.
Oracle 실패를 일반 GPT나 agbrowse로 자동 전환하지 않습니다.

## 종합모드

`codex.chatgpt.oracle-comprehensive/v1` manifest를
`chatgpt_oracle_comprehensive.py`에 전달합니다. 웹 단계가 다음 미션과
작은 영수증을 직접 작성하며 로컬은 문장을 다시 쓰지 않습니다.

- 영수증은 workflow/stage/attempt/input SHA에 결합
- PASS + output SHA + 다음 mission SHA만 전이
- 실패·누락·재시작 불명확 시 새 제출 없이 attention-required
- 마지막 웹 PASS 뒤 지정한 로컬 검사 한 번만 실행

## 실제 Web Multi-GPT

`chatgpt_oracle_multi.py`는 한 GPT의 역할극이 아니라 서로 다른 Oracle
세션을 실행합니다. 6개 이상도 lane을 줄이지 않고 최대 5개씩 wave로
나눕니다. 읽기 전용 solver는 같은 작업공간을 볼 수 있지만 쓰기 solver는
서로 다른 사전 생성 Git worktree를 사용해야 합니다. 결과는 짧은 handoff
파일로 남기고 merger 한 개가 모두 읽습니다. Windows에서는 각 lane이
로그인된 Oracle 프로필의 독립 throwaway 복사본을 사용하므로 실제로
서로 다른 Chrome 세션이 동시에 동작합니다.

## 상태와 복구

실행 장부·Oracle 최종 출력·transcript는 DevSpace가 쓸 수 없는
`%USERPROFILE%\.codex\state\chatgpt-oracle` 아래에 저장됩니다.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" recover `
  --run-dir C:\exact\host-run `
  --action harvest
```

새 실행은 로그인된 Oracle 프로필의 실행별 임시 복사본과 숨김 창을
사용하므로, 다른 프로젝트의 완료 작업이 진행 중인 Chrome을 함께 닫지
않습니다. 복구는 저장된 slug만 사용하며 재시작·재제출하지 않습니다.
기존 CDP가 끊겼다면 저장된 프로필 원본으로 복구 창을 만들고 그 slug에
기록된 정확한 대화 URL만 다시 열어 결과를 회수합니다. durable COMPLETE를
failed로 되돌리지 않습니다. 한 관찰자가 terminal을 본 뒤 다른 관찰자가
running을 반환해도 상태를 live로 되돌리지 않고 동일 프로젝트 잠금을
유지합니다. 이후 exact terminal과 비어 있지 않은 새 결과를 함께 확인해야만
COMPLETE로 정산합니다.

Oracle을 실제로 제출한 뒤 브라우저 응답 시간 초과나 로컬 프로세스 비정상
종료가 발생해도 웹 세션 실패로 단정하지 않습니다. 이 상태는
attention-required와 submitted-unknown 권위로 남아 같은 프로젝트를
보호하며, 저장된 정확한 slug의 live/harvest 회수만 허용합니다.

종합모드의 검토 GPT는 단순 지적자가 아니라 계획 수정·확정 책임자입니다.
현재 미션, DevSpace 작업공간, 프로젝트 규칙과 증거로 고칠 수 있는 결함은
검토 단계 안에서 직접 고쳐 최종 계획과 구현 미션을 작성합니다. `PASS`와
`PASS_WITH_NOTES`는 곧바로 구현으로 진행하며 참고 사항도 구현 미션 안에
전달됩니다. 새 작업은 `REVISE`를 만들지 않습니다. 과거 `REVISE` 영수증은
호환용으로만 읽고 새 계획을 만들지 않은 채 사용자 확인 필요 상태로
정산합니다. 실제 외부 입력·권한 부재, 해결되지 않은 안전 경계, 실행 자체가
불가능한 경우에만 `FAIL`로 멈춥니다.

각 웹 단계는 정확한 프로젝트 루트와 입력 미션 경로를 고정합니다.
DevSpace는 그 루트와 정확히 일치하는 작업공간만 재사용하거나 열며, 목록
확인 뒤 동일 루트 재시도는 한 번뿐입니다. 상위 루트, 하위 폴더, 이름이
비슷한 작업공간, 현재 활성 작업공간, 셸 경계 우회로 범위를 바꾸지
않습니다. 미션과 적용되는 `AGENTS.md`를 먼저 끝까지 읽은 뒤 탐색·수정을
시작합니다.

## 업데이트·롤백·제거

```powershell
.\update.ps1
.\rollback.ps1
.\uninstall.ps1
```

`update.ps1`은 이 저장소가 배포하는 파일만 갱신합니다. 기본 실행은
동결된 agbrowse·CodexPro 의존성을 설치하거나 갱신하지 않으며, 그 상태
파일도 삭제하지 않습니다. 예전 실행을 복구해야 하는 컴퓨터에서만
`-InstallLegacyRecoveryDependency`를 명시하세요.

## 보안과 라이선스

프로젝트는 MIT 라이선스입니다. Oracle·DevSpace·agbrowse도 외부 MIT
의존성입니다. Oracle 본체를 vendor하지 않지만, 검증된 0.16.1 설치본에만
적용되는 최소 호환 패치와 원 저작권 고지를 함께 배포합니다. Tailscale
Funnel은 공개 endpoint이므로 Tailnet 정책·HTTPS·공개 범위를 확인하세요.
비밀값, Owner 비밀번호, 토큰, 브라우저 프로필은 저장소에 넣지 마세요.

자세한 제3자 고지는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md),
보안 정책은 [SECURITY.md](SECURITY.md)를 참고하세요.

## 문서 색인

현행 문서만 읽으면 충분합니다.

| 문서 | 내용 |
|---|---|
| [GLOBAL_CHATGPT_ROUTING.md](docs/GLOBAL_CHATGPT_ROUTING.md) | 전역 `AGENTS.md`에 넣을 모드 라우팅 규칙 |
| [DEVSPACE_TAILSCALE_SETUP.md](docs/DEVSPACE_TAILSCALE_SETUP.md) | DevSpace + Tailscale Funnel 최초 1회 연결 |
| [FROZEN_LEGACY.md](docs/FROZEN_LEGACY.md) | 동결된 agbrowse/CodexPro 자산의 정확한 경계 |
| [RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) | 릴리스 전 게이트 |

아래 파일들은 링크 호환을 위해 남긴 **레거시 스텁**입니다. 새 작업의
지시로 사용하지 마세요.

`ARCHITECTURE_V2.md`, `ARCHITECTURE_V3.md`, `ARCHITECTURE_V4.md`,
`ARCHITECTURE_GOAL_SUPERVISOR_V1.md`,
`codexpro-gpt55-orchestrator-runbook.md`,
`gpt55-operation-mode-prompts.md`, `DCINSIDE_POST_KO.md`
