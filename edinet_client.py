"""
EDINET API Client - type=5 CSV (XBRL pre-processed TSV) downloader & parser.

有価証券報告書・半期報告書の財務データを EDINET API type=5 CSV 形式で取得し、
要素IDベースで財務数値を抽出する。

EDINET type=5 CSV 仕様:
- エンコーディング: UTF-16 (BOM付き)
- 区切り: タブ
- カラム: "要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "単位", "値"
- 相対年度: "当期", "当期末", "前期", "前期末" 等
- 値: 円単位（百万円ではない）
"""

import io
import os
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

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

# 要素ID → 内部キー マッピング（XBRL taxonomy 標準名）
# 有報(asr) / 半期報(ssr) 共通
ELEMENT_MAP = {
    # === J-GAAP ===
    # P&L
    'jppfs_cor:NetSales': 'revenue',
    'jppfs_cor:Revenue': 'revenue',
    'jppfs_cor:OperatingIncome': 'operating_income',
    'jppfs_cor:OrdinaryIncome': 'ordinary_income',
    'jppfs_cor:ProfitLossAttributableToOwnersOfParent': 'net_income',
    'jppfs_cor:ProfitLoss': 'profit_loss',
    # CF statement D&A
    'jppfs_cor:DepreciationAndAmortizationOpeCF': 'depreciation',
    # BS
    'jppfs_cor:CashAndDeposits': 'cash',
    'jppfs_cor:CashAndCashEquivalents': 'cash',
    'jppfs_cor:InvestmentSecurities': 'investment_securities',
    'jppfs_cor:ShortTermLoansPayable': 'short_term_debt',
    'jppfs_cor:ShortTermBorrowings': 'short_term_debt',
    'jppfs_cor:LongTermLoansPayable': 'long_term_debt',
    'jppfs_cor:LongTermDebt': 'long_term_debt',
    'jppfs_cor:BondsPayable': 'bonds',
    'jppfs_cor:CurrentPortionOfLongTermLoansPayable': 'current_long_term_debt',
    'jppfs_cor:CurrentPortionOfBondsPayable': 'current_bonds',
    'jppfs_cor:LeaseObligationsCL': 'lease_debt_current',
    'jppfs_cor:LeaseObligationsNCL': 'lease_debt_noncurrent',
    'jppfs_cor:NetAssets': 'net_assets',
    'jppfs_cor:ShareholdersEquity': 'shareholders_equity',
    'jppfs_cor:EquityAttributableToOwnersOfParent': 'equity_parent',

    # === IFRS (jpigp_cor: prefix) ===
    # P&L
    'jpigp_cor:NetSalesIFRS': 'revenue',
    'jpigp_cor:RevenueIFRS': 'revenue',
    'jpigp_cor:OperatingIncomeIFRS': 'operating_income',
    'jpigp_cor:OperatingProfitIFRS': 'operating_income',
    'jpigp_cor:ProfitLossAttributableToOwnersOfParentIFRS': 'net_income',
    'jpigp_cor:ProfitLossIFRS': 'profit_loss',
    # CF statement D&A
    'jpigp_cor:DepreciationAndAmortizationOpeCFIFRS': 'depreciation',
    # BS
    'jpigp_cor:CashAndCashEquivalentsIFRS': 'cash',
    'jpigp_cor:InvestmentSecuritiesIFRS': 'investment_securities',
    'jpigp_cor:ShortTermBorrowingsIFRS': 'short_term_debt',
    'jpigp_cor:ShortTermLoansPayableIFRS': 'short_term_debt',
    'jpigp_cor:LongTermLoansPayableIFRS': 'long_term_debt',
    'jpigp_cor:LongTermDebtIFRS': 'long_term_debt',
    'jpigp_cor:BondsPayableIFRS': 'bonds',
    'jpigp_cor:CurrentPortionOfLongTermLoansPayableIFRS': 'current_long_term_debt',
    'jpigp_cor:LeaseLiabilitiesCLIFRS': 'lease_debt_current',
    'jpigp_cor:LeaseLiabilitiesNCLIFRS': 'lease_debt_noncurrent',
    'jpigp_cor:NetAssetsIFRS': 'net_assets',
    'jpigp_cor:EquityAttributableToOwnersOfParentIFRS': 'equity_parent',
    'jpigp_cor:ShareholdersEquityIFRS': 'shareholders_equity',
}

# 経営指標等セクションのフォールバック用（項目名ベース）
SUMMARY_ELEMENT_MAP = {
    # J-GAAP
    'jpcrp_cor:NetSalesSummaryOfBusinessResults': 'revenue',
    'jpcrp_cor:OperatingIncomeLossSummaryOfBusinessResults': 'operating_income',
    'jpcrp_cor:OrdinaryIncomeLossSummaryOfBusinessResults': 'ordinary_income',
    'jpcrp_cor:ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults': 'net_income',
    'jpcrp_cor:EquityToAssetRatioSummaryOfBusinessResults': 'equity_ratio',
    'jpcrp_cor:NumberOfIssuedSharesEndOfTermIncludingTreasurySharesSummaryOfBusinessResults': 'shares_issued',
    'jpcrp_cor:NumberOfTreasurySharesEndOfTermSummaryOfBusinessResults': 'treasury_shares',
    'jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults': 'dps',
    # IFRS
    'jpcrp_cor:RevenueIFRSSummaryOfBusinessResults': 'revenue',
    'jpcrp_cor:NetSalesIFRSSummaryOfBusinessResults': 'revenue',
    'jpcrp_cor:OperatingProfitLossIFRSSummaryOfBusinessResults': 'operating_income',
    'jpcrp_cor:ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults': 'net_income',
    'jpcrp_cor:ProfitLossIFRSSummaryOfBusinessResults': 'profit_loss',
    'jpcrp_cor:EquityToAssetRatioIFRSSummaryOfBusinessResults': 'equity_ratio',
    'jpcrp_cor:DividendPaidPerShareIFRSSummaryOfBusinessResults': 'dps',
}


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
    yuho = []
    hanki = []

    for doc in docs:
        dt = doc.get("docTypeCode", "")
        if dt in ("120", "130"):
            yuho.append(doc)
        elif dt in ("160", "170"):
            hanki.append(doc)

    def sort_key(d):
        return d.get("periodEnd") or ""

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


def parse_csv_lines(zip_bytes):
    """
    type=5 ZIP からメインの財務CSVを読み込み、行リストで返す。

    ZIPには複数CSVが含まれるが、ファイル名が 'jpcrp030000' で始まるものが
    有報/半期報の本体財務データ。最大サイズのCSVをフォールバックとして使用。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            csv_files = [n for n in zf.namelist()
                         if n.endswith('.csv') and 'XBRL_TO_CSV' in n]

            if not csv_files:
                return []

            # jpcrp030000 (有報/半期報本体) を優先、なければ最大ファイル
            target = None
            for f in csv_files:
                if 'jpcrp030000' in f or 'jpcrp050000' in f:
                    target = f
                    break
            if target is None:
                target = max(csv_files, key=lambda n: zf.getinfo(n).file_size)

            raw = zf.read(target)

            # UTF-16 (BOM付き) でデコード
            for enc in ['utf-16', 'utf-8-sig', 'utf-8', 'cp932']:
                try:
                    text = raw.decode(enc)
                    lines = text.strip().split('\n')
                    if len(lines) > 1:
                        return lines
                except (UnicodeDecodeError, UnicodeError):
                    continue

    except zipfile.BadZipFile:
        pass
    return []


def extract_financial_data(zip_bytes):
    """
    type=5 CSV ZIP から財務数値を抽出。

    CSVカラム: "要素ID" "項目名" "コンテキストID" "相対年度" "連結・個別" "期間・時点" "ユニットID" "単位" "値"
    インデックス:   0       1         2              3          4            5           6        7      8

    Returns: dict of financial values (百万円単位に変換済み)
    """
    lines = parse_csv_lines(zip_bytes)
    if not lines:
        return {}

    result = {}

    for line in lines[1:]:  # ヘッダースキップ
        parts = line.split('\t')
        if len(parts) < 9:
            continue

        # ダブルクォートとCR/LFを除去
        def clean(s):
            return s.strip().strip('"').strip()

        element_id = clean(parts[0])
        # label = clean(parts[1])
        # context_id = clean(parts[2])
        relative_year = clean(parts[3])
        consolidated = clean(parts[4])
        # period_type = clean(parts[5])
        # unit_id = clean(parts[6])
        unit = clean(parts[7])
        value_str = clean(parts[8])

        # 当期（有報: 当期/当期末、半期報: 当中間期/当中間期末）
        current_periods = ('当期', '当期末', '当中間期', '当中間期末')
        if relative_year not in current_periods:
            continue
        # 連結 or その他（IFRS企業は「その他」）を受け入れ。個別は除外。
        if consolidated not in ('連結', 'その他'):
            continue

        # 要素IDでマッチング（完全一致 + IFRS接尾辞付きの動的マッチ）
        key = ELEMENT_MAP.get(element_id) or SUMMARY_ELEMENT_MAP.get(element_id)
        # 半期報告書で会社固有プレフィックス（例: jpcrp040300-ssr_E01807-000:〜IFRS）の場合
        # コロン以降の要素名部分で再マッチ
        if key is None and ':' in element_id:
            local_name = element_id.split(':', 1)[1]
            for map_id, map_key in ELEMENT_MAP.items():
                if map_id.split(':', 1)[1] == local_name:
                    key = map_key
                    break
            if key is None:
                for map_id, map_key in SUMMARY_ELEMENT_MAP.items():
                    if map_id.split(':', 1)[1] == local_name:
                        key = map_key
                        break
        if key is None:
            continue

        # 既に値がある場合はスキップ（最初の一致を優先）
        if key in result:
            continue

        # 値のパース
        if value_str in ('', '－', '-', '―'):
            continue

        try:
            val = float(value_str.replace(',', ''))
        except ValueError:
            continue

        # 円単位 → 百万円単位に変換（比率系は除く）
        if unit == '円' or unit == 'JPY':
            if key == 'equity_ratio':
                # %表記の場合（0.829 等）→ そのまま
                result[key] = val
            elif key == 'dps':
                # 1株あたり配当は円のまま
                result[key] = val
            else:
                result[key] = val / 1_000_000  # 百万円
        else:
            # 株数等
            if key in ('shares_issued', 'treasury_shares'):
                result[key] = val / 1_000  # 千株
            else:
                result[key] = val

    return result


# ---------------------------------------------------------------------------
# High-level API: 1社分の財務データ取得
# ---------------------------------------------------------------------------

def fetch_company_financials(code_4, days=400, progress_callback=None):
    """
    証券コード（4桁）から EDINET type=5 CSV 経由で財務データを取得。
    """
    api_key = load_api_key()
    session = make_session(verify_ssl=False)
    sec_code = to_sec_code(code_4)

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

    # 有報 type=5 CSV
    debug_info = {}
    if 'yuho' in classified:
        doc_id = classified['yuho'].get('docID', '')
        if doc_id:
            zip_bytes = download_csv_zip(session, api_key, doc_id)
            time.sleep(1)
            lines = parse_csv_lines(zip_bytes)
            debug_info['yuho_zip_size'] = len(zip_bytes)
            debug_info['yuho_csv_lines'] = len(lines)
            result['yuho_data'] = extract_financial_data(zip_bytes)

    # 半期報告書 type=5 CSV
    if 'hanki' in classified:
        doc_id = classified['hanki'].get('docID', '')
        if doc_id:
            zip_bytes = download_csv_zip(session, api_key, doc_id)
            time.sleep(1)
            lines = parse_csv_lines(zip_bytes)
            debug_info['hanki_zip_size'] = len(zip_bytes)
            debug_info['hanki_csv_lines'] = len(lines)
            result['hanki_data'] = extract_financial_data(zip_bytes)

    result['_debug'] = debug_info
    return result
