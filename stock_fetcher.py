"""
Stock Fetcher - yfinance経由で最新株価・時価総額を取得。

設計方針:
  1. キャッシュ (data/stock/{code_4}/stock.json) を常に最優先で読む
  2. キャッシュがない場合のみ yfinance にアクセス
  3. yfinance が失敗してもキャッシュがあればそれを返す（例外を投げない）
  4. 株価の更新はサイドバーの「キャッシュクリア」で明示的に実行
"""

import json
import os
import ssl
import time
import urllib3
from datetime import date
from pathlib import Path

# curl_cffi の SSL 問題を回避
os.environ['CURL_CA_BUNDLE'] = ''
os.environ['REQUESTS_CA_BUNDLE'] = ''

import yfinance as yf

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


def validate_stock_code(code_4):
    """
    証券コードが東証に存在するか軽量チェック。
    キャッシュ（ローカル/Supabase）があれば即OK（yfinance不要）。
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

    _disable_ssl_verification()
    try:
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
      2. yfinance (キャッシュなしの場合のみ)
      3. yfinance失敗時 → 空dictを返す（例外を投げない）

    Returns: dict with keys:
        stock_price, shares_outstanding, market_cap, company_name_en
    """
    # 1. キャッシュを常に最優先
    cached = _load_stock_cache(code_4)
    if cached and use_cache:
        return cached

    # 2. yfinance にアクセス
    _disable_ssl_verification()
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

            result = {
                'stock_price': stock_price,
                'shares_outstanding': shares_thousands,
                'market_cap': market_cap_millions,
                'company_name_en': info.get('shortName', ''),
            }
            _save_stock_cache(code_4, result)
            # Supabase にも保存
            if _HAS_SUPABASE:
                try:
                    sb_save_stock_data(code_4, result)
                except Exception:
                    pass
            return result

        except Exception as e:
            err_msg = str(e)
            is_rate_limit = 'Rate' in err_msg or 'Too Many' in err_msg or 'Empty response' in err_msg
            if is_rate_limit and attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                time.sleep(wait)
                continue
            break  # リトライ不要なエラー → ループ脱出

    # 3. yfinance失敗 → キャッシュがあればそれを返す（use_cache=Falseでも最終手段）
    if cached:
        return cached

    # 4. 完全にデータなし → 空dictを返す（例外を投げない）
    return {'stock_price': None, 'shares_outstanding': None, 'market_cap': None, 'company_name_en': ''}
