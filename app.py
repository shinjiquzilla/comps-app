"""
Comps自動生成 Streamlit アプリ
==============================
証券コードを入力するだけで Comparable Company Analysis 表を自動生成。

データソース:
- EDINET API type=5 CSV: 有報・半期報告書の財務データ
- TDnet スクレイピング + PyMuPDF: 決算短信の業績予想
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

from edinet_client import fetch_company_financials, load_api_key
from tdnet_client import fetch_tanshin_forecasts
from stock_fetcher import fetch_stock_info
from financial_calc import build_company_data
from comps_generator import generate_comps
from auth import is_authenticated, show_login_page, logout, get_current_user

# ---------------------------------------------------------------------------
# Auth Gate - set_page_config はここで1回だけ呼ぶ
# ---------------------------------------------------------------------------

# 認証が必要かどうか（secrets に supabase 設定があれば認証ON）
AUTH_ENABLED = "supabase" in st.secrets if hasattr(st, 'secrets') else False

if AUTH_ENABLED and not is_authenticated():
    # show_login_page() 内で set_page_config を呼ぶ
    show_login_page()
    st.stop()

# 認証不要 or 認証済み → メインUI
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
    # ログイン情報表示 + ログアウト
    if AUTH_ENABLED and is_authenticated():
        user = get_current_user()
        st.markdown(f"👤 **{user['email']}**")
        if st.button("ログアウト"):
            logout()
            st.rerun()
        st.divider()

    st.header("設定")

    # EDINET API Key（secrets にあればそちらを優先）
    default_edinet_key = ""
    if hasattr(st, 'secrets') and "edinet" in st.secrets:
        default_edinet_key = st.secrets["edinet"].get("api_key", "")
        os.environ["EDINET_API_KEY"] = default_edinet_key

    if not default_edinet_key:
        api_key_input = st.text_input(
            "EDINET API Key",
            type="password",
            help="https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1 から取得",
        )
        if api_key_input:
            os.environ["EDINET_API_KEY"] = api_key_input

    search_days = st.slider("検索期間（日数）", 60, 730, 400, step=30,
                            help="EDINET/TDnetを過去何日分検索するか")

    st.divider()

    st.markdown("""
    **データソース:**
    - 📄 EDINET API (有報・半期報)
    - 📰 TDnet (決算短信・業績予想)
    - 📈 yfinance (株価)
    """)

# ---------------------------------------------------------------------------
# Main Input
# ---------------------------------------------------------------------------

col1, col2 = st.columns([3, 1])

with col1:
    codes_input = st.text_input(
        "証券コード（カンマ区切り）",
        value="6763,6989,6768,6779",
        placeholder="例: 6763,6989,6768,6779",
    )

with col2:
    st.write("")  # spacer
    st.write("")
    generate_btn = st.button("🚀 Comps表を生成", type="primary", use_container_width=True)

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

def process_company(code_4, search_days, progress_container):
    """1社分のデータを取得・計算。"""
    result = {
        'code': code_4,
        'status': 'processing',
        'errors': [],
        'data': None,
        'edinet_raw': None,
        'tdnet_raw': None,
        'stock_raw': None,
    }

    # Step 1: EDINET（API Key がある場合のみ）
    edinet_available = False
    try:
        load_api_key()
        edinet_available = True
    except RuntimeError:
        pass

    edinet_data = {
        'company_name': '',
        'yuho_data': {},
        'hanki_data': {},
        'yuho_doc': None,
        'hanki_doc': None,
    }

    if edinet_available:
        progress_container.text(f"  📄 {code_4}: EDINET検索中...")
        try:
            edinet_data = fetch_company_financials(code_4, days=search_days)
            result['edinet_raw'] = edinet_data
        except Exception as e:
            result['errors'].append(f"EDINET: {e}")
    else:
        result['errors'].append("EDINET: API Key未設定（スキップ）")

    # Step 2: TDnet
    progress_container.text(f"  📰 {code_4}: TDnet決算短信検索中...")
    try:
        tdnet_data = fetch_tanshin_forecasts(code_4, days=search_days)
        result['tdnet_raw'] = tdnet_data
    except Exception as e:
        result['errors'].append(f"TDnet: {e}")
        tdnet_data = {'forecast': {}}

    # Step 3: Stock Price
    progress_container.text(f"  📈 {code_4}: 株価取得中...")
    try:
        stock_data = fetch_stock_info(code_4)
        result['stock_raw'] = stock_data
    except Exception as e:
        result['errors'].append(f"株価: {e}")
        stock_data = {'stock_price': None, 'shares_outstanding': None, 'market_cap': None}

    # Step 4: Build company data
    progress_container.text(f"  🔢 {code_4}: 計算中...")
    try:
        company = build_company_data(code_4, edinet_data, tdnet_data, stock_data)
        if not company.get('name') and stock_data.get('company_name_en'):
            company['name'] = stock_data['company_name_en']
        result['data'] = company
        result['status'] = 'done'
    except Exception as e:
        result['errors'].append(f"計算: {e}")
        result['status'] = 'error'

    return result


if generate_btn:
    codes = [c.strip() for c in codes_input.split(",") if c.strip()]
    if not codes:
        st.error("証券コードを入力してください。")
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

        results = []
        for i, code in enumerate(codes):
            status_container.info(f"処理中: {code} ({i+1}/{len(codes)})")
            progress_bar.progress((i) / len(codes))

            result = process_company(code, search_days, progress_text)
            results.append(result)

            if result['errors']:
                for err in result['errors']:
                    st.session_state.errors.append(f"{code}: {err}")

        progress_bar.progress(1.0)
        status_container.success(f"✅ 完了: {len(codes)}社の処理が終わりました。下にスクロールして結果を確認してください。")
        progress_text.empty()

        st.session_state.company_data = results
        st.session_state.generation_done = True

# ---------------------------------------------------------------------------
# Display Results
# ---------------------------------------------------------------------------

if st.session_state.generation_done:
    if st.session_state.errors:
        with st.expander("⚠️ エラー・警告", expanded=True):
            for err in st.session_state.errors:
                st.warning(err)

    companies_for_config = []
    for r in st.session_state.company_data:
        if r.get('data'):
            companies_for_config.append(r['data'])

    if companies_for_config:
        st.subheader("取得データ一覧")

        summary_rows = []
        for c in companies_for_config:
            summary_rows.append({
                'コード': c.get('code', ''),
                '企業名': c.get('name', ''),
                '株価': c.get('stock_price'),
                '時価総額(百万)': c.get('market_cap'),
                '売上高LTM': c.get('rev_ltm'),
                '営業利益LTM': c.get('op_ltm'),
                'EBITDA LTM': c.get('ebitda_ltm'),
                'EV/EBITDA': f"{c['_multiples']['ev_ebitda_ltm']:.1f}x" if c.get('_multiples', {}).get('ev_ebitda_ltm') else 'N/A',
                'PER': f"{c['_multiples']['per_fwd']:.1f}x" if c.get('_multiples', {}).get('per_fwd') else 'N/A',
                'PBR': f"{c['_multiples']['pbr']:.2f}x" if c.get('_multiples', {}).get('pbr') else 'N/A',
            })

        df_summary = pd.DataFrame(summary_rows)
        st.dataframe(df_summary, use_container_width=True, hide_index=True)

        # --- 手動補完セクション ---
        st.subheader("手動データ補完")
        st.caption("自動取得できなかった項目を手動で入力・修正できます。")

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

                with col2:
                    st.markdown("**P&L (LTM)**")
                    rev = st.number_input("売上高", value=company.get('rev_ltm') or 0,
                                          key=f"rev_{idx}", step=100)
                    op = st.number_input("営業利益", value=company.get('op_ltm') or 0,
                                         key=f"op_{idx}", step=100)
                    ni = st.number_input("純利益", value=company.get('ni_ltm') or 0,
                                         key=f"ni_{idx}", step=100)
                    da = st.number_input("減価償却費", value=company.get('da_ltm') or 0,
                                         key=f"da_{idx}", step=100)
                    ebitda = st.number_input("EBITDA", value=company.get('ebitda_ltm') or 0,
                                             key=f"ebitda_{idx}", step=100)

                with col3:
                    st.markdown("**BS・株式**")
                    cash = st.number_input("現金及び預金", value=company.get('cash') or 0,
                                           key=f"cash_{idx}", step=100)
                    debt = st.number_input("有利子負債", value=company.get('total_debt') or 0,
                                           key=f"debt_{idx}", step=100)
                    eq = st.number_input("純資産", value=company.get('equity_parent') or 0,
                                         key=f"eq_{idx}", step=100)
                    dps = st.number_input("DPS(配当)", value=company.get('dps') or 0.0,
                                          key=f"dps_{idx}", step=1.0)

                col4, col5 = st.columns(2)
                with col4:
                    st.markdown("**予想値 (FY E)**")
                    rev_e = st.number_input("売上高予想", value=company.get('rev_forecast') or 0,
                                            key=f"reve_{idx}", step=100)
                    op_e = st.number_input("営業利益予想", value=company.get('op_forecast') or 0,
                                           key=f"ope_{idx}", step=100)
                    ni_e = st.number_input("純利益予想", value=company.get('ni_forecast') or 0,
                                           key=f"nie_{idx}", step=100)
                    ebitda_e = st.number_input("EBITDA予想", value=company.get('ebitda_forecast') or 0,
                                               key=f"ebitdae_{idx}", step=100)

                edited = dict(company)
                edited['name'] = name
                edited['sector'] = sector
                edited['accounting'] = accounting
                edited['fy_end'] = fy_end
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
                             value="Source: EDINET, TDnet, Yahoo Finance\n"
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

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption("くじらキャピタル株式会社 | Comps自動生成ツール v1.0")
