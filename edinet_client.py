"""
EDINET API Client - type=5 CSV (XBRL pre-processed TSV) downloader & parser.

有価証券報告書・半期報告書の財務データを EDINET API type=5 CSV 形式で取得し、
pandas DataFrame に読み込む。
"""

import io
import os
import re
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import urllib3

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"

DOC_TYPE_MAP = {
    "120": "有価証券報告書",
    "130": "訂正有価証券報告書",
    "160": "半期報告書",
    "170": "訂正半期報告書",
}

KEY_FILE = Path.home() / ".edinet_key"


def to_sec_code(code: str) -> str:
    """4桁証券コード → 5桁（末尾0）変換。"""
    code = code.strip()
    return code + "0" if len(code) == 4 else code


def load_api_key() -> str:
    """~/.edinet_key からキーを読み込む。無ければ環境変数を参照。"""
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = os.environ.get("EDINET_API_KEY", "")
    if key:
        return key
    raise RuntimeError(
        "EDINET API Key が未設定です。\n"
        "~/.edinet_key にキーを保存するか、環境変数 EDINET_API_KEY を設定してください。\n"
        "取得先: https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1"
    )


# ---------------------------------------------------------------------------
# HTTP Session
# ---------------------------------------------------------------------------

def make_session(verify_ssl: bool = False) -> requests.Session:
    session = requests.Session()
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        session.verify = False
    return session


def request_with_retry(session, url, params=None, max_retries=3, stream=False):
    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, stream=stream, timeout=60)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 5))
                time.sleep(wait)
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
# EDINET Document Search
# ---------------------------------------------------------------------------

def fetch_doc_list(session, api_key, date_str):
    url = f"{EDINET_API_BASE}/documents.json"
    params = {"date": date_str, "type": 2, "Subscription-Key": api_key}
    resp = request_with_retry(session, url, params=params)
    data = resp.json()
    if data.get("metadata", {}).get("status") != "200":
        return []
    return data.get("results", [])


def search_documents(session, api_key, sec_code_5, days=400, doc_types=None,
                     progress_callback=None):
    """
    指定証券コード（5桁）の有報・半期報告書を EDINET から検索。
    doc_types: 取得する docTypeCode のリスト（デフォルト: 有報+半期報）
    Returns: list of EDINET document metadata dicts
    """
    if doc_types is None:
        doc_types = {"120", "130", "160", "170"}

    found = []
    end_date = datetime.today()
    start_date = end_date - timedelta(days=days)
    total_days = (end_date - start_date).days + 1

    current = start_date
    processed = 0
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        processed += 1
        if progress_callback and processed % 10 == 0:
            progress_callback(processed / total_days)

        docs = fetch_doc_list(session, api_key, date_str)
        for doc in docs:
            sc = doc.get("secCode") or ""
            dt = doc.get("docTypeCode") or ""
            if sc == sec_code_5 and dt in doc_types:
                found.append(doc)

        current += timedelta(days=1)
        time.sleep(1)

    return found


def classify_documents(docs):
    """
    検出した書類を分類して最新のものを返す。
    Returns: dict with keys 'yuho' (有報), 'hanki' (半期報告書)
    """
    yuho = []
    hanki = []

    for doc in docs:
        dt = doc.get("docTypeCode", "")
        if dt in ("120", "130"):
            yuho.append(doc)
        elif dt in ("160", "170"):
            hanki.append(doc)

    # 期末日で降順ソート → 最新を取得
    def sort_key(d):
        return d.get("periodEnd", "")

    yuho.sort(key=sort_key, reverse=True)
    hanki.sort(key=sort_key, reverse=True)

    result = {}
    if yuho:
        result["yuho"] = yuho[0]
    if hanki:
        result["hanki"] = hanki[0]
    return result


# ---------------------------------------------------------------------------
# type=5 CSV Download & Parse
# ---------------------------------------------------------------------------

def download_csv_zip(session, api_key, doc_id):
    """EDINET API type=5 (CSV ZIP) をダウンロードしてバイトで返す。"""
    url = f"{EDINET_API_BASE}/documents/{doc_id}"
    params = {"type": 5, "Subscription-Key": api_key}
    resp = request_with_retry(session, url, params=params, stream=True)
    return resp.content


def parse_csv_from_zip(zip_bytes):
    """
    type=5 ZIP から CSV/TSV ファイルを読み込んで DataFrame のリストで返す。
    EDINET type=5 は UTF-16 タブ区切り TSV。
    """
    dfs = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            csv_files = [n for n in zf.namelist()
                         if n.lower().endswith(('.csv', '.tsv'))]
            if not csv_files:
                # CSVが無い場合はXBRL_TO_CSV配下を探す
                csv_files = [n for n in zf.namelist()
                             if 'XBRL_TO_CSV' in n and not n.endswith('/')]

            for fname in csv_files:
                raw = zf.read(fname)
                # UTF-16 (BOM付き) or UTF-8 を試行
                for enc in ['utf-16', 'utf-8', 'cp932']:
                    try:
                        text = raw.decode(enc)
                        df = pd.read_csv(io.StringIO(text), sep='\t',
                                         on_bad_lines='skip')
                        if len(df.columns) > 1:
                            dfs.append((fname, df))
                            break
                    except (UnicodeDecodeError, pd.errors.ParserError):
                        continue
    except zipfile.BadZipFile:
        pass
    return dfs


def extract_financial_data(dfs, doc_type="yuho"):
    """
    type=5 CSVのDataFrameリストから財務数値を抽出。

    EDINET type=5 CSV の主要カラム:
    - 要素ID / 項目名 / コンテキストID / 値
    コンテキストIDで当期/前期を判別。

    Returns: dict of financial values
    """
    result = {}

    # 全DataFrameを結合
    all_data = []
    for fname, df in dfs:
        all_data.append(df)

    if not all_data:
        return result

    combined = pd.concat(all_data, ignore_index=True)

    # カラム名を正規化（空白除去）
    combined.columns = [str(c).strip() for c in combined.columns]

    # type=5 CSVの典型的なカラム名パターンを検出
    # 「要素ID」「項目名」「コンテキストID」「値」等
    label_col = None
    value_col = None
    context_col = None

    for col in combined.columns:
        col_lower = col.lower()
        if '項目' in col or 'label' in col_lower or '要素' in col:
            if label_col is None:
                label_col = col
        if '値' in col or 'value' in col_lower:
            if value_col is None:
                value_col = col
        if 'コンテキスト' in col or 'context' in col_lower:
            if context_col is None:
                context_col = col

    if label_col is None or value_col is None:
        # フォールバック: 列数の多いDFの最初と最後のカラムを使う
        return result

    # 当期コンテキストのフィルタ
    # EDINET type=5 の contextRef: "CurrentYearDuration" = 当期通期, etc.
    current_contexts = [
        'CurrentYearDuration',
        'CurrentYearInstant',
        'CurrentYTDDuration',  # 半期累計
        'FilingDateInstant',   # BS日付時点
    ]

    # 財務項目マッピング（日本語ラベル → 内部キー）
    item_patterns = {
        '売上高': 'revenue',
        '売上収益': 'revenue',
        '営業収益': 'revenue',
        '営業利益': 'operating_income',
        '経常利益': 'ordinary_income',
        '親会社株主に帰属する当期純利益': 'net_income',
        '当期純利益': 'net_income',
        '親会社株主に帰属する四半期純利益': 'net_income',
        '減価償却費': 'depreciation',
        '減価償却費及び償却費': 'depreciation',
        '現金及び預金': 'cash',
        '現金及び現金同等物': 'cash',
        '短期借入金': 'short_term_debt',
        '長期借入金': 'long_term_debt',
        '社債': 'bonds',
        '１年内返済予定の長期借入金': 'current_long_term_debt',
        '１年内償還予定の社債': 'current_bonds',
        'リース債務': 'lease_debt',
        '純資産合計': 'net_assets',
        '株主資本合計': 'equity',
        '自己資本比率': 'equity_ratio',
        '発行済株式総数': 'shares_outstanding',
        '自己株式数': 'treasury_shares',
        '１株当たり配当額': 'dps',
        '投資有価証券': 'investment_securities',
    }

    for _, row in combined.iterrows():
        label = str(row.get(label_col, ''))
        value = row.get(value_col, None)
        ctx = str(row.get(context_col, '')) if context_col else 'CurrentYearDuration'

        # 当期データのみ
        is_current = any(c in ctx for c in current_contexts)
        if not is_current:
            continue

        for pattern, key in item_patterns.items():
            if pattern in label:
                try:
                    val = pd.to_numeric(value, errors='coerce')
                    if pd.notna(val):
                        # 既に値がある場合は上書きしない（最初の一致を優先）
                        if key not in result:
                            result[key] = float(val)
                except (ValueError, TypeError):
                    pass
                break

    return result


# ---------------------------------------------------------------------------
# High-level API: 1社分の財務データ取得
# ---------------------------------------------------------------------------

def fetch_company_financials(code_4, days=400, progress_callback=None):
    """
    証券コード（4桁）から EDINET type=5 CSV 経由で財務データを取得。

    Returns: dict with keys:
        'yuho_data': 有報から抽出した数値 dict
        'hanki_data': 半期報告書から抽出した数値 dict
        'yuho_doc': 有報メタデータ
        'hanki_doc': 半期報告書メタデータ
        'company_name': 企業名
    """
    api_key = load_api_key()
    session = make_session(verify_ssl=False)
    sec_code = to_sec_code(code_4)

    # 書類検索
    docs = search_documents(session, api_key, sec_code, days=days,
                            progress_callback=progress_callback)

    if not docs:
        raise ValueError(f"証券コード {code_4} の書類が EDINET で見つかりません。")

    classified = classify_documents(docs)

    company_name = ""
    for doc in docs:
        name = doc.get("filerName", "")
        if name:
            company_name = name.replace("株式会社", "").strip()
            break

    result = {
        'company_name': company_name,
        'yuho_data': {},
        'hanki_data': {},
        'yuho_doc': classified.get('yuho'),
        'hanki_doc': classified.get('hanki'),
    }

    # 有報 type=5 CSV 取得
    if 'yuho' in classified:
        doc = classified['yuho']
        doc_id = doc.get('docID', '')
        if doc_id:
            zip_bytes = download_csv_zip(session, api_key, doc_id)
            time.sleep(1)
            dfs = parse_csv_from_zip(zip_bytes)
            result['yuho_data'] = extract_financial_data(dfs, doc_type='yuho')

    # 半期報告書 type=5 CSV 取得
    if 'hanki' in classified:
        doc = classified['hanki']
        doc_id = doc.get('docID', '')
        if doc_id:
            zip_bytes = download_csv_zip(session, api_key, doc_id)
            time.sleep(1)
            dfs = parse_csv_from_zip(zip_bytes)
            result['hanki_data'] = extract_financial_data(dfs, doc_type='hanki')

    return result
