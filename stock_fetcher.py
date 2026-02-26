"""
Stock Fetcher - yfinance経由で最新株価・時価総額を取得。

社内ネットワーク対応のSSL検証バイパス付き。
yfinance が内部で curl_cffi を使うため、環境変数でSSL検証を無効化。

日次キャッシュ: 取得済みの株価データは data/stock/{code_4}/YYYY-MM-DD.json に
保存し、同日中は再取得しない（前日終値なので日中変わらない）。
"""

import json
import os
import ssl
import time
import urllib3
from datetime import date
from pathlib import Path

# curl_cffi の SSL 問題を回避: 空文字列を設定してSSL検証をスキップ
os.environ['CURL_CA_BUNDLE'] = ''
os.environ['REQUESTS_CA_BUNDLE'] = ''

import yfinance as yf


def _disable_ssl_verification():
    """SSL検証を無効化（社内ネットワーク対応）。"""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    ssl._create_default_https_context = ssl._create_unverified_context


def validate_stock_code(code_4):
    """
    証券コードが東証に存在するか軽量チェック。

    Returns:
        (True, company_name) or (False, error_message)
    """
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
            return True, ''  # レート制限時は通過させる（生成時に再取得する）
        return False, f"検証エラー: {err_msg}"


_STOCK_CACHE_DIR = Path(__file__).parent / "data" / "stock"


def _load_stock_cache(code_4):
    """今日の日付のキャッシュがあれば読み込む。なければNone。"""
    cache_file = _STOCK_CACHE_DIR / code_4 / f"{date.today().isoformat()}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding='utf-8'))
        except Exception:
            return None
    return None


def _save_stock_cache(code_4, data):
    """株価データを今日の日付でキャッシュ保存。"""
    cache_dir = _STOCK_CACHE_DIR / code_4
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{date.today().isoformat()}.json"
    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def fetch_stock_info(code_4, max_retries=3, use_cache=True):
    """
    証券コード（4桁）から株価情報を取得。
    同日中はキャッシュから返す（前日終値は日中変わらないため）。
    レート制限時は最大max_retries回リトライ。

    Returns: dict with keys:
        stock_price: 最新株価
        shares_outstanding: 発行済株式数（千株）
        market_cap: 時価総額（百万円）
        company_name_en: 企業名（英語）
    """
    # キャッシュチェック
    if use_cache:
        cached = _load_stock_cache(code_4)
        if cached:
            return cached

    _disable_ssl_verification()

    for attempt in range(max_retries):
        try:
            ticker = yf.Ticker(f"{code_4}.T")
            try:
                ticker._data._session.verify = False
            except AttributeError:
                pass

            info = ticker.info

            # レート制限チェック（yfinanceはエラーをinfoのdictで返す場合がある）
            if not info or len(info) <= 1:
                raise ValueError("Empty response (possible rate limit)")

            stock_price = info.get('currentPrice') or info.get('regularMarketPrice')
            if stock_price is None:
                # フォールバック: historyから取得
                hist = ticker.history(period="5d")
                if not hist.empty:
                    stock_price = float(hist['Close'].iloc[-1])

            if stock_price is None:
                raise ValueError(f"株価を取得できません: {code_4}")

            shares = info.get('sharesOutstanding')
            market_cap_raw = info.get('marketCap')

            # shares_outstanding: 株数 → 千株
            shares_thousands = None
            if shares:
                shares_thousands = int(shares / 1000)

            # market_cap: 円 → 百万円
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
            # キャッシュ保存
            if use_cache:
                try:
                    _save_stock_cache(code_4, result)
                except Exception:
                    pass
            return result

        except Exception as e:
            err_msg = str(e)
            is_rate_limit = 'Rate' in err_msg or 'Too Many' in err_msg or 'Empty response' in err_msg
            if is_rate_limit and attempt < max_retries - 1:
                wait = 5 * (attempt + 1)  # 5秒、10秒、15秒
                time.sleep(wait)
                continue
            raise
