# CLAUDE.md - comps_app

## 概要
Streamlit製 Comps（比較会社分析）自動生成ツール。証券コード入力でEDINET/TDnet/yfinanceからデータ取得し、Comps表Excelを出力する。

## 起動
```bash
cd C:\Users\竹内真二\comps_app
streamlit run app.py
```

## モジュール構成

| ファイル | 役割 | 主要関数 |
|---------|------|---------|
| `app.py` | Streamlit UI・メインフロー | `process_company()` |
| `edinet_client.py` | EDINET API type=5 CSV取得・パース | `fetch_company_financials()` |
| `tdnet_client.py` | TDnetスクレイピング+PyMuPDF PDF解析 | `fetch_tanshin_forecasts()` |
| `stock_fetcher.py` | yfinance株価取得 | `fetch_stock_info()` |
| `financial_calc.py` | LTM・EBITDA・EV・マルチプル計算 | `build_company_data()` |
| `comps_generator.py` | Excel Comps表生成（openpyxl） | `generate_comps(config, path)` |
| `auth.py` | Supabase GoTrue認証（オプション） | `login()`, `signup()`, `show_login_page()` |

## データフロー
```
証券コード → edinet_client (有報・半期報) → financial_calc (LTM計算)
           → tdnet_client (決算短信→業績予想)   ↓
           → stock_fetcher (株価)          → build_company_data() → app.py 表示
                                                                  → comps_generator (Excel出力)
```

## 数値単位の規約
- **内部データ**: 金額は全て**百万円**、株数は**千株**、株価・DPSは**円**
- **EDINET CSV**: 元データは円単位 → `edinet_client.py`で百万円に変換
- **yfinance**: marketCapは円 → `stock_fetcher.py`で百万円に変換

## 主要な技術的判断

### LTM計算の制約
`financial_calc.py:128` - EDINET CSVから前期H1が自動取得困難なため、**通期値をそのままLTMの代理値として使用**（近似）。正確なLTMは手動補完UIで修正する想定。

### SSL検証バイパス
社内ネットワーク対応のため、`CURL_CA_BUNDLE=''` と `REQUESTS_CA_BUNDLE=''` を設定。`stock_fetcher.py`と`app.py`の両方で設定。

### 認証（オプション）
`st.secrets`に`supabase`設定がある場合のみ認証ON。ない場合は認証なしで動作。

## comps_generator JSON形式
`comps_generator.py`はCLIでも使用可能:
```bash
python comps_generator.py config.json output.xlsx
```
config形式は`comps_generator.py`冒頭のdocstringを参照。

## 依存ライブラリ
streamlit, yfinance, openpyxl, pymupdf, requests, beautifulsoup4, pandas, gotrue

## デフォルト対象企業
6763(帝国通信工業), 6989(北陸電気工業), 6768(タムラ製作所), 6779(日本電波工業)

## 既知の課題・TODO
- LTM計算が通期値の近似になっている（前期H1自動取得未対応）
- TDnet決算短信PDFの業績予想抽出は正規表現ベースで、フォーマット差異に弱い
- EDINET APIは1日1リクエスト/秒のレート制限あり（`time.sleep(1)`で対応済み）
