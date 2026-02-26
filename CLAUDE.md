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
| `auth.py` | Supabase GoTrue認証（オプション・無効化済み） | — |

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

## キャッシュ階層（3段階）＋完全オフラインパス

全データをgitに永続化し、reboot/再デプロイ後もAPI再取得なしで即座にデータ利用可能。
**全社キャッシュ済みの場合、外部API（EDINET/yfinance）を一切呼ばない完全オフラインパスで処理。**
`fetch_companies_batch()`や`fetch_stock_info()`すら呼ばず、ファイルI/Oのみで完結する。

### 1. EDINETキャッシュ (`data/edinet/{code_4}/`)
- `meta.json`: 書類リスト（docID, periodEnd等）
- `yuho_*.zip` / `hanki_*.zip`: EDINET CSV ZIPファイル
- `yuho_*.pdf` / `hanki_*.pdf`: 有報・半期報PDF
- **`yuho_parsed.json` / `hanki_parsed.json`**: CSVパース結果（最優先で読み込み、ZIP解凍不要）
- gitに永続化済み → Streamlit Cloud reboot後もAPI不要

### 2. 株価キャッシュ (`data/stock/{code_4}/`)
- `stock.json`: 永続キャッシュ（gitにコミットして保持）
- キャッシュがあればyfinanceアクセスをスキップ
- 株価更新はサイドバーの「キャッシュクリア」で明示的に実行

### 3. 決算短信 (`data/tanshin/{code_4}/`)
- `tanshin_{YYYY-MM}_{FY|Q1|Q2|Q3}.pdf`: アップロードPDF
- `data/tanshin_forecasts.json`: 全社の業績予想パース結果（永続キャッシュ）
- gitに永続化済み → 再アップロード不要、起動時に自動パース

### 完全オフラインパスの仕組み
`app.py`の生成ロジックは2つのパスに分岐:
1. **完全キャッシュパス** (`_all_fully_cached=True`): `data/edinet/`の`_parsed.json`と`data/stock/`の`stock.json`から直接データを組み立て。`fetch_companies_batch()`、`fetch_stock_info()`、`validate_stock_code()`等の関数呼び出し自体をスキップ。HTTPセッション作成・APIキー読み込みも不要。
2. **通常パス**: キャッシュミスがある企業のみEDINET/yfinanceにアクセス。

### 新しい企業を追加する手順
Streamlit Cloudの一時ファイルシステムではランタイム中に生成されたキャッシュは再起動で消える。新企業の追加手順:
1. **ローカルでデータ取得**: `fetch_companies_batch(['XXXX'], days=400)` + `fetch_stock_info('XXXX')`
2. **決算短信をローカルでパース**: `parse_tanshin_pdf()` → `tanshin_forecasts.json` 更新
3. **gitにコミット＆プッシュ**: `data/edinet/XXXX/`, `data/stock/XXXX/`, `data/tanshin/XXXX/`, `data/tanshin_forecasts.json`

### 証券コード検証のキャッシュ活用
Step 0の検証で以下のいずれかがあればyfinance検証をスキップ:
- `data/tanshin/{code}/` にPDFあり
- `data/edinet/{code}/meta.json` あり
- `data/stock/{code}/stock.json` あり

## 主要な技術的判断

### EDINET CSV パーサー（IFRS対応済み）
- **J-GAAP**: `jppfs_cor:` プレフィックスの要素IDでマッチ
- **IFRS**: `jpigp_cor:〜IFRS` プレフィックスの要素IDを追加済み（6779 日本電波工業で確認）
- **連結フィルタ**: J-GAAPは`連結`、IFRSは`その他`。両方受け入れ、`個別`のみ除外
- **会社固有プレフィックス対応**: 半期報告書で`jpcrp040300-ssr_E01807-000:〜IFRS`のようなプレフィックスが使われる場合、コロン以降の要素名部分で動的マッチ
- **経営指標等（SUMMARY_ELEMENT_MAP）**: J-GAAP/IFRS両方の`jpcrp_cor:`要素をフォールバック用に登録。DPS（実績配当）も`DividendPaidPerShareSummaryOfBusinessResults`で取得

### LTM計算（Calendarize対応済み）
`edinet_client.py` の `extract_financial_data(include_prior=True)` で半期報告書CSVから前期H1データ（`相対年度=前中間期/前中間期末`）を抽出。`financial_calc.py` の `build_company_data()` で **正確なLTM = FY通期 − 前期H1 + 今期H1** を自動計算。前期H1が取れない場合は通期値にフォールバック。

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

- **列**: コード、企業名、株価（円）＋取得日付、時価総額（百万円）、EV（百万円）、売上高LTM（百万円）、営業利益LTM（百万円）、EBITDA LTM（百万円）、EV/EBITDA LTM、Forward PER、直近四半期末PBR、配当利回り（直近年度末）
- **ヘッダー2行表示**: 1行目に項目名、2行目に単位（百万円）・日付・サブラベル（グレー小文字）
- **ソート**: 列ヘッダークリックで昇順/降順トグル（▲▼アイコン付き）。生の数値データでソート後にフォーマット表示。
- **フォーマット**: Python側で事前適用。整数列は桁区切りカンマ、マルチプルは`x`付き、配当利回りは`%`付き。
- **右揃え**: 数値列すべてCSS `text-align:right`
- **横スクロール**: テーブルが画面幅を超える場合、太め(10px)・グレー・丸角の見やすいスクロールバーを表示
- Forward PERは決算短信の`ni_forecast`から動的再計算（アップロード後に即反映）

### 証券コード検証
`validate_stock_code(code_4)` — yfinance `history(period="5d")` で東証に存在するか軽量チェック。ローカルにキャッシュ（EDINET/株価/tanshin）がある企業はyfinance不要。フォーマット検証（4桁数字）は入力欄の下にリアルタイム表示。

### 手動補完UI
- 株価・発行済株式数を手入力可能（yfinanceレート制限時の対応）
- 入力値から時価総額・EV・EV/EBITDA・PER・PBRをリアルタイム自動計算（st.metric表示）
- 金額は整数表示（百万円）、DPSは小数1桁（円・有報実績）

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
streamlit, yfinance, openpyxl, pymupdf, requests, beautifulsoup4, pandas

## キャッシュ済み対象企業

### Comps Set 1（帝国通信工業）
| コード | 会社名 | 会計基準 | EDINET | 株価 | 決算短信 |
|--------|--------|---------|--------|------|---------|
| 6763 | 帝国通信工業 | J-GAAP | OK | OK | — |
| 6989 | 北陸電気工業 | J-GAAP | OK | OK | — |
| 6768 | タムラ製作所 | J-GAAP | OK | OK | — |
| 6779 | 日本電波工業 | IFRS | OK | OK | — |

### Comps Set 2（ローム）
| コード | 会社名 | 会計基準 | EDINET | 株価 | 決算短信 |
|--------|--------|---------|--------|------|---------|
| 6616 | トレックス・セミコンダクター | J-GAAP | OK | OK | Q1 |
| 6999 | KOA | J-GAAP | OK | OK | Q1 |
| 6962 | 大真空 | J-GAAP | OK | OK | Q1 |
| 6963 | ローム | J-GAAP | OK | OK | Q1 |

## デプロイ
- **Streamlit Cloud**: `shinjiquzilla/comps-app` リポジトリ main ブランチ連携
- **URL**: `comps-app-msdep7hegjdzmqjr3r4smg.streamlit.app`
- `runtime.txt` に `python-3.11` 指定（実際はCloud側で3.13が使われる場合あり）
- pushすると自動デプロイ

## Streamlit Cloud固有の注意点
- **一時ファイルシステム**: ランタイム中に生成されたファイルは再起動/再デプロイで消失。キャッシュはgitに永続化が必須。
- **`st.rerun()` は使わない**: Cloud環境ではst.rerun()がsession state消失を引き起こす場合がある。生成後の結果表示はsession stateフラグ（generation_done）で制御
- **session stateの肥大化に注意**: 大データをsession stateに入れるとメモリ圧迫→アプリ再起動→session state消失のリスク
- **結果表示セクションはtry-exceptでラップ**: エラー時にサイレントに元の画面に戻る問題を防止
- **EDINET API Key**: Cloud上では`st.secrets`の`edinet.api_key`で設定。未設定の場合はスキップされ株価のみ取得。`st.secrets`アクセスはtry-exceptで囲むこと
- **EDINET一括検索**: `fetch_companies_batch()`で全社まとめて1回の日付ループ。N社×日数→1×日数に削減
- **デフォルト検索期間400日**: 有報は決算後3ヶ月（3月決算→6月提出）、半期報も同様
- **sys.stdout書き換え禁止**: モジュールのトップレベルで`sys.stdout`を書き換えるとStreamlitのIO破壊でクラッシュ。`if __name__ == '__main__':`ガード必須
- **未使用.pyでもimportに注意**: Streamlit Cloudは全.pyを走査する場合がある。使わないモジュールでもimportエラーがあるとクラッシュ（auth.pyのgotrueで発生→try/except化）
- **yfinanceレート制限**: 複数銘柄連続取得でToo Many Requests。各社間に3秒ディレイ＋リトライ（5/10/15秒バックオフ）で対応済み。キャッシュヒット時は待機なし
- **NumberColumnのformat非対応**: Streamlit Cloud上ではNumberColumnのformat文字列（`%,.0f`等）が効かない場合がある。サマリーテーブルはJavaScript付きHTMLテーブル（`st.components.v1.html`）で回避済み。

## 修正履歴
| 日付 | コミット | 内容 |
|------|---------|------|
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
