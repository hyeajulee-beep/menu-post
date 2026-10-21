"""카카오톡 채널의 중식/석식 포스트를 슬랙 채널로 전송한다.

사용법:
    python menu_to_slack.py --meal lunch --no-wait --max-wait 8   # 외부 dispatch 1건이 8분 창을 덮음
    python menu_to_slack.py --meal lunch --no-wait --max-wait 50 --allow-partial  # 긴 백스톱 창
    python menu_to_slack.py --meal lunch --once            # 카카오 1회 조회 후 종료(수동 실행용)
    python menu_to_slack.py --meal lunch                   # 정각(11:00 KST)까지 대기 후 폴링(레거시)
    python menu_to_slack.py --meal dinner
    python menu_to_slack.py --meal lunch --dry-run         # 슬랙 전송 없이 메시지만 출력
    python menu_to_slack.py --meal lunch --once --resend   # 기존 메시지 삭제 후 재전송
    python menu_to_slack.py --meal dinner --once --date 2026-10-21  # 지난 날짜 끼니를 늦게 전송
    python menu_to_slack.py --list-channels                # 봇이 초대된 채널 목록만 출력

게시 대상: 봇이 멤버인 모든 public/private 채널(/invite @메뉴봇 한 채널 전부). 매 실행 때
Slack에서 목록을 조회하므로 초대·추방은 다음 끼니부터 반영된다.

필요 환경변수: SLACK_BOT_TOKEN (dry-run 시 불필요).
SLACK_CHANNEL_ID는 선택 — channels:read/groups:read 스코프가 없는 구 토큰일 때만 폴백으로 쓴다.
"""

import argparse
import os
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
KAKAO_POSTS_URL = "https://pf.kakao.com/rocket-web/web/profiles/_BxexgQb/posts"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

MEALS = {
    "lunch": {"keyword": "중식", "send_at": (11, 0), "expected_posts": 3},
    "dinner": {"keyword": "석식", "send_at": (17, 30), "expected_posts": 2},
}

POLL_INTERVAL = 2 * 60  # 2분 — 짧은 폴링 창(--max-wait 8)에서도 5회 조회되도록
POLL_TIMEOUT = 2 * 60 * 60  # 최대 2시간
GRACE_POLL_DELAY = 90  # 본식 발견 후 나머지 포스트를 기다리는 추가 폴링 간격(초)

# 카카오가 성인인증 잠금(adult_only) 등으로 가린 이미지는 실사진 대신 수 KB짜리
# 회색 placeholder를 내려준다 (2026-07-31 본식 포스트: 2,907B). 정상 음식 사진은
# 수십~수백 KB이므로 이 미만은 placeholder로 간주해 첨부하지 않는다.
MIN_IMAGE_BYTES = 10 * 1024

# 제목 속 임의의 날짜 표기 ('10월12일'/'10월 12' 등). 날짜 오기 포스트 판별·라벨 정리용.
ANY_DATE_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일?")

# --date로 지정한 대상 날짜. None이면 실행 시점의 오늘(KST).
TARGET_DATE = None


def target_date():
    """메뉴를 찾을 날짜. 평소엔 오늘이고, 누락된 끼니를 다음날 늦게 보낼 때만 --date로 바꾼다."""
    return TARGET_DATE or datetime.now(KST).date()


# 재시도·보류가 무의미한 영구(인증/설정) 오류 — 삼키지 않고 즉시 실패로 노출한다.
FATAL_SLACK_ERRORS = {
    "account_inactive",
    "token_revoked",
    "token_expired",
    "invalid_auth",
    "not_authed",
    "channel_not_found",
    "not_in_channel",
    "is_archived",
    "missing_scope",
}

# 특정 채널에만 해당하는 오류 — 그 채널만 건너뛰고 나머지 채널 게시는 계속한다.
# (채널 목록 조회 뒤 추방·보관된 경우 등) 토큰 오류와 달리 run 전체를 중단할 이유가 없다.
CHANNEL_SCOPED_ERRORS = {"channel_not_found", "not_in_channel", "is_archived"}


def log(msg):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_posts():
    resp = requests.get(
        KAKAO_POSTS_URL, headers={"User-Agent": USER_AGENT}, timeout=30
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def meal_of(title):
    """제목에 들어 있는 끼니 키워드를 돌려준다 (없으면 None)."""
    for cfg in MEALS.values():
        if cfg["keyword"] in title:
            return cfg["keyword"]
    return None


def is_misdated_today(post):
    """제목 날짜가 과거인데 실제로는 오늘 발행된 포스트면 True (식당 측 날짜 오기).

    2026-10-13 중식 3개 포스트가 오늘 11:01에 발행됐지만 제목이 전부 '10월12일'로
    올라와 날짜 매칭에서 통째로 빠졌다. 발행일(published_at, KST)이 오늘이면서 제목
    날짜가 오늘보다 과거인 경우만 오늘 것으로 본다 — 미래 날짜(다음날 메뉴 선게시)는
    오기로 단정할 수 없으므로 제외한다.
    """
    ts = post.get("published_at")
    m = ANY_DATE_RE.search(post.get("title", ""))
    if not ts or not m:
        return False
    today = target_date()
    if datetime.fromtimestamp(ts / 1000, KST).date() != today:
        return False
    try:
        titled = date(today.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return False
    if titled > today:  # 연초에 작년 12월 날짜로 오기된 경우
        titled = titled.replace(year=today.year - 1)
    return titled < today


def resolve_meals(posts, date_re):
    """오늘 포스트를 피드 순서대로 훑어 각 포스트의 끼니를 확정한다.

    제목에 끼니 표기가 빠진 포스트(2026-09-04 '9월4일 샐러드인더키친')는 피드에서
    앞뒤로 가장 가까운 끼니 확정 포스트의 끼니로 귀속한다. 하루 포스트는 끼니별로
    연속된 묶음으로 올라오는데(카테고리들 → 본식 순, 묶음 사이 간격도 수십 초라
    시간만으로는 경계를 못 가른다), 그래서 시간 간격보다 피드상의 인접 관계가
    믿을 만하다. 앞뒤 끼니가 서로 다른 경계 위치라면 모호하므로 귀속하지 않고
    경고만 남긴다 — 조용히 빠지는 것보다 로그에 드러나는 편이 낫다.

    반환: [(post, 끼니 키워드 또는 None), ...] (피드 순서 유지)
    """
    today = []
    for p in posts:
        if p.get("pinned"):
            continue
        if date_re.search(p.get("title", "")):
            today.append(p)
        elif is_misdated_today(p):
            log(f"제목 날짜 오기 의심 포스트 '{p.get('title', '').strip()}' — 발행일 기준 오늘로 귀속")
            today.append(p)
    meals = [meal_of(p.get("title", "")) for p in today]

    resolved = list(meals)
    for i, m in enumerate(meals):
        if m is not None:
            continue
        title = today[i].get("title", "").strip()
        prev = next((x for x in reversed(meals[:i]) if x), None)
        nxt = next((x for x in meals[i + 1 :] if x), None)
        if prev and nxt and prev != nxt:
            log(f"끼니 표기 없는 포스트 '{title}' — 앞({prev})/뒤({nxt}) 끼니가 달라 제외")
            continue
        resolved[i] = prev or nxt
        if resolved[i]:
            log(f"끼니 표기 없는 포스트 '{title}' — 인접 포스트 기준 {resolved[i]}으로 귀속")
        else:
            log(f"끼니 표기 없는 포스트 '{title}' — 기준 삼을 인접 포스트가 없어 제외")
    return list(zip(today, resolved))


def match_posts(posts, date_re, keyword):
    """오늘 날짜 + 해당 끼니로 확정된 포스트만 반환 (고정 포스트 제외).

    날짜는 '7월15일'/'7월15'/'7월 15' 등 표기 흔들림을 흡수하도록 정규식으로 매칭하고,
    끼니는 제목의 키워드로 판정하되 표기가 빠진 포스트는 resolve_meals가 인접 포스트
    기준으로 구제한다.
    """
    return [p for p, meal in resolve_meals(posts, date_re) if meal == keyword]


def category_label(title, date_re, keyword):
    """제목에서 날짜/끼니를 제거해 카테고리 라벨을 뽑는다 (없으면 끼니명)."""
    label = ANY_DATE_RE.sub("", date_re.sub("", title)).replace(keyword, "").strip()
    return label or keyword


def has_main_post(posts, date_re, keyword):
    """본식(날짜+끼니 외 카테고리 접미어가 없는) 포스트가 있으면 True."""
    return any(
        category_label(p.get("title", ""), date_re, keyword) == keyword
        for p in posts
    )


def build_message(posts, date_str, date_re, keyword):
    # 본식 포스트를 맨 앞으로, 나머지는 API 순서 유지
    def is_main(p):
        return category_label(p.get("title", ""), date_re, keyword) == keyword

    ordered = sorted(posts, key=lambda p: 0 if is_main(p) else 1)

    lines = [f"{date_str} {keyword}", ""]
    for p in ordered:
        items = []
        for c in p.get("contents", []):
            if c.get("t") == "text" and c.get("v"):
                items.extend(
                    line.strip() for line in c["v"].splitlines() if line.strip()
                )
        label = category_label(p["title"], date_re, keyword)
        lines.append(f"[{label}] {' / '.join(items) if items else '(메뉴 텍스트 없음)'}")
    return "\n".join(lines), ordered


def collect_images(posts):
    """포스트의 가로(음식) 사진만 다운로드해 (filename, bytes) 리스트로 반환.

    세로 이미지(width <= height)는 영양표이므로 제외한다. width/height
    메타데이터가 없으면 음식 사진 누락을 막기 위해 포함한다.
    """
    images = []
    skipped = 0
    for p in posts:
        if p.get("adult_only"):
            log(
                f"주의: '{p.get('title', '').strip()}' 포스트가 성인인증 잠금 상태 — "
                "비로그인 API에는 실사진 대신 placeholder만 내려옴"
            )
        for i, m in enumerate(p.get("media", [])):
            url = m.get("xlarge_url") or m.get("url")
            if not url or m.get("type") != "image":
                continue
            w, h = m.get("width") or 0, m.get("height") or 0
            if w and h and w <= h:  # 세로 = 영양표 → 제외
                skipped += 1
                continue
            try:
                resp = requests.get(
                    url, headers={"User-Agent": USER_AGENT}, timeout=30
                )
                resp.raise_for_status()
                if len(resp.content) < MIN_IMAGE_BYTES:
                    log(f"placeholder 의심 이미지 제외 ({len(resp.content)}B): {url}")
                    continue
                images.append((f"menu_{p['id']}_{i + 1}.jpg", resp.content))
            except Exception as e:
                log(f"이미지 다운로드 실패 (건너뜀): {url} — {e}")
    return images, skipped


def already_sent(client, channel, header_line):
    """채널에 오늘 같은 끼니(header_line으로 시작하는 봇 메시지)가 이미 있으면 True.

    중복 전송 방지용. 네트워크 오류(IncompleteRead 등)에 대비해 여러 번 재시도하고,
    끝까지 조회에 실패하면 안전하게 True를 반환해 전송을 보류한다 — 중복보다 누락이
    낫고, 누락은 다음 백업 크론 실행이 재시도로 메운다. limit은 응답 크기(첨부 메타로
    커짐)를 줄여 IncompleteRead를 완화하려고 작게 잡는다.

    단, 인증/설정 오류(FATAL_SLACK_ERRORS)는 재시도·보류가 무의미하므로 삼키지 않고
    그대로 raise한다 — 조용히 "이미 전송됨"으로 위장돼 run이 success로 뜨는 걸 막는다.
    """
    from slack_sdk.errors import SlackApiError

    last_err = None
    for attempt in range(4):
        try:
            resp = client.conversations_history(channel=channel, limit=15)
            for msg in resp.get("messages", []):
                if msg.get("bot_id") and msg.get("text", "").startswith(header_line):
                    return True
            return False
        except SlackApiError as e:
            if e.response.get("error") in FATAL_SLACK_ERRORS:
                raise
            last_err = e
            log(f"중복 확인 재시도 {attempt + 1}/4: {e}")
            time.sleep(2)
        except Exception as e:
            last_err = e
            log(f"중복 확인 재시도 {attempt + 1}/4: {e}")
            time.sleep(2)
    log(f"중복 확인 4회 실패 — 중복 방지 위해 전송 보류(백업 크론이 재시도): {last_err}")
    return True


def delete_existing(client, channel, header_line):
    """header_line으로 시작하는 기존 봇 메시지와 그 첨부 파일을 삭제한다.

    chat:write는 봇 자신의 메시지 삭제를, files:write는 봇이 올린 파일 삭제를
    허용하므로 추가 스코프가 필요 없다. 메시지 삭제가 실패하면 raise한다 —
    조용히 넘어가면 already_sent가 재전송을 차단해 run만 success로 위장된다.
    """
    from slack_sdk.errors import SlackApiError

    deleted = 0
    resp = client.conversations_history(channel=channel, limit=15)
    for msg in resp.get("messages", []):
        if not (msg.get("bot_id") and msg.get("text", "").startswith(header_line)):
            continue
        client.chat_delete(channel=channel, ts=msg["ts"])
        deleted += 1
        log(f"기존 메시지 삭제: ts={msg['ts']}")
        for f in msg.get("files", []):
            try:
                client.files_delete(file=f["id"])
            except SlackApiError as e:
                log(f"첨부 파일 삭제 실패(무시): {f.get('id')} — {e.response.get('error')}")
    if not deleted:
        log(f"'{header_line}' 기존 메시지 없음 — 삭제 생략")


def list_member_channels(client):
    """봇이 멤버인 public/private 채널 [(id, name), ...]을 반환한다.

    /invite된 채널을 매 실행 때 탐색해 전부에 게시하기 위한 것. channels:read·groups:read
    스코프가 필요하며, 스코프가 없는 구 토큰(앱 재설치 전)이면 None을 반환해 호출부가
    SLACK_CHANNEL_ID 폴백을 쓰게 한다 — 코드 배포와 토큰 갱신 순서가 어긋나도 기존 채널
    게시가 끊기지 않게. DM(im/mpim)은 조회하지 않는다.
    """
    from slack_sdk.errors import SlackApiError

    channels = []
    cursor = None
    while True:
        try:
            resp = client.users_conversations(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
        except SlackApiError as e:
            if e.response.get("error") == "missing_scope":
                log(
                    "채널 목록 조회 불가(missing_scope) — 토큰에 channels:read/groups:read가 "
                    "없음. 앱 재설치 후 SLACK_BOT_TOKEN 갱신 필요. SLACK_CHANNEL_ID로 폴백"
                )
                return None
            raise
        channels.extend(
            (c["id"], c.get("name", c["id"])) for c in resp.get("channels", [])
        )
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            return channels


def resolve_channels(client):
    """게시 대상 채널 목록을 정한다. 하나도 없으면 raise — run이 조용히 success로 뜨지 않게."""
    channels = list_member_channels(client)
    if channels is None:
        fallback = os.environ.get("SLACK_CHANNEL_ID")
        if not fallback:
            raise RuntimeError(
                "채널 목록 조회 스코프가 없고 SLACK_CHANNEL_ID도 없음 — "
                "앱 재설치 후 토큰을 갱신하거나 SLACK_CHANNEL_ID를 설정할 것"
            )
        return [(fallback, fallback)]
    if not channels:
        raise RuntimeError("봇이 초대된 채널이 없음 — 받을 채널에서 /invite @메뉴봇 후 재실행")
    return channels


def post_to_channel(client, channel, label, message, images, resend):
    header_line = message.splitlines()[0]
    if resend:
        # 삭제 직후 채널 기록 조회는 레이스가 있으므로 already_sent 체크는 건너뛴다.
        delete_existing(client, channel, header_line)
    elif already_sent(client, channel, header_line):
        log(f"{label}: '{header_line}' 이미 전송됨 — 중복 방지로 생략")
        return

    if images:
        try:
            client.files_upload_v2(
                channel=channel,
                initial_comment=message,
                file_uploads=[
                    {"content": data, "filename": name, "title": name}
                    for name, data in images
                ],
            )
            log(f"{label}: 슬랙 전송 완료 (이미지 {len(images)}장 첨부)")
            return
        except Exception as e:
            log(f"{label}: 이미지 첨부 전송 실패, 텍스트만 전송으로 폴백: {e}")

    client.chat_postMessage(channel=channel, text=message)
    log(f"{label}: 슬랙 전송 완료 (텍스트만)")


def send_to_slack(message, images, resend=False):
    """봇이 초대된 모든 채널에 게시한다. 채널별로 중복 확인·전송을 독립 수행한다.

    한 채널의 채널 한정 오류(CHANNEL_SCOPED_ERRORS)는 그 채널만 건너뛰고 계속 진행하되,
    끝에 RuntimeError로 run을 실패 표시해 Actions에서 보이게 한다. 이미 게시된 채널은
    백스톱 재실행 때 already_sent로 skip되므로 멱등성은 유지된다.
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

    # 토큰 유효성을 먼저 확인해 무효 토큰이면 여기서 바로(명확히) 실패시킨다.
    # 이렇게 안 하면 인증 오류가 "이미 전송됨"으로 위장돼 run이 조용히 success로 뜬다.
    client.auth_test()
    log("봇 인증 확인")

    # 공개 저장소라 Actions 로그가 누구에게나 보인다 — 워크스페이스·채널 이름/ID(private
    # 채널 포함)를 찍지 않고 순번만 남긴다. 순번↔채널 대응은 로컬에서 --list-channels로 확인.
    channels = resolve_channels(client)
    log(f"게시 대상 채널 {len(channels)}개")

    failed = []
    for i, (channel, _name) in enumerate(channels, 1):
        label = f"채널 {i}/{len(channels)}"
        try:
            post_to_channel(client, channel, label, message, images, resend)
        except SlackApiError as e:
            err = e.response.get("error")
            if err not in CHANNEL_SCOPED_ERRORS:
                raise
            log(f"{label}: 채널 오류({err}) — 이 채널만 건너뜀")
            failed.append(f"{label}({err})")

    if failed:
        raise RuntimeError(f"일부 채널 전송 실패: {', '.join(failed)}")


def wait_until_send_time(meal):
    hour, minute = MEALS[meal]["send_at"]
    now = datetime.now(KST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target:
        wait = (target - now).total_seconds()
        log(f"전송 시각 {target.strftime('%H:%M')} KST까지 {int(wait)}초 대기")
        time.sleep(wait)


def grace_supplement(matched, date_re, keyword, meal):
    """본식 발견 후 카테고리 포스트가 덜 올라왔으면 잠깐 더 기다렸다 한 번 보완한다."""
    if len(matched) < MEALS[meal]["expected_posts"]:
        # 카테고리 포스트가 몇 초 간격으로 올라오므로 잠깐 더 기다렸다 한 번 보완
        log(f"본식 포함 {len(matched)}개 발견, {GRACE_POLL_DELAY}초 후 나머지 포스트 재확인")
        time.sleep(GRACE_POLL_DELAY)
        try:
            matched = match_posts(fetch_posts(), date_re, keyword)
        except Exception as e:
            log(f"보완 폴링 실패, 현재까지 발견분으로 진행: {e}")
    return matched


def find_once(date_re, keyword, meal, date_str):
    """카카오를 1회만 조회한다. 본식 포함 매칭이 있으면 (grace 보완 후) 반환, 없으면 [].

    폴링은 하지 않는다 — 반복 시도는 외부 스케줄러/백업 크론의 실행 빈도가 담당한다.
    """
    try:
        matched = match_posts(fetch_posts(), date_re, keyword)
    except Exception as e:
        log(f"카카오 API 호출 실패: {e}")
        return []

    if matched and has_main_post(matched, date_re, keyword):
        return grace_supplement(matched, date_re, keyword, meal)

    log(f"{date_str} {keyword} 아직 미게시(본식 포스트 없음) — 종료(다음 발동이 재시도)")
    return []


def poll_until_found(
    date_re, keyword, meal, date_str, timeout=POLL_TIMEOUT, allow_partial=False
):
    """본식 포스트가 올라올 때까지 POLL_INTERVAL 간격으로 최대 timeout초 폴링한다.

    발견 시 (grace 보완 후) 매칭을 반환한다. 타임아웃 시에는 allow_partial일 때만
    본식 없는 발견분으로 진행하고, 아니면 []를 반환해 다음 트리거가 재시도하게 한다.

    짧은 폴링 창(외부 dispatch)에서 부분 전송을 허용하면, 카테고리 포스트만 올라온
    수십 초 사이에 창이 끝났을 때 불완전한 메시지가 나가고 그 뒤 already_sent가 완전한
    메시지를 영구 차단한다. 그래서 부분 전송은 최후의 수단인 긴 백스톱 창에만 허용한다.
    """
    deadline = time.monotonic() + timeout
    matched = []
    while True:
        try:
            matched = match_posts(fetch_posts(), date_re, keyword)
        except Exception as e:
            log(f"카카오 API 호출 실패: {e}")
            matched = []

        if matched and has_main_post(matched, date_re, keyword):
            return grace_supplement(matched, date_re, keyword, meal)

        if time.monotonic() >= deadline:
            if matched and allow_partial:
                log("타임아웃 — 본식 포스트는 없지만 발견된 포스트로 진행")
                return matched
            if matched:
                log(
                    f"타임아웃({timeout // 60}분) — 본식 포스트 없음(카테고리 "
                    f"{len(matched)}개만 발견), 부분 전송 안 함 — 다음 발동이 재시도"
                )
                return []
            log(f"타임아웃({timeout // 60}분) — {date_str} {keyword} 포스트 없음, 종료")
            return []

        log(f"{date_str} {keyword} 포스트 미발견, {POLL_INTERVAL // 60}분 후 재시도")
        time.sleep(POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--meal", choices=MEALS)
    parser.add_argument(
        "--list-channels",
        action="store_true",
        help="봇이 초대된(게시 대상) 채널 목록만 출력하고 종료. SLACK_BOT_TOKEN 필요",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="카카오를 1회만 조회하고 종료(폴링 안 함). 반복 시도는 외부 스케줄러가 담당",
    )
    parser.add_argument("--no-wait", action="store_true", help="정각 대기 없이 즉시 폴링")
    parser.add_argument(
        "--max-wait",
        type=int,
        default=None,
        metavar="분",
        help="폴링 모드 최대 대기(분). 미지정 시 기본 2시간. --once에는 무영향",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="폴링 타임아웃 시 본식 포스트가 없어도 발견분으로 전송(긴 백스톱 창 전용)",
    )
    parser.add_argument(
        "--resend",
        action="store_true",
        help="전송 전에 오늘 같은 끼니의 기존 봇 메시지를 삭제하고 다시 전송(--once와 함께 사용)",
    )
    parser.add_argument("--dry-run", action="store_true", help="슬랙 전송 없이 출력만")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="오늘 대신 이 날짜의 끼니를 찾는다(누락분 지연 전송용, --once와 함께 사용)",
    )
    args = parser.parse_args()

    if args.list_channels:
        from slack_sdk import WebClient

        client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        auth = client.auth_test()
        log(f"봇 {auth.get('user')} 인증 확인 (team: {auth.get('team')})")
        for i, (cid, name) in enumerate(resolve_channels(client), 1):
            print(f"{i}\t{cid}\t#{name}")
        return
    if not args.meal:
        parser.error("--meal 또는 --list-channels 중 하나가 필요")

    global TARGET_DATE
    TARGET_DATE = args.date
    keyword = MEALS[args.meal]["keyword"]
    day = target_date()
    date_str = f"{day.month}월{day.day}일"
    date_re = re.compile(rf"{day.month}\s*월\s*{day.day}\s*일?")
    log(f"대상: {date_str} {keyword}")

    if args.once:
        matched = find_once(date_re, keyword, args.meal, date_str)
    else:
        if not args.no_wait and not args.dry_run:
            wait_until_send_time(args.meal)
        timeout = args.max_wait * 60 if args.max_wait is not None else POLL_TIMEOUT
        matched = poll_until_found(
            date_re, keyword, args.meal, date_str, timeout, args.allow_partial
        )

    if not matched:
        return

    message, ordered = build_message(matched, date_str, date_re, keyword)
    log(f"포스트 {len(ordered)}개 매칭:\n{message}")

    expected = MEALS[args.meal]["expected_posts"]
    if len(ordered) < expected:
        # 조용히 불완전한 메시지가 나가는 걸 막기 위한 경고 (전송 자체는 막지 않는다 —
        # 일부라도 올라가는 편이 낫고, 빠진 포스트가 있다면 resolve_meals가 제목까지
        # 이미 로그에 남겼다). 2026-09-04 중식이 이 경우로 샐러드 코너를 빠뜨렸다.
        log(
            f"경고: {keyword} 포스트가 기대치({expected}개)보다 적은 {len(ordered)}개 — "
            "카카오에 아직 안 올라왔거나 제목 표기가 달라 매칭에서 빠졌을 수 있다"
        )

    images, skipped = collect_images(ordered)
    log(f"가로 음식 사진 {len(images)}장 첨부 (영양표 {skipped}장 제외)")

    if args.dry_run:
        log("dry-run 모드 — 슬랙 전송 생략")
        return

    send_to_slack(message, images, resend=args.resend)


if __name__ == "__main__":
    main()
