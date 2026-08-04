# 보안 정책

보안 취약점은 이 저장소의 GitHub **Security** 탭에서 비공개 취약점 신고로 알려 주세요.

공개 Issue에는 다음 정보를 올리지 마세요.

- `codexpro_token`이나 기타 접근 토큰
- ChatGPT·GitHub·Cloudflare·ngrok 계정 정보
- 브라우저 프로필, 쿠키, 로그인 상태
- 개인용 전체 MCP URL
- DevSpace Owner 비밀번호, OAuth token, 허용 root 목록
- 프롬프트·응답·로그에 포함된 비공개 소스 코드나 개인정보

재현에 로그가 필요하면 비밀 값을 삭제하거나 `<redacted>`로 바꾼 최소 재현 자료만 첨부해 주세요.

DevSpace는 허용한 프로젝트에서 로컬 사용자 권한으로 파일과 명령을
다룹니다. 드라이브 루트 전체를 허용하지 말고 필요한 프로젝트만
등록하세요. Tailscale Funnel은 공개 인터넷 endpoint이므로 Tailnet
정책, HTTPS, hostname과 공개 범위를 먼저 확인해야 합니다. 평상시 GPT
실행은 Funnel이나 ChatGPT 앱 설정을 변경하지 않습니다.

Local Multi-GPT에 파일을 전달할 때는 필요한 증거 디렉터리만
`MULTI_GPT_ALLOWED_ROOTS_JSON`에 JSON 배열로 설정하세요. 드라이브 루트나 홈
전체는 허용되지 않습니다. 민감 파일이 필요하면 정책을 약화하지 말고 비밀을
제거한 사본을 별도의 허용 디렉터리에 만드세요. 실제 허용 root 목록은 저장소,
공개 Issue, 로그에 기록하지 마세요.
