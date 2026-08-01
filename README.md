# Codex Web GPT Orchestrator

한국어 | [English](README.en.md)

Codex가 웹 ChatGPT에 계획·리서치·검토·코드 구현을 맡기고, 로컬 Codex는
제출·복구·해시·최종 테스트만 담당하도록 만드는 Windows용 자동화 도구입니다.

이 프로젝트는 다음 두 도구를 연결합니다.

- [Oracle](https://github.com/steipete/oracle): 로그인된 ChatGPT 브라우저
  세션 생성, 모델 선택, 응답 대기와 결과 회수
- [DevSpace](https://github.com/Waishnav/devspace): 사용자가 허용한 로컬
  프로젝트의 파일 읽기·쓰기와 명령 실행

일반 GPT 작업은 Oracle이 `@DevSpace`와 미션 파일 경로를 ChatGPT에
전달합니다. Pro 작업은 DevSpace 없이 정확한 첨부 파일만 사용합니다.

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
| 종합모드 | comprehensive mode | 계획부터 구현·최종 게이트까지 자동 연결 | plan → optional Pro/Multi → review → implementation → gate |
| Pro | `pro` / Pro | 독립적인 최종 판단·설계 검토 후 결과만 반환 | Oracle 첨부 전용, DevSpace 없음 |

지휘는 웹 제출 한 번으로 끝나는 실행 모드입니다. 종합모드는 지휘와 같은
구현 단계를 포함하면서 계획·독립 검토·선택적 Pro/Web Multi·최종 게이트를
추가한 다단계 워크플로입니다.

단순 Pro는 종합모드와 별개인 한 번짜리 검토 경로입니다. 첨부된 계획·코드·문서를
검토하고 결과 파일을 반환하면 끝나며, 자동으로 구현이나 다음 단계로 넘어가지
않습니다. 계획부터 구현까지 이어야 할 때만 종합모드를 사용합니다.

Local Multi-GPT와 Web Multi-GPT는 서로 다른 경로입니다. Local Multi-GPT는
PC의 Codex 하위 레인을 사용하는 선택적 자문 도구이며, 모든 단계가
`gpt-5.6-luna`와 `max` 사고 레벨로 고정됩니다. 다른 모델이나 낮은 사고
레벨을 요청하면 하위 프로세스를 시작하기 전에 거부합니다. Web Multi-GPT는
Oracle이 여러 독립 ChatGPT 웹 세션을 실행한 뒤 결과를 병합합니다.

## 요구사항

- Windows 11
- Python
- Node.js 22.19 이상, 27 미만
- Git for Windows / Git Bash
- Tailscale
- 브라우저에서 ChatGPT에 로그인된 Oracle 프로필
- ChatGPT Developer Mode에 최초 한 번 수동 등록한 DevSpace 앱

현재 검증된 조합은 Oracle `0.16.1`과 DevSpace `1.0.4`입니다. 설치기는
정확한 파일 해시가 일치할 때만 Windows 호환 패치를 적용합니다.

## 설치

```powershell
git clone https://github.com/ventianima-lab/codexpro-automation.git
cd codexpro-automation
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

## 일반 GPT 실행 예시

프로젝트 안에 UTF-8 미션 파일을 만든 뒤 먼저 미리보기 합니다.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" `
  --mode orchestrator `
  --project-root C:\project `
  --mission-path C:\project\mission.md `
  --manifest-output C:\project\.ai-bridge\oracle.json `
  --reasoning-level "Very High" `
  --dry-run
```

실제 실행 승인이 있을 때만 `--dry-run`을 제거합니다.

## Pro 실행 예시

Pro는 프로젝트 앱을 사용하지 않습니다. 미션과 필요한 증거 파일을 정확한
해시로 고정해 첨부합니다.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" `
  --mode pro `
  --project-root C:\project `
  --mission-path C:\project\pro.md `
  --attachment C:\project\evidence.zip `
  --manifest-output C:\project\.ai-bridge\pro.json `
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

## 업데이트와 제거

```powershell
.\install.ps1 -WhatIf
.\install.ps1
.\rollback.ps1
.\uninstall.ps1
```

기존에 저장된 구형 실행을 복구해야 하는 컴퓨터에서만
`-InstallLegacyRecoveryDependency`를 사용합니다.

## 문서

- [전역 ChatGPT 라우팅과 모드 선택](docs/GLOBAL_CHATGPT_ROUTING.md)
- [DevSpace + Tailscale 최초 설정](docs/DEVSPACE_TAILSCALE_SETUP.md)
- [기술 변경 기록](docs/CHANGELOG.md)
- [구형 실행 복구용 동결 자산](docs/FROZEN_LEGACY.md)
- [릴리스 검증 절차](docs/RELEASE_CHECKLIST.md)
- [보안 정책](SECURITY.md)
- [제3자 라이선스](THIRD_PARTY_NOTICES.md)

## 레거시 호환

과거 CodexPro·agbrowse 기반 실행 파일은 이미 저장된 구형 작업을 원래
실행 신원으로 정확히 복구하기 위해서만 남아 있습니다. 새 작업의 실행 경로나 fallback으로 사용하지
않습니다. 상세 파일 목록은 [동결 자산 문서](docs/FROZEN_LEGACY.md)에
분리했습니다.

## 라이선스

MIT License. Oracle·DevSpace 등 제3자 구성요소의 저작권과 라이선스는
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 정리되어 있습니다.
