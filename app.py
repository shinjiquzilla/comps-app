"""
Comps自動生成 Streamlit アプリ
==============================
証券コードを入力するだけで Comparable Company Analysis 表を自動生成。

データソース:
- EDINET API type=5 CSV: 有報・半期報告書の財務データ
- yfinance: 最新株価・時価総額

使い方:
    streamlit run app.py
"""

import io
import os
import sys
import time
import traceback
from datetime import datetime

# SSL検証バイパス（社内ネットワーク対応）- yfinance/curl_cffi用
os.environ['CURL_CA_BUNDLE'] = ''
os.environ['REQUESTS_CA_BUNDLE'] = ''

import streamlit as st
import pandas as pd

# 自身のディレクトリをパスに追加
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from edinet_client import fetch_company_financials, fetch_companies_batch, load_api_key, clear_cache, load_cached_meta
from stock_fetcher import fetch_stock_info, validate_stock_code
from financial_calc import build_company_data
from comps_generator import generate_comps
from tanshin_parser import parse_tanshin_pdf, save_tanshin_pdf, identify_tanshin_pdf

# ---------------------------------------------------------------------------
# 決算短信 予想値の永続キャッシュ（data/tanshin_forecasts.json）
# ---------------------------------------------------------------------------
import json as _json
from pathlib import Path as _Path

_FORECASTS_FILE = _Path(__file__).parent / "data" / "tanshin_forecasts.json"


def _load_forecasts_cache():
    """永続キャッシュから予想値を読み込む。"""
    if _FORECASTS_FILE.exists():
        try:
            return _json.loads(_FORECASTS_FILE.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def _save_forecasts_cache(forecasts):
    """予想値を永続キャッシュに保存。"""
    try:
        _FORECASTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FORECASTS_FILE.write_text(
            _json.dumps(forecasts, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Page Config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Comps自動生成ツール",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Comps（比較会社分析）自動生成ツール")
st.caption("証券コードを入力して「生成」ボタンを押すだけで、Comps表を自動作成します。")

# ---------------------------------------------------------------------------
# Sidebar: Settings
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("設定")

    # EDINET API Key（secrets にあればそちらを優先）
    default_edinet_key = ""
    try:
        if "edinet" in st.secrets:
            default_edinet_key = st.secrets["edinet"].get("api_key", "")
            os.environ["EDINET_API_KEY"] = default_edinet_key
    except Exception:
        pass

    if not default_edinet_key:
        api_key_input = st.text_input(
            "EDINET API Key",
            type="password",
            help="https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1 から取得",
        )
        if api_key_input:
            os.environ["EDINET_API_KEY"] = api_key_input

    search_days = st.slider("検索期間（日数）", 30, 730, 400, step=30,
                            help="EDINETを過去何日分検索するか（有報は決算後3ヶ月、半期報も同様に提出されるため、400日程度が推奨）")

    st.divider()
    st.subheader("キャッシュ設定")
    use_cache = st.checkbox("キャッシュを使用", value=True,
                            help="ダウンロード済みのEDINETデータをローカルに保存し、次回以降はAPIアクセスをスキップします")
    if st.button("キャッシュをクリア"):
        clear_cache()
        st.success("キャッシュを全削除しました。")

    st.divider()

    st.markdown("""
    **データソース:**
    - 📄 EDINET API (有報・半期報)
    - 📈 yfinance (株価)
    """)

# ---------------------------------------------------------------------------
# Main Input
# ---------------------------------------------------------------------------

col1, col2 = st.columns([3, 1])

with col1:
    codes_input = st.text_input(
        "証券コード（カンマ区切り）",
        value="",
        placeholder="例: 6763,6989,6768,6779",
    )

with col2:
    st.write("")  # spacer
    st.write("")
    generate_btn = st.button("🚀 Comps表を生成", type="primary", use_container_width=True)

# --- 入力フォーマット即時検証 ---
_input_codes_raw = [c.strip() for c in codes_input.split(",") if c.strip()]
_format_errors = []
for _c in _input_codes_raw:
    if not _c.isdigit() or len(_c) != 4:
        _format_errors.append(f"「{_c}」は4桁の数字ではありません")
if _format_errors and codes_input.strip():
    for _fe in _format_errors:
        st.error(_fe)

# ---------------------------------------------------------------------------
# Session State Init
# ---------------------------------------------------------------------------

if 'company_data' not in st.session_state:
    st.session_state.company_data = []
if 'generation_done' not in st.session_state:
    st.session_state.generation_done = False
# 決算短信予想値: 永続キャッシュから復元
if 'tanshin_forecasts' not in st.session_state:
    st.session_state.tanshin_forecasts = _load_forecasts_cache()
if 'errors' not in st.session_state:
    st.session_state.errors = []

# ---------------------------------------------------------------------------
# Generation Logic
# ---------------------------------------------------------------------------

if generate_btn:
    codes = [c.strip() for c in codes_input.split(",") if c.strip()]
    if not codes:
        st.error("証券コードを入力してください。")
    elif any(not c.isdigit() or len(c) != 4 for c in codes):
        st.error("証券コードは4桁の数字で入力してください。")
    else:
        # EDINET API Key チェック（全社キャッシュ済みならキー不要）
        # まず全社のキャッシュ状況を事前チェック
        _all_edinet_cached = True
        if use_cache:
            for _c in codes:
                _meta = load_cached_meta(_c)
                if not (_meta and _meta.get("docs") is not None):
                    _all_edinet_cached = False
                    break
        else:
            _all_edinet_cached = False

        edinet_available = False
        if _all_edinet_cached:
            # 全社キャッシュ済み: APIキー不要、EDINET利用可能扱い
            edinet_available = True
        else:
            try:
                load_api_key()
                edinet_available = True
            except RuntimeError:
                pass

        if not edinet_available:
            st.warning("EDINET API Key が未設定のため、財務データ（P&L/BS）は自動取得できません。株価のみ取得し、その他は手動入力で補完できます。")

        st.session_state.company_data = []
        st.session_state.errors = []
        st.session_state.generation_done = False

        progress_bar = st.progress(0)
        status_container = st.empty()
        progress_text = st.empty()

        # ---- 全社キャッシュ判定 ----
        from pathlib import Path
        from stock_fetcher import _load_stock_cache
        _tanshin_base = Path(__file__).parent / "data" / "tanshin"
        _edinet_base = Path(__file__).parent / "data" / "edinet"
        _stock_base = Path(__file__).parent / "data" / "stock"

        # 各社のキャッシュ状況を事前確認
        _cache_status = {}  # code -> {'edinet': bool, 'stock': bool, 'any': bool}
        for _c in codes:
            _has_edinet = (_edinet_base / _c / "meta.json").exists()
            _has_edinet_parsed = (
                (_edinet_base / _c / "yuho_parsed.json").exists() or
                (_edinet_base / _c / "hanki_parsed.json").exists()
            )
            _has_stock = (_stock_base / _c / "stock.json").exists()
            _has_tanshin = (_tanshin_base / _c).is_dir() and any((_tanshin_base / _c).glob("*.pdf"))
            _cache_status[_c] = {
                'edinet': _has_edinet and _has_edinet_parsed,
                'stock': _has_stock,
                'any': _has_edinet or _has_stock or _has_tanshin,
            }

        _all_fully_cached = all(
            cs['edinet'] and cs['stock'] for cs in _cache_status.values()
        )

        # ---- 完全キャッシュパス: 外部API一切なし ----
        if _all_fully_cached and use_cache:
            progress_bar = st.progress(0)
            status_container = st.empty()
            progress_text = st.empty()

            status_container.info(f"⚡ 全{len(codes)}社のデータをローカルキャッシュから読み込み中...")
            progress_bar.progress(0.3)

            # EDINETデータをパース済みJSONから直接読み込み
            edinet_results = {}
            for code in codes:
                code_dir = _edinet_base / code
                meta = load_cached_meta(code)
                company_name = ""
                yuho_doc = None
                hanki_doc = None
                if meta:
                    for doc in meta.get("docs", []):
                        if not company_name and doc.get("filerName"):
                            company_name = doc["filerName"].replace("株式会社", "").strip()
                        dt = doc.get("docTypeCode", "")
                        if dt in ("120", "130") and not yuho_doc:
                            yuho_doc = doc
                        elif dt in ("160", "170") and not hanki_doc:
                            hanki_doc = doc

                yuho_data = {}
                yuho_parsed_path = code_dir / "yuho_parsed.json"
                if yuho_parsed_path.exists():
                    try:
                        yuho_data = _json.loads(yuho_parsed_path.read_text(encoding='utf-8'))
                    except Exception:
                        pass

                hanki_data = {}
                hanki_prior_data = {}
                hanki_parsed_path = code_dir / "hanki_parsed.json"
                if hanki_parsed_path.exists():
                    try:
                        hanki_parsed = _json.loads(hanki_parsed_path.read_text(encoding='utf-8'))
                        hanki_data = hanki_parsed.get('current', {})
                        hanki_prior_data = hanki_parsed.get('prior', {})
                    except Exception:
                        pass

                edinet_results[code] = {
                    'company_name': company_name,
                    'yuho_data': yuho_data,
                    'hanki_data': hanki_data,
                    'hanki_prior_data': hanki_prior_data,
                    'yuho_doc': yuho_doc,
                    'hanki_doc': hanki_doc,
                    '_debug': {'fully_cached': True},
                }

            progress_bar.progress(0.6)

            # 株価データをJSONから直接読み込み＆計算
            results = []
            for i, code in enumerate(codes):
                edinet_data = edinet_results.get(code, {
                    'company_name': '', 'yuho_data': {}, 'hanki_data': {},
                    'yuho_doc': None, 'hanki_doc': None,
                })
                stock_data = _load_stock_cache(code) or {
                    'stock_price': None, 'shares_outstanding': None,
                    'market_cap': None, 'company_name_en': '',
                }
                tdnet_data = {'forecast': {}}

                result = {
                    'code': code, 'status': 'processing', 'errors': [],
                    'data': None, 'edinet_raw': None, 'stock_raw': stock_data,
                }
                result['edinet_raw'] = edinet_data if edinet_data.get('yuho_data') or edinet_data.get('hanki_data') else None

                try:
                    company = build_company_data(code, edinet_data, tdnet_data, stock_data)
                    if not company.get('name') and stock_data.get('company_name_en'):
                        company['name'] = stock_data['company_name_en']
                    result['data'] = company
                    result['status'] = 'done'
                except Exception as e:
                    result['errors'].append(f"計算: {e}")
                    result['status'] = 'error'

                if result['errors']:
                    for err in result['errors']:
                        st.session_state.errors.append(f"{code}: {err}")
                results.append(result)

            progress_bar.progress(1.0)
            status_container.success(f"✅ 完了: 全{len(codes)}社をキャッシュから即座に読み込みました（外部API通信なし）。")
            progress_text.empty()

        else:
            # ---- 通常パス: キャッシュミスあり → 外部API使用 ----
            progress_bar = st.progress(0)
            status_container = st.empty()
            progress_text = st.empty()

            # Step 0: 証券コード存在チェック
            status_container.info("🔍 証券コードを検証中...")
            invalid_codes = []
            valid_codes = []
            _need_yf_check = []
            for vc in codes:
                if _cache_status[vc]['any']:
                    valid_codes.append(vc)
                else:
                    _need_yf_check.append(vc)

            for vi, vc in enumerate(_need_yf_check):
                progress_text.text(f"  🔍 {vc}: yfinanceで検証中... ({vi+1}/{len(_need_yf_check)})")
                progress_bar.progress((vi + 1) / len(_need_yf_check) * 0.1 if _need_yf_check else 0.1)
                is_valid, msg = validate_stock_code(vc)
                if is_valid:
                    valid_codes.append(vc)
                else:
                    invalid_codes.append((vc, msg))
                if vi < len(_need_yf_check) - 1:
                    time.sleep(2)

            if not _need_yf_check:
                progress_bar.progress(0.1)

            if invalid_codes:
                status_container.empty()
                progress_bar.empty()
                progress_text.empty()
                for inv_code, inv_msg in invalid_codes:
                    st.error(f"❌ {inv_msg}")
                st.warning("無効な証券コードを修正してから再度実行してください。")
                st.stop()

            _valid_set = set(valid_codes)
            codes = [c for c in codes if c in _valid_set]
            status_container.success(f"✅ {len(codes)}社の証券コードを確認しました。")

            # Step 1: EDINET一括検索
            edinet_results = {}
            if edinet_available:
                cached_codes = []
                uncached_codes = []
                if use_cache:
                    for c in codes:
                        meta = load_cached_meta(c)
                        if meta and meta.get("docs") is not None:
                            cached_codes.append(c)
                        else:
                            uncached_codes.append(c)
                else:
                    uncached_codes = list(codes)

                if cached_codes and not uncached_codes:
                    status_container.info(f"📄 EDINET: {len(cached_codes)}社すべてキャッシュから読み込み")
                elif cached_codes:
                    status_container.info(f"📄 EDINET: {len(cached_codes)}社はキャッシュから読み込み、{len(uncached_codes)}社をAPI検索中...")
                else:
                    status_container.info(f"📄 EDINET: {len(codes)}社分を一括検索中（{search_days}日間）...")

                def edinet_progress(current, total):
                    progress_bar.progress(0.1 + current / total * 0.55)
                    progress_text.text(f"  📄 EDINET検索: {current}/{total}日")

                try:
                    edinet_results = fetch_companies_batch(
                        codes, days=search_days, progress_callback=edinet_progress,
                        use_cache=use_cache
                    )
                    if cached_codes and not uncached_codes:
                        progress_bar.progress(0.65)
                except Exception as e:
                    st.session_state.errors.append(f"EDINET一括検索エラー: {e}")
            else:
                st.session_state.errors.append("EDINET: API Key未設定（スキップ）")

            # Step 2: 各社ごとに株価・計算
            results = []
            for i, code in enumerate(codes):
                result = {
                    'code': code, 'status': 'processing', 'errors': [],
                    'data': None, 'edinet_raw': None, 'stock_raw': None,
                }

                edinet_data = edinet_results.get(code, {
                    'company_name': '', 'yuho_data': {}, 'hanki_data': {},
                    'yuho_doc': None, 'hanki_doc': None,
                })
                result['edinet_raw'] = edinet_data if edinet_data.get('yuho_data') or edinet_data.get('hanki_data') else None

                tdnet_data = {'forecast': {}}

                base_progress = 0.65 + (i / len(codes)) * 0.30
                progress_bar.progress(min(base_progress, 0.95))
                _stock_cached = use_cache and _load_stock_cache(code) is not None
                if _stock_cached:
                    status_container.info(f"📈 株価: {code} キャッシュから読み込み ({i+1}/{len(codes)})")
                    progress_text.text(f"  📈 {code}: キャッシュ読み込み...")
                else:
                    status_container.info(f"📈 株価: {code} ({i+1}/{len(codes)})")
                    progress_text.text(f"  📈 {code}: yfinanceから取得中...")
                stock_data = {'stock_price': None, 'shares_outstanding': None, 'market_cap': None}
                try:
                    stock_data = fetch_stock_info(code, use_cache=use_cache)
                    result['stock_raw'] = stock_data
                except Exception as e:
                    result['errors'].append(f"株価: {e}")

                progress_text.text(f"  🔢 {code}: 計算中...")
                try:
                    company = build_company_data(code, edinet_data, tdnet_data, stock_data)
                    if not company.get('name') and stock_data.get('company_name_en'):
                        company['name'] = stock_data['company_name_en']
                    result['data'] = company
                    result['status'] = 'done'
                except Exception as e:
                    result['errors'].append(f"計算: {e}")
                    result['status'] = 'error'

                if result['errors']:
                    for err in result['errors']:
                        st.session_state.errors.append(f"{code}: {err}")
                results.append(result)

                if not _stock_cached and i < len(codes) - 1:
                    progress_text.text("  ⏳ レート制限回避のため待機中...")
                    time.sleep(3)

            progress_bar.progress(1.0)
            status_container.success(f"✅ 完了: {len(codes)}社の処理が終わりました。")
            progress_text.empty()

        st.session_state.company_data = results
        st.session_state.generation_done = True

# ---------------------------------------------------------------------------
# Display Results
# ---------------------------------------------------------------------------

if st.session_state.generation_done:
    try:
        st.divider()
        st.subheader(f"取得結果: {len(st.session_state.company_data)}社処理済み / エラー: {len(st.session_state.errors)}件")

        if st.session_state.errors:
            with st.expander("⚠️ エラー・警告", expanded=True):
                for err in st.session_state.errors:
                    st.warning(err)

        # デバッグ情報
        with st.expander("🔍 デバッグ: 取得データ詳細", expanded=False):
            for r in st.session_state.company_data:
                code = r.get('code', '?')
                st.markdown(f"**{code}** (status: {r.get('status', '?')})")
                if r.get('edinet_raw'):
                    ed = r['edinet_raw']
                    st.json({
                        'company_name': ed.get('company_name', ''),
                        'yuho_data': ed.get('yuho_data', {}),
                        'hanki_data': ed.get('hanki_data', {}),
                        'hanki_prior_data': ed.get('hanki_prior_data', {}),
                        'yuho_doc_id': ed.get('yuho_doc', {}).get('docID') if ed.get('yuho_doc') else None,
                        'hanki_doc_id': ed.get('hanki_doc', {}).get('docID') if ed.get('hanki_doc') else None,
                        '_debug': ed.get('_debug', {}),
                    })
                else:
                    st.text("EDINET: データなし")
                if r.get('stock_raw'):
                    st.json(r['stock_raw'])
                else:
                    st.text("株価: データなし")
                st.divider()

        companies_for_config = []
        for r in st.session_state.company_data:
            if r.get('data'):
                companies_for_config.append(r['data'])

        if companies_for_config:
            st.subheader("取得データ一覧")

            summary_rows = []
            for c in companies_for_config:
                multiples = c.get('_multiples') or {}
                # PERを決算短信の予想純利益で再計算（アップロード後に反映）
                tanshin = st.session_state.get('tanshin_forecasts', {}).get(c.get('code', ''), {})
                ni_fwd = tanshin.get('ni_forecast') or c.get('ni_forecast')
                mcap = c.get('market_cap')
                stock_px = c.get('stock_price')
                per_fwd = mcap / ni_fwd if mcap and ni_fwd and ni_fwd > 0 else None
                # 配当利回り: 有報実績DPS / 現在株価
                dps_actual = c.get('dps')
                div_yield = (dps_actual / stock_px * 100) if dps_actual and stock_px and stock_px > 0 else None
                ev = c.get('_ev') or (multiples.get('ev') if multiples else None)
                # EVが_multiplesにない場合は手動計算
                if ev is None and mcap is not None:
                    _cash = c.get('cash') or 0
                    _debt = c.get('total_debt') or 0
                    ev = mcap + _debt - _cash
                summary_rows.append({
                    'コード': c.get('code', ''),
                    '企業名': c.get('name', ''),
                    '株価（円）': int(stock_px) if stock_px else None,
                    '時価総額（百万円）': mcap,
                    'EV（百万円）': ev,
                    '売上高LTM（百万円）': c.get('rev_ltm'),
                    '営業利益LTM（百万円）': c.get('op_ltm'),
                    'EBITDA LTM（百万円）': c.get('ebitda_ltm'),
                    'EV/EBITDA LTM': multiples.get('ev_ebitda_ltm'),
                    'Forward PER': per_fwd,
                    '直近四半期PBR': multiples.get('pbr'),
                    '配当利回り': div_yield,
                })

            df_summary = pd.DataFrame(summary_rows)

            # 数値列をfloat型に統一
            _num_cols = ['株価（円）', '時価総額（百万円）', 'EV（百万円）',
                         '売上高LTM（百万円）', '営業利益LTM（百万円）', 'EBITDA LTM（百万円）',
                         'EV/EBITDA LTM', 'Forward PER', '直近四半期PBR', '配当利回り']
            for col in _num_cols:
                df_summary[col] = pd.to_numeric(df_summary[col], errors='coerce')

            # フォーマット関数
            def _fmt_int(v):
                return f"{int(v):,}" if pd.notna(v) else "—"
            def _fmt_1f_x(v):
                return f"{v:.1f}x" if pd.notna(v) else "—"
            def _fmt_2f_x(v):
                return f"{v:.2f}x" if pd.notna(v) else "—"
            def _fmt_pct(v):
                return f"{v:.1f}%" if pd.notna(v) else "—"
            _fmt_map = {
                '株価（円）': _fmt_int, '時価総額（百万円）': _fmt_int,
                'EV（百万円）': _fmt_int, '売上高LTM（百万円）': _fmt_int,
                '営業利益LTM（百万円）': _fmt_int, 'EBITDA LTM（百万円）': _fmt_int,
                'EV/EBITDA LTM': _fmt_1f_x, 'Forward PER': _fmt_1f_x,
                '直近四半期PBR': _fmt_2f_x, '配当利回り': _fmt_pct,
            }
            _right_cols = set(_num_cols)

            # 株価取得日を取得（最初の企業のキャッシュから）
            _stock_date = ""
            for _sc in companies_for_config:
                _sc_cache = _load_stock_cache(_sc.get('code', ''))
                if _sc_cache and _sc_cache.get('_fetched_date'):
                    _sd = _sc_cache['_fetched_date']  # "2026-02-26"
                    _stock_date = _sd.replace('-', '/')
                    break

            # 列ヘッダー表示名（2行目に単位・日付を折り返し）
            _col_display = {
                'コード': 'コード',
                '企業名': '企業名',
                '株価（円）': f'株価（円）<br><span class="sub">{_stock_date}</span>' if _stock_date else '株価<br><span class="sub">（円）</span>',
                '時価総額（百万円）': '時価総額<br><span class="sub">（百万円）</span>',
                'EV（百万円）': 'EV<br><span class="sub">（百万円）</span>',
                '売上高LTM（百万円）': '売上高LTM<br><span class="sub">（百万円）</span>',
                '営業利益LTM（百万円）': '営業利益LTM<br><span class="sub">（百万円）</span>',
                'EBITDA LTM（百万円）': 'EBITDA LTM<br><span class="sub">（百万円）</span>',
                'EV/EBITDA LTM': 'EV/EBITDA<br><span class="sub">LTM</span>',
                'Forward PER': 'PER<br><span class="sub">Forward</span>',
                '直近四半期PBR': 'PBR<br><span class="sub">直近四半期末</span>',
                '配当利回り': '配当利回り',
            }

            # テーブルデータをJSON化（ソート用に生数値も保持）
            import json as _tbl_json
            _tbl_rows = []
            for _, row in df_summary.iterrows():
                _r = {}
                for col in df_summary.columns:
                    raw = row[col]
                    fmt_fn = _fmt_map.get(col)
                    _r[col] = {
                        'raw': float(raw) if pd.notna(raw) and col in _right_cols else (raw if pd.notna(raw) else None),
                        'display': fmt_fn(raw) if fmt_fn else (str(raw) if pd.notna(raw) else '—'),
                    }
                _tbl_rows.append(_r)
            _cols_json = _tbl_json.dumps(list(df_summary.columns), ensure_ascii=False)
            _rows_json = _tbl_json.dumps(_tbl_rows, ensure_ascii=False)
            _right_json = _tbl_json.dumps(list(_right_cols), ensure_ascii=False)
            _display_json = _tbl_json.dumps(_col_display, ensure_ascii=False)

            # JavaScript付きHTMLテーブル（列ヘッダークリックでソート）
            _table_height = 95 + len(df_summary) * 38
            import streamlit.components.v1 as components
            components.html(f"""
<style>
  body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size:14px; }}
  .table-wrap {{ overflow-x:auto; width:100%; }}
  .table-wrap::-webkit-scrollbar {{ height:10px; }}
  .table-wrap::-webkit-scrollbar-track {{ background:#f0f0f0; border-radius:5px; }}
  .table-wrap::-webkit-scrollbar-thumb {{ background:#aaa; border-radius:5px; }}
  .table-wrap::-webkit-scrollbar-thumb:hover {{ background:#888; }}
  .table-wrap {{ scrollbar-width:auto; scrollbar-color:#aaa #f0f0f0; }}
  table {{ border-collapse:collapse; min-width:100%; }}
  th {{ padding:8px 12px; border-bottom:2px solid #ddd; cursor:pointer; user-select:none; background:#fafafa; position:sticky; top:0; vertical-align:bottom; line-height:1.4; }}
  th:hover {{ background:#f0f0f0; }}
  td {{ padding:6px 12px; border-bottom:1px solid #eee; white-space:nowrap; }}
  tr:hover {{ background:#f8f8ff; }}
  .sort-arrow {{ font-size:10px; margin-left:4px; color:#999; }}
  .sort-arrow.active {{ color:#333; }}
  .sub {{ font-size:11px; color:#888; font-weight:normal; }}
</style>
<div class="table-wrap">
<table id="comps-table">
  <thead><tr id="header-row"></tr></thead>
  <tbody id="table-body"></tbody>
</table>
</div>
<script>
const cols = {_cols_json};
const rows = {_rows_json};
const rightCols = new Set({_right_json});
const colDisplay = {_display_json};
let sortCol = null;
let sortAsc = true;

function render() {{
  const sorted = [...rows];
  if (sortCol !== null) {{
    sorted.sort((a, b) => {{
      const av = a[sortCol].raw;
      const bv = b[sortCol].raw;
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      return sortAsc ? (av < bv ? -1 : av > bv ? 1 : 0) : (av > bv ? -1 : av < bv ? 1 : 0);
    }});
  }}
  // Header
  const hr = document.getElementById('header-row');
  hr.innerHTML = '';
  cols.forEach(col => {{
    const th = document.createElement('th');
    const align = rightCols.has(col) ? 'right' : 'left';
    th.style.textAlign = align;
    let arrow = '';
    if (sortCol === col) {{
      arrow = sortAsc ? ' <span class="sort-arrow active">▲</span>' : ' <span class="sort-arrow active">▼</span>';
    }} else {{
      arrow = ' <span class="sort-arrow">▲▼</span>';
    }}
    const displayName = colDisplay[col] || col;
    th.innerHTML = displayName + arrow;
    th.onclick = () => {{
      if (sortCol === col) {{ sortAsc = !sortAsc; }}
      else {{ sortCol = col; sortAsc = true; }}
      render();
    }};
    hr.appendChild(th);
  }});
  // Body
  const tb = document.getElementById('table-body');
  tb.innerHTML = '';
  sorted.forEach(row => {{
    const tr = document.createElement('tr');
    cols.forEach(col => {{
      const td = document.createElement('td');
      td.style.textAlign = rightCols.has(col) ? 'right' : 'left';
      td.textContent = row[col].display;
      tr.appendChild(td);
    }});
    tb.appendChild(tr);
  }});
}}
render();
</script>
""", height=_table_height)

            # --- 決算短信セクション ---
            st.subheader("📄 決算短信")

            candidate_codes = [c.get('code', '') for c in companies_for_config if c.get('code')]
            code_name_map = {c.get('code', ''): c.get('name', '') for c in companies_for_config}

            # --- Step 1: 保存済みPDFの自動パース ---
            from pathlib import Path
            _tanshin_base = Path(__file__).parent / "data" / "tanshin"
            _existing_files = []
            for code in candidate_codes:
                code_dir = _tanshin_base / code
                if not code_dir.is_dir():
                    continue
                for pdf_path in sorted(code_dir.glob("tanshin_*.pdf"), reverse=True):
                    parts = pdf_path.stem.split('_')
                    if len(parts) >= 3:
                        _ptype_ja = {'FY': '通期', 'Q1': 'Q1', 'Q2': 'Q2', 'Q3': 'Q3'}
                        _existing_files.append({
                            '企業': f"{code} {code_name_map.get(code, '')}",
                            '期間': parts[1].replace('-', '/') + ' ' + _ptype_ja.get(parts[2], parts[2]),
                            'ファイル': pdf_path.name,
                        })
                if code not in st.session_state.tanshin_forecasts:
                    for pdf_path in sorted(code_dir.glob("tanshin_*.pdf"), reverse=True):
                        parsed = parse_tanshin_pdf(pdf_path.read_bytes())
                        if parsed:
                            st.session_state.tanshin_forecasts[code] = parsed
                            _save_forecasts_cache(st.session_state.tanshin_forecasts)
                            break

            # --- Step 2: アップロードファイルの処理（不足チェックの前に実行） ---
            uploaded_files = st.file_uploader(
                "決算短信PDFをまとめてアップロード",
                type=['pdf'],
                accept_multiple_files=True,
                key="tanshin_bulk",
            )

            if uploaded_files:
                id_results = []
                for uf in uploaded_files:
                    pdf_bytes = uf.read()
                    uf.seek(0)
                    identification = identify_tanshin_pdf(pdf_bytes, candidate_codes)
                    id_results.append({
                        'file': uf,
                        'pdf_bytes': pdf_bytes,
                        'identification': identification,
                    })

                # 判定結果テーブル
                _period_type_ja = {'FY': '通期', 'Q1': 'Q1', 'Q2': 'Q2', 'Q3': 'Q3'}
                table_rows = []
                for ir in id_results:
                    ident = ir['identification']
                    code = ident['code_4']
                    if code and ident['fy_end']:
                        company_display = f"{code} {code_name_map.get(code, ident['company_name'])}"
                        fy = ident['fy_end'].replace('-', '/')
                        period_display = _period_type_ja.get(ident['period_type'], ident['period_type']) + ' ' + fy
                        filename = ident['suggested_filename'] or '—'
                        if code not in candidate_codes:
                            status = "⚠️ 対象外の企業"
                        else:
                            status = "✅ OK"
                    elif code:
                        company_display = f"{code} {code_name_map.get(code, ident['company_name'])}"
                        period_display = "—（期間判定失敗）"
                        filename = '—'
                        status = "⚠️ 期間不明"
                    else:
                        company_display = "—"
                        period_display = "—"
                        filename = "—"
                        status = "❌ 判定失敗"
                    table_rows.append({
                        'ファイル': ir['file'].name,
                        '判定企業': company_display,
                        '期間': period_display,
                        '保存ファイル名': filename,
                        'ステータス': status,
                    })
                st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

                # 判定失敗の警告
                _fail_files = [ir['file'].name for ir in id_results if not ir['identification']['code_4']]
                if _fail_files:
                    st.warning(f"⚠️ {len(_fail_files)}件は企業を判定できませんでした: {', '.join(_fail_files)}")

                # 判定成功分を自動パース＆保存（session stateを即時更新）
                _parsed_count = 0
                _parse_failures = []
                for ir in id_results:
                    ident = ir['identification']
                    code = ident['code_4']
                    if code and code in candidate_codes:
                        parsed = parse_tanshin_pdf(ir['pdf_bytes'])
                        if parsed:
                            st.session_state.tanshin_forecasts[code] = parsed
                            _parsed_count += 1
                        else:
                            _parse_failures.append((code, code_name_map.get(code, ''), ir['file'].name, ir['pdf_bytes']))
                        save_name = ident['suggested_filename'] or ir['file'].name
                        try:
                            save_tanshin_pdf(ir['pdf_bytes'], code, save_name)
                        except Exception:
                            pass
                if _parsed_count:
                    _save_forecasts_cache(st.session_state.tanshin_forecasts)
                    st.success(f"✅ {_parsed_count}件の業績予想を抽出しました。")
                for _pf_code, _pf_name, _pf_file, _pf_bytes in _parse_failures:
                    st.warning(
                        f"**{_pf_code} {_pf_name}**（{_pf_file}）: "
                        f"通期業績予想の記載が見つかりませんでした。下の手動補完セクションから入力してください。"
                    )
                    # デバッグ: PDFテキストの冒頭を表示
                    try:
                        import fitz
                        doc = fitz.open(stream=_pf_bytes, filetype="pdf")
                        _debug_text = ""
                        for _pg in range(min(3, len(doc))):
                            _debug_text += doc[_pg].get_text() + "\n"
                        doc.close()
                        with st.expander(f"🔍 デバッグ: {_pf_file} のテキスト（先頭2000文字）"):
                            st.text(_debug_text[:2000])
                    except Exception:
                        pass

            # --- Step 3: 不足データの自動検出（アップロード処理の後に実行） ---
            # DPS（配当利回り用）は有報から自動取得されるため、ここではPER用のni_forecastのみチェック
            _critical_missing = []   # PER計算不可（ni_forecast なし）
            for comp in companies_for_config:
                code = comp.get('code', '')
                if not code:
                    continue
                tanshin = st.session_state.get('tanshin_forecasts', {}).get(code, {})
                ni_forecast = tanshin.get('ni_forecast') or comp.get('ni_forecast')

                # 決算期を特定: EDINETの半期報periodEndから推定
                _fy_label = ""
                for r in st.session_state.company_data:
                    if r.get('code') == code and r.get('edinet_raw'):
                        hd = r['edinet_raw'].get('hanki_doc')
                        if hd and hd.get('periodEnd'):
                            pe = hd['periodEnd']
                            pe_clean = pe.replace('/', '-')
                            parts = pe_clean.split('-')
                            if len(parts) >= 2:
                                _fy_label = f"{parts[0]}年{int(parts[1])}月期"
                        break

                # 最新の四半期を推定（決算月と現在日付から）
                _quarter_hint = ""
                if _fy_label:
                    month = datetime.today().month
                    if month in (1, 2, 3):
                        _quarter_hint = "第3四半期"
                    elif month in (4, 5):
                        _quarter_hint = "通期"
                    elif month in (6, 7, 8):
                        _quarter_hint = "第1四半期"
                    else:
                        _quarter_hint = "第2四半期"

                suggestion = _fy_label
                if _quarter_hint:
                    suggestion += f" {_quarter_hint}決算短信"
                if not suggestion:
                    suggestion = "最新の決算短信"

                if not ni_forecast:
                    _critical_missing.append({
                        'code': code,
                        'name': code_name_map.get(code, ''),
                        'suggestion': suggestion,
                    })

            # --- Step 4: 不足状況の表示 ---
            if _critical_missing:
                st.error(f"❌ {len(_critical_missing)}/{len(candidate_codes)}社の業績予想が不足しています。")
                for mi in _critical_missing:
                    st.warning(
                        f"**{mi['code']} {mi['name']}**: PER（通期予想当期純利益が必要）が計算できません。\n\n"
                        f"通期の業績予想が掲載されている **{mi['suggestion']}** をアップロードしてください。"
                    )
            else:
                st.success(f"✅ 全{len(candidate_codes)}社の業績予想データが揃っています。")

            if _existing_files:
                with st.expander(f"📁 保存済み決算短信（{len(_existing_files)}件）", expanded=False):
                    st.dataframe(pd.DataFrame(_existing_files), use_container_width=True, hide_index=True)

            # --- 手動補完セクション ---
            st.subheader("手動データ補完")
            st.caption("自動取得できなかった項目を手動で入力・修正できます。金額単位: 百万円 / DPS: 円")

            edited_companies = []
            tabs = st.tabs([f"{c.get('code', '')} {c.get('name', '')}" for c in companies_for_config])

            for idx, (tab, company) in enumerate(zip(tabs, companies_for_config)):
                with tab:
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        st.markdown("**基本情報**")
                        name = st.text_input("企業名", value=company.get('name', ''), key=f"name_{idx}")
                        sector = st.text_input("セクター", value=company.get('sector', ''), key=f"sector_{idx}")
                        accounting = st.selectbox("会計基準",
                                                  ["J-GAAP", "IFRS", "US-GAAP"],
                                                  index=0, key=f"acc_{idx}")
                        fy_end = st.text_input("決算月", value=company.get('fy_end', 'Mar'), key=f"fy_{idx}")
                        st.markdown("**株価・株式**")
                        stock_price = st.number_input("株価（円）", value=float(company.get('stock_price') or 0),
                                                      key=f"price_{idx}", step=1.0, format="%.0f")
                        shares = st.number_input("発行済株式数（千株）", value=int(company.get('shares_outstanding') or 0),
                                                 key=f"shares_{idx}", step=1, format="%d")

                    with col2:
                        st.markdown("**P&L - LTM（百万円）**")
                        rev = st.number_input("売上高（百万円）", value=int(company.get('rev_ltm') or 0),
                                              key=f"rev_{idx}", step=1, format="%d")
                        op = st.number_input("営業利益（百万円）", value=int(company.get('op_ltm') or 0),
                                             key=f"op_{idx}", step=1, format="%d")
                        ni = st.number_input("純利益（百万円）", value=int(company.get('ni_ltm') or 0),
                                             key=f"ni_{idx}", step=1, format="%d")
                        da = st.number_input("減価償却費（百万円）", value=int(company.get('da_ltm') or 0),
                                             key=f"da_{idx}", step=1, format="%d")
                        ebitda = st.number_input("EBITDA（百万円）", value=int(company.get('ebitda_ltm') or 0),
                                                 key=f"ebitda_{idx}", step=1, format="%d")

                    # 決算短信の抽出値があれば予想値・DPSにプリフィル
                    tanshin = st.session_state.get('tanshin_forecasts', {}).get(company.get('code', ''), {})
                    _rev_e_default = int(tanshin.get('rev_forecast') or company.get('rev_forecast') or 0)
                    _op_e_default = int(tanshin.get('op_forecast') or company.get('op_forecast') or 0)
                    _ni_e_default = int(tanshin.get('ni_forecast') or company.get('ni_forecast') or 0)
                    _ebitda_e_default = int(company.get('ebitda_forecast') or 0)
                    _dps_default = float(company.get('dps') or 0)  # 有報記載の実績配当

                    with col3:
                        st.markdown("**BS（百万円）**")
                        cash = st.number_input("現金及び預金（百万円）", value=int(company.get('cash') or 0),
                                               key=f"cash_{idx}", step=1, format="%d")
                        debt = st.number_input("有利子負債（百万円）", value=int(company.get('total_debt') or 0),
                                               key=f"debt_{idx}", step=1, format="%d")
                        eq = st.number_input("純資産（百万円）", value=int(company.get('equity_parent') or 0),
                                             key=f"eq_{idx}", step=1, format="%d")
                        dps = st.number_input("DPS - 実績配当（円・有報）", value=_dps_default,
                                              key=f"dps_{idx}", step=1.0, format="%.1f")

                    col4, col5 = st.columns(2)
                    with col4:
                        st.markdown("**予想値 - FY E（百万円）**")
                        if tanshin:
                            st.caption("📄 決算短信から自動プリフィル済み")
                        rev_e = st.number_input("売上高予想（百万円）", value=_rev_e_default,
                                                key=f"reve_{idx}", step=1, format="%d")
                        op_e = st.number_input("営業利益予想（百万円）", value=_op_e_default,
                                               key=f"ope_{idx}", step=1, format="%d")
                        ni_e = st.number_input("純利益予想（百万円）", value=_ni_e_default,
                                               key=f"nie_{idx}", step=1, format="%d")
                        ebitda_e = st.number_input("EBITDA予想（百万円）", value=_ebitda_e_default,
                                                   key=f"ebitdae_{idx}", step=1, format="%d")

                    # --- 時価総額・EV・マルチプル自動計算 ---
                    mcap = int(stock_price * shares / 1000) if stock_price and shares else 0
                    ev = mcap + (debt or 0) - (cash or 0) if mcap else 0

                    with col5:
                        st.markdown("**自動計算値**")
                        st.metric("時価総額（百万円）", f"{mcap:,}" if mcap else "N/A")
                        st.metric("EV（百万円）", f"{ev:,}" if ev else "N/A")
                        if ebitda and ebitda > 0 and ev > 0:
                            st.metric("EV/EBITDA (LTM)", f"{ev / ebitda:.1f}x")
                        else:
                            st.metric("EV/EBITDA (LTM)", "N/A")
                        if ni_e and ni_e > 0 and mcap > 0:
                            st.metric("PER (FY E)", f"{mcap / ni_e:.1f}x")
                        else:
                            st.metric("PER (FY E)", "N/A")
                        if eq and eq > 0 and mcap > 0:
                            st.metric("PBR", f"{mcap / eq:.2f}x")
                        else:
                            st.metric("PBR", "N/A")

                    edited = dict(company)
                    edited['name'] = name
                    edited['sector'] = sector
                    edited['accounting'] = accounting
                    edited['fy_end'] = fy_end
                    edited['stock_price'] = stock_price if stock_price != 0 else None
                    edited['shares_outstanding'] = shares if shares != 0 else None
                    edited['market_cap'] = mcap if mcap != 0 else None
                    edited['rev_ltm'] = rev if rev != 0 else None
                    edited['op_ltm'] = op if op != 0 else None
                    edited['ni_ltm'] = ni if ni != 0 else None
                    edited['da_ltm'] = da if da != 0 else None
                    edited['ebitda_ltm'] = ebitda if ebitda != 0 else None
                    edited['cash'] = cash if cash != 0 else None
                    edited['total_debt'] = debt if debt != 0 else None
                    edited['equity_parent'] = eq if eq != 0 else None
                    edited['dps'] = dps if dps != 0 else None
                    edited['rev_forecast'] = rev_e if rev_e != 0 else None
                    edited['op_forecast'] = op_e if op_e != 0 else None
                    edited['ni_forecast'] = ni_e if ni_e != 0 else None
                    edited['ebitda_forecast'] = ebitda_e if ebitda_e != 0 else None
                    edited.pop('_ev', None)
                    edited.pop('_multiples', None)
                    edited_companies.append(edited)

            # --- Excel生成 ---
            st.divider()
            st.subheader("Excel出力")

            col_dl1, col_dl2 = st.columns(2)
            with col_dl1:
                title = st.text_input("タイトル", value="Comparable Company Analysis (Comps)")
            with col_dl2:
                date_str = st.text_input("日付", value=datetime.today().strftime("%Y/%m/%d"))

            notes = st.text_area("ノート（1行1項目）",
                                 value="Source: EDINET, Yahoo Finance\n"
                                       "Unit: JPY millions\n"
                                       "LTM = Last Twelve Months")

            if st.button("📥 Excelファイルを生成・ダウンロード", type="primary"):
                config = {
                    'title': title,
                    'date': date_str,
                    'currency': 'JPY',
                    'unit': 'millions',
                    'companies': edited_companies,
                    'notes': [n.strip() for n in notes.split('\n') if n.strip()],
                }

                import tempfile
                with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tmp:
                    tmp_path = tmp.name

                try:
                    generate_comps(config, tmp_path)
                    with open(tmp_path, 'rb') as f:
                        excel_bytes = f.read()

                    st.download_button(
                        label="📥 Comps_Table.xlsx をダウンロード",
                        data=excel_bytes,
                        file_name=f"Comps_Table_{datetime.today().strftime('%Y%m%d')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                    st.success("Excel生成完了！")
                except Exception as e:
                    st.error(f"Excel生成エラー: {e}")
                    st.code(traceback.format_exc())
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        else:
            st.warning("データが取得できた企業がありません。証券コードと設定を確認してください。")

    except Exception as e:
        st.error(f"結果表示エラー: {e}")
        st.code(traceback.format_exc())

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption("くじらキャピタル株式会社 | Comps自動生成ツール v1.1 (build 3d33357)")
