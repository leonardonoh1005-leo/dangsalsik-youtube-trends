#!/usr/bin/env python3
"""
당신을살릴식사 (당살식) - 유튜브 건강 콘텐츠 바이럴 지수 TOP 10 자동 수집기
최근 7일 이내 업로드된 건강 관련 전체 영상(쇼츠+롱폼)을 검색하여
이전 날짜에 이미 보고된 영상을 제외하고, 롱폼 비율 70% 이상(최소 7개)을 보장하면서
[바이럴 지수 = 조회수 ÷ 채널 구독자 수] 기준 1위~10위를 정리합니다.
"""

import csv
import datetime
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ==========================================
# 설정값 (필요에 따라 자유롭게 수정하세요)
# ==========================================
KEYWORDS = [
    "혈당",
    "당뇨",
    "염증",
    "독소 배출",
    "혈관",
    "중년 건강",
    "아침 공복",
    "면역력",
    "지방간",
    "장 건강",
]

# 비건강 주제(역사·정치·장례·재테크 등) 제외 키워드
EXCLUDE_TITLE_KEYWORDS = [
    "장례", "동맹", "전쟁", "역사", "정치", "선거", "대통령", "국회",
    "주식", "부동산", "코인", "게임", "사주", "운세", "타로", "귀신",
    "연예", "결혼", "이혼", "범죄", "사건",
]

# 해시태그를 제외한 실제 제목에 반드시 포함되어야 하는 건강/신체/식단 키워드
HEALTH_TITLE_KEYWORDS = [
    "혈당", "당뇨", "염증", "독소", "해독", "혈관", "혈압", "콜레스테롤",
    "건강", "공복", "면역", "지방간", "간수치", "장건강", "장 건강", "유산균",
    "신장", "콩팥", "위장", "소화", "역류성", "뼈", "관절", "근육", "근감소",
    "단백질", "식단", "음식", "식품", "식사", "영양", "비타민", "질병", "질환",
    "치매", "뇌", "심장", "심혈관", "폐", "눈", "노화", "수명", "소변", "대변",
    "숙면", "수면", "불면", "피로", "보약", "약", "병원", "의사", "한의사",
    "암", "통증", "부종", "붓기", "탈모", "다이어트", "체중", "비만",
]

DAYS_AGO = 7                  # 최근 N일 이내 영상
MAX_RESULTS_PER_KEYWORD = 50  # 키워드당 검색 수 (최대 50, API 비용 동일)
MIN_SUBSCRIBERS = 1000        # 왜곡 방지용 최소 구독자 수
MIN_VIEWS = 5000              # 최소 조회수 기준
TOP_N = 10                    # 최종 출력 순위 개수
MIN_LONGFORM_RATIO = 0.70     # 롱폼 최소 비율 (70% 이상 = 최소 7개)

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
REPORT_DIR = BASE_DIR / "reports"
# ==========================================


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_previously_reported_video_ids(report_dir: Path, today_kst: str) -> set[str]:
    """이전 날짜 리포트(CSV)에 이미 수록된 영상 ID 목록을 불러와 중복 노출을 방지"""
    seen_ids: set[str] = set()
    if not report_dir.exists():
        return seen_ids
    for csv_file in sorted(report_dir.glob("*_당살식_건강트렌드_TOP10.csv")):
        date_prefix = csv_file.name[:10]
        if date_prefix >= today_kst:
            continue
        try:
            with csv_file.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    url = row.get("영상URL", "")
                    if "v=" in url:
                        vid = url.split("v=", 1)[1].split("&", 1)[0].strip()
                        if vid:
                            seen_ids.add(vid)
        except Exception as e:
            print(f"[경고] 이전 리포트 읽기 실패 ({csv_file.name}): {e}", file=sys.stderr)
    return seen_ids


def api_get(endpoint: str, params: dict) -> dict:
    query_string = urllib.parse.urlencode(params)
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{query_string}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"YouTube API 오류 ({e.code}) [{endpoint}]: {body}") from e


def parse_iso8601_duration(duration_str: str) -> tuple[int, str]:
    """ISO 8601 duration (e.g. PT12M34S) -> (total_seconds, '12:34')"""
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_str or "")
    if not match:
        return 0, "0:00"
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    if hours > 0:
        formatted = f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        formatted = f"{minutes}:{seconds:02d}"
    return total, formatted


def is_health_related_title(title: str) -> bool:
    """해시태그를 제거한 순수 제목을 기준으로 실제 건강 주제인지 판별"""
    clean_title = re.sub(r"#\S+", "", title or "").strip()
    if any(bad in clean_title for bad in EXCLUDE_TITLE_KEYWORDS):
        return False
    return any(kw in clean_title for kw in HEALTH_TITLE_KEYWORDS)


def chunks(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def main() -> int:
    load_env(ENV_PATH)
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key or api_key == "여기에_API_키를_입력하세요":
        print(
            "[오류] YOUTUBE_API_KEY가 설정되지 않았습니다.\n"
            f"'{ENV_PATH}' 파일 또는 환경변수에 YouTube Data API v3 키를 입력해 주세요.",
            file=sys.stderr,
        )
        return 1

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    published_after = (now_utc - datetime.timedelta(days=DAYS_AGO)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d")

    seen_video_ids = load_previously_reported_video_ids(REPORT_DIR, today_kst)
    print(f"[{today_kst}] 최근 {DAYS_AGO}일 건강 콘텐츠 검색 시작 (이전 보고 영상 {len(seen_video_ids)}개 제외)...")

    video_keywords: dict[str, set[str]] = {}
    for kw in KEYWORDS:
        try:
            res = api_get(
                "search",
                {
                    "part": "snippet",
                    "q": kw,
                    "type": "video",
                    "order": "viewCount",
                    "publishedAfter": published_after,
                    "regionCode": "KR",
                    "relevanceLanguage": "ko",
                    "maxResults": MAX_RESULTS_PER_KEYWORD,
                    "key": api_key,
                },
            )
            for item in res.get("items", []):
                vid = item.get("id", {}).get("videoId")
                if vid and vid not in seen_video_ids:
                    video_keywords.setdefault(vid, set()).add(kw)
        except Exception as e:
            print(f"[경고] 키워드 '{kw}' 검색 실패: {e}", file=sys.stderr)

    all_video_ids = list(video_keywords.keys())
    if not all_video_ids:
        print("새로 검색된 영상이 없습니다.")
        return 0

    print(f"중복·기출 제외 후 총 {len(all_video_ids)}개 신규 영상 상세 정보 조회 중...")

    videos_data = []
    channel_ids = set()
    for batch in chunks(all_video_ids, 50):
        res = api_get(
            "videos",
            {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(batch),
                "key": api_key,
            },
        )
        for item in res.get("items", []):
            videos_data.append(item)
            cid = item.get("snippet", {}).get("channelId")
            if cid:
                channel_ids.add(cid)

    channels_map: dict[str, dict] = {}
    for batch in chunks(list(channel_ids), 50):
        res = api_get(
            "channels",
            {
                "part": "snippet,statistics",
                "id": ",".join(batch),
                "key": api_key,
            },
        )
        for item in res.get("items", []):
            channels_map[item["id"]] = item

    ranked = []
    for v in videos_data:
        vid = v["id"]
        snippet = v.get("snippet", {})
        stats = v.get("statistics", {})
        content = v.get("contentDetails", {})

        title = snippet.get("title", "")
        if not is_health_related_title(title):
            continue

        view_count = int(stats.get("viewCount", 0))
        if view_count < MIN_VIEWS:
            continue

        cid = snippet.get("channelId", "")
        ch = channels_map.get(cid, {})
        ch_stats = ch.get("statistics", {})
        if ch_stats.get("hiddenSubscriberCount"):
            continue

        sub_count = int(ch_stats.get("subscriberCount", 0))
        if sub_count < MIN_SUBSCRIBERS:
            continue

        viral_score = view_count / sub_count
        total_sec, dur_fmt = parse_iso8601_duration(content.get("duration", ""))
        desc = snippet.get("description", "")
        is_shorts = total_sec <= 60 or (total_sec <= 180 and "#shorts" in (title + " " + desc).lower())
        fmt_label = "쇼츠" if is_shorts else "롱폼"

        ranked.append(
            {
                "video_id": vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "title": title,
                "channel_title": snippet.get("channelTitle", ""),
                "channel_url": f"https://www.youtube.com/channel/{cid}",
                "subscribers": sub_count,
                "views": view_count,
                "viral_score": viral_score,
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
                "duration": dur_fmt,
                "format": fmt_label,
                "published_at": snippet.get("publishedAt", "")[:10],
                "keywords": ", ".join(sorted(video_keywords.get(vid, set()))),
            }
        )

    ranked.sort(key=lambda x: x["viral_score"], reverse=True)

    # 롱폼 최소 70% 보장 (TOP 10 기준 최소 7개 롱폼, 최대 3개 쇼츠)
    min_longform = math.ceil(TOP_N * MIN_LONGFORM_RATIO)
    max_shorts = TOP_N - min_longform

    longform_items = [x for x in ranked if x["format"] == "롱폼"]
    shorts_items = [x for x in ranked if x["format"] == "쇼츠"]

    selected = list(longform_items[:min_longform])
    remaining_pool = sorted(
        longform_items[min_longform:] + shorts_items,
        key=lambda x: x["viral_score"],
        reverse=True,
    )

    shorts_count = 0
    for item in remaining_pool:
        if len(selected) >= TOP_N:
            break
        if item["format"] == "쇼츠":
            if shorts_count >= max_shorts:
                continue
            shorts_count += 1
        selected.append(item)

    selected.sort(key=lambda x: x["viral_score"], reverse=True)
    top_items = selected[:TOP_N]

    actual_longform = sum(1 for x in top_items if x["format"] == "롱폼")
    actual_shorts = len(top_items) - actual_longform

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORT_DIR / f"{today_kst}_당살식_건강트렌드_TOP10.md"
    latest_md_path = BASE_DIR / "latest_report.md"
    csv_path = REPORT_DIR / f"{today_kst}_당살식_건강트렌드_TOP10.csv"

    md_lines = [
        f"# 당신을살릴식사 - 유튜브 건강 트렌드 TOP {TOP_N} ({today_kst})",
        "",
        f"- **분석 기준**: 최근 {DAYS_AGO}일 업로드 전체 영상 (이전 보고 영상 {len(seen_video_ids)}개 제외 / 최소 조회수 {MIN_VIEWS:,}회 / 최소 구독자 {MIN_SUBSCRIBERS:,}명 이상 / 순수 제목 건강 키워드 검증)",
        f"- **포맷 비율**: 롱폼 {actual_longform}개 ({actual_longform * 100 // max(len(top_items), 1)}%) / 쇼츠 {actual_shorts}개 (롱폼 70% 이상 필수 적용)",
        f"- **정렬 기준**: 바이럴 지수 (조회수 ÷ 채널 구독자 수) 내림차순",
        f"- **검색 키워드**: {', '.join(KEYWORDS)}",
        "",
        "| 순위 | 바이럴 지수 | 포맷(길이) | 제목 | 채널명 | 조회수 | 구독자 수 | 업로드일 | 키워드 |",
        "| :---: | :---: | :---: | :--- | :--- | ---: | ---: | :---: | :--- |",
    ]

    for idx, item in enumerate(top_items, start=1):
        safe_title = item["title"].replace("|", "\\|")
        safe_channel = item["channel_title"].replace("|", "\\|")
        md_lines.append(
            f"| {idx} | **{item['viral_score']:.2f}배** | {item['format']} ({item['duration']}) | "
            f"[{safe_title}]({item['url']}) | [{safe_channel}]({item['channel_url']}) | "
            f"{item['views']:,} | {item['subscribers']:,} | {item['published_at']} | {item['keywords']} |"
        )

    md_content = "\n".join(md_lines) + "\n"
    md_path.write_text(md_content, encoding="utf-8")
    latest_md_path.write_text(md_content, encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "순위",
                "바이럴지수(조회수/구독자)",
                "포맷",
                "길이",
                "제목",
                "영상URL",
                "채널명",
                "채널URL",
                "조회수",
                "구독자수",
                "좋아요",
                "댓글수",
                "업로드일",
                "매칭키워드",
            ]
        )
        for idx, item in enumerate(top_items, start=1):
            writer.writerow(
                [
                    idx,
                    round(item["viral_score"], 2),
                    item["format"],
                    item["duration"],
                    item["title"],
                    item["url"],
                    item["channel_title"],
                    item["channel_url"],
                    item["views"],
                    item["subscribers"],
                    item["likes"],
                    item["comments"],
                    item["published_at"],
                    item["keywords"],
                ]
            )

    print(f"완료! TOP {len(top_items)}개 (롱폼 {actual_longform}개 / 쇼츠 {actual_shorts}개) 결과를 저장했습니다:")
    print(f"- 마크다운: {md_path}")
    print(f"- CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
