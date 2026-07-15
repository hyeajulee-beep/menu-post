# menu-post

카카오톡 채널([pf.kakao.com/_BxexgQb](https://pf.kakao.com/_BxexgQb/posts))에 올라오는
구내식당 중식/석식 메뉴를 슬랙 채널로 자동 전송한다.

- **중식**: 평일 11:00 KST
- **석식**: 평일 17:30 KST
- 포스트가 아직 안 올라왔으면 5분마다 재시도 (최대 2시간)
- 메시지 형식: `7월13일 중식` 제목 + 카테고리별 메뉴 텍스트 + 음식 사진 첨부

## 최초 설정

### 1. 슬랙 앱 만들기 (매니페스트로 한 번에)

1. https://api.slack.com/apps → **Create New App** → **From an app manifest**
2. 워크스페이스 선택 → `slack-app-manifest.yaml` 내용을 붙여넣기 (스코프가 자동 설정됨)
3. **Install to Workspace** → `xoxb-`로 시작하는 **Bot User OAuth Token** 복사
4. 메뉴를 받을 슬랙 채널에서 `/invite @메뉴봇` 으로 봇 초대
5. 채널 ID 확인: 채널 이름 클릭 → 하단의 `C`로 시작하는 ID 복사

봇 스코프(매니페스트에 포함): `chat:write`, `files:write`, `channels:history`, `groups:history`
— 뒤 두 개는 **중복 전송 방지**를 위해 채널 기록을 읽는 데 쓴다.

> 봇 이름/설명은 `slack-app-manifest.yaml`에서 바꿀 수 있다.

#### 기존 앱에 스코프를 추가할 때 (재설치 필요)

봇에 새 권한(scope)을 추가하면 반드시 재설치해야 새 토큰에 반영된다.

1. https://api.slack.com/apps → 해당 앱 → **OAuth & Permissions** → Bot Token Scopes에
   `channels:history`, `groups:history` 추가
2. 상단 배너의 **Reinstall to Workspace** → 새 `xoxb-` 토큰 복사
3. GitHub Secret 갱신: `gh secret set SLACK_BOT_TOKEN --repo <owner>/menu-post --body '<새 토큰>'`

### 2. GitHub 설정

1. 이 디렉토리를 private repo로 push
2. repo **Settings → Secrets and variables → Actions** 에 등록:
   - `SLACK_BOT_TOKEN`: 위에서 복사한 `xoxb-` 토큰
   - `SLACK_CHANNEL_ID`: 채널 ID
3. **Actions** 탭 → `menu-to-slack` → **Run workflow** 로 수동 실행해 동작 확인

### 3. 정시 실행용 외부 스케줄러 (cron-job.org)

GitHub 자체 크론은 정시 발동이 매우 불안정하다(수십 분~수 시간 지연/누락 빈번).
정확한 정시 실행을 위해 외부 무료 스케줄러가 `repository_dispatch`로 워크플로를 호출한다.

1. **GitHub PAT 발급**: https://github.com/settings/tokens → **Fine-grained token**
   - Repository access: `menu-post`만 선택
   - Permissions → **Contents: Read and write** (repository_dispatch 트리거에 필요)
   - 만료일 설정 후 토큰 복사
2. **cron-job.org 가입(무료)** → 크론잡 2개 생성. 공통 설정:
   - URL: `https://api.github.com/repos/hyeajulee-beep/menu-post/dispatches`
   - Method: `POST`
   - Request headers:
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <위에서 발급한 PAT>`
     - `X-GitHub-Api-Version: 2022-11-28`
   - Timezone: `Asia/Seoul`
   - 중식 잡: 평일(월~금) **11:00**, Request body `{"event_type":"lunch"}`
   - 석식 잡: 평일(월~금) **17:30**, Request body `{"event_type":"dinner"}`
3. 저장 후 cron-job.org의 **TEST RUN**으로 즉시 한 번 호출 → Actions 탭에 `repository_dispatch` 실행이 뜨는지 확인

> GitHub 크론(백업)은 그대로 둔다 — 외부 호출과 겹쳐도 멱등성으로 중복이 나지 않는다.

## 로컬 테스트

```bash
pip install -r requirements.txt

# 슬랙 전송 없이 메시지/이미지 수집만 확인
python menu_to_slack.py --meal lunch --dry-run

# 실제 전송 (정각 대기 없이 즉시)
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_CHANNEL_ID=C...
python menu_to_slack.py --meal lunch --no-wait
```

## 동작 방식

- GitHub Actions cron이 평일 10:50 / 17:20 KST에 시작 (cron 지연 대비 10분 여유)
- 스크립트가 정각(11:00 / 17:30 KST)까지 대기한 뒤 카카오 채널 API를 폴링
- 오늘 날짜(`N월N일`) + 끼니 키워드(`중식`/`석식`)가 제목에 포함된 포스트를 수집
  (상단 고정 주간메뉴표는 제외)
- 본식 포스트 발견 시 나머지 카테고리 포스트(샐러드인더키친, T/O도시락&베이커리)를
  90초 더 기다렸다가 한 메시지로 합쳐 전송
- **중복 방지(멱등성)**: 전송 직전 채널 최근 메시지를 조회해, 오늘 같은 끼니(`N월N일 끼니`로
  시작하는 봇 메시지)가 이미 있으면 전송을 생략 — 크론이 뒤늦게 또 발동하거나 수동+자동이
  겹쳐도 같은 끼니를 두 번 올리지 않는다
- 이미지 첨부 실패 시 텍스트만이라도 전송

## 알려진 제약

- 카카오 비공식 API(`rocket-web`)를 사용하므로 카카오 측 변경 시 수정 필요
- 석식 포스트 제목 형식은 유연 매칭(`날짜 + 석식 포함`)으로 처리
- 공휴일 등 포스트가 없는 날은 2시간 폴링 후 조용히 종료
