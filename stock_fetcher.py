"""
Stock Fetcher - J-Quants API経由で最新株価を取得。yfinanceはフォールバック。

設計方針:
  1. キャッシュ (data/stock/{code_4}/stock.json) を常に最優先で読む
  2. キャッシュがない場合のみ J-Quants API にアクセス
  3. J-Quants 失敗時は yfinance にフォールバック
  4. 両方失敗してもキャッシュがあればそれを返す（例外を投げない）
  5. 株価の更新はサイドバーの「キャッシュクリア」で明示的に実行
  6. 発行済株式数はEDINET有報/半期報から取得（financial_calc.pyで算出）
"""

import json
import os
import ssl
import time
import urllib3
from datetime import date, timedelta
from pathlib import Path

# curl_cffi の SSL 問題を回避
os.environ['CURL_CA_BUNDLE'] = ''
os.environ['REQUESTS_CA_BUNDLE'] = ''

# Supabase client (optional)
try:
    from supabase_client import (
        load_stock_data as sb_load_stock_data,
        save_stock_data as sb_save_stock_data,
    )
    _HAS_SUPABASE = True
except ImportError:
    _HAS_SUPABASE = False


def _disable_ssl_verification():
    """SSL検証を無効化（社内ネットワーク対応）。"""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    ssl._create_default_https_context = ssl._create_unverified_context


_STOCK_CACHE_DIR = Path(__file__).parent / "data" / "stock"


def _load_stock_cache(code_4):
    """キャッシュがあれば読み込む。ローカル → Supabase の順。なければNone。"""
    cache_file = _STOCK_CACHE_DIR / code_4 / "stock.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding='utf-8'))
        except Exception:
            pass
    # Supabase フォールバック
    if _HAS_SUPABASE:
        try:
            sb_data = sb_load_stock_data(code_4)
            if sb_data and sb_data.get('stock_price') is not None:
                return sb_data
        except Exception:
            pass
    return None


def _save_stock_cache(code_4, data):
    """株価データをキャッシュ保存（取得日も記録）。"""
    try:
        cache_dir = _STOCK_CACHE_DIR / code_4
        cache_dir.mkdir(parents=True, exist_ok=True)
        data_with_date = dict(data)
        data_with_date['_fetched_date'] = date.today().isoformat()
        cache_file = cache_dir / "stock.json"
        cache_file.write_text(json.dumps(data_with_date, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# J-Quants API
# ---------------------------------------------------------------------------

def _load_jquants_api_key():
    """J-Quants APIキーを読み込む。st.secrets → Supabase app_config → None。"""
    # st.secrets
    try:
        import streamlit as st
        if "jquants" in st.secrets:
            key = st.secrets["jquants"].get("api_key", "")
            if key:
                return key
    except Exception:
        pass
    # Supabase app_config
    if _HAS_SUPABASE:
        try:
            from supabase_client import get_supabase
            sb = get_supabase()
            if sb:
                cfg = sb.table("app_config").select("value").eq("key", "jquants_api_key").limit(1).execute()
                if cfg.data:
                    key = cfg.data[0].get("value", "")
                    if key:
                        return key
        except Exception:
            pass
    return None


def _fetch_jquants_stock(code_4, api_key):
    """
    J-Quants APIから前日終値を取得。

    Returns: dict with keys: stock_price, company_name_en, _price_date, _source
    Raises: Exception on failure
    """
    import requests

    code_5 = code_4 + "0"
    today = date.today()
    # 直近5営業日分を取得（休日をまたぐため余裕を持たせる）
    from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    headers = {"x-api-key": api_key}

    # 株価取得
    url = "https://api.jquants.com/v2/equities/bars/daily"
    params = {"code": code_5, "from": from_date, "to": to_date}
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    bars = data.get("daily_quotes") or data.get("data") or []
    if not bars:
        raise ValueError(f"J-Quants: {code_4} の株価データが取得できません")

    # 最新日の調整済み終値
    latest = bars[-1]
    stock_price = latest.get("AdjustmentClose") or latest.get("AdjC") or latest.get("Close") or latest.get("C")
    price_date = latest.get("Date", "")
    if stock_price is None:
        raise ValueError(f"J-Quants: {code_4} の終値がありません")

    # 英語社名を取得（masterエンドポイント）
    company_name_en = ""
    try:
        master_url = "https://api.jquants.com/v2/listed/info"
        master_params = {"code": code_5}
        master_resp = requests.get(master_url, headers=headers, params=master_params, timeout=10)
        master_resp.raise_for_status()
        master_data = master_resp.json()
        info_list = master_data.get("info", [])
        if info_list:
            company_name_en = info_list[0].get("CompanyNameEnglish", "")
    except Exception:
        pass

    return {
        'stock_price': float(stock_price),
        'company_name_en': company_name_en,
        '_price_date': price_date,
        '_source': 'jquants',
    }


def _fetch_yfinance_stock(code_4, max_retries=3):
    """
    yfinanceから株価を取得（フォールバック用）。

    Returns: dict with keys: stock_price, shares_outstanding, market_cap, company_name_en, _source
    Raises: Exception on failure
    """
    _disable_ssl_verification()

    import yfinance as yf

    for attempt in range(max_retries):
        try:
            ticker = yf.Ticker(f"{code_4}.T")
            try:
                ticker._data._session.verify = False
            except AttributeError:
                pass

            info = ticker.info
            if not info or len(info) <= 1:
                raise ValueError("Empty response (possible rate limit)")

            stock_price = info.get('currentPrice') or info.get('regularMarketPrice')
            if stock_price is None:
                hist = ticker.history(period="5d")
                if not hist.empty:
                    stock_price = float(hist['Close'].iloc[-1])

            if stock_price is None:
                raise ValueError(f"株価を取得できません: {code_4}")

            shares = info.get('sharesOutstanding')
            market_cap_raw = info.get('marketCap')

            shares_thousands = int(shares / 1000) if shares else None

            market_cap_millions = None
            if market_cap_raw:
                market_cap_millions = int(market_cap_raw / 1_000_000)
            elif stock_price and shares:
                market_cap_millions = int(stock_price * shares / 1_000_000)

            return {
                'stock_price': stock_price,
                'shares_outstanding': shares_thousands,
                'market_cap': market_cap_millions,
                'company_name_en': info.get('shortName', ''),
                '_source': 'yfinance',
            }

        except Exception as e:
            err_msg = str(e)
            is_rate_limit = 'Rate' in err_msg or 'Too Many' in err_msg or 'Empty response' in err_msg
            if is_rate_limit and attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                time.sleep(wait)
                continue
            raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_stock_code(code_4):
    """
    証券コードが東証に存在するか軽量チェック。
    キャッシュ（ローカル/Supabase）があれば即OK（外部API不要）。
    """
    # キャッシュがあれば検証済みとみなす（ローカル → Supabase）
    cached = _load_stock_cache(code_4)
    if cached:
        return True, cached.get('company_name_en', '')
    # Supabase companies テーブルに存在すれば検証済み
    if _HAS_SUPABASE:
        try:
            from supabase_client import get_supabase
            sb = get_supabase()
            if sb:
                resp = sb.table("companies").select("code,name_en").eq("code", code_4).limit(1).execute()
                if resp.data:
                    return True, resp.data[0].get('name_en', '')
        except Exception:
            pass

    # J-Quants APIで検証
    api_key = _load_jquants_api_key()
    if api_key:
        try:
            result = _fetch_jquants_stock(code_4, api_key)
            return True, result.get('company_name_en', '')
        except Exception:
            pass

    # yfinanceフォールバック
    _disable_ssl_verification()
    try:
        import yfinance as yf
        ticker = yf.Ticker(f"{code_4}.T")
        try:
            ticker._data._session.verify = False
        except AttributeError:
            pass
        hist = ticker.history(period="5d")
        if hist.empty:
            return False, f"証券コード {code_4} は東証に存在しないか、データを取得できません"
        name = ''
        try:
            name = ticker.info.get('shortName', '')
        except Exception:
            pass
        return True, name
    except Exception as e:
        err_msg = str(e)
        if 'Rate' in err_msg or 'Too Many' in err_msg:
            return True, ''  # レート制限時は通過させる
        return False, f"検証エラー: {err_msg}"


def fetch_stock_info(code_4, max_retries=3, use_cache=True):
    """
    証券コード（4桁）から株価情報を取得。

    優先順位:
      1. キャッシュ (常にチェック)
      2. J-Quants API (キャッシュなしの場合のみ)
      3. yfinance (J-Quants失敗時のフォールバック)
      4. 全失敗時 → キャッシュがあればそれを返す

    Returns: dict with keys:
        stock_price, company_name_en
        (shares_outstanding, market_cap はyfinanceフォールバック時のみ。
         通常はfinancial_calc.pyでEDINETデータから算出)
    """
    # 1. キャッシュを常に最優先
    cached = _load_stock_cache(code_4)
    if cached and use_cache:
        return cached

    # 2. J-Quants API
    api_key = _load_jquants_api_key()
    if api_key:
        try:
            result = _fetch_jquants_stock(code_4, api_key)
            # 既存キャッシュのshares_outstanding/market_capを引き継ぎ（後方互換）
            if cached:
                if 'shares_outstanding' in cached and 'shares_outstanding' not in result:
                    result['shares_outstanding'] = cached['shares_outstanding']
                if 'market_cap' in cached and 'market_cap' not in result:
                    result['market_cap'] = cached['market_cap']
            _save_stock_cache(code_4, result)
            if _HAS_SUPABASE:
                try:
                    sb_save_stock_data(code_4, result)
                except Exception:
                    pass
            return result
        except Exception:
            pass  # J-Quants失敗 → yfinanceフォールバック

    # 3. yfinance フォールバック
    try:
        result = _fetch_yfinance_stock(code_4, max_retries=max_retries)
        _save_stock_cache(code_4, result)
        if _HAS_SUPABASE:
            try:
                sb_save_stock_data(code_4, result)
            except Exception:
                pass
        return result
    except Exception:
        pass

    # 4. 全失敗 → キャッシュがあればそれを返す（use_cache=Falseでも最終手段）
    if cached:
        return cached

    # 5. 完全にデータなし → 空dictを返す（例外を投げない）
    return {'stock_price': None, 'shares_outstanding': None, 'market_cap': None, 'company_name_en': ''}
