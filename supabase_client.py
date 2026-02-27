"""
Supabase Client — Comps App データベースラッパー

既存モジュール (edinet_client, stock_fetcher, app.py) と同じdict形式で
データを読み書きする。ローカルキャッシュのフォールバックは呼び出し側で対応。
"""

import os
from datetime import date, datetime

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None
    Client = None

# ---------------------------------------------------------------------------
# Singleton Connection
# ---------------------------------------------------------------------------

_supabase_client = None


def get_supabase():
    """Supabaseクライアントをシングルトンで返す。接続不可ならNone。"""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    if create_client is None:
        return None

    url = None
    key = None

    # 1. Streamlit secrets
    try:
        import streamlit as st
        url = st.secrets["supabase"]["url"]
        key = st.secrets["supabase"]["anon_key"]
    except Exception:
        pass

    # 2. 環境変数フォールバック
    if not url:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_ANON_KEY")

    if not url or not key:
        return None

    try:
        _supabase_client = create_client(url, key)
        return _supabase_client
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

def upsert_company(code, name=None, name_en=None, sector=None,
                   accounting=None, fy_end=None):
    """companies テーブルに upsert。"""
    sb = get_supabase()
    if sb is None:
        return
    row = {"code": code, "updated_at": datetime.utcnow().isoformat()}
    if name is not None:
        row["name"] = name
    if name_en is not None:
        row["name_en"] = name_en
    if sector is not None:
        row["sector"] = sector
    if accounting is not None:
        row["accounting"] = accounting
    if fy_end is not None:
        row["fy_end"] = fy_end
    try:
        sb.table("companies").upsert(row, on_conflict="code").execute()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# EDINET Meta
# ---------------------------------------------------------------------------

def save_edinet_meta(code, doc_id, doc_type, period_end, filer_name=None,
                     last_searched=None, search_days=None, raw_meta=None):
    """edinet_meta テーブルに upsert (doc_id がユニーク)。"""
    sb = get_supabase()
    if sb is None:
        return
    row = {
        "code": code,
        "doc_id": doc_id,
        "doc_type": doc_type,
        "period_end": period_end,
    }
    if filer_name is not None:
        row["filer_name"] = filer_name
    if last_searched is not None:
        row["last_searched"] = last_searched
    if search_days is not None:
        row["search_days"] = search_days
    if raw_meta is not None:
        row["raw_meta"] = raw_meta
    try:
        sb.table("edinet_meta").upsert(row, on_conflict="doc_id").execute()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Financials
# ---------------------------------------------------------------------------

def save_financials(code, doc_type, period_end, data_dict):
    """
    financials テーブルに upsert。
    data_dict: edinet_client の extract_financial_data() が返す形式のdict。
    """
    sb = get_supabase()
    if sb is None:
        return

    # 既知カラムをマッピング
    known_cols = [
        'revenue', 'operating_income', 'ordinary_income', 'net_income',
        'depreciation', 'cash', 'investment_securities',
        'short_term_debt', 'long_term_debt', 'bonds',
        'current_long_term_debt', 'current_bonds',
        'lease_debt_current', 'lease_debt_noncurrent',
        'net_assets', 'shareholders_equity', 'equity_parent',
        'equity_ratio', 'dps', 'goodwill_amortization',
    ]
    row = {
        "code": code,
        "doc_type": doc_type,
        "period_end": period_end,
        "raw_data": data_dict,  # 全体をJSONBに保持
    }
    for col in known_cols:
        if col in data_dict:
            row[col] = data_dict[col]

    try:
        sb.table("financials").upsert(
            row, on_conflict="code,doc_type,period_end"
        ).execute()
    except Exception:
        pass


def load_financials(code, doc_type):
    """
    financials テーブルから最新の1行を読み込み、raw_data を返す。
    Returns: dict | None
    """
    sb = get_supabase()
    if sb is None:
        return None
    try:
        resp = (sb.table("financials")
                .select("*")
                .eq("code", code)
                .eq("doc_type", doc_type)
                .order("period_end", desc=True)
                .limit(1)
                .execute())
        if resp.data:
            row = resp.data[0]
            return row.get("raw_data") or row
        return None
    except Exception:
        return None


def load_edinet_data(code):
    """
    Supabase から edinet_client 互換の dict を構築。
    Returns: edinet_client.fetch_companies_batch() と同じ形式の dict | None
    """
    sb = get_supabase()
    if sb is None:
        return None

    try:
        # financials テーブルから yuho, hanki_current, hanki_prior を取得
        resp = (sb.table("financials")
                .select("*")
                .eq("code", code)
                .order("period_end", desc=True)
                .execute())
        if not resp.data:
            return None

        yuho_data = {}
        hanki_data = {}
        hanki_prior_data = {}

        for row in resp.data:
            dt = row.get("doc_type", "")
            rd = row.get("raw_data") or {}
            if dt == "yuho" and not yuho_data:
                yuho_data = rd
            elif dt == "hanki_current" and not hanki_data:
                hanki_data = rd
            elif dt == "hanki_prior" and not hanki_prior_data:
                hanki_prior_data = rd

        if not yuho_data and not hanki_data:
            return None

        # edinet_meta からドキュメント情報を取得
        meta_resp = (sb.table("edinet_meta")
                     .select("*")
                     .eq("code", code)
                     .order("period_end", desc=True)
                     .execute())
        yuho_doc = None
        hanki_doc = None
        company_name = ""
        for m in (meta_resp.data or []):
            dt = m.get("doc_type", "")
            if dt == "yuho" and yuho_doc is None:
                yuho_doc = {
                    "docID": m.get("doc_id"),
                    "docTypeCode": "120",
                    "periodEnd": str(m.get("period_end", "")),
                    "filerName": m.get("filer_name", ""),
                }
            elif dt == "hanki" and hanki_doc is None:
                hanki_doc = {
                    "docID": m.get("doc_id"),
                    "docTypeCode": "160",
                    "periodEnd": str(m.get("period_end", "")),
                    "filerName": m.get("filer_name", ""),
                }
            if not company_name and m.get("filer_name"):
                company_name = m["filer_name"].replace("株式会社", "").strip()

        # companies テーブルからも名前を取得
        if not company_name:
            comp_resp = (sb.table("companies")
                         .select("name")
                         .eq("code", code)
                         .limit(1)
                         .execute())
            if comp_resp.data:
                company_name = comp_resp.data[0].get("name", "")

        return {
            'company_name': company_name,
            'yuho_data': yuho_data,
            'hanki_data': hanki_data,
            'hanki_prior_data': hanki_prior_data,
            'yuho_doc': yuho_doc,
            'hanki_doc': hanki_doc,
            '_debug': {'source': 'supabase'},
        }
    except Exception:
        return None


def save_edinet_data(code, edinet_result):
    """
    edinet_client の fetch_companies_batch() 出力を Supabase に保存。
    """
    if not edinet_result:
        return

    company_name = edinet_result.get('company_name', '')
    upsert_company(code, name=company_name)

    # yuho_doc / hanki_doc の periodEnd を取得
    yuho_doc = edinet_result.get('yuho_doc')
    hanki_doc = edinet_result.get('hanki_doc')

    # edinet_meta 保存
    if yuho_doc and yuho_doc.get('docID'):
        save_edinet_meta(
            code=code,
            doc_id=yuho_doc['docID'],
            doc_type='yuho',
            period_end=yuho_doc.get('periodEnd', '').replace('/', '-'),
            filer_name=yuho_doc.get('filerName'),
        )
    if hanki_doc and hanki_doc.get('docID'):
        save_edinet_meta(
            code=code,
            doc_id=hanki_doc['docID'],
            doc_type='hanki',
            period_end=hanki_doc.get('periodEnd', '').replace('/', '-'),
            filer_name=hanki_doc.get('filerName'),
        )

    # financials 保存
    yuho_data = edinet_result.get('yuho_data', {})
    hanki_data = edinet_result.get('hanki_data', {})
    hanki_prior_data = edinet_result.get('hanki_prior_data', {})

    if yuho_data:
        period_end = (yuho_doc.get('periodEnd', '').replace('/', '-')
                      if yuho_doc else date.today().isoformat())
        save_financials(code, 'yuho', period_end, yuho_data)

    if hanki_data:
        period_end = (hanki_doc.get('periodEnd', '').replace('/', '-')
                      if hanki_doc else date.today().isoformat())
        save_financials(code, 'hanki_current', period_end, hanki_data)

    if hanki_prior_data:
        period_end = (hanki_doc.get('periodEnd', '').replace('/', '-')
                      if hanki_doc else date.today().isoformat())
        save_financials(code, 'hanki_prior', period_end, hanki_prior_data)


# ---------------------------------------------------------------------------
# Stock Data
# ---------------------------------------------------------------------------

def save_stock_data(code, stock_info):
    """stock_fetcher の出力を Supabase に保存。"""
    sb = get_supabase()
    if sb is None:
        return
    if not stock_info or stock_info.get('stock_price') is None:
        return

    upsert_company(code, name_en=stock_info.get('company_name_en'))

    fetched = stock_info.get('_fetched_date', date.today().isoformat())
    row = {
        "code": code,
        "stock_price": stock_info.get('stock_price'),
        "shares_outstanding": stock_info.get('shares_outstanding'),
        "market_cap": stock_info.get('market_cap'),
        "company_name_en": stock_info.get('company_name_en', ''),
        "fetched_date": fetched,
    }
    try:
        sb.table("stock_data").upsert(
            row, on_conflict="code,fetched_date"
        ).execute()
    except Exception:
        pass


def load_stock_data(code):
    """
    Supabase から最新の株価データを読み込み、stock_fetcher 互換 dict を返す。
    Returns: dict | None
    """
    sb = get_supabase()
    if sb is None:
        return None
    try:
        resp = (sb.table("stock_data")
                .select("*")
                .eq("code", code)
                .order("fetched_date", desc=True)
                .limit(1)
                .execute())
        if resp.data:
            row = resp.data[0]
            return {
                'stock_price': row.get('stock_price'),
                'shares_outstanding': row.get('shares_outstanding'),
                'market_cap': row.get('market_cap'),
                'company_name_en': row.get('company_name_en', ''),
                '_fetched_date': str(row.get('fetched_date', '')),
            }
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tanshin Forecasts
# ---------------------------------------------------------------------------

def load_forecasts():
    """
    tanshin_forecasts テーブルから各社の最新予想値を dict で返す。
    各社について最も新しい fy_month → 最も進んだ period_type のレコードを選択。
    Returns: {code: {rev_forecast, op_forecast, ni_forecast, fy_month, period_type}}
    """
    sb = get_supabase()
    if sb is None:
        return {}
    try:
        resp = (sb.table("tanshin_forecasts")
                .select("*")
                .order("fy_month", desc=True)
                .order("period_type", desc=True)
                .execute())
        result = {}
        for row in (resp.data or []):
            code = row.get("code")
            if code and code not in result:
                # 最初に出てくるのが最新（fy_month DESC, period_type DESC）
                entry = {
                    'rev_forecast': row.get('rev_forecast'),
                    'op_forecast': row.get('op_forecast'),
                    'ni_forecast': row.get('ni_forecast'),
                }
                if row.get('fy_month'):
                    entry['fy_month'] = row['fy_month']
                if row.get('period_type'):
                    entry['period_type'] = row['period_type']
                result[code] = entry
        return result
    except Exception:
        return {}


def load_forecast_history(code=None):
    """
    予想値の全履歴を取得。過去の予想修正を検証可能。

    Args:
        code: 証券コード（Noneなら全社）

    Returns:
        list of dict: [{code, fy_month, period_type, rev_forecast, op_forecast, ni_forecast, updated_at}, ...]

    使用例（2030年に2027年3月期の予想推移を確認）:
        history = load_forecast_history('6763')
        fy2027 = [h for h in history if h['fy_month'] == '2027-03']
        # → Q1時点, Q2時点, Q3時点, FY確定 の各予想が取得可能
    """
    sb = get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("tanshin_forecasts").select("*")
        if code:
            q = q.eq("code", code)
        resp = q.order("fy_month", desc=True).order("period_type").execute()
        return resp.data or []
    except Exception:
        return []


def save_forecast(code, forecast_data):
    """
    1社分の業績予想を upsert。
    fy_month + period_type が必須。不明な場合は 'unknown' で保存。
    同じ (code, fy_month, period_type) の組み合わせは上書き（予想修正への対応）。
    """
    sb = get_supabase()
    if sb is None:
        return
    if not forecast_data:
        return

    upsert_company(code)

    fy_month = forecast_data.get('fy_month', 'unknown')
    period_type = forecast_data.get('period_type', 'unknown')

    row = {
        "code": code,
        "rev_forecast": forecast_data.get('rev_forecast'),
        "op_forecast": forecast_data.get('op_forecast'),
        "ni_forecast": forecast_data.get('ni_forecast'),
        "fy_month": fy_month,
        "period_type": period_type,
        "updated_at": datetime.utcnow().isoformat(),
    }
    try:
        sb.table("tanshin_forecasts").upsert(
            row, on_conflict="code,fy_month,period_type"
        ).execute()
    except Exception:
        pass


def save_all_forecasts(forecasts_dict):
    """全社の業績予想を一括保存。"""
    for code, data in forecasts_dict.items():
        save_forecast(code, data)


# ---------------------------------------------------------------------------
# Tanshin PDF Storage
# ---------------------------------------------------------------------------

TANSHIN_BUCKET = "tanshin-pdfs"


def upload_tanshin_pdf(code, filename, pdf_bytes):
    """Supabase Storage に決算短信PDFをアップロード。"""
    sb = get_supabase()
    if sb is None:
        return
    path = f"{code}/{filename}"
    try:
        sb.storage.from_(TANSHIN_BUCKET).upload(
            path, pdf_bytes,
            file_options={"content-type": "application/pdf", "upsert": "true"}
        )
    except Exception:
        pass


def download_tanshin_pdf(code, filename):
    """Supabase Storage から決算短信PDFをダウンロード。"""
    sb = get_supabase()
    if sb is None:
        return None
    path = f"{code}/{filename}"
    try:
        data = sb.storage.from_(TANSHIN_BUCKET).download(path)
        return data
    except Exception:
        return None
