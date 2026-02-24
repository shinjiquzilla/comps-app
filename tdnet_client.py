"""
TDnet Client - 決算短信PDFの取得と業績予想の抽出。

TDnetの適時開示ページをスクレイピングして決算短信PDFを取得し、
PyMuPDF でテキスト抽出 → 正規表現で業績予想を解析する。
"""

import io
import re
import time
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TDNET_BASE_URL = "https://www.release.tdnet.info/inbs"


# ---------------------------------------------------------------------------
# HTTP Session
# ---------------------------------------------------------------------------

def make_session(verify_ssl=False):
    session = requests.Session()
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        session.verify = False
    return session


def request_with_retry(session, url, max_retries=3, stream=False):
    for attempt in range(max_retries):
        try:
            resp = session.get(url, stream=stream, timeout=60)
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", 5)))
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
    raise RuntimeError(f"リトライ上限到達: {url}")


# ---------------------------------------------------------------------------
# TDnet Search
# ---------------------------------------------------------------------------

def search_tanshin(session, code_4, days=400, progress_callback=None):
    """
    TDnet適時開示ページから指定証券コードの決算短信を検索。
    Returns: list of dict with keys: code, title, pdf_url, date, period_type, fy_label
    """
    found = []
    end_date = datetime.today()
    start_date = end_date - timedelta(days=days)
    total_days = (end_date - start_date).days + 1

    current = start_date
    processed = 0
    while current <= end_date:
        date_str = current.strftime("%Y%m%d")
        processed += 1
        if progress_callback and processed % 10 == 0:
            progress_callback(processed / total_days)

        url = f"{TDNET_BASE_URL}/I_list_001_{date_str}.html"
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 404:
                current += timedelta(days=1)
                time.sleep(0.5)
                continue
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            items = _parse_tdnet_page(resp.text, code_4, current)
            found.extend(items)
        except requests.exceptions.RequestException:
            pass

        current += timedelta(days=1)
        time.sleep(0.5)

    return found


def _parse_tdnet_page(html, target_code, date):
    results = []
    soup = BeautifulSoup(html, "html.parser")

    for row in soup.select("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        row_text = row.get_text()
        if target_code not in row_text:
            continue
        if "決算短信" not in row_text:
            continue

        link = row.find("a", href=True)
        if not link:
            continue
        href = link["href"]
        if not href.lower().endswith(".pdf"):
            continue

        if href.startswith("/"):
            pdf_url = f"https://www.release.tdnet.info{href}"
        elif not href.startswith("http"):
            pdf_url = f"{TDNET_BASE_URL}/{href}"
        else:
            pdf_url = href

        title = link.get_text(strip=True)
        period_type = _detect_period(title, row_text)
        fy_label = _detect_fy(title, row_text)

        results.append({
            "code": target_code,
            "title": title,
            "pdf_url": pdf_url,
            "date": date.strftime("%Y-%m-%d"),
            "period_type": period_type,
            "fy_label": fy_label,
        })

    return results


def _detect_period(title, row_text):
    text = title + " " + row_text
    if "第1四半期" in text or "第１四半期" in text:
        return "Q1"
    elif "第2四半期" in text or "第２四半期" in text:
        return "Q2"
    elif "第3四半期" in text or "第３四半期" in text:
        return "Q3"
    elif "通期" in text or "年度" in text or ("決算短信" in text and "四半期" not in text):
        return "通期"
    return "通期"


def _detect_fy(title, row_text):
    text = title + " " + row_text
    m = re.search(r"(\d{4})年(\d{1,2})月期", text)
    if m:
        return f"FY{m.group(1)}{m.group(2).zfill(2)}"
    return "FY不明"


def classify_tanshin(items):
    """
    決算短信を分類して最新の通期・Q2を返す。
    Returns: dict with keys 'full_year' (通期), 'q2' (第2四半期)
    """
    full_year = [i for i in items if i["period_type"] == "通期"]
    q2 = [i for i in items if i["period_type"] == "Q2"]

    # 日付降順で最新を取得
    full_year.sort(key=lambda x: x["date"], reverse=True)
    q2.sort(key=lambda x: x["date"], reverse=True)

    result = {}
    if full_year:
        result["full_year"] = full_year[0]
    if q2:
        result["q2"] = q2[0]
    return result


# ---------------------------------------------------------------------------
# PDF Download & Text Extraction
# ---------------------------------------------------------------------------

def download_pdf(session, pdf_url):
    """PDFをダウンロードしてバイトで返す。"""
    resp = request_with_retry(session, pdf_url, stream=True)
    return resp.content


def extract_text_from_pdf(pdf_bytes):
    """PyMuPDFでPDFからテキスト抽出。"""
    if fitz is None:
        raise ImportError("PyMuPDF (fitz) がインストールされていません。pip install pymupdf")

    text = ""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            text += page.get_text()
    return text


# ---------------------------------------------------------------------------
# Forecast Extraction from Tanshin Text
# ---------------------------------------------------------------------------

def extract_forecast(text):
    """
    決算短信のテキストから業績予想（次期）を抽出。

    典型的な決算短信フォーマット:
    「次期の連結業績予想」テーブル:
      売上高    XX,XXX  〜  XX,XXX
      営業利益   X,XXX  〜   X,XXX
      ...

    Returns: dict with forecast values (revenue, op, ni, dps)
    """
    result = {}

    # --- 業績予想の抽出 ---
    # 「通期」「業績予想」の近辺を探す
    # パターン1: テーブル形式の業績予想
    # 「売上高  16,800  ...」のような行を探す

    # 全角数字→半角変換
    text = _normalize_numbers(text)

    # 業績予想セクションを探す
    forecast_section = _find_forecast_section(text)

    if forecast_section:
        # 売上高
        rev = _extract_amount(forecast_section, r'売上[高収益].*?([\d,]+)')
        if rev:
            result['rev_forecast'] = rev

        # 営業利益
        op = _extract_amount(forecast_section, r'営業利益.*?([\d,]+)')
        if op:
            result['op_forecast'] = op

        # 経常利益
        ordinary = _extract_amount(forecast_section, r'経常利益.*?([\d,]+)')
        if ordinary:
            result['ordinary_forecast'] = ordinary

        # 純利益
        ni = _extract_amount(forecast_section,
                             r'(?:親会社株主に帰属する)?(?:当期)?純利益.*?([\d,]+)')
        if ni:
            result['ni_forecast'] = ni

    # --- 配当予想の抽出 ---
    dps = _extract_dividend_forecast(text)
    if dps is not None:
        result['dps'] = dps

    return result


def _normalize_numbers(text):
    """全角数字・カンマを半角に変換。"""
    zen = '０１２３４５６７８９，'
    han = '0123456789,'
    for z, h in zip(zen, han):
        text = text.replace(z, h)
    return text


def _find_forecast_section(text):
    """業績予想セクションを抽出（通期予想を優先）。"""
    # 「通期の連結業績予想」「通期の業績予想」等のパターン
    patterns = [
        r'通期[のの]?(?:連結)?業績予想.{0,500}',
        r'次期[のの]?(?:連結)?業績予想.{0,500}',
        r'業績予想.*通期.{0,500}',
        # Q2短信の場合、通期予想テーブルが含まれる
        r'通期.*予想.*\n(?:.*\n){0,20}',
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            return m.group(0)

    # フォールバック: 「業績予想」を含む広範囲を返す
    m = re.search(r'業績予想.{0,1000}', text, re.DOTALL)
    if m:
        return m.group(0)

    return None


def _extract_amount(text, pattern):
    """正規表現パターンで金額（百万円）を抽出。"""
    m = re.search(pattern, text)
    if m:
        amount_str = m.group(1).replace(',', '')
        try:
            return int(amount_str)
        except ValueError:
            pass
    return None


def _extract_dividend_forecast(text):
    """配当予想（年間）を抽出。"""
    # 「年間配当金  XX円」パターン
    patterns = [
        r'年間[配当金額]*\s*(\d+(?:\.\d+)?)\s*円',
        r'合計\s*(\d+(?:\.\d+)?)\s*円',
        r'1株当たり配当金.*?合計.*?(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass

    # フォールバック: 中間+期末配当を合算
    interim = None
    yearend = None
    m = re.search(r'中間\s*(\d+(?:\.\d+)?)', text)
    if m:
        interim = float(m.group(1))
    m = re.search(r'期末\s*(\d+(?:\.\d+)?)', text)
    if m:
        yearend = float(m.group(1))
    if interim is not None and yearend is not None:
        return interim + yearend

    return None


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def fetch_tanshin_forecasts(code_4, days=400, progress_callback=None):
    """
    TDnetから決算短信を取得し、業績予想を抽出。

    Returns: dict with keys:
        'forecast': 業績予想 dict (rev_forecast, op_forecast, ni_forecast, dps)
        'tanshin_items': 検出した決算短信リスト
        'text': 抽出テキスト（デバッグ用）
    """
    session = make_session(verify_ssl=False)

    items = search_tanshin(session, code_4, days=days,
                           progress_callback=progress_callback)

    if not items:
        return {'forecast': {}, 'tanshin_items': [], 'text': ''}

    classified = classify_tanshin(items)

    # Q2短信を優先（通期予想が含まれる）、なければ通期短信
    target = classified.get('q2') or classified.get('full_year')

    if not target:
        return {'forecast': {}, 'tanshin_items': items, 'text': ''}

    pdf_bytes = download_pdf(session, target['pdf_url'])
    text = extract_text_from_pdf(pdf_bytes)
    forecast = extract_forecast(text)

    return {
        'forecast': forecast,
        'tanshin_items': items,
        'text': text,
        'target_tanshin': target,
    }
