"""카카오톡 채널의 중식/석식 포스트를 슬랙 채널로 전송한다.

사용법:
    python menu_to_slack.py --meal lunch          # 정각(11:00 KST)까지 대기 후 전송
    python menu_to_slack.py --meal dinner
    python menu_to_slack.py --meal lunch --no-wait  # 대기 없이 즉시 폴링
    python menu_to_slack.py --meal lunch --dry-run  # 슬랙 전송 없이 메시지만 출력

필요 환경변수: SLACK_BOT_TOKEN, SLACK_CHANNEL_ID (dry-run 시 불필요)
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
KAKAO_POSTS_URL = "https://pf.kakao.com/rocket-web/web/profiles/_BxexgQb/posts"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

MEALS = {
    "lunch": {"keyword": "중식", "send_at": (11, 0)},
    "dinner": {"keyword": "석식", "send_at": (17, 30)},
}

POLL_INTERVAL = 5 * 60  # 5분
POLL_TIMEOUT = 2 * 60 * 60  # 최대 2시간
EXPECTED_POST_COUNT = 3  # 본식 / 샐러드인더키친 / T/O도시락&베이커리
GRACE_POLL_DELAY = 90  # 본식 발견 후 나머지 포스트를 기다리는 추가 폴링 간격(초)


def log(msg):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_posts():
    resp = requests.get(
        KAKAO_POSTS_URL, headers={"User-Agent": USER_AGENT}, timeout=30
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def match_posts(posts, date_str, keyword):
    """오늘 날짜 + 끼니 키워드가 제목에 있는 포스트만 반환 (고정 포스트 제외)."""
    matched = []
    for p in posts:
        if p.get("pinned"):
            continue
        title = p.get("title", "")
        if date_str in title and keyword in title:
            matched.append(p)
    return matched


def has_main_post(posts, date_str, keyword):
    """제목이 정확히 'N월N일 중식' 형태인 본식 포스트가 있는지 확인."""
    for p in posts:
        normalized = re.sub(r"\s+", "", p.get("title", ""))
        if normalized == f"{date_str}{keyword}":
            return True
    return False


def category_label(title, date_str, keyword):
    """제목에서 날짜/끼니 접두어를 제거해 카테고리 라벨을 뽑는다."""
    label = title.replace(date_str, "").replace(keyword, "").strip()
    return label or keyword


def build_message(posts, date_str, keyword):
    # 본식 포스트를 맨 앞으로, 나머지는 API 순서 유지
    def is_main(p):
        return re.sub(r"\s+", "", p.get("title", "")) == f"{date_str}{keyword}"

    ordered = sorted(posts, key=lambda p: 0 if is_main(p) else 1)

    lines = [f"{date_str} {keyword}", ""]
    for p in ordered:
        items = []
        for c in p.get("contents", []):
            if c.get("t") == "text" and c.get("v"):
                items.extend(
                    line.strip() for line in c["v"].splitlines() if line.strip()
                )
        label = category_label(p["title"], date_str, keyword)
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
    """
    last_err = None
    for attempt in range(4):
        try:
            resp = client.conversations_history(channel=channel, limit=15)
            for msg in resp.get("messages", []):
                if msg.get("bot_id") and msg.get("text", "").startswith(header_line):
                    return True
            return False
        except Exception as e:
            last_err = e
            log(f"중복 확인 재시도 {attempt + 1}/4: {e}")
            time.sleep(2)
    log(f"중복 확인 4회 실패 — 중복 방지 위해 전송 보류(백업 크론이 재시도): {last_err}")
    return True


def send_to_slack(message, images):
    from slack_sdk import WebClient

    token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CHANNEL_ID"]
    client = WebClient(token=token)

    header_line = message.splitlines()[0]
    if already_sent(client, channel, header_line):
        log(f"'{header_line}' 이미 전송됨 — 중복 방지로 생략")
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
            log(f"슬랙 전송 완료 (이미지 {len(images)}장 첨부)")
            return
        except Exception as e:
            log(f"이미지 첨부 전송 실패, 텍스트만 전송으로 폴백: {e}")

    client.chat_postMessage(channel=channel, text=message)
    log("슬랙 전송 완료 (텍스트만)")


def wait_until_send_time(meal):
    hour, minute = MEALS[meal]["send_at"]
    now = datetime.now(KST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target:
        wait = (target - now).total_seconds()
        log(f"전송 시각 {target.strftime('%H:%M')} KST까지 {int(wait)}초 대기")
        time.sleep(wait)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--meal", choices=MEALS, required=True)
    parser.add_argument("--no-wait", action="store_true", help="정각 대기 없이 즉시 폴링")
    parser.add_argument("--dry-run", action="store_true", help="슬랙 전송 없이 출력만")
    args = parser.parse_args()

    keyword = MEALS[args.meal]["keyword"]
    now = datetime.now(KST)
    date_str = f"{now.month}월{now.day}일"
    log(f"대상: {date_str} {keyword}")

    if not args.no_wait and not args.dry_run:
        wait_until_send_time(args.meal)

    deadline = time.monotonic() + POLL_TIMEOUT
    matched = []
    while True:
        try:
            posts = fetch_posts()
            matched = match_posts(posts, date_str, keyword)
        except Exception as e:
            log(f"카카오 API 호출 실패: {e}")
            matched = []

        if matched and has_main_post(matched, date_str, keyword):
            if len(matched) < EXPECTED_POST_COUNT:
                # 카테고리 포스트가 몇 초 간격으로 올라오므로 잠깐 더 기다렸다 한 번 보완
                log(
                    f"본식 포함 {len(matched)}개 발견, {GRACE_POLL_DELAY}초 후 나머지 포스트 재확인"
                )
                time.sleep(GRACE_POLL_DELAY)
                try:
                    matched = match_posts(fetch_posts(), date_str, keyword)
                except Exception as e:
                    log(f"보완 폴링 실패, 현재까지 발견분으로 진행: {e}")
            break

        if time.monotonic() >= deadline:
            if matched:
                log("타임아웃 — 본식 포스트는 없지만 발견된 포스트로 진행")
                break
            log(f"타임아웃({POLL_TIMEOUT // 60}분) — {date_str} {keyword} 포스트 없음, 종료")
            sys.exit(0)

        log(f"{date_str} {keyword} 포스트 미발견, {POLL_INTERVAL // 60}분 후 재시도")
        time.sleep(POLL_INTERVAL)

    message, ordered = build_message(matched, date_str, keyword)
    log(f"포스트 {len(ordered)}개 매칭:\n{message}")

    images, skipped = collect_images(ordered)
    log(f"가로 음식 사진 {len(images)}장 첨부 (영양표 {skipped}장 제외)")

    if args.dry_run:
        log("dry-run 모드 — 슬랙 전송 생략")
        return

    send_to_slack(message, images)


if __name__ == "__main__":
    main()
