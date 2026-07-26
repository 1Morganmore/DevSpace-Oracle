# Oracle GPT Automation for Codex

Codex가 웹 ChatGPT에 계획·검토·수정·구현을 맡기고 로컬 토큰 사용을
줄이기 위한 Windows 자동화입니다.

새 기본 경로는 두 공개 프로젝트를 수정하지 않고 조합합니다.

- [Oracle](https://github.com/steipete/oracle): ChatGPT 브라우저 세션,
  응답 대기·회수, 완료 대화 보관
- [DevSpace](https://github.com/Waishnav/devspace): 허용한 로컬
  작업공간의 읽기·쓰기·명령 실행 MCP

과거 `agbrowse`·CodexPro 기반 실행 파일은 삭제하지 않았지만 새 질문에는
쓰지 않습니다. 기존 실행의 정확한 복구와 롤백 증거로만 남습니다. 일반
GPT부터 Pro까지 모든 신규 브라우저 실행은 Oracle 하나로 통일합니다.

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

| 모드 | 새 실행 경로 |
|---|---|
| 일반 GPT | Oracle direct + DevSpace |
| 계획 | Oracle plan + DevSpace |
| 검토 | Oracle review + DevSpace |
| 수정 | Oracle edit + DevSpace |
| 지휘 | Oracle orchestrator + DevSpace |
| 심층 리서치 | Oracle browser research deep + DevSpace |
| 종합모드 | Oracle plan → optional Pro/Multi → review → implementation → final web gate → local gate |
| Web Multi-GPT | 독립 Oracle solver 2~25개, 최대 5개씩 wave, 별도 merger |
| Pro | Oracle attachment-only, DevSpace 금지 |

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
failed로 되돌리지 않습니다.

종합모드의 계획 검토는 `PASS`, `PASS_WITH_NOTES`, `REVISE`, `FAIL`을
구분합니다. `PASS_WITH_NOTES`는 구현으로 진행하며, 치명적 결함에 대한 계획
수정은 동일 작업 계열 전체에서 최대 두 번입니다. 첫 검토의 치명적 finding
ID를 고정하므로 후속 검토가 새 비치명 요구나 표현 개선을 이유로 다시
계획을 돌릴 수 없습니다. 세 번째 `REVISE`는 새 계획 없이 사용자 확인
필요 상태로 끝납니다.

## 업데이트·롤백·제거

```powershell
.\update.ps1
.\rollback.ps1
.\uninstall.ps1
```

기존 agbrowse와 CodexPro 상태 파일은 삭제하지 않습니다. 신규 제출에는
쓰지 않고 기존 실행의 정확한 복구 증거로만 보존합니다.

## 보안과 라이선스

프로젝트는 MIT 라이선스입니다. Oracle·DevSpace·agbrowse도 외부 MIT
의존성입니다. Oracle 본체를 vendor하지 않지만, 검증된 0.16.1 설치본에만
적용되는 최소 호환 패치와 원 저작권 고지를 함께 배포합니다. Tailscale
Funnel은 공개 endpoint이므로 Tailnet 정책·HTTPS·공개 범위를 확인하세요.
비밀값, Owner 비밀번호, 토큰, 브라우저 프로필은 저장소에 넣지 마세요.

자세한 제3자 고지는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md),
보안 정책은 [SECURITY.md](SECURITY.md)를 참고하세요.
