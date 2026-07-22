# menu-post

카카오톡 채널([pf.kakao.com/_BxexgQb](https://pf.kakao.com/_BxexgQb/posts))에 올라오는
구내식당 중식/석식 메뉴를 슬랙 채널로 자동 전송한다.

- **중식**: 평일 11:00 KST
- **석식**: 평일 17:30 KST
- 두 경로로 이중화: 외부 스케줄러(cron-job.org)가 창 동안 10분 간격으로 호출하는 빠른
  경로(`--once`)와, GitHub 크론이 창 초입에 띄우는 자립형 백스톱(한 번만 떠도 게시 시점까지
  폴링하는 `--no-wait --max-wait 50`). 한쪽이 죽어도 나머지가 커버한다
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
정확한 실행을 위해 외부 무료 스케줄러가 `repository_dispatch`로 워크플로를 호출한다.
스크립트가 `--once`로 1회 조회 후 종료(수초~90초)하므로, **메뉴 창 동안 10분 간격으로
반복 호출**해 포스트가 올라오는 시점을 커버한다. 첫 호출이 메뉴를 잡으면 전송하고,
이후 호출은 멱등성(채널 기록 확인)으로 수초 내 skip된다.

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
   - 중식 잡: 평일(월~금) **11:00~11:50, 10분 간격**(분 `0,10,20,30,40,50`, 시 `11`),
     Request body `{"event_type":"lunch"}`
   - 석식 잡: 평일(월~금) **17:30~18:20, 10분 간격**(분 `0,10,20,30,40,50`, 시 `17,18`
     중 창에 맞게), Request body `{"event_type":"dinner"}`
3. 저장 후 cron-job.org의 **TEST RUN**으로 즉시 한 번 호출 → Actions 탭에 `repository_dispatch` 실행이 뜨는지 확인
4. **점검(중요)**: 크론잡이 *단발*이 아니라 창 동안 **10분 간격으로 실제 반복 발동**하는지
   cron-job.org 실행 로그로 확인한다. 간격 설정이 누락돼 하루 1회(11:00)만 발동하면, 카카오가
   11:00 이후 늦게 올라오는 날 이 경로가 놓친다(그럴 땐 아래 GitHub 백스톱이 커버).

> GitHub 크론(백스톱)은 그대로 둔다 — schedule로 뜬 run은 바운드 폴링(`--max-wait 50`)이라
> 창 초입에 한 번만 떠도 게시 시점까지 잡는다. 외부 호출과 겹쳐도 멱등성으로 중복이 나지 않는다.

## 로컬 테스트

```bash
pip install -r requirements.txt

# 슬랙 전송 없이 메시지/이미지 수집만 확인 (1회 조회 후 종료)
python menu_to_slack.py --meal lunch --once --dry-run

# 백스톱과 동일한 바운드 폴링 모드 확인 (메뉴 있으면 즉시 종료, 없으면 최대 N분 폴링)
python menu_to_slack.py --meal lunch --no-wait --max-wait 1 --dry-run

# 실제 전송 (1회 조회 후 종료 — 워크플로가 쓰는 방식)
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_CHANNEL_ID=C...
python menu_to_slack.py --meal lunch --once
```

## 동작 방식

- **빠른 경로**: 외부 스케줄러(cron-job.org)가 메뉴 창 동안 10분 간격으로 `repository_dispatch`
  호출 → `--once`로 카카오를 1회만 조회하고 종료(수초). 러너를 거의 안 붙잡아 무료 한도 절약.
- **자립형 백스톱**: GitHub `schedule` 크론이 창 초입(중식 11:00·11:05·11:10·11:15·11:20·11:40,
  석식 17:30·17:35·17:40·17:45·17:50·18:10 KST)에 뜨면 `--no-wait --max-wait 50`으로 5분 간격
  최대 50분 폴링 → 외부 스케줄러가 죽어도 이 크론이 '한 번만' 떠주면 게시 시점까지 기다렸다
  잡는다. GitHub 크론은 발동 자체가 불안정하므로(11:00이 드롭될 수 있음) 창 초입을 5분 간격으로
  촘촘히 깔되, '한 번 뜨면 창 전체를 덮는' 폴링 구조로 안정성을 확보한다.
- `concurrency: menu-to-slack`(cancel-in-progress: false)로 두 경로가 겹쳐도 러너를 다중
  점유하지 않는다 — 폴링 중인 run은 유지되고, 뒤에 겹친 run은 큐잉됐다 멱등성으로 빠르게 skip.
- 오늘 날짜(`N월N일`) + 끼니 키워드(`중식`/`석식`)가 제목에 포함된 포스트를 수집
  (상단 고정 주간메뉴표는 제외)
- 본식 포스트 발견 시 나머지 카테고리 포스트(샐러드인더키친, T/O도시락&베이커리)가 덜
  올라왔으면 90초 더 기다렸다가 한 메시지로 합쳐 전송. 본식이 아직 없으면 즉시 종료하고
  다음 스케줄 발동이 재시도한다
- **중복 방지(멱등성)**: 전송 직전 채널 최근 메시지를 조회해, 오늘 같은 끼니(`N월N일 끼니`로
  시작하는 봇 메시지)가 이미 있으면 전송을 생략 — 스케줄러가 10분 간격으로 여러 번 발동하거나
  수동+자동이 겹쳐도 같은 끼니를 두 번 올리지 않는다
- 이미지 첨부 실패 시 텍스트만이라도 전송

## 알려진 제약

- 카카오 비공식 API(`rocket-web`)를 사용하므로 카카오 측 변경 시 수정 필요
- 석식 포스트 제목 형식은 유연 매칭(`날짜 + 석식 포함`)으로 처리
- 공휴일 등 포스트가 없는 날은 매 발동마다 미게시 확인 후 조용히 종료(창이 끝나면 전송 없음)
- 커버 범위는 백스톱 폴링 창(중식 11:00~약 12:30, 석식 17:30~약 19:00 KST)에 의존한다 —
  이보다 더 늦게 포스트가 올라오면 놓칠 수 있으므로, 필요 시 크론 진입 시각이나 `--max-wait`를
  넓힌다. 단 GitHub 크론이 창 안에 하나도 안 뜨는 극단적 날엔 외부 스케줄러 경로가 커버한다
