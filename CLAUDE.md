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
| `stock_fetcher.py` | yfinance株価取得＋証券コード検証＋日次キャッシュ | `fetch_stock_info()`, `validate_stock_code()` |
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

## キャッシュ階層（3段階）

全データをgitに永続化し、reboot/再デプロイ後もAPI再取得なしで即座にデータ利用可能。

### 1. EDINETキャッシュ (`data/edinet/{code_4}/`)
- `meta.json`: 書類リスト（docID, periodEnd等）
- `yuho_*.zip` / `hanki_*.zip`: EDINET CSV ZIPファイル
- `yuho_*.pdf` / `hanki_*.pdf`: 有報・半期報PDF
- **`yuho_parsed.json` / `hanki_parsed.json`**: CSVパース結果（最優先で読み込み、ZIP解凍不要）
- gitに永続化済み → Streamlit Cloud reboot後もAPI不要

### 2. 株価キャッシュ (`data/stock/{code_4}/`)
- `YYYY-MM-DD.json`: 日次キャッシュ（前日終値は日中変わらないため）
- 同日中はyfinanceアクセスをスキップ
- 3秒待機もスキップ（キャッシュミス時のみ待機）

### 3. 決算短信 (`data/tanshin/{code_4}/`)
- `tanshin_{YYYY-MM}_{FY|Q1|Q2|Q3}.pdf`: アップロードPDF
- gitに永続化済み → 再アップロード不要、起動時に自動パース

### 証券コード検証のキャッシュ活用
Step 0の検証で以下のいずれかがあればyfinance検証をスキップ:
- `data/tanshin/{code}/` にPDFあり
- `data/edinet/{code}/meta.json` あり
- `data/stock/{code}/` に当日の株価キャッシュあり

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

### サマリーテーブル
- 列: コード、企業名、株価（円）、時価総額（百万円）、EV（百万円）、売上高LTM（百万円）、営業利益LTM（百万円）、EBITDA LTM（百万円）、EV/EBITDA LTM、FY PER、直近四半期PBR、配当利回り
- 数値は`NumberColumn`で生データ保持 → ソート(ascend/descend)が正しく動作
- 整数列: 桁区切りカンマ、小数点なし
- マルチプル: `x`付き（EV/EBITDA `5.8x`、PER `21.4x`、PBR `0.94x`）
- 配当利回り: `%`付き（`3.6%`）
- FY PERは決算短信の`ni_forecast`から動的再計算（アップロード後に即反映）

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

## デフォルト対象企業
6763(帝国通信工業), 6989(北陸電気工業), 6768(タムラ製作所), 6779(日本電波工業)

## デプロイ
- **Streamlit Cloud**: `shinjiquzilla/comps-app` リポジトリ main ブランチ連携
- **URL**: `comps-app-msdep7hegjdzmqjr3r4smg.streamlit.app`
- `runtime.txt` に `python-3.11` 指定（実際はCloud側で3.13が使われる場合あり）
- pushすると自動デプロイ

## Streamlit Cloud固有の注意点
- **`st.rerun()` は使わない**: Cloud環境ではst.rerun()がsession state消失を引き起こす場合がある。生成後の結果表示はsession stateフラグ（generation_done）で制御
- **session stateの肥大化に注意**: 大データをsession stateに入れるとメモリ圧迫→アプリ再起動→session state消失のリスク
- **結果表示セクションはtry-exceptでラップ**: エラー時にサイレントに元の画面に戻る問題を防止
- **EDINET API Key**: Cloud上では`st.secrets`の`edinet.api_key`で設定。未設定の場合はスキップされ株価のみ取得。`st.secrets`アクセスはtry-exceptで囲むこと
- **EDINET一括検索**: `fetch_companies_batch()`で全社まとめて1回の日付ループ。N社×日数→1×日数に削減
- **デフォルト検索期間400日**: 有報は決算後3ヶ月（3月決算→6月提出）、半期報も同様
- **sys.stdout書き換え禁止**: モジュールのトップレベルで`sys.stdout`を書き換えるとStreamlitのIO破壊でクラッシュ。`if __name__ == '__main__':`ガード必須
- **未使用.pyでもimportに注意**: Streamlit Cloudは全.pyを走査する場合がある。使わないモジュールでもimportエラーがあるとクラッシュ（auth.pyのgotrueで発生→try/except化）
- **yfinanceレート制限**: 複数銘柄連続取得でToo Many Requests。各社間に3秒ディレイ＋リトライ（5/10/15秒バックオフ）で対応済み。キャッシュヒット時は待機なし

## 修正履歴
| 日付 | コミット | 内容 |
|------|---------|------|
| 2026/2/26 | 7313449 | 証券コード検証: EDINET/株価キャッシュがある企業もyfinanceスキップ |
| 2026/2/26 | 06afb73 | EDINETパース結果キャッシュ時にZIP/PDF読み込みを完全スキップ |
| 2026/2/26 | 5abc43e | 株価キャッシュヒット時に待機・取得メッセージを表示しない |
| 2026/2/26 | b21d269 | 配当利回りを有報実績DPSベースに変更、サマリーに配当利回り列追加 |
| 2026/2/26 | 02b0de1 | サマリーテーブル: EV/EBITDA → EV/EBITDA LTM に列名修正 |
| 2026/2/26 | 504563f | サマリーテーブル: PERを決算短信から再計算、列名をFY PER/直近四半期PBRに修正 |
| 2026/2/26 | 61e1493 | 不足データ表示を改善: PER必須 vs 配当利回り任意を分離 |
| 2026/2/26 | a137b1c | EDINETパース結果をJSONキャッシュ化（reboot時の再パース不要） |
| 2026/2/26 | ccb68a0 | 株価データの日次キャッシュを追加 |
| 2026/2/26 | 0bc96f2 | パーサー: 業績予想ヘッダー欠落時のフォールバック追加（KOA等） |
| 2026/2/26 | 34590c6 | パーサー: PyMuPDFの複数行テーブル抽出に対応 |
| 2026/2/26 | 1de5723 | パーサー: 百万円/増減率が交互に並ぶテーブル形式に対応 |
| 2026/2/26 | — | 不足データ自動検出（PER→具体的な決算短信をリクエスト）、保存済みPDF自動パース |
| 2026/2/26 | 5f79763 | 決算短信PDFをgit永続化、保存済みPDFで過不足チェック |
| 2026/2/26 | 1f3dfa9 | 決算短信一括アップロード＆自動判定、証券コード検証 |
| 2026/2/26 | 6813b01 | Calendarize: 正確なLTM計算（前期H1自動抽出）+ 決算短信PDFアップロード＆パース |
| 2026/2/26 | 522831e | TDnet機能を完全無効化（有料サービスでログイン必要）、検索期間400日に戻す |
| 2026/2/26 | 0392c01 | EDINET一括検索で高速化（N社×日数→1×日数） |
| 2026/2/26 | d73ccfc | EDINET CSVパーサーにIFRS対応追加（6779空データ修正） |
| 2026/2/26 | 5cb98db | 手動補完UIに株価・株式数入力＋マルチプル自動再計算を追加 |
| 2026/2/26 | 31b775f | 手動補完UIの金額を整数（百万円）表示に変更 |
| 2026/2/26 | d25c30c | EDINET periodEnd=Noneでのソートクラッシュ修正 |
| 2026/2/26 | 8fc1b25 | yfinanceレート制限対策（リトライ+バックオフ+社間ディレイ） |
| 2026/2/26 | 9647a47 | auth.pyのgotrue importをtry/except化（Cloud未使用.pyクラッシュ対策） |
| 2026/2/26 | fbdfb58 | comps_generatorのsys.stdout書き換えをCLIのみに限定 |
| 2026/2/26 | baee8f1 | st.secretsアクセスをtry-exceptでラップ（secrets未設定対応） |
| 2026/2/26 | aa347aa | ログイン機能を無効化（auth.pyは残置、app.pyからの呼び出し・gotrueを削除） |
| 2026/2/26 | b7c90b4 | Cloud上で生成後に結果が表示されない問題を修正 |
| 2026/2/25 | a21e95c | runtime.txt追加、type hint互換性修正 |
| 〜2026/2/25 | 初期構築 | EDINET/TDnet/yfinance連携、手動補完UI、Excel出力 |

## 対象企業の会計基準
| コード | 会社名 | 会計基準 | EDINET取得状況 |
|--------|--------|---------|---------------|
| 6763 | 帝国通信工業 | J-GAAP | OK |
| 6989 | 北陸電気工業 | J-GAAP | OK |
| 6768 | タムラ製作所 | J-GAAP | OK |
| 6779 | 日本電波工業 | IFRS | OK |

## 既知の課題・TODO
- TDnet機能は無効化済み（有料サービス）。業績予想は決算短信PDFアップロード or 手動入力で対応
- 決算短信パーサーは東証規定フォーマットを前提。企業ごとの微差でパース失敗する場合あり → 3段階フォールバックで対応
- EDINET APIは1日1リクエスト/秒のレート制限あり（`time.sleep(1)`で対応済み）
- yfinanceレート制限は完全には回避できない→日次キャッシュ＋手動株価入力で代替可能
