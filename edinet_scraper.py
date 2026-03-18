"""
EDINET Playwright Scraper — 証券コードで有報・半報CSVを高速ダウンロード。

EDINET API の日付スキャン（730日×1req/秒=最大12分）を置き換え。
Playwright でブラウザ操作し、検索1回+DL数回で数十秒で完了。

Usage:
    py edinet_scraper.py 4709              # 有報+半報をDL & パース
    py edinet_scraper.py 4709 --period 7   # 全期間で検索
    py edinet_scraper.py 4709 --raw        # DLのみ（パースしない）
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# EDINET Search Page
# ---------------------------------------------------------------------------
EDINET_URL = "https://disclosure2.edinet-fsa.go.jp/WEEK0010.aspx"

# D_KIKAN dropdown values
PERIOD_MAP = {
    "today": "1",
    "3days": "2",
    "1week": "3",
    "1month": "4",
    "6months": "5",
    "1year": "6",
    "all": "7",
}

CACHE_BASE = Path(__file__).parent / "data" / "edinet"


def search_and_download(code_4: str, period: str = "1year", cache_dir: Path | None = None, max_yuho: int = 1):
    """
    EDINET Web UIから証券コードで検索し、有報・半報のCSV ZIPをダウンロード。

    Parameters:
        code_4: 4桁証券コード
        period: 検索期間キー（PERIOD_MAP参照）
        cache_dir: 保存先ディレクトリ（デフォルト: data/edinet/{code}/）
        max_yuho: ダウンロードする有報の最大件数（デフォルト1、複数年度取得時は2）

    Returns:
        list[dict]: ダウンロードした書類のリスト
            [{"doc_type": "yuho"|"hanki"|"quarterly", "date": "2025/06/19",
              "title": "有価証券報告書...", "period_end": "2025-03-31",
              "zip_path": Path}]
    """
    from playwright.sync_api import sync_playwright

    if cache_dir is None:
        cache_dir = CACHE_BASE / code_4
    cache_dir.mkdir(parents=True, exist_ok=True)

    period_val = PERIOD_MAP.get(period, "6")
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        print(f"[EDINET] {code_4} を検索中（期間: {period}）...")
        page.goto(EDINET_URL, timeout=30000)

        # 検索条件
        page.fill("#W0018vD_KEYWORD", code_4)
        page.select_option("#W0018vD_KIKAN", period_val)

        # 有報/半報/四半期報告書チェック（デフォルトONだが確実に）
        chk = page.query_selector("#W0018vCHKSYORUI1")
        if chk and not chk.is_checked():
            chk.check()

        # 検索実行
        page.click("#W0018BTNBTN_SEARCH")
        page.wait_for_load_state("networkidle", timeout=15000)

        # 結果テーブルを解析
        rows = page.query_selector_all("table tr")
        doc_rows = []
        for row in rows:
            cells = row.query_selector_all("td")
            if len(cells) < 8:
                continue
            date_text = cells[0].inner_text().strip()
            doc_text = cells[1].inner_text().strip()
            if "報告書" not in doc_text:
                continue

            # CSV ボタンを探す
            csv_cell = cells[7]  # CSV列
            csv_links = csv_cell.query_selector_all("a")
            if not csv_links:
                continue

            # 書類種別判定
            doc_type = _classify_doc(doc_text)
            period_end = _extract_period_end(doc_text)

            doc_rows.append({
                "date": date_text,
                "title": doc_text,
                "doc_type": doc_type,
                "period_end": period_end,
                "csv_link": csv_links[0],
            })

        if not doc_rows:
            print(f"[EDINET] {code_4}: 検索結果なし")
            browser.close()
            return results

        # 有報を period_end 降順ソートし max_yuho 件に絞る
        yuho_rows = [d for d in doc_rows if d["doc_type"] == "yuho"]
        other_rows = [d for d in doc_rows if d["doc_type"] != "yuho"]
        yuho_rows.sort(key=lambda d: d.get("period_end", ""), reverse=True)
        yuho_rows = yuho_rows[:max_yuho]
        doc_rows = yuho_rows + other_rows

        print(f"[EDINET] {len(doc_rows)}件の書類を検出（有報{len(yuho_rows)}件）")

        # 各書類のCSVをダウンロード
        for doc in doc_rows:
            doc_type = doc["doc_type"]
            period_end = doc["period_end"]
            # 有報で複数件の場合は period_end を含むファイル名で区別
            if doc_type == "yuho" and max_yuho > 1 and period_end:
                filename = f"{doc_type}_{period_end}_{code_4}.zip"
            else:
                filename = f"{doc_type}_{code_4}.zip"

            print(f"  DL: {doc['title'][:60]}...")
            try:
                with page.expect_download(timeout=30000) as dl_info:
                    doc["csv_link"].click()
                download = dl_info.value

                zip_path = cache_dir / filename
                download.save_as(str(zip_path))
                zip_size = zip_path.stat().st_size

                results.append({
                    "doc_type": doc_type,
                    "date": doc["date"],
                    "title": doc["title"],
                    "period_end": period_end,
                    "zip_path": zip_path,
                })
                print(f"    -> {zip_path.name} ({zip_size:,} bytes)")

                # 次のDLの前に少し待つ
                page.wait_for_timeout(500)

            except Exception as e:
                print(f"    DL失敗: {e}")

        browser.close()

    # メタデータ保存
    meta = {
        "code": code_4,
        "search_period": period,
        "docs": [
            {
                "doc_type": r["doc_type"],
                "date": r["date"],
                "title": r["title"],
                "period_end": r["period_end"],
                "filename": r["zip_path"].name,
            }
            for r in results
        ],
    }
    meta_path = cache_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return results


def _classify_doc(title: str) -> str:
    """書類タイトルから種別を判定。"""
    if "有価証券報告書" in title:
        return "yuho"
    elif "半期報告書" in title:
        return "hanki"
    elif "四半期報告書" in title:
        # Q1/Q2/Q3 を判別
        m = re.search(r"第(\d+)四半期", title)
        if m:
            return f"quarterly_Q{m.group(1)}"
        return "quarterly"
    return "other"


def _extract_period_end(title: str) -> str:
    """書類タイトルから期間末日を抽出。"""
    # "第57期(2024/04/01－2025/03/31)" → "2025-03-31"
    m = re.search(r"(\d{4})/(\d{2})/(\d{2})\)", title)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def download_and_parse(code_4: str, period: str = "1year"):
    """
    CSVダウンロード + 既存パーサーで財務データ抽出。

    Returns:
        dict: {"yuho": {financial_data}, "hanki": {"current": {...}, "prior": {...}}, ...}
    """
    from edinet_client import extract_financial_data

    docs = search_and_download(code_4, period=period)
    parsed = {}

    for doc in docs:
        doc_type = doc["doc_type"]
        zip_bytes = doc["zip_path"].read_bytes()

        if doc_type == "yuho":
            data = extract_financial_data(zip_bytes)
            parsed["yuho"] = data
            # Save parsed
            out = CACHE_BASE / code_4 / "yuho_parsed.json"
            out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            print(f"  Parsed yuho: revenue={data.get('revenue')}, cash={data.get('cash')}")

        elif doc_type == "hanki":
            data = extract_financial_data(zip_bytes, include_prior=True)
            parsed["hanki"] = data
            out = CACHE_BASE / code_4 / "hanki_parsed.json"
            out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            if isinstance(data, dict) and "current" in data:
                print(f"  Parsed hanki: current revenue={data['current'].get('revenue')}")
            else:
                print(f"  Parsed hanki: revenue={data.get('revenue')}")

        elif doc_type.startswith("quarterly"):
            data = extract_financial_data(zip_bytes, include_prior=True)
            parsed[doc_type] = data
            out = CACHE_BASE / code_4 / f"{doc_type}_parsed.json"
            out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            print(f"  Parsed {doc_type}")

    return parsed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="EDINET Playwright Scraper")
    parser.add_argument("code", help="4桁証券コード")
    parser.add_argument("--period", default="1year", choices=list(PERIOD_MAP.keys()),
                        help="検索期間（デフォルト: 1year）")
    parser.add_argument("--raw", action="store_true", help="DLのみ（パースしない）")
    args = parser.parse_args()

    if args.raw:
        docs = search_and_download(args.code, period=args.period)
        print(f"\n{len(docs)}件ダウンロード完了")
    else:
        parsed = download_and_parse(args.code, period=args.period)
        print(f"\n完了: {list(parsed.keys())}")


if __name__ == "__main__":
    main()
