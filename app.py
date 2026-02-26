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
        # EDINET API Key チェック（なくても株価取得は可能）
        edinet_available = False
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

        # ---- Step 0: 証券コード存在チェック ----
        # 既にPDF格納済みの企業はスキップ（yfinance不要）
        from pathlib import Path
        _tanshin_base = Path(__file__).parent / "data" / "tanshin"

        status_container.info("🔍 証券コードを検証中...")
        invalid_codes = []
        valid_codes = []
        _need_yf_check = []
        for vc in codes:
            local_dir = _tanshin_base / vc
            if local_dir.is_dir() and any(local_dir.iterdir()):
                valid_codes.append(vc)  # PDF格納済み → 検証不要
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
            # yfinanceレート制限回避
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

        # 元の入力順序を維持
        _valid_set = set(valid_codes)
        codes = [c for c in codes if c in _valid_set]
        status_container.success(f"✅ {len(codes)}社の証券コードを確認しました。")

        # ---- Step 1: EDINET一括検索（全社まとめて1回の日付ループ） ----
        edinet_results = {}  # code_4 -> edinet_data
        if edinet_available:
            # キャッシュ状況を確認
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

            if cached_codes:
                status_container.info(f"📄 EDINET: {len(cached_codes)}社はキャッシュから読み込み、{len(uncached_codes)}社をAPI検索中...")
            else:
                status_container.info(f"📄 EDINET: {len(codes)}社分を一括検索中（{search_days}日間）...")

            def edinet_progress(current, total):
                progress_bar.progress(0.1 + current / total * 0.55)  # 10-65%をEDINETに割り当て
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

        # ---- Step 2: 各社ごとに株価・計算 ----
        results = []
        for i, code in enumerate(codes):
            result = {
                'code': code,
                'status': 'processing',
                'errors': [],
                'data': None,
                'edinet_raw': None,
                'stock_raw': None,
            }

            # EDINET結果を取得
            edinet_data = edinet_results.get(code, {
                'company_name': '',
                'yuho_data': {},
                'hanki_data': {},
                'yuho_doc': None,
                'hanki_doc': None,
            })
            result['edinet_raw'] = edinet_data if edinet_data.get('yuho_data') or edinet_data.get('hanki_data') else None

            tdnet_data = {'forecast': {}}

            # 株価
            base_progress = 0.65 + (i / len(codes)) * 0.30
            progress_bar.progress(min(base_progress, 0.95))
            status_container.info(f"📈 株価: {code} ({i+1}/{len(codes)})")
            progress_text.text(f"  📈 {code}: 株価取得中...")
            stock_data = {'stock_price': None, 'shares_outstanding': None, 'market_cap': None}
            try:
                stock_data = fetch_stock_info(code)
                result['stock_raw'] = stock_data
            except Exception as e:
                result['errors'].append(f"株価: {e}")

            # 計算
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

            # yfinance レート制限回避: 各社の間に3秒待機
            if i < len(codes) - 1:
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
                summary_rows.append({
                    'コード': c.get('code', ''),
                    '企業名': c.get('name', ''),
                    '株価': c.get('stock_price'),
                    '時価総額(百万)': c.get('market_cap'),
                    '売上高LTM': c.get('rev_ltm'),
                    '営業利益LTM': c.get('op_ltm'),
                    'EBITDA LTM': c.get('ebitda_ltm'),
                    'EV/EBITDA': f"{multiples['ev_ebitda_ltm']:.1f}x" if multiples.get('ev_ebitda_ltm') else 'N/A',
                    'PER': f"{multiples['per_fwd']:.1f}x" if multiples.get('per_fwd') else 'N/A',
                    'PBR': f"{multiples['pbr']:.2f}x" if multiples.get('pbr') else 'N/A',
                })

            df_summary = pd.DataFrame(summary_rows)
            st.dataframe(df_summary, use_container_width=True, hide_index=True)

            # --- 決算短信一括アップロードセクション ---
            st.subheader("📄 決算短信アップロード（任意）")
            st.caption("決算短信PDFをまとめてアップロードすると、PDF内容から会社・期間を自動判定し、業績予想値をプリフィルします。")

            if 'tanshin_forecasts' not in st.session_state:
                st.session_state.tanshin_forecasts = {}

            candidate_codes = [c.get('code', '') for c in companies_for_config if c.get('code')]
            # code→企業名のマップ
            code_name_map = {c.get('code', ''): c.get('name', '') for c in companies_for_config}

            # --- 必要な決算短信の指定 ---
            st.markdown("**必要な決算短信（全社共通）**")
            _cur_year = datetime.today().year
            _period_options = []
            for _y in range(_cur_year - 1, _cur_year + 2):
                _fy = f"{_y}-03"
                for _pt in ["通期", "Q1", "Q2", "Q3"]:
                    _period_options.append(f"{_fy} {_pt}")

            required_periods_raw = st.multiselect(
                "各社に必要な期間を選択してください",
                options=_period_options,
                default=[f"{_cur_year}-03 通期", f"{_cur_year + 1}-03 Q2"],
                key="required_periods",
                help="選択した期間 × 全企業 の決算短信が必要と判定します。3月期以外の企業がある場合は手動で調整してください。",
            )

            # "2025-03 通期" → ("2025-03", "FY") にパース
            required_periods = []
            for rp in required_periods_raw:
                parts = rp.split(' ', 1)
                ptype = 'FY' if parts[1] == '通期' else parts[1]
                required_periods.append((parts[0], ptype))

            # 期待される全ドキュメント: 企業 × 期間
            expected_set = set()
            for code in candidate_codes:
                for fy_end, ptype in required_periods:
                    expected_set.add((code, fy_end, ptype))

            # --- 保存済みPDFのスキャン ---
            # data/tanshin/{code}/ に既にあるPDFは「取得済み」として扱う
            from pathlib import Path
            _tanshin_base = Path(__file__).parent / "data" / "tanshin"
            _existing_set = set()  # (code, fy_end, period_type)
            _existing_files = []   # 表示用
            for code in candidate_codes:
                code_dir = _tanshin_base / code
                if not code_dir.is_dir():
                    continue
                for pdf_path in code_dir.glob("tanshin_*.pdf"):
                    # tanshin_2026-03_FY.pdf → fy_end=2026-03, period_type=FY
                    parts = pdf_path.stem.split('_')  # ['tanshin', '2026-03', 'FY']
                    if len(parts) >= 3:
                        fy_end = parts[1]
                        ptype = parts[2]
                        _existing_set.add((code, fy_end, ptype))
                        _existing_files.append({
                            '企業': f"{code} {code_name_map.get(code, '')}",
                            '期間': fy_end.replace('-', '/') + ' ' + {'FY': '通期', 'Q1': 'Q1', 'Q2': 'Q2', 'Q3': 'Q3'}.get(ptype, ptype),
                            'ファイル': pdf_path.name,
                        })

            if required_periods:
                _already_have = expected_set & _existing_set
                _still_need = expected_set - _existing_set
                st.caption(f"必要書類: {len(candidate_codes)}社 × {len(required_periods)}期間 = **{len(expected_set)}件**（保存済み: {len(_already_have)}件 / 未取得: {len(_still_need)}件）")

            if _existing_files:
                with st.expander(f"📁 保存済み決算短信（{len(_existing_files)}件）", expanded=False):
                    st.dataframe(pd.DataFrame(_existing_files), use_container_width=True, hide_index=True)

            # --- ファイルアップロード ---
            uploaded_files = st.file_uploader(
                "PDFファイルをまとめてアップロード（保存済み以外のものをアップロード）",
                type=['pdf'],
                accept_multiple_files=True,
                key="tanshin_bulk",
            )

            if uploaded_files:
                # 判定結果テーブル
                id_results = []
                for uf in uploaded_files:
                    pdf_bytes = uf.read()
                    uf.seek(0)  # reset for potential re-read
                    identification = identify_tanshin_pdf(pdf_bytes, candidate_codes)
                    id_results.append({
                        'file': uf,
                        'pdf_bytes': pdf_bytes,
                        'identification': identification,
                    })

                # アップロード済みセット（既存ローカルファイル + 今回アップロード分）
                uploaded_set = set(_existing_set)
                for ir in id_results:
                    ident = ir['identification']
                    if ident['code_4'] and ident['fy_end']:
                        uploaded_set.add((ident['code_4'], ident['fy_end'], ident['period_type']))

                # 判定結果をテーブル表示（ステータス列付き）
                _period_type_ja = {'FY': '通期', 'Q1': 'Q1', 'Q2': 'Q2', 'Q3': 'Q3'}
                table_rows = []
                for ir in id_results:
                    ident = ir['identification']
                    code = ident['code_4']
                    if code and ident['fy_end']:
                        company_display = f"{code} {code_name_map.get(code, ident['company_name'])}"
                        fy = ident['fy_end'].replace('-', '/')
                        period_display = f"{_period_type_ja.get(ident['period_type'], ident['period_type'])} {fy}"
                        filename = ident['suggested_filename'] or '—'
                        key = (code, ident['fy_end'], ident['period_type'])
                        if key in expected_set:
                            status = "✅ OK"
                        elif code not in candidate_codes:
                            status = "⚠️ 対象外の企業"
                        else:
                            status = "⚠️ 対象外の期間"
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

                # --- 過不足チェック ---
                if required_periods:
                    missing = expected_set - uploaded_set

                    # 対象外のアップロード（企業が対象外 or 期間が対象外）
                    unexpected = []
                    for ir in id_results:
                        ident = ir['identification']
                        code = ident['code_4']
                        if code is None:
                            unexpected.append({
                                'ファイル': ir['file'].name,
                                '理由': '企業・期間を判定できませんでした',
                            })
                        elif code and ident['fy_end']:
                            key = (code, ident['fy_end'], ident['period_type'])
                            if key not in expected_set:
                                fy = ident['fy_end'].replace('-', '/')
                                pt_ja = _period_type_ja.get(ident['period_type'], ident['period_type'])
                                if code not in candidate_codes:
                                    reason = f"対象外の企業です（{code}）"
                                else:
                                    reason = f"必要な期間に含まれていません（{fy} {pt_ja}）"
                                unexpected.append({
                                    'ファイル': ir['file'].name,
                                    '理由': reason,
                                })

                    if not missing and not unexpected:
                        st.success(f"✅ 全{len(expected_set)}件の決算短信が揃っています。")
                    else:
                        if missing:
                            st.error(f"❌ 不足: {len(missing)}件の決算短信がアップロードされていません。")
                            missing_rows = []
                            for code, fy_end, ptype in sorted(missing):
                                fy = fy_end.replace('-', '/')
                                pt_ja = _period_type_ja.get(ptype, ptype)
                                missing_rows.append({
                                    '企業': f"{code} {code_name_map.get(code, '')}",
                                    '必要な期間': f"{fy} {pt_ja}",
                                    'ファイル名（期待）': f"tanshin_{fy_end}_{ptype}.pdf",
                                })
                            st.dataframe(pd.DataFrame(missing_rows), use_container_width=True, hide_index=True)

                        if unexpected:
                            st.warning(f"⚠️ 対象外: {len(unexpected)}件のファイルは必要な書類に該当しません。")
                            st.dataframe(pd.DataFrame(unexpected), use_container_width=True, hide_index=True)

                        matched = len(uploaded_set & expected_set)
                        st.info(f"📊 進捗: {matched}/{len(expected_set)}件 完了")

                # 判定成功分を自動パース＆保存
                for ir in id_results:
                    ident = ir['identification']
                    code = ident['code_4']
                    if code:
                        parsed = parse_tanshin_pdf(ir['pdf_bytes'])
                        if parsed:
                            st.session_state.tanshin_forecasts[code] = parsed
                        # EDINET命名規則に準拠したファイル名で保存
                        save_name = ident['suggested_filename'] or ir['file'].name
                        try:
                            save_tanshin_pdf(ir['pdf_bytes'], code, save_name)
                        except Exception:
                            pass  # 保存失敗は非致命的

            # --- 保存済みのみで過不足チェック（アップロードなしの場合） ---
            if not uploaded_files and required_periods:
                _period_type_ja = {'FY': '通期', 'Q1': 'Q1', 'Q2': 'Q2', 'Q3': 'Q3'}
                missing = expected_set - _existing_set
                if not missing:
                    st.success(f"✅ 全{len(expected_set)}件の決算短信が揃っています。追加アップロードは不要です。")
                else:
                    st.warning(f"❌ 不足: {len(missing)}件の決算短信がまだ保存されていません。上のアップローダーからアップロードしてください。")
                    missing_rows = []
                    for code, fy_end, ptype in sorted(missing):
                        fy = fy_end.replace('-', '/')
                        pt_ja = _period_type_ja.get(ptype, ptype)
                        missing_rows.append({
                            '企業': f"{code} {code_name_map.get(code, '')}",
                            '必要な期間': f"{fy} {pt_ja}",
                            'ファイル名（期待）': f"tanshin_{fy_end}_{ptype}.pdf",
                        })
                    st.dataframe(pd.DataFrame(missing_rows), use_container_width=True, hide_index=True)

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
                    _dps_default = float(tanshin.get('dps') or company.get('dps') or 0)

                    with col3:
                        st.markdown("**BS（百万円）**")
                        cash = st.number_input("現金及び預金（百万円）", value=int(company.get('cash') or 0),
                                               key=f"cash_{idx}", step=1, format="%d")
                        debt = st.number_input("有利子負債（百万円）", value=int(company.get('total_debt') or 0),
                                               key=f"debt_{idx}", step=1, format="%d")
                        eq = st.number_input("純資産（百万円）", value=int(company.get('equity_parent') or 0),
                                             key=f"eq_{idx}", step=1, format="%d")
                        dps = st.number_input("DPS - 配当（円）", value=_dps_default,
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
st.caption("くじらキャピタル株式会社 | Comps自動生成ツール v1.0")
