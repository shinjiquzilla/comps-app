"""
Stock Fetcher - yfinance経由で最新株価・時価総額を取得。

社内ネットワーク対応のSSL検証バイパス付き。
yfinance が内部で curl_cffi を使うため、環境変数でSSL検証を無効化。
"""

import os
import ssl
import urllib3

# curl_cffi の SSL 問題を回避: 空文字列を設定してSSL検証をスキップ
os.environ['CURL_CA_BUNDLE'] = ''
os.environ['REQUESTS_CA_BUNDLE'] = ''

import yfinance as yf


def _disable_ssl_verification():
    """SSL検証を無効化（社内ネットワーク対応）。"""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    ssl._create_default_https_context = ssl._create_unverified_context


def fetch_stock_info(code_4):
    """
    証券コード（4桁）から株価情報を取得。

    Returns: dict with keys:
        stock_price: 最新株価
        shares_outstanding: 発行済株式数（千株）
        market_cap: 時価総額（百万円）
        company_name_en: 企業名（英語）
    """
    _disable_ssl_verification()

    # yfinance の内部セッションの verify を無効化
    ticker = yf.Ticker(f"{code_4}.T")
    try:
        ticker._data._session.verify = False
    except AttributeError:
        pass

    info = ticker.info

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

    return {
        'stock_price': stock_price,
        'shares_outstanding': shares_thousands,
        'market_cap': market_cap_millions,
        'company_name_en': info.get('shortName', ''),
    }
