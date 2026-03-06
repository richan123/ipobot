#!/usr/bin/env python3
"""
공모주 텔레그램 알림 봇
- http://www.ipostock.co.kr/sub03/05_7.asp?page=1 에서 공모주 목록 수집
- 평일 매일 오전 9시: 내일 상장 종목 알림
- TEST_MODE=1: 가장 가까운 종목 즉시 전송 (테스트용)
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import re
import time
import os

# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", None)
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "http://www.ipostock.co.kr/",
}

LIST_URL    = "http://www.ipostock.co.kr/sub03/05_7.asp?page=1"
DETAIL_BASE = "http://www.ipostock.co.kr"


# ──────────────────────────────────────────────────────────────
# 1. 공모주 목록 파싱
# ──────────────────────────────────────────────────────────────
def fetch_ipo_list():
    res = requests.get(LIST_URL, headers=HEADERS, timeout=15)
    res.encoding = "euc-kr"
    soup = BeautifulSoup(res.text, "html.parser")

    items = []
    for row in soup.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) < 3:
            continue

        # 첫 번째 td가 날짜 형식인지 확인 (예: 2026.03.09)
        date_txt = cols[0].get_text(strip=True)
        if not re.match(r"\d{4}\.\d{2}\.\d{2}", date_txt):
            continue

        # 종목명과 상세 링크
        link_tag = cols[1].find("a", href=True)
        if not link_tag:
            continue

        name        = link_tag.get_text(strip=True)
        detail_href = link_tag["href"]
        if not detail_href.startswith("http"):
            detail_href = DETAIL_BASE + "/" + detail_href.lstrip("/")

        # 날짜 파싱
        try:
            listing_date = datetime.strptime(date_txt, "%Y.%m.%d").date()
        except ValueError:
            continue

        items.append({
            "name":         name,
            "listing_date": listing_date,
            "detail_href":  detail_href,
        })

    return items


# ──────────────────────────────────────────────────────────────
# 2. 상세 페이지 파싱
# ──────────────────────────────────────────────────────────────
def fetch_ipo_detail(url):
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.encoding = "euc-kr"
    soup = BeautifulSoup(res.text, "html.parser")
    full_text = soup.get_text(separator="\n")

    info = {
        "공모가":          None,
        "상장주식수":       None,
        "초기시가총액":     None,
        "유통가능물량_pct": None,
        "기존주주_pct":    None,
        "기업개요":        None,
    }

    # 확정 공모가
    m = re.search(r"확정\s*공모가[^\d]*([0-9,]+)\s*원", full_text)
    if not m:
        m = re.search(r"공모가[^\d]*([0-9,]+)\s*원", full_text)
    if m:
        info["공모가"] = int(m.group(1).replace(",", ""))

    # 상장주식수
    m = re.search(r"상장\s*주식수[^\d]*([0-9,]+)", full_text)
    if not m:
        m = re.search(r"총\s*발행\s*주식수[^\d]*([0-9,]+)", full_text)
    if m:
        info["상장주식수"] = int(m.group(1).replace(",", ""))

    # 초기 시가총액
    if info["공모가"] and info["상장주식수"]:
        info["초기시가총액"] = info["공모가"] * info["상장주식수"]

    # 유통가능물량 %
    m = re.search(r"유통\s*가능\s*(?:주식수|물량)[^\d]*([0-9.]+)\s*%", full_text)
    if not m:
        m = re.search(r"유통\s*가능[^\d]*([0-9.]+)\s*%", full_text)
    if m:
        info["유통가능물량_pct"] = float(m.group(1))

    # 기존주주 %
    m = re.search(r"기존\s*주주[^\d]*([0-9.]+)\s*%", full_text)
    if not m:
        m = re.search(r"의무\s*보호\s*예수[^\d]*([0-9.]+)\s*%", full_text)
    if m:
        info["기존주주_pct"] = float(m.group(1))

    # 기업 개요
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        info["기업개요"] = meta["content"].strip()
    else:
        for tag in soup.find_all(["p", "td"], limit=50):
            txt = tag.get_text(strip=True)
            if len(txt) > 50 and any(kw in txt for kw in ["사업", "제조", "개발", "서비스", "플랫폼"]):
                info["기업개요"] = txt[:300]
                break

    return info


# ──────────────────────────────────────────────────────────────
# 3. Claude AI 요약 (선택)
# ──────────────────────────────────────────────────────────────
def summarize_with_claude(company_name, raw_text):
    if not ANTHROPIC_API_KEY or not raw_text:
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
                    f"'{company_name}' 기업이 어떤 사업을 하는지 "
                    f"2~3문장으로 쉽게 요약해주세요.\n\n{raw_text}"
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"[Claude 오류] {e}")
        return raw_text or "정보 없음"


# ──────────────────────────────────────────────────────────────
# 4. 텔레그램 전송
# ──────────────────────────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("[텔레그램 전송 성공]")
        return True
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")
        return False


# ──────────────────────────────────────────────────────────────
# 5. 메시지 포맷
# ──────────────────────────────────────────────────────────────
def format_message(name, listing_date, d_day, detail, summary):
    if d_day == 0:
        dday_str = "🔴 <b>오늘 상장!</b>  D-Day"
    elif d_day == 1:
        dday_str = "🚨 <b>내일 상장!</b>  D-1"
    else:
        dday_str = f"📅 상장 <b>D-{d_day}</b>"

    mktcap_str = "정보 없음"
    if detail["초기시가총액"]:
        cap = detail["초기시가총액"]
        mktcap_str = (
            f"{cap / 1_000_000_000_000:.2f}조 원"
            if cap >= 1_000_000_000_000
            else f"{cap / 100_000_000:.0f}억 원"
        )

    공모가_str = f"{detail['공모가']:,}원" if detail["공모가"] else "정보 없음"
    유통_str   = f"{detail['유통가능물량_pct']}%" if detail["유통가능물량_pct"] else "정보 없음"
    주주_str   = f"{detail['기존주주_pct']}%" if detail["기존주주_pct"] else "정보 없음"

    return (
        f"🔔 <b>공모주 알림</b>  |  {dday_str}\n"
        f"{'─' * 30}\n"
        f"📌 <b>{name}</b>  |  상장일 {listing_date}\n\n"
        f"💰 <b>공모가 기준 초기 시가총액</b>\n"
        f"   공모가 {공모가_str}  →  <b>{mktcap_str}</b>\n\n"
        f"📊 <b>주주 구성</b>\n"
        f"   유통 가능 물량         : <b>{유통_str}</b>\n"
        f"   기존 주주(보호예수 등) : <b>{주주_str}</b>\n\n"
        f"🏢 <b>기업 소개</b>\n"
        f"   {summary}\n\n"
        f"🔗 <a href='http://www.ipostock.co.kr/sub03/05_7.asp?page=1'>ipostock 바로가기</a>"
    )


# ──────────────────────────────────────────────────────────────
# 6. 메인
# ──────────────────────────────────────────────────────────────
def main():
    test_mode = os.environ.get("TEST_MODE", "0") == "1"
    today     = datetime.today().date()
    tomorrow  = today + timedelta(days=1)

    print(f"[실행] {today}  |  {'테스트 모드' if test_mode else '일반 모드'}")

    ipo_list = fetch_ipo_list()
    print(f"[목록] 총 {len(ipo_list)}개 종목 파싱됨")
    for x in ipo_list:
        print(f"       - {x['listing_date']}  {x['name']}")

    future = sorted(
        [x for x in ipo_list if x["listing_date"] >= today],
        key=lambda x: x["listing_date"]
    )

    if test_mode:
        if not future:
            print("상장 예정 공모주 없음. 종료.")
            return
        targets = [future[0]]
        print(f"[테스트] 가장 가까운 종목: {targets[0]['name']} ({targets[0]['listing_date']})")
    else:
        targets = [x for x in future if x["listing_date"] == tomorrow]
        print(f"[대상] 내일 상장 종목: {len(targets)}개")
        if not targets:
            print("내일 상장 예정 공모주 없음. 종료.")
            return

    for ipo in targets:
        d_day = (ipo["listing_date"] - today).days
        print(f"\n→ {ipo['name']} D-{d_day} 상세 정보 수집 중...")
        try:
            detail  = fetch_ipo_detail(ipo["detail_href"])
            summary = summarize_with_claude(ipo["name"], detail.get("기업개요") or "")
            msg     = format_message(ipo["name"], ipo["listing_date"], d_day, detail, summary)
            print(msg)
            send_telegram(msg)
            time.sleep(1)
        except Exception as e:
            print(f"[오류] {ipo['name']}: {e}")


if __name__ == "__main__":
    main()
