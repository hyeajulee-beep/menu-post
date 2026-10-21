# menu-post

카카오톡 채널([pf.kakao.com/_BxexgQb](https://pf.kakao.com/_BxexgQb/posts))에 올라오는
구내식당 중식/석식 메뉴를 슬랙 채널로 자동 전송한다.

**받고 싶은 채널(public/private 모두)에서 `/invite @메뉴봇` 하면 다음 끼니부터 그 채널에도
올라온다.** 봇이 매 실행 때 자기가 멤버인 채널 목록을 조회해 전부에 게시하므로 별도 설정이
없다. 그만 받으려면 `/remove @메뉴봇`(채널에서 내보내기) — 역시 다음 끼니부터 반영.

- **중식**: 평일 11:00 KST
- **석식**: 평일 17:30 KST
- 두 경로로 이중화: 외부 스케줄러(cron-job.org)가 창 동안 10분 간격으로 호출하는 주
  경로(`--no-wait --max-wait 8`)와, GitHub 크론이 창 초입에 띄우는 자립형 백스톱(한 번만
  떠도 게시 시점까지 폴링하는 `--no-wait --max-wait 50 --allow-partial`).
  한쪽이 죽어도 나머지가 커버한다
- 메시지 형식: `7월13일 중식` 제목 + 카테고리별 메뉴 텍스트 + 음식 사진 첨부

## 최초 설정

### 1. 슬랙 앱 만들기 (매니페스트로 한 번에)

1. https://api.slack.com/apps → **Create New App** → **From an app manifest**
2. 워크스페이스 선택 → `slack-app-manifest.yaml` 내용을 붙여넣기 (스코프가 자동 설정됨)
3. **Install to Workspace** → `xoxb-`로 시작하는 **Bot User OAuth Token** 복사
4. 메뉴를 받을 슬랙 채널(private 포함)에서 `/invite @메뉴봇` 으로 봇 초대 — 이것으로 끝.
   채널 ID를 따로 적어둘 필요 없이 다음 끼니부터 게시된다

봇 스코프(매니페스트에 포함): `chat:write`, `files:write`, `channels:history`, `groups:history`,
`channels:read`, `groups:read`
— `*:history` 두 개는 **중복 전송 방지**를 위해 채널 기록을 읽는 데, `*:read` 두 개는 **초대된
채널 자동 탐색**(`users.conversations`)에 쓴다.

> 봇 이름/설명은 `slack-app-manifest.yaml`에서 바꿀 수 있다.

#### 기존 앱에 스코프를 추가할 때 (재설치 필요)

봇에 새 권한(scope)을 추가하면 반드시 재설치해야 새 토큰에 반영된다.

1. https://api.slack.com/apps → 해당 앱 → **OAuth & Permissions** → Bot Token Scopes에
   추가 (예: 초대된 채널 자동 탐색을 위해 `channels:read`, `groups:read`)
2. 상단 배너의 **Reinstall to Workspace** → 새 `xoxb-` 토큰 복사
3. GitHub Secret 갱신: `gh secret set SLACK_BOT_TOKEN --repo <owner>/menu-post --body '<새 토큰>'`

> `channels:read`/`groups:read`가 없는 구 토큰으로 돌면 스크립트가 `missing_scope`를 로그하고
> `SLACK_CHANNEL_ID` 시크릿의 채널 하나로 폴백한다. 재설치 전까지 기존 채널 게시는 끊기지 않는다.

### 2. GitHub 설정

1. 이 디렉토리를 private repo로 push
2. repo **Settings → Secrets and variables → Actions** 에 등록:
   - `SLACK_BOT_TOKEN`: 위에서 복사한 `xoxb-` 토큰
   - `SLACK_CHANNEL_ID` (선택, 폴백용): 토큰에 `channels:read`/`groups:read`가 없을 때만 쓰는
     단일 채널 ID. 정상 상태에서는 초대된 채널을 자동 탐색하므로 필요 없다
3. **Actions** 탭 → `menu-to-slack` → **Run workflow** 로 수동 실행해 동작 확인

### 3. 정시 실행용 외부 스케줄러 (cron-job.org)

GitHub 자체 크론은 정시 발동이 매우 불안정하다(수십 분~수 시간 지연/누락 빈번).
정확한 실행을 위해 외부 무료 스케줄러가 `repository_dispatch`로 워크플로를 호출한다.
각 호출은 `--no-wait --max-wait 8`로 **최대 8분간 2분 간격 폴링**하다 메뉴를 찾으면 즉시
전송하고 끝난다(보통 수초~90초). 여기에 **메뉴 창 동안 10분 간격 반복 호출**을 겹쳐,
호출 하나가 8분 창을 덮고 그 창들이 겹침 없이 이어지도록 한다. 첫 호출이 메뉴를 잡으면
전송하고, 이후 호출은 멱등성(채널 기록 확인)으로 수초 내 skip된다.

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
   11:00 이후 늦게 올라오는 날 이 경로가 놓친다.
   > **실제 사고 (2026-07-27)**: 중식 잡이 단발로 설정돼 있어 매일 11:00 딱 1회만 발동했다
   > (7/16~7/27 전 기간 확인). 그날 카카오 게시가 11:03:17~11:03:58이었고 11:00:34 조회는
   > 3분 차로 놓쳐 중식이 누락됐다. GitHub 백스톱은 상시 2~2.6시간 지연 배달돼 커버하지
   > 못했다. 이후 dispatch 실행 모드를 8분 바운드 폴링으로 바꿔 저장소 쪽 방어선을 뒀지만,
   > **10분 간격 반복 발동은 여전히 주 경로다.** 반드시 실행 로그로 확인할 것.

> GitHub 크론(백스톱)은 그대로 둔다 — schedule로 뜬 run은 바운드 폴링(`--max-wait 50
> --allow-partial`)이라 창 초입에 한 번만 떠도 게시 시점까지 잡는다. 외부 호출과 겹쳐도
> 멱등성으로 중복이 나지 않는다. 다만 GitHub 크론은 발동 자체가 2시간 이상 늦는 일이
> 잦으니 백스톱으로만 취급한다.

## 로컬 테스트

```bash
pip install -r requirements.txt

# 슬랙 전송 없이 메시지/이미지 수집만 확인 (1회 조회 후 종료)
python menu_to_slack.py --meal lunch --once --dry-run

# 주 경로와 동일한 바운드 폴링 (메뉴 있으면 즉시 종료, 없으면 2분 간격으로 최대 N분)
python menu_to_slack.py --meal lunch --no-wait --max-wait 1 --dry-run

# 백스톱 모드: 타임아웃 시 본식 포스트가 없어도 발견된 카테고리 포스트로 전송한다.
# --allow-partial 없이는 부분 전송을 하지 않고 종료해 다음 발동이 재시도한다.
python menu_to_slack.py --meal lunch --no-wait --max-wait 1 --allow-partial --dry-run

# 봇이 초대된(게시 대상) 채널 목록 확인
export SLACK_BOT_TOKEN=xoxb-...
python menu_to_slack.py --list-channels

# 실제 전송 (1회 조회 후 종료 — 수동 실행 방식). 초대된 모든 채널에 게시된다
python menu_to_slack.py --meal lunch --once
```

## 동작 방식

- **주 경로**: 외부 스케줄러(cron-job.org)가 메뉴 창 동안 10분 간격으로 `repository_dispatch`
  호출 → 각 run이 `--no-wait --max-wait 8`로 2분 간격 최대 8분 폴링. 메뉴를 찾으면 즉시
  전송하고 끝나므로 보통 수초 만에 종료해 러너를 거의 안 붙잡는다. 8분 창은 10분 호출 간격
  안에 들어가므로 창끼리 겹치지 않고, **스케줄러가 단발로 잘못 설정돼도 창 초입 8분은
  이 경로 하나가 덮는다**(2026-07-27 누락 사고의 재발 방지선).
- **자립형 백스톱**: GitHub `schedule` 크론이 창 초입(중식 11:00·11:05·11:10·11:15·11:20·11:40,
  석식 17:30·17:35·17:40·17:45·17:50·18:10 KST)에 뜨면 `--no-wait --max-wait 50
  --allow-partial`로 2분 간격 최대 50분 폴링 → 외부 스케줄러가 죽어도 이 크론이 '한 번만'
  떠주면 게시 시점까지 기다렸다 잡는다. GitHub 크론은 발동 자체가 불안정하므로(11:00이
  드롭되거나 2시간 이상 늦게 배달될 수 있음) 창 초입을 5분 간격으로 촘촘히 깔되,
  '한 번 뜨면 창 전체를 덮는' 폴링 구조로 안정성을 확보한다.
- **부분 전송은 백스톱에서만**: 폴링이 타임아웃했는데 본식 포스트 없이 카테고리 포스트만
  발견된 경우, `--allow-partial`이 붙은 백스톱만 그 발견분으로 전송한다. 주 경로(8분 창)는
  전송하지 않고 종료해 다음 호출이 재시도한다 — 짧은 창에서 부분 전송을 허용하면 카테고리
  포스트만 올라온 수십 초 사이에 불완전한 메시지가 나가고, 그 뒤 멱등성 검사가 완전한
  메시지를 영구 차단해 버리기 때문이다.
- `concurrency: menu-to-slack`(cancel-in-progress: false)로 두 경로가 겹쳐도 러너를 다중
  점유하지 않는다 — 폴링 중인 run은 유지되고, 뒤에 겹친 run은 큐잉됐다 멱등성으로 빠르게 skip.
- 오늘 날짜(`N월N일`) + 끼니 키워드(`중식`/`석식`)가 제목에 포함된 포스트를 수집
  (상단 고정 주간메뉴표는 제외)
- **끼니 표기가 빠진 포스트 구제**: 제목에 날짜는 있는데 `중식`/`석식`이 없는 포스트는
  피드에서 앞뒤로 가장 가까운 끼니 확정 포스트의 끼니로 귀속한다. 하루 포스트는 끼니별로
  연속된 묶음(카테고리들 → 본식 순)으로 올라오고 묶음 사이 간격도 수십 초라 시간으로는
  경계를 못 가르므로, 인접 관계로 판정한다. 앞뒤 끼니가 서로 다른 경계 위치면 모호하므로
  귀속하지 않고 경고 로그만 남긴다.
  > 2026-09-04 중식에서 샐러드 코너 제목만 `9월4일 샐러드인더키친`으로 올라와(전날까지는
  > `9월3일 중식 샐러드인더키친`) 매칭에서 통째로 빠졌고, 메뉴 5개 중 4개만 전송됐다.
  > 이 규칙은 그 사례에 대응한 것이다.
- 매칭 개수가 끼니별 `expected_posts`보다 적으면 전송은 하되 경고를 로그에 남긴다 —
  불완전한 메시지가 조용히 나가지 않도록
- 본식 포스트 발견 시 나머지 카테고리 포스트(샐러드인더키친, T/O도시락&베이커리)가 덜
  올라왔으면 90초 더 기다렸다가 한 메시지로 합쳐 전송. 본식이 아직 없으면 즉시 종료하고
  다음 스케줄 발동이 재시도한다
- **게시 대상 채널 자동 탐색**: 전송 때마다 `users.conversations`로 봇이 멤버인 public/private
  채널을 전부 조회해 각각에 게시한다. `/invite`·`/remove`가 다음 끼니부터 반영되는 이유.
  한 채널에서만 나는 오류(추방됨·보관됨 등)는 그 채널만 건너뛰고 나머지는 계속 게시하되,
  run은 실패로 표시해 Actions 로그에서 보이게 한다. 저장소가 public이라 로그에는 채널
  이름/ID 대신 `채널 2/3` 같은 순번만 남는다 — 어느 채널인지는 로컬에서 `--list-channels`로 대조
- **중복 방지(멱등성)**: 채널마다 전송 직전 최근 메시지를 조회해, 오늘 같은 끼니(`N월N일 끼니`로
  시작하는 봇 메시지)가 이미 있으면 그 채널은 생략 — 스케줄러가 10분 간격으로 여러 번 발동하거나
  수동+자동이 겹쳐도 같은 끼니를 두 번 올리지 않고, 일부 채널만 실패한 run의 재실행도 안전하다
- 이미지 첨부 실패 시 텍스트만이라도 전송

## 알려진 제약

- 카카오 비공식 API(`rocket-web`)를 사용하므로 카카오 측 변경 시 수정 필요
- 석식 포스트 제목 형식은 유연 매칭(`날짜 + 석식 포함`)으로 처리
- 공휴일 등 포스트가 없는 날은 매 발동마다 미게시 확인 후 조용히 종료(창이 끝나면 전송 없음)
- 커버 범위는 백스톱 폴링 창(중식 11:00~약 12:30, 석식 17:30~약 19:00 KST)에 의존한다 —
  이보다 더 늦게 포스트가 올라오면 놓칠 수 있으므로, 필요 시 크론 진입 시각이나 `--max-wait`를
  넓힌다. 단 GitHub 크론이 창 안에 하나도 안 뜨는 극단적 날엔 외부 스케줄러 경로가 커버한다
