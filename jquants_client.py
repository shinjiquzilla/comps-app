"""
J-Quants Client - /v2/fins/summary からP&L・業績予想・株式数を取得。

設計方針:
  J-Quantsで取れるものは全部J-Quantsから取る。EDINETは残りだけ。
  /v2/fins/summary (Lightプラン) で決算短信ベースの累計P&L・予想・株式数が一発で取れる。

取得項目:
  - 売上高・営業利益・純利益（各四半期累計） → LTM計算
  - 通期業績予想（FSales/FOP/FNP/FEPS/FDivAnn）
  - 発行済株式数・自己株式数
  - 純資産・自己資本比率
  - 会計基準（DocType から推定）

キャッシュ:
  ローカル: data/jquants/{code_4}/fins_summary.json
  Supabase: jquants_fins テーブル
  読み込み順: ローカル → Supabase → API
"""

import json
import os
from datetime import date
from pathlib import Path

# Supabase client (optional)
try:
    from supabase_client import (
        save_jquants_fins as sb_save_jquants_fins,
        load_jquants_fins as sb_load_jquants_fins,
    )
    _HAS_SUPABASE = True
except ImportError:
    _HAS_SUPABASE = False

_JQUANTS_CACHE_DIR = Path(__file__).parent / "data" / "jquants"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _load_local_cache(code_4):
    """ローカルキャッシュから読み込み。なければNone。"""
    cache_file = _JQUANTS_CACHE_DIR / code_4 / "fins_summary.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding='utf-8'))
        except Exception:
            pass
    return None


def _save_local_cache(code_4, data):
    """ローカルキャッシュに保存。"""
    try:
        cache_dir = _JQUANTS_CACHE_DIR / code_4
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "fins_summary.json"
        cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def _load_cache(code_4):
    """ローカル → Supabase の順でキャッシュ読み込み。"""
    data = _load_local_cache(code_4)
    if data:
        return data
    if _HAS_SUPABASE:
        try:
            data = sb_load_jquants_fins(code_4)
            if data:
                _save_local_cache(code_4, data)
                return data
        except Exception:
            pass
    return None


def _save_cache(code_4, data):
    """ローカル + Supabase の両方に保存。"""
    _save_local_cache(code_4, data)
    if _HAS_SUPABASE:
        try:
            sb_save_jquants_fins(code_4, data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------

def _get_api_key():
    """J-Quants APIキーを取得。stock_fetcher と同じロジック。"""
    from stock_fetcher import _load_jquants_api_key
    return _load_jquants_api_key()


# ---------------------------------------------------------------------------
# API Fetch
# ---------------------------------------------------------------------------

def _fetch_fins_summary_api(code_4, api_key):
    """
    /v2/fins/summary から全レコードを取得。

    Returns: list of dict (APIレスポンスの statements 配列)
    Raises: Exception on failure
    """
    import requests

    code_5 = code_4 + "0"
    url = "https://api.jquants.com/v2/fins/summary"
    params = {"code": code_5}
    headers = {"x-api-key": api_key}

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    statements = data.get("fins_summary") or data.get("data") or data.get("statements") or []
    if not statements:
        raise ValueError(f"J-Quants fins/summary: {code_4} のデータがありません")

    return statements


# ---------------------------------------------------------------------------
# Data Organization
# ---------------------------------------------------------------------------

def _detect_accounting(doc_type_str):
    """DocType文字列から会計基準を推定。"""
    if not doc_type_str:
        return 'J-GAAP'
    s = str(doc_type_str)
    if 'IFRS' in s or '_ifrs' in s.lower():
        return 'IFRS'
    if 'US' in s or '_us' in s.lower():
        return 'US-GAAP'
    return 'J-GAAP'


def _safe_millions(val):
    """円(str/int/float) → 百万円 float。None/空文字の場合はNone。"""
    if val is None or val == '' or val == 'null':
        return None
    try:
        return float(val) / 1_000_000
    except (ValueError, TypeError):
        return None


def _safe_thousands(val):
    """株数(str/int/float) → 千株 float。None/空文字の場合はNone。"""
    if val is None or val == '' or val == 'null':
        return None
    try:
        return float(val) / 1_000
    except (ValueError, TypeError):
        return None


def _safe_float(val):
    """安全にfloat変換。"""
    if val is None or val == '' or val == 'null':
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _organize_quarterly_data(records):
    """
    APIレスポンスの全レコードから四半期データを整理。

    J-Quants fins/summary のフィールド:
      - CurFYEn: 当期年度末日 (例: "2025-03-31")
      - CurPerType: 期間種別 (例: "FY", "1Q", "2Q", "3Q")
      - DiscDate: 開示日
      - DocType: 書類タイプ (会計基準推定用)
      - Sales, OP, NP: 売上高・営業利益・純利益（円）
      - FSales, FOP, FNP, FEPS: 通期予想（円）
      - FDivAnn: 年間配当予想（円）
      - ShOutFY, TrShFY: 発行済株式数・自己株式数（株）
      - Eq: 純資産（円）
      - EqAR: 自己資本比率（%表記の場合あり）
      - TA: 総資産（円）

    Returns: dict with keys:
        fy_end_month, accounting, quarters, forecast, shares
    """
    if not records:
        return None

    # DiscDate降順でソート（最新開示を優先）
    sorted_records = sorted(records, key=lambda r: r.get('DiscDate', ''), reverse=True)

    # 最新レコードからメタ情報を取得
    latest = sorted_records[0]
    fy_end_str = latest.get('CurFYEn', '')  # "2025-03-31"
    fy_end_month = 3
    if fy_end_str and len(fy_end_str) >= 7:
        try:
            fy_end_month = int(fy_end_str.split('-')[1])
        except (ValueError, IndexError):
            pass

    accounting = _detect_accounting(latest.get('DocType', ''))

    # 当期FY年度末を特定（最新レコードのCurFYEn）
    current_fy_end = fy_end_str  # 例: "2026-03-31"

    # 前期FY年度末を推定
    if current_fy_end and len(current_fy_end) >= 4:
        try:
            _yr = int(current_fy_end[:4])
            prior_fy_end = f"{_yr - 1}{current_fy_end[4:]}"  # "2025-03-31"
        except ValueError:
            prior_fy_end = ""
    else:
        prior_fy_end = ""

    # 四半期データを整理
    # key: (CurFYEn, CurPerType) → 最新DiscDateのレコードを保持
    best_records = {}
    for rec in sorted_records:
        fy = rec.get('CurFYEn', '')
        pt = rec.get('CurPerType', '')
        key = (fy, pt)
        if key not in best_records:
            best_records[key] = rec

    # quarters を構築
    quarters = {}

    # 当期の最新四半期を特定
    # CurPerType の優先順: 3Q > 2Q > 1Q (FYは通期なので別扱い)
    _q_priority = {'3Q': 3, '2Q': 2, '1Q': 1}
    current_quarter = None
    current_quarter_type = None

    for (fy, pt), rec in best_records.items():
        if fy == current_fy_end and pt in _q_priority:
            if current_quarter is None or _q_priority[pt] > _q_priority.get(current_quarter_type, 0):
                current_quarter = rec
                current_quarter_type = pt

    # FY通期（前年度）
    fy_rec = best_records.get((prior_fy_end, 'FY'))
    if fy_rec:
        quarters['FY'] = {
            'fy_year': prior_fy_end,
            'revenue': _safe_millions(fy_rec.get('Sales')),
            'op': _safe_millions(fy_rec.get('OP')),
            'ni': _safe_millions(fy_rec.get('NP')),
            'equity': _safe_millions(fy_rec.get('Eq')),
            'equity_ratio': _safe_float(fy_rec.get('EqAR')),
        }

    # 当期四半期累計
    _pt_map = {'1Q': 'Q1', '2Q': '2Q', '3Q': 'Q3'}
    if current_quarter and current_quarter_type:
        q_key = _pt_map.get(current_quarter_type, current_quarter_type)
        quarters[q_key] = {
            'fy_year': current_fy_end,
            'revenue': _safe_millions(current_quarter.get('Sales')),
            'op': _safe_millions(current_quarter.get('OP')),
            'ni': _safe_millions(current_quarter.get('NP')),
            'equity': _safe_millions(current_quarter.get('Eq')),
            'equity_ratio': _safe_float(current_quarter.get('EqAR')),
        }

        # 前年同期（prior）
        prior_q_rec = best_records.get((prior_fy_end, current_quarter_type))
        if prior_q_rec:
            quarters[q_key + '_prior'] = {
                'fy_year': prior_fy_end,
                'revenue': _safe_millions(prior_q_rec.get('Sales')),
                'op': _safe_millions(prior_q_rec.get('OP')),
                'ni': _safe_millions(prior_q_rec.get('NP')),
                'equity': _safe_millions(prior_q_rec.get('Eq')),
                'equity_ratio': _safe_float(prior_q_rec.get('EqAR')),
            }

    # 予想値（最新四半期 or FY通期から）
    forecast_source = current_quarter or fy_rec or latest
    forecast = {
        'rev_forecast': _safe_millions(forecast_source.get('FSales')),
        'op_forecast': _safe_millions(forecast_source.get('FOP')),
        'ni_forecast': _safe_millions(forecast_source.get('FNP')),
        'eps_forecast': _safe_float(forecast_source.get('FEPS')),
        'dps_forecast': _safe_float(forecast_source.get('FDivAnn')),
    }

    # 株式数（最新レコードから）
    shares_source = current_quarter or fy_rec or latest
    shares = {
        'shares_issued': _safe_thousands(shares_source.get('ShOutFY')),
        'treasury_shares': _safe_thousands(shares_source.get('TrShFY')),
    }

    # fy_history: 過去FYデータ（直近3年分）
    fy_history = []
    for (fy, pt), rec in best_records.items():
        if pt == 'FY':
            fy_history.append({
                'fy_year': fy,
                'revenue': _safe_millions(rec.get('Sales')),
                'op': _safe_millions(rec.get('OP')),
                'ni': _safe_millions(rec.get('NP')),
            })
    fy_history.sort(key=lambda x: x['fy_year'], reverse=True)
    fy_history = fy_history[:3]

    return {
        'fy_end_month': fy_end_month,
        'accounting': accounting,
        'quarters': quarters,
        'forecast': forecast,
        'shares': shares,
        'fy_history': fy_history,
        '_source': 'jquants',
        '_fetched_date': date.today().isoformat(),
        '_raw_record_count': len(records),
    }


# ---------------------------------------------------------------------------
# LTM Computation
# ---------------------------------------------------------------------------

def compute_ltm_from_jquants(quarters):
    """
    J-Quantsの四半期累計データからLTM直接計算。

    パターン:
      最新四半期が2Q → LTM = FY通期 - 2Q_prior + 2Q
      最新四半期がQ1 → LTM = FY通期 - Q1_prior + Q1
      最新四半期がQ3 → LTM = FY通期 - Q3_prior + Q3
      FY通期のみ → LTM = FY通期

    Returns: (rev_ltm, op_ltm, ni_ltm, used_pattern)
    """
    if not quarters:
        return None, None, None, 'no_data'

    fy = quarters.get('FY')

    # 最新四半期を特定（Q3 > 2Q > Q1）
    for q_type in ['Q3', '2Q', 'Q1']:
        q_data = quarters.get(q_type)
        q_prior = quarters.get(q_type + '_prior')

        if q_data and q_prior and fy:
            rev = _ltm_calc(
                fy.get('revenue'), q_prior.get('revenue'), q_data.get('revenue'))
            op = _ltm_calc(
                fy.get('op'), q_prior.get('op'), q_data.get('op'))
            ni = _ltm_calc(
                fy.get('ni'), q_prior.get('ni'), q_data.get('ni'))
            return rev, op, ni, q_type

    # FY通期のみ
    if fy:
        return fy.get('revenue'), fy.get('op'), fy.get('ni'), 'FY'

    return None, None, None, 'no_data'


def _ltm_calc(fy_val, prior_val, current_val):
    """単一項目のLTM = FY - prior + current。Noneセーフ。"""
    if fy_val is None:
        return None
    if prior_val is None or current_val is None:
        return fy_val  # フォールバック: FY値
    return fy_val - prior_val + current_val


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_fins_summary(code_4, use_cache=True):
    """
    J-Quants /v2/fins/summary から財務サマリーを取得。

    キャッシュ読み込み順: ローカル → Supabase → API

    Parameters:
        code_4: 証券コード（4桁）
        use_cache: キャッシュを使用するか

    Returns: dict with keys:
        fy_end_month, accounting, quarters, forecast, shares,
        _source, _fetched_date, _raw_record_count
        失敗時はNone
    """
    # 1. キャッシュ
    if use_cache:
        cached = _load_cache(code_4)
        if cached:
            return cached

    # 2. API
    api_key = _get_api_key()
    if not api_key:
        # APIキーなし → キャッシュがあればそれを返す
        if use_cache:
            return _load_cache(code_4)
        return None

    try:
        records = _fetch_fins_summary_api(code_4, api_key)
        result = _organize_quarterly_data(records)
        if result:
            _save_cache(code_4, result)
            return result
    except Exception:
        pass

    # 3. API失敗 → キャッシュをフォールバック
    cached = _load_cache(code_4)
    if cached:
        return cached

    return None
