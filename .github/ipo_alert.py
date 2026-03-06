#!/usr/bin/env python3
"""
공모주 알림 봇
- ipostock.co.kr에서 공모주 정보 수집
- 상장 하루 전날 텔레그램으로 알림 발송
- 매일 오전 9시 실행 권장 (cron 설정)
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import re
import time
import os

# ============================================================
# 설정 (본인 값으로 변경하세요)
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "여기에_봇_토큰_입력")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "여기에_챗_ID_입력")

# Claude API 키 (기업 설명 AI 요약에 사용, 없으면 None)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", None)
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "http://www.ipostock.co.kr/",
}

LIST_URL   = "http://www.ipostock.co.kr/sub03/ipo04.asp"
DETAIL_URL = "http://www.ipostock.co.kr/sub03/ipo04.asp"


# ──────────────────────────────────────────────────────────────
# 1. 공모주 목록 가져오기
# ──────────────────────────────────────────────────────────────
def fetch_ipo_list() -> list[dict]:
    """상장 예정 공모주 목록 반환"""
    res = requests.get(LIST_URL, headers=HEADERS, timeout=15)
    res.encoding = "euc-kr"
    soup = BeautifulSoup(res.text, "html.parser")

    items = []
    # 테이블 행 파싱 (종목명 / 상장일 / 링크)
    for row in soup.select("table.listTable tbody tr, table.list_t tbody tr, table tbody tr"):
        cols = row.find_all("td")
        if len(cols) < 5:
            continue

        link_tag = row.find("a", href=True)
        name = cols[0].get_text(strip=True) if cols else ""
        
        # 상장일: 보통 4~6번째 컬럼에 위치
        listing_date_str = ""
        for col in cols[3:7]:
            txt = col.get_text(strip=True)
            if re.match(r"\d{4}[-./]\d{2}[-./]\d{2}", txt) or re.match(r"\d{2}[-./]\d{2}[-./]\d{2}", txt):
                listing_date_str = txt
                break

        if not name or not listing_date_str:
            continue

        # 날짜 파싱
        listing_date_str = listing_date_str.replace(".", "-").replace("/", "-")
        try:
            if len(listing_date_str) == 8:  # YYYY-MM-DD
                listing_date = datetime.strptime(listing_date_str, "%Y-%m-%d").date()
            else:
                listing_date = datetime.strptime(listing_date_str, "%y-%m-%d").date()
        except ValueError:
            continue

        detail_href = link_tag["href"] if link_tag else ""
        items.append({
            "name": name,
            "listing_date": listing_date,
            "detail_href": detail_href,
        })

    return items


# ──────────────────────────────────────────────────────────────
# 2. 상세 페이지에서 핵심 정보 파싱
# ──────────────────────────────────────────────────────────────
def fetch_ipo_detail(href: str) -> dict:
    """공모주 상세 페이지에서 필요한 정보 추출"""
    if href.startswith("http"):
        url = href
    else:
        url = f"http://www.ipostock.co.kr{href}"

    res = requests.get(url, headers=HEADERS, timeout=15)
    res.encoding = "euc-kr"
    soup = BeautifulSoup(res.text, "html.parser")

    info = {
        "공모가": None,
        "공모주식수": None,
        "상장주식수": None,
        "초기시가총액": None,
        "유통가능물량_pct": None,
        "기존주주_pct": None,
        "기업개요": None,
    }

    full_text = soup.get_text(separator="\n")

    # ── 공모가 ──
    m = re.search(r"확정\s*공모가[^\d]*([0-9,]+)\s*원", full_text)
    if not m:
        m = re.search(r"공모가[^\d]*([0-9,]+)\s*원", full_text)
    if m:
        info["공모가"] = int(m.group(1).replace(",", ""))

    # ── 상장주식수 (총발행주식수) ──
    m = re.search(r"상장\s*주식수[^\d]*([0-9,]+)\s*주", full_text)
    if not m:
        m = re.search(r"총\s*발행\s*주식수[^\d]*([0-9,]+)", full_text)
    if m:
        info["상장주식수"] = int(m.group(1).replace(",", ""))

    # ── 초기 시가총액 계산 ──
    if info["공모가"] and info["상장주식수"]:
        info["초기시가총액"] = info["공모가"] * info["상장주식수"]

    # ── 유통가능물량 % ──
    m = re.search(r"유통\s*가능\s*물량[^\d]*([0-9.]+)\s*%", full_text)
    if not m:
        m = re.search(r"유통\s*가능[^\d]*([0-9.]+)\s*%", full_text)
    if m:
        info["유통가능물량_pct"] = float(m.group(1))

    # ── 기존주주 % (보호예수) ──
    m = re.search(r"기존\s*주주[^\d]*([0-9.]+)\s*%", full_text)
    if not m:
        m = re.search(r"의무\s*보호\s*예수[^\d]*([0-9.]+)\s*%", full_text)
    if m:
        info["기존주주_pct"] = float(m.group(1))

    # ── 기업 개요 (메타 description 또는 첫 문단) ──
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        info["기업개요"] = meta_desc["content"].strip()
    else:
        # 본문에서 첫 의미있는 단락 추출
        for p in soup.find_all(["p", "div"], limit=30):
            txt = p.get_text(strip=True)
            if len(txt) > 40 and "공모" in txt:
                info["기업개요"] = txt[:200]
                break

    return info


# ──────────────────────────────────────────────────────────────
# 3. (선택) Claude API로 기업 설명 AI 요약
# ──────────────────────────────────────────────────────────────
def summarize_with_claude(company_name: str, raw_text: str) -> str:
    """Anthropic Claude API를 사용해 기업 설명을 2~3줄로 요약"""
    if not ANTHROPIC_API_KEY:
        return raw_text or "정보 없음"

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"다음은 '{company_name}'이라는 기업에 대한 설명입니다. "
                    f"이 기업이 어떤 사업을 하는지 2~3문장으로 쉽게 요약해주세요.\n\n{raw_text}"
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"[Claude 요약 오류] {e}")
        return raw_text or "정보 없음"


# ──────────────────────────────────────────────────────────────
# 4. 텔레그램 메시지 전송
# ──────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print(f"[텔레그램 전송 성공]")
        return True
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")
        return False


# ──────────────────────────────────────────────────────────────
# 5. 메시지 포맷
# ──────────────────────────────────────────────────────────────
def format_message(name: str, listing_date, d_day: int, detail: dict, summary: str) -> str:
    # D-day 텍스트
    if d_day == 0:
        dday_str = "🔴 <b>오늘 상장!</b>  D-Day"
    elif d_day == 1:
        dday_str = "🚨 <b>내일 상장!</b>  D-1"
    else:
        dday_str = f"📅 상장 <b>D-{d_day}</b>"

    # 시가총액 포맷 (억/조 단위)
    mktcap_str = "정보 없음"
    if detail["초기시가총액"]:
        cap = detail["초기시가총액"]
        if cap >= 1_000_000_000_000:
            mktcap_str = f"{cap / 1_000_000_000_000:.2f}조 원"
        else:
            mktcap_str = f"{cap / 100_000_000:.0f}억 원"

    공모가_str  = f"{detail['공모가']:,}원" if detail["공모가"] else "정보 없음"
    유통_str    = f"{detail['유통가능물량_pct']}%" if detail["유통가능물량_pct"] else "정보 없음"
    기존주주_str = f"{detail['기존주주_pct']}%" if detail["기존주주_pct"] else "정보 없음"

    msg = (
        f"🔔 <b>공모주 알림</b>  |  {dday_str}\n"
        f"{'─' * 28}\n"
        f"📌 <b>{name}</b>  |  상장일 {listing_date}\n\n"
        f"💰 <b>공모가 기준 초기 시가총액</b>\n"
        f"   공모가 {공모가_str} → <b>{mktcap_str}</b>\n\n"
        f"📊 <b>주주 구성</b>\n"
        f"   유통 가능 물량         : <b>{유통_str}</b>\n"
        f"   기존 주주(보호예수 등) : <b>{기존주주_str}</b>\n\n"
        f"🏢 <b>기업 소개</b>\n"
        f"   {summary}\n\n"
        f"🔗 <a href='http://www.ipostock.co.kr/sub03/ipo04.asp'>ipostock 바로가기</a>"
    )
    return msg


# ──────────────────────────────────────────────────────────────
# 6. 메인 실행
# ──────────────────────────────────────────────────────────────
def main():
    # TEST_MODE=1 이면 가장 가까운 종목 무조건 전송 (테스트용)
    test_mode = os.environ.get("TEST_MODE", "0") == "1"

    today    = datetime.today().date()
    tomorrow = today + timedelta(days=1)

    print(f"[실행] {today}  |  {'테스트 모드' if test_mode else '일반 모드'}")

    ipo_list = fetch_ipo_list()
    print(f"[목록] 총 {len(ipo_list)}개 종목 파싱됨")

    # 오늘 이후 상장 예정 종목만 필터 후 날짜순 정렬
    future = [x for x in ipo_list if x["listing_date"] >= today]
    future.sort(key=lambda x: x["listing_date"])

    if test_mode:
        # 테스트: 가장 가까운 종목 1개 무조건 전송
        if not future:
            print("상장 예정 공모주 없음. 종료.")
            return
        targets = [future[0]]
        print(f"[테스트] 가장 가까운 종목: {targets[0]['name']} ({targets[0]['listing_date']})")
    else:
        # 일반: 내일 상장 종목만
        targets = [x for x in future if x["listing_date"] == tomorrow]
        print(f"[대상] 내일 상장 종목: {len(targets)}개")
        if not targets:
            print("내일 상장 예정 공모주 없음. 종료.")
            return

    for ipo in targets:
        d_day = (ipo["listing_date"] - today).days
        print(f"\n  → {ipo['name']} (D-{d_day}) 상세 정보 수집 중...")
        try:
            detail  = fetch_ipo_detail(ipo["detail_href"])
            summary = summarize_with_claude(ipo["name"], detail.get("기업개요") or "")
            msg     = format_message(ipo["name"], ipo["listing_date"], d_day, detail, summary)

            print(msg)
            send_telegram(msg)
            time.sleep(1)

        except Exception as e:
            print(f"  [오류] {ipo['name']}: {e}")


if __name__ == "__main__":
    main()
