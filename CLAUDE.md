# CLAUDE.md - comps_app

## 概要
Streamlit製 Comps（比較会社分析）自動生成ツール。証券コード入力でEDINET/yfinanceからデータ取得し、Comps表Excelを出力する。

## 起動
```bash
cd C:\Users\竹内真二\comps_app
streamlit run app.py
```

## モジュール構成

| ファイル | 役割 | 主要関数 |
|---------|------|---------|
| `app.py` | Streamlit UI・メインフロー | — |
| `edinet_client.py` | EDINET API type=5 CSV取得・パース | `fetch_companies_batch()` |
| `tdnet_client.py` | TDnetスクレイピング+PyMuPDF PDF解析（**無効化済み**） | — |
| `tanshin_parser.py` | 決算短信PDFパーサー（業績予想自動抽出＋一括判定） | `parse_tanshin_pdf()`, `identify_tanshin_pdf()`, `save_tanshin_pdf()` |
| `stock_fetcher.py` | yfinance株価取得＋証券コード検証＋永続キャッシュ | `fetch_stock_info()`, `validate_stock_code()` |
| `financial_calc.py` | LTM・EBITDA・EV・マルチプル計算 | `build_company_data()` |
| `comps_generator.py` | Excel Comps表生成（openpyxl） | `generate_comps(config, path)` |
| `supabase_client.py` | Supabase DB/Storage ラッパー（全データの永続化） | `load_edinet_data()`, `load_stock_data()`, `load_forecasts()` |
| `auth.py` | Supabase GoTrue認証（オプション・無効化済み） | — |
| `schema.sql` | PostgreSQLテーブル定義（5テーブル） | — |
| `migrate_to_supabase.py` | ローカルdata/ → Supabase一括移行スクリプト | — |

## データフロー
```
証券コード → edinet_client (有報・半期報) → financial_calc (LTM計算)
           → stock_fetcher (株価)          → build_company_data() → app.py 表示
                                                                  → comps_generator (Excel出力)
```

## 数値単位の規約
- **内部データ**: 金額は全て**百万円**、株数は**千株**、株価・DPSは**円**
- **EDINET CSV**: 元データは円単位 → `edinet_client.py`で百万円に変換
- **yfinance**: marketCapは円 → `stock_fetcher.py`で百万円に変換

## データ永続化: Supabase + ローカルキャッシュ

### Supabase（メインデータストア）
全データは Supabase PostgreSQL + Storage に永続保存。Streamlit Cloud の再起動やgit管理の制約に依存しない。
- **プロジェクト**: `fuugtyvluxegzrdlfeml.supabase.co`（Quzilla Capital DevOps）
- **接続**: `.streamlit/secrets.toml` の `[supabase]` セクション（url + anon_key）
- **RLS**: 現時点では未設定（anon keyで全アクセス可）

### テーブル構成（`schema.sql`）
| テーブル | 用途 | ユニーク制約 |
|---------|------|------------|
| `companies` | 企業マスター | `code` |
| `edinet_meta` | EDINET書類メタデータ | `doc_id` |
| `financials` | パース済み財務データ（raw_data JSONBに全項目保持） | `(code, doc_type, period_end)` |
| `stock_data` | 株価データ（日次） | `(code, fetched_date)` |
| `tanshin_forecasts` | 業績予想（**決算期×四半期ごとに履歴保持**） | `(code, fy_month, period_type)` |

### Storage
- **バケット**: `tanshin-pdfs`（Private）
- **パス構造**: `{code}/{filename}` (例: `6963/tanshin_2026-03_Q3.pdf`)

### 予想値の履歴保持
`tanshin_forecasts` は `UNIQUE(code, fy_month, period_type)` で、同一企業でも決算期×四半期ごとに別レコード。
過去の予想推移を遡って検証可能（例: 2027年3月期のQ1→Q2→Q3→FY確定の各予想値）。
`load_forecasts()` は各社の最新予想のみ返す（Forward PER計算用）。
`load_forecast_history(code)` で全履歴を取得可能。

### ローカルキャッシュ（フォールバック＋高速パス）
ローカル `data/` ディレクトリはSupabase障害時・オフライン開発時のフォールバックとして残す。

### データ読み込み優先順位
```
1. ローカル _parsed.json / stock.json（最速）
2. Supabase DB（ローカルにない場合のフォールバック）
3. EDINET API / yfinance（両方にない場合のみ）
```

### データ書き込み
EDINET取得・株価取得・決算短信アップロード時に**ローカル + Supabase の両方に自動保存**。
git commit & pushによるデータ永続化は不要。

### 新しい企業を追加する手順
1. アプリで証券コードを入力して生成 → EDINET/yfinanceから自動取得
2. 決算短信PDFをアップロード → 自動パース
3. **データはSupabaseに自動保存される**（git pushは不要だが、コードの変更がある場合はpush）

### 完全オフラインパスの仕組み
`app.py`の生成ロジックは2つのパスに分岐:
1. **完全キャッシュパス** (`_all_fully_cached=True`): ローカル`_parsed.json`/`stock.json`、またはSupabaseからデータを組み立て。外部API不要。
2. **通常パス**: キャッシュミスがある企業のみEDINET/yfinanceにアクセス → 取得後にSupabase + ローカルに保存。

### 証券コード検証のキャッシュ活用
Step 0の検証で以下のいずれかがあればyfinance検証をスキップ:
- `data/tanshin/{code}/` にPDFあり
- `data/edinet/{code}/meta.json` あり
- `data/stock/{code}/stock.json` あり

## 主要な技術的判断

### EDINET CSV パーサー（IFRS・12月決算対応済み）
- **J-GAAP**: `jppfs_cor:` プレフィックスの要素IDでマッチ
- **IFRS**: `jpigp_cor:〜IFRS` プレフィックスの要素IDを追加済み（6779 日本電波工業、3197 すかいらーくHDで確認）
- **連結フィルタ**: J-GAAPは`連結`、IFRSは`その他`。両方受け入れ、`個別`のみ除外
- **会社固有プレフィックス対応**: 半期報告書で`jpcrp040300-ssr_E01807-000:〜IFRS`のようなプレフィックスが使われる場合、コロン以降の要素名部分で動的マッチ
- **経営指標等（SUMMARY_ELEMENT_MAP）**: J-GAAP/IFRS両方の`jpcrp_cor:`要素をフォールバック用に登録。DPS（実績配当）も`DividendPaidPerShareSummaryOfBusinessResults`で取得
- **12月決算企業対応**: 相対年度フィルタに`当四半期累計期間`/`当四半期会計期間末`/`前年度同四半期累計期間`/`前年度同四半期会計期間末`を追加（2702マクドナルド、3197すかいらーくで確認）
- **IFRS営業利益**: `OperatingProfitLossIFRS`と`OperatingIncomeLossIFRS`の両方をマッピング（企業によりどちらが使われるか異なる）
- **IFRS総合商社の営業利益制約**: 三菱商事（8058）等のIFRS総合商社は有報連結P/Lに「営業利益」行がなく（IAS 1ではオプション）、EDINET CSVにも経営指標等サマリーにも連結営業利益が含まれない。手動補完フォームで入力するか決算短信PDFアップロードで対応
- **IFRS D&A追加マッピング**: `DepreciationExpenseOpeCFIFRS`（三菱商事等のCF計算書上の減価償却費）を追加済み
- **IFRS有利子負債**: `BondsAndBorrowingsCLIFRS`/`BondsAndBorrowingsNCLIFRS`（借入金＋社債合算）をマッピング
- **IFRS自己資本比率**: `EquityToAssetRatioIFRSSummaryOfBusinessResults`は実際には「1株当たり親会社所有者帰属持分」を返すため除外。J-GAAP版のみ使用
- **のれん償却費D&A**: `DepreciationAndAmortizationOfGoodwillOpeCF`をマッピング（2702マクドナルドで確認）

### LTM計算（Calendarize対応済み）
`edinet_client.py` の `extract_financial_data(include_prior=True)` で半期報告書CSVから前期H1データ（`相対年度=前中間期/前中間期末` or `前年度同四半期累計期間/前年度同四半期会計期間末`）を抽出。`financial_calc.py` の `build_company_data()` で **正確なLTM = FY通期 − 前期H1 + 今期H1** を自動計算。前期H1が取れない場合は通期値にフォールバック。

### 有利子負債の計算
`calc_total_debt()` — 以下の項目を合算:
`short_term_debt` + `long_term_debt` + `bonds` + `current_long_term_debt` + `current_bonds` + `lease_debt_current` + `lease_debt_noncurrent`
IFRS企業の`BondsAndBorrowings`（借入金＋社債合算）は`short_term_debt`/`long_term_debt`にマッピング。

### PBR計算の純資産
`equity_parent`（親会社帰属持分） → `shareholders_equity` → `net_assets` の優先順でフォールバック。IFRS企業は`equity_parent`のみ持つ場合がある。

### 配当利回りの計算
**有報記載の直近終了フル年度の実績配当額** ÷ 現在株価。今期の予想配当額ではなく実績値を使用。DPSは`edinet_client.py`の`SUMMARY_ELEMENT_MAP`から自動取得（`jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults`）。

### 決算短信パーサー（3段階フォールバック）
`tanshin_parser.py` — PyMuPDF（`fitz`）で決算短信PDFからテキスト抽出し、正規表現で業績予想（売上高・営業利益・純利益）をパース。

1. **メインパーサー** (`_extract_forecast`): 「業績予想」セクションヘッダーを検出 → 「通期」行から整数値を抽出（小数=増減率/EPSをスキップ）。同一行・複数行（PyMuPDFのセル別抽出）両対応。
2. **通期フォールバック** (`_extract_tsuuki_fallback`): セクションヘッダーがPyMuPDFで抽出できない場合、テキスト全体から「通期」行を探し後続行の整数値を収集。
3. **ラベルフォールバック** (`_extract_forecast_by_label`): テーブル形式でない場合、「売上高」「営業利益」「純利益」のラベル＋数値パターンで個別抽出。

### 決算短信一括アップロード＆不足自動検出
`identify_tanshin_pdf(pdf_bytes, candidate_codes)` — PDF1ページ目から証券コード・決算期・期間種別（FY/Q1/Q2/Q3）を正規表現で自動判定。一括アップロードUI（`accept_multiple_files=True`）で複数PDFを同時処理。保存済みPDFは `data/tanshin/{code_4}/` にgit管理され、自動パースで予想値をプリフィル。不足データ（PER用の純利益予想）を自動検出し、「○○年○月期 第○四半期決算短信をアップロードしてください」と具体的にリクエスト表示。配当利回り用のDPSは有報から自動取得されるため不足チェック不要。

### サマリーテーブル（JavaScript付きHTMLテーブル）
`st.components.v1.html`でiframe内にJavaScript付きHTMLテーブルをレンダリング。Streamlitの`NumberColumn`フォーマット制約を完全に回避。

- **列**: コード、決算月、企業名、株価（円）＋取得日付、時価総額（百万円）、EV（百万円）、売上高LTM（百万円）、営業利益LTM（百万円）、EBITDA LTM（百万円）、EV/EBITDA LTM、Forward PER、直近四半期末PBR、配当利回り（直近年度末）
- **ヘッダー2行表示**: 1行目に項目名、2行目に単位（百万円）・日付・サブラベル（グレー小文字）
- **ソート**: 列ヘッダークリックで昇順/降順トグル（▲▼アイコン付き）。生の数値データでソート後にフォーマット表示。
- **フォーマット**: Python側で事前適用。整数列は桁区切りカンマ、マルチプルは`x`付き、配当利回りは`%`付き。
- **右揃え**: 数値列すべてCSS `text-align:right`
- **横スクロール**: テーブルが画面幅を超える場合、太め(10px)・グレー・丸角の見やすいスクロールバーを表示
- Forward PERは決算短信の`ni_forecast`から動的再計算（決算短信パース処理をテーブル構築より先に実行することで即反映）

### 証券コード検証
`validate_stock_code(code_4)` — yfinance `history(period="5d")` で東証に存在するか軽量チェック。ローカルにキャッシュ（EDINET/株価/tanshin）がある企業はyfinance不要。Supabase `companies`テーブルに存在する企業もスキップ。フォーマット検証（4桁英数字、2024年1月〜アルファベット対応: 241A等）は入力欄の下にリアルタイム表示。

### 手動補完UI
- `st.form("manual_edit_form")`でラップ（入力ごとの再実行を防止、ホワイトアウト対策）
- 「データを反映」ボタン押下時に `st.toast` + `st.spinner` で処理中フィードバック表示
- 株価・発行済株式数を手入力可能（yfinanceレート制限時の対応）
- 予想値（進行期末）: 売上高予想・営業利益予想・純利益予想・減価償却費予想・EBITDA予想を入力可能
- **EBITDA予想の計算ロジック**:
  - EBITDA予想は`st.metric`で読み取り専用表示（`st.form`内のwidget更新制約を回避）
  - 「データを反映」押下時に自動計算: `営業利益予想 + D&A` → session_stateに保存
  - D&A予想が0の場合 → 直近年度末の減価償却費実績で簡便計算
  - 簡便計算時はEBITDA下に注記表示: 「※ 簡便計算: 営業利益予想 + 直近年度末D&A実績（xxx）」
- 手動入力した予想値はSupabase `tanshin_forecasts`テーブルに自動保存
- 入力値から時価総額・EV・EV/EBITDA・PER・PBRをリアルタイム自動計算（st.metric表示）
- 金額は整数表示（百万円）、DPSは小数1桁（円・有報実績）

### UIテーマ・カラー設定
- **カラースキーム**: 白背景（`#ffffff`）+ 水色アクセント（`#45b5e6`、quzilla.co.jpコーポレートサイトと統一）+ ダークグレーテキスト（`#333`）
- **`.streamlit/config.toml`**: Streamlit標準テーマ設定（primaryColor, backgroundColor, secondaryBackgroundColor, textColor）
- **カスタムCSS注入**: `st.markdown(unsafe_allow_html=True)`でタブ、ボタン、number_input右寄せ、placeholder色等を追加調整
- **タイトル**: SVGアイコン（棒グラフ風・水色）+ HTMLカスタムレンダリング
- **絵文字不使用**: UI全体でUnicode記号（▶◆◇●⬇▸等）に統一
- **`st.number_input`のformat制約**: `%,d`（桁区切り）は非対応。`%d`のみ使用可能

### 決算短信不足検出ロジック（決算月ベース）
年度末から現在日付までの経過日数に基づき、要求する決算短信の種類を自動判定:
- **年度末から92日以内**: 通期決算短信を要求（例: 12月決算企業に対し2月時点→「2025年12月期 通期決算短信」）
- **92日超**: 経過月数から四半期を推定（〜6ヶ月=Q1、〜9ヶ月=Q2、それ以降=Q3）

### 決算月の動的判定
`financial_calc.py`の`build_company_data()`でEDINETの`periodEnd`から決算月を動的に取得。有報のperiodEndが年度末、半期報のperiodEndは中間期末（+6ヶ月が年度末）。ハードコード`'Mar'`ではなく企業ごとに正しい決算月を設定。

### SSL検証バイパス
社内ネットワーク対応のため、`CURL_CA_BUNDLE=''` と `REQUESTS_CA_BUNDLE=''` を設定。`stock_fetcher.py`と`app.py`の両方で設定。

### 認証（現在無効）
`auth.py`はリポジトリに残っているが、`app.py`からのimport・呼び出しは削除済み（2026/2/26）。`gotrue`もrequirements.txtから除外。再度有効にする場合は`auth.py`のimportとAuth Gateを`app.py`に戻す。

## comps_generator JSON形式
`comps_generator.py`はCLIでも使用可能:
```bash
python comps_generator.py config.json output.xlsx
```
config形式は`comps_generator.py`冒頭のdocstringを参照。

## 依存ライブラリ
streamlit, yfinance, openpyxl, pymupdf, requests, beautifulsoup4, pandas, supabase

## キャッシュ済み対象企業

### Comps Set 1（帝国通信工業）
| コード | 会社名 | 会計基準 | 決算期 | EDINET | 株価 | 決算短信 |
|--------|--------|---------|--------|--------|------|---------|
| 6763 | 帝国通信工業 | J-GAAP | 3月 | OK | OK | — |
| 6989 | 北陸電気工業 | J-GAAP | 3月 | OK | OK | — |
| 6768 | タムラ製作所 | J-GAAP | 3月 | OK | OK | — |
| 6779 | 日本電波工業 | IFRS | 3月 | OK | OK | — |

### Comps Set 2（ローム）
| コード | 会社名 | 会計基準 | 決算期 | EDINET | 株価 | 決算短信 |
|--------|--------|---------|--------|--------|------|---------|
| 6616 | トレックス・セミコンダクター | J-GAAP | 3月 | OK | OK | Q1 |
| 6999 | KOA | J-GAAP | 3月 | OK | OK | Q1 |
| 6962 | 大真空 | J-GAAP | 3月 | OK | OK | Q1 |
| 6963 | ローム | J-GAAP | 3月 | OK | OK | Q1 |

### Comps Set 3（外食）
| コード | 会社名 | 会計基準 | 決算期 | EDINET | 株価 | 決算短信 |
|--------|--------|---------|--------|--------|------|---------|
| 7550 | ゼンショーホールディングス | J-GAAP | 3月 | OK | OK | Q1 |
| 2702 | 日本マクドナルドホールディングス | J-GAAP | 12月 | OK | OK | — |
| 3197 | すかいらーくホールディングス | IFRS | 12月 | OK | OK | — |

### Comps Set 4（総合商社）
| コード | 会社名 | 会計基準 | 決算期 | EDINET | 株価 | 決算短信 | 備考 |
|--------|--------|---------|--------|--------|------|---------|------|
| 8053 | 住友商事 | IFRS | 3月 | OK | OK | — | |
| 8058 | 三菱商事 | IFRS | 3月 | OK | OK | — | 営業利益はEDINET CSV非収録→手動入力 |

## デプロイ
- **Streamlit Cloud**: `shinjiquzilla/comps-app` リポジトリ main ブランチ連携
- **URL**: `comps-app-msdep7hegjdzmqjr3r4smg.streamlit.app`
- `runtime.txt` に `python-3.11` 指定（実際はCloud側で3.13が使われる場合あり）
- pushすると自動デプロイ

## Streamlit Cloud固有の注意点
- **一時ファイルシステム**: ランタイム中に生成されたファイルは再起動/再デプロイで消失。Supabase移行後はDBから自動復元されるためgit永続化は不要。
- **`st.rerun()` は使わない**: Cloud環境ではst.rerun()がsession state消失を引き起こす場合がある。生成後の結果表示はsession stateフラグ（generation_done）で制御
- **session stateの肥大化に注意**: 大データをsession stateに入れるとメモリ圧迫→アプリ再起動→session state消失のリスク
- **結果表示セクションはtry-exceptでラップ**: エラー時にサイレントに元の画面に戻る問題を防止
- **EDINET API Key**: Cloud上では`st.secrets`の`edinet.api_key`で設定。Supabase `app_config`テーブルからのフォールバック読み込みあり。未設定の場合はスキップされ株価のみ取得。`st.secrets`アクセスはtry-exceptで囲むこと
- **Python 3.13互換性**: Streamlit Cloudは3.13を使用。f-string内のネストクォート（`f'{s["key"]}'`）は3.14では動作するが3.13ではSyntaxError。str連結を使用すること
- **print()はCloud上で非表示**: Streamlit Cloudのダウンロード可能ログにprint()出力は出ない。デバッグには`st.caption`/`st.warning`を使用
- **EDINET一括検索**: `fetch_companies_batch()`で全社まとめて1回の日付ループ。N社×日数→1×日数に削減
- **デフォルト検索期間400日**: 有報は決算後3ヶ月（3月決算→6月提出）、半期報も同様
- **sys.stdout書き換え禁止**: モジュールのトップレベルで`sys.stdout`を書き換えるとStreamlitのIO破壊でクラッシュ。`if __name__ == '__main__':`ガード必須
- **未使用.pyでもimportに注意**: Streamlit Cloudは全.pyを走査する場合がある。使わないモジュールでもimportエラーがあるとクラッシュ（auth.pyのgotrueで発生→try/except化）
- **yfinanceレート制限**: 複数銘柄連続取得でToo Many Requests。各社間に3秒ディレイ＋リトライ（5/10/15秒バックオフ）で対応済み。キャッシュヒット時は待機なし
- **NumberColumnのformat非対応**: Streamlit Cloud上ではNumberColumnのformat文字列（`%,.0f`等）が効かない場合がある。サマリーテーブルはJavaScript付きHTMLテーブル（`st.components.v1.html`）で回避済み。

## 修正履歴
| 日付 | コミット | 内容 |
|------|---------|------|
| 2026/2/27 | — | フッターからバージョン番号を削除（git履歴で十分なため） |
| 2026/2/27 | 9555d29 | IFRS D&A: DepreciationExpenseOpeCFIFRS追加（三菱商事等の総合商社対応） |
| 2026/2/27 | — | EBITDA予想をst.metric表示に変更（st.form内widget更新制約の回避） |
| 2026/2/27 | — | Comps再生成時にsession_stateのフォーム関連キーをクリア（前回値のリーク防止） |
| 2026/2/27 | — | キャッシュクリア時にSupabaseデータも削除 |
| 2026/2/27 | — | EDINET API KeyのSupabase app_config フォールバック読み込み |
| 2026/2/27 | — | デバッグ出力: 未マッチ要素のexpander表示（営業利益・D&A関連） |
| 2026/2/27 | 917ca76 | キャプション文言修正: 「自動作成」→「自動生成」 |
| 2026/2/27 | bf76e43 | 決算短信アップロードUX改善: 確認中メッセージ表示、再生成ボタン配置改善 |
| 2026/2/27 | 112eee6 | 生成ボタン押下直後に「Comps生成を開始しています...」を表示 |
| 2026/2/27 | 50f673c | EBITDA予想を自動計算化: 営業利益予想 + 減価償却費予想（LTM実績デフォルト） |
| 2026/2/27 | 5357e61 | 手動補完: 「予想値 - FY E」→「予想値（進行期末）」に表記修正 |
| 2026/2/27 | 8e8ac3d | サマリーテーブルのソート: 初回クリックで降順（大→小）に変更 |
| 2026/2/27 | — | 決算短信未アップロード vs パース失敗の判定メッセージ分離（Supabase Storage確認） |
| 2026/2/27 | — | 手動入力した予想値をSupabaseに自動保存 |
| 2026/2/27 | — | fetch_companies_batch にSupabaseフォールバック追加（キャッシュ済み企業のEDINET再取得を防止） |
| 2026/2/27 | — | 証券コード: アルファベット対応（241A等、2024年1月〜）、validate/parser/migration全修正 |
| 2026/2/27 | — | サービス業の売上高取得: OperatingRevenue マッピング追加（9376,9436等） |
| 2026/2/27 | — | 手動データ補完をst.formでラップ（ホワイトアウト防止） |
| 2026/2/27 | — | Supabase既知企業のyfinance検証スキップ（companiesテーブル参照） |
| 2026/2/27 | — | Supabase移行: 全データをPostgreSQL+Storageに永続化、予想値履歴保持、ローカルキャッシュフォールバック |
| 2026/2/27 | — | UIリニューアル: 白背景+水色アクセント(#45b5e6)、絵文字→Unicode記号、決算月列追加、決算短信不足検出ロジック改善、決算月動的判定 |
| 2026/2/27 | bc02efc | 7550ゼンショー決算短信Q1追加、ルートPDF整理 |
| 2026/2/27 | 1f31cff | IFRS/12月決算企業対応バグ修正7件 + 新3社(7550,2702,3197)データ追加 |
| 2026/2/26 | 97aa2ef | サマリーヘッダー: 配当利回りに「直近年度末」追記 |
| 2026/2/26 | debf4a1 | サマリーヘッダー: PER Forward / PBR 直近四半期末に変更 |
| 2026/2/26 | 81a55a1 | FY PER → Forward PER に列名変更 |
| 2026/2/26 | 09893e8 | サマリーテーブルに見やすい横スクロールバー追加 |
| 2026/2/26 | 91d59ac | サマリーヘッダー2行表示（百万円・日付を折り返し） |
| 2026/2/26 | 6b11a10 | サマリーテーブルをJS付きHTMLに変更（カンマ＋右揃え＋ソート） |
| 2026/2/26 | c33f8ed | 新4社(6616,6999,6962,6963)の決算短信・予想値をgitに永続化 |
| 2026/2/26 | adcc75d | 新4社のEDINET/株価キャッシュをgitに永続化 |
| 2026/2/26 | 3d33357 | 全社キャッシュ済み時に完全オフラインパスを実装 |
| 2026/2/26 | 1253902 | fetch_companies_batchのセッション作成を遅延 |
| 2026/2/26 | 5eb2f21 | stock_fetcher全面改修: キャッシュ最優先、例外を投げない |
| 2026/2/26 | b767299 | 決算短信の予想値をJSONに永続化 |
| 2026/2/26 | dd9366a | 株価キャッシュを永続化: stock.json、gitにコミット |
| 2026/2/26 | 7313449 | 証券コード検証: EDINET/株価キャッシュがある企業もyfinanceスキップ |
| 2026/2/26 | 06afb73 | EDINETパース結果キャッシュ時にZIP/PDF読み込みを完全スキップ |
| 2026/2/26 | 5abc43e | 株価キャッシュヒット時に待機・取得メッセージを表示しない |
| 2026/2/26 | b21d269 | 配当利回りを有報実績DPSベースに変更 |
| 2026/2/26 | a137b1c | EDINETパース結果をJSONキャッシュ化 |
| 2026/2/26 | ccb68a0 | 株価データの日次キャッシュを追加 |
| 2026/2/26 | 0bc96f2 | パーサー: 業績予想ヘッダー欠落時のフォールバック追加 |
| 2026/2/26 | 1f3dfa9 | 決算短信一括アップロード＆自動判定 |
| 2026/2/26 | 6813b01 | Calendarize: 正確なLTM計算 |
| 2026/2/26 | 0392c01 | EDINET一括検索で高速化 |
| 2026/2/26 | d73ccfc | EDINET CSVパーサーにIFRS対応追加 |
| 〜2026/2/25 | 初期構築 | EDINET/TDnet/yfinance連携、手動補完UI、Excel出力 |

## 既知の課題・TODO
- TDnet機能は無効化済み（有料サービス）。業績予想は決算短信PDFアップロード or 手動入力で対応
- 決算短信パーサーは東証規定フォーマットを前提。企業ごとの微差でパース失敗する場合あり → 3段階フォールバックで対応
- EDINET APIは1日1リクエスト/秒のレート制限あり（`time.sleep(1)`で対応済み）
- yfinanceレート制限は完全には回避できない→永続キャッシュ＋手動株価入力で代替可能
- Streamlit CloudのNumberColumn formatが効かない→JS付きHTMLテーブルで回避済み
- IFRS企業のリース負債（`OtherFinancialLiabilities`）は`BondsAndBorrowings`と分離されている場合があり、現状はBondsAndBorrowingsのみ計上。すかいらーく等のIFRS16リース負債は含まれていない可能性がある
- 2702（マクドナルド）・3197（すかいらーく）は12月決算。Forward PER用の決算短信（Q3）が未アップロード
- IFRS総合商社（8058三菱商事等）はEDINET CSVに連結営業利益が含まれない → 手動入力 or 決算短信PDF必須
