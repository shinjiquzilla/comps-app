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
import json
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


# ---------------------------------------------------------------------------
# Local Cache
# ---------------------------------------------------------------------------

CACHE_BASE = Path(__file__).parent / "data" / "edinet"


def get_cache_dir(code_4):
    """data/edinet/{code_4}/ のパスを返す。なければ作成。"""
    d = CACHE_BASE / str(code_4)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _doc_filename(doc, ext):
    """書類の命名: {yuho|hanki}_{periodEnd}_{docID}.{ext}"""
    dt = doc.get("docTypeCode", "")
    prefix = "yuho" if dt in ("120", "130") else "hanki"
    period = (doc.get("periodEnd") or "unknown").replace("/", "-")
    doc_id = doc.get("docID", "unknown")
    return f"{prefix}_{period}_{doc_id}.{ext}"


def load_cached_meta(code_4):
    """meta.json を読み込む。なければ None。"""
    meta_path = CACHE_BASE / str(code_4) / "meta.json"
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_meta(code_4, docs, search_days, company_name=""):
    """meta.json に書類検索結果を保存。"""
    d = get_cache_dir(code_4)
    meta = {
        "last_searched": datetime.today().strftime("%Y-%m-%d"),
        "search_days": search_days,
        "company_name": company_name,
        "docs": [
            {
                "docID": doc.get("docID"),
                "docTypeCode": doc.get("docTypeCode"),
                "periodEnd": doc.get("periodEnd"),
                "filerName": doc.get("filerName"),
                "secCode": doc.get("secCode"),
            }
            for doc in docs
        ],
    }
    with open(d / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def clear_cache(code_4=None):
    """キャッシュ削除（EDINET＋株価）。code_4 指定で1社、None で全社。"""
    import shutil
    stock_base = CACHE_BASE.parent / "stock"
    if code_4:
        target = CACHE_BASE / str(code_4)
        if target.exists():
            shutil.rmtree(target)
        stock_target = stock_base / str(code_4)
        if stock_target.exists():
            shutil.rmtree(stock_target)
    else:
        if CACHE_BASE.exists():
            shutil.rmtree(CACHE_BASE)
        if stock_base.exists():
            shutil.rmtree(stock_base)


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


def search_documents(session, api_key, sec_code_5, days=90, doc_types=None,
                     progress_callback=None):
    """1社分の書類検索（後方互換用）。"""
    results = search_documents_batch(
        session, api_key, [sec_code_5], days=days,
        doc_types=doc_types, progress_callback=progress_callback
    )
    return results.get(sec_code_5, [])


def search_documents_batch(session, api_key, sec_codes_5, days=90, doc_types=None,
                           progress_callback=None):
    """
    複数社の書類を一括検索。日付ループは1回だけ。
    Returns: dict {sec_code_5: [doc, ...]}
    """
    if doc_types is None:
        doc_types = {"120", "130", "160", "170"}

    sec_set = set(sec_codes_5)
    found = {sc: [] for sc in sec_codes_5}

    end_date = datetime.today()
    start_date = end_date - timedelta(days=days)
    total_days = (end_date - start_date).days + 1

    current = start_date
    processed = 0
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        processed += 1
        if progress_callback:
            progress_callback(processed, total_days)

        docs = fetch_doc_list(session, api_key, date_str)
        for doc in docs:
            sc = doc.get("secCode") or ""
            dt = doc.get("docTypeCode") or ""
            if sc in sec_set and dt in doc_types:
                found[sc].append(doc)

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


def download_pdf(session, api_key, doc_id):
    """EDINET API type=2 (PDF) をダウンロードしてバイトで返す。"""
    url = f"{EDINET_API_BASE}/documents/{doc_id}"
    params = {"type": 2, "Subscription-Key": api_key}
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


def extract_financial_data(zip_bytes, include_prior=False):
    """
    type=5 CSV ZIP から財務数値を抽出。

    CSVカラム: "要素ID" "項目名" "コンテキストID" "相対年度" "連結・個別" "期間・時点" "ユニットID" "単位" "値"
    インデックス:   0       1         2              3          4            5           6        7      8

    Parameters:
        zip_bytes: type=5 CSV ZIP のバイト列
        include_prior: True の場合、前期データも別dictで返す

    Returns:
        include_prior=False: dict（従来通り当期データのみ）
        include_prior=True:  {'current': dict, 'prior': dict}
    """
    lines = parse_csv_lines(zip_bytes)
    if not lines:
        return {} if not include_prior else {'current': {}, 'prior': {}}

    result = {}
    prior_result = {}

    # 当期（有報: 当期/当期末、半期報: 当中間期/当中間期末）
    current_periods = ('当期', '当期末', '当中間期', '当中間期末')
    # 前期（有報: 前期/前期末、半期報: 前中間期/前中間期末）
    prior_periods = ('前期', '前期末', '前中間期', '前中間期末')

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

        # 当期 or 前期を判定
        is_current = relative_year in current_periods
        is_prior = include_prior and relative_year in prior_periods
        if not is_current and not is_prior:
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

        # 格納先を選択
        target = result if is_current else prior_result

        # 既に値がある場合はスキップ（最初の一致を優先）
        if key in target:
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
                target[key] = val
            elif key == 'dps':
                # 1株あたり配当は円のまま
                target[key] = val
            else:
                target[key] = val / 1_000_000  # 百万円
        else:
            # 株数等
            if key in ('shares_issued', 'treasury_shares'):
                target[key] = val / 1_000  # 千株
            else:
                target[key] = val

    if include_prior:
        return {'current': result, 'prior': prior_result}
    return result


# ---------------------------------------------------------------------------
# High-level API: 1社分の財務データ取得
# ---------------------------------------------------------------------------

def fetch_company_financials(code_4, days=90, progress_callback=None, use_cache=True):
    """
    証券コード（4桁）から EDINET type=5 CSV 経由で財務データを取得。
    """
    api_key = load_api_key()
    session = make_session(verify_ssl=False)

    # キャッシュ確認
    if use_cache:
        meta = load_cached_meta(code_4)
        if meta and meta.get("docs") is not None:
            return _process_docs_for_company(
                session, api_key, code_4, meta["docs"], use_cache=True
            )

    sec_code = to_sec_code(code_4)
    docs = search_documents(session, api_key, sec_code, days=days,
                            progress_callback=progress_callback)

    result = _process_docs_for_company(session, api_key, code_4, docs, use_cache=use_cache)

    # meta.json に保存
    if use_cache and docs:
        company_name = ""
        for doc in docs:
            name = doc.get("filerName", "")
            if name:
                company_name = name.replace("株式会社", "").strip()
                break
        save_meta(code_4, docs, days, company_name)

    return result


def fetch_companies_batch(codes_4, days=90, progress_callback=None, use_cache=True):
    """
    複数社の財務データを一括取得。EDINET日付検索は1回のみ。
    use_cache=True の場合、ローカルキャッシュを優先し、EDINET APIアクセスを最小化する。
    Returns: dict {code_4: result_dict}
    """
    api_key = load_api_key()
    session = make_session(verify_ssl=False)

    results = {}
    codes_to_search = []  # キャッシュにない企業

    # キャッシュ確認
    if use_cache:
        for code4 in codes_4:
            meta = load_cached_meta(code4)
            if meta and meta.get("docs") is not None:
                # キャッシュ済み: meta.json の docs を使って処理
                docs = meta["docs"]
                results[code4] = _process_docs_for_company(
                    session, api_key, code4, docs, use_cache=True
                )
            else:
                codes_to_search.append(code4)
    else:
        codes_to_search = list(codes_4)

    # キャッシュにない企業のみ EDINET 検索
    if codes_to_search:
        sec_codes = {to_sec_code(c): c for c in codes_to_search}

        all_docs = search_documents_batch(
            session, api_key, list(sec_codes.keys()), days=days,
            progress_callback=progress_callback
        )

        for sec5, code4 in sec_codes.items():
            docs = all_docs.get(sec5, [])
            results[code4] = _process_docs_for_company(
                session, api_key, code4, docs, use_cache=use_cache
            )
            # meta.json に保存
            if use_cache and docs:
                company_name = ""
                for doc in docs:
                    name = doc.get("filerName", "")
                    if name:
                        company_name = name.replace("株式会社", "").strip()
                        break
                save_meta(code4, docs, days, company_name)

    return results


def _process_docs_for_company(session, api_key, code_4, docs, use_cache=True):
    """1社分の書類リストからCSVダウンロード・パースを行う。キャッシュ対応。"""
    if not docs:
        return {
            'company_name': '',
            'yuho_data': {},
            'hanki_data': {},
            'yuho_doc': None,
            'hanki_doc': None,
            '_debug': {},
        }

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
        'hanki_prior_data': {},
        'yuho_doc': classified.get('yuho'),
        'hanki_doc': classified.get('hanki'),
    }

    cache_dir = get_cache_dir(code_4) if use_cache else None
    debug_info = {}

    for doc_type in ('yuho', 'hanki'):
        if doc_type not in classified:
            continue
        doc = classified[doc_type]
        doc_id = doc.get('docID', '')
        if not doc_id:
            continue

        zip_filename = _doc_filename(doc, "zip")
        pdf_filename = _doc_filename(doc, "pdf")

        # --- パース結果キャッシュを最優先チェック（ZIP読み込み不要） ---
        parsed_cache_file = cache_dir / f"{doc_type}_parsed.json" if cache_dir else None
        parsed_from_cache = False

        if parsed_cache_file and parsed_cache_file.exists():
            try:
                import json
                cached_parsed = json.loads(parsed_cache_file.read_text(encoding='utf-8'))
                if doc_type == 'hanki':
                    result['hanki_data'] = cached_parsed.get('current', {})
                    result['hanki_prior_data'] = cached_parsed.get('prior', {})
                    debug_info['hanki_prior_keys'] = list(result['hanki_prior_data'].keys())
                else:
                    result[f'{doc_type}_data'] = cached_parsed
                parsed_from_cache = True
                debug_info[f'{doc_type}_parsed_cache'] = True
                debug_info[f'{doc_type}_cache_hit'] = True
            except Exception:
                pass

        if parsed_from_cache:
            continue  # ZIP/PDFの読み込み・ダウンロードを完全スキップ

        # --- CSV ZIP（パース結果キャッシュがない場合のみ） ---
        zip_bytes = None
        cache_hit = False

        if cache_dir:
            zip_path = cache_dir / zip_filename
            if zip_path.exists():
                zip_bytes = zip_path.read_bytes()
                cache_hit = True

        if zip_bytes is None:
            zip_bytes = download_csv_zip(session, api_key, doc_id)
            time.sleep(1)
            # キャッシュに保存
            if cache_dir and zip_bytes:
                (cache_dir / zip_filename).write_bytes(zip_bytes)

        debug_info[f'{doc_type}_zip_size'] = len(zip_bytes)
        debug_info[f'{doc_type}_cache_hit'] = cache_hit

        if not parsed_from_cache:
            lines = parse_csv_lines(zip_bytes)
            debug_info[f'{doc_type}_csv_lines'] = len(lines)

            # 半期報告書の場合: 前期H1データも抽出
            if doc_type == 'hanki':
                parsed = extract_financial_data(zip_bytes, include_prior=True)
                result['hanki_data'] = parsed['current']
                result['hanki_prior_data'] = parsed['prior']
                debug_info['hanki_prior_keys'] = list(parsed['prior'].keys())
                # パース結果をキャッシュ保存
                if parsed_cache_file:
                    try:
                        import json
                        parsed_cache_file.write_text(
                            json.dumps({'current': parsed['current'], 'prior': parsed['prior']},
                                       ensure_ascii=False), encoding='utf-8')
                    except Exception:
                        pass
            else:
                parsed_data = extract_financial_data(zip_bytes)
                result[f'{doc_type}_data'] = parsed_data
                if parsed_cache_file:
                    try:
                        import json
                        parsed_cache_file.write_text(
                            json.dumps(parsed_data, ensure_ascii=False), encoding='utf-8')
                    except Exception:
                        pass

        # --- PDF ---
        if cache_dir:
            pdf_path = cache_dir / pdf_filename
            if not pdf_path.exists():
                try:
                    pdf_bytes = download_pdf(session, api_key, doc_id)
                    time.sleep(1)
                    if pdf_bytes:
                        pdf_path.write_bytes(pdf_bytes)
                except Exception:
                    pass  # PDF取得失敗は非致命的

    result['_debug'] = debug_info
    return result
