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

### EDINET CSV パーサー（IFRS対応済み）
- **J-GAAP**: `jppfs_cor:` プレフィックスの要素IDでマッチ
- **IFRS**: `jpigp_cor:〜IFRS` プレフィックスの要素IDを追加済み（6779 日本電波工業で確認）
- **連結フィルタ**: J-GAAPは`連結`、IFRSは`その他`。両方受け入れ、`個別`のみ除外
- **会社固有プレフィックス対応**: 半期報告書で`jpcrp040300-ssr_E01807-000:〜IFRS`のようなプレフィックスが使われる場合、コロン以降の要素名部分で動的マッチ
- **経営指標等（SUMMARY_ELEMENT_MAP）**: J-GAAP/IFRS両方の`jpcrp_cor:`要素をフォールバック用に登録

### LTM計算の制約
`financial_calc.py:128` - EDINET CSVから前期H1が自動取得困難なため、**通期値をそのままLTMの代理値として使用**（近似）。正確なLTMは手動補完UIで修正する想定。

### 手動補完UI
- 株価・発行済株式数を手入力可能（yfinanceレート制限時の対応）
- 入力値から時価総額・EV・EV/EBITDA・PER・PBRをリアルタイム自動計算（st.metric表示）
- 金額は整数表示（百万円）、DPSは小数1桁（円）

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
- **session stateの肥大化に注意**: TDnet PDFの全文テキスト等の大データをsession stateに入れるとメモリ圧迫→アプリ再起動→session state消失のリスク。`tdnet_raw`から`text`キーを除外済み
- **結果表示セクションはtry-exceptでラップ**: エラー時にサイレントに元の画面に戻る問題を防止。`st.error()`でエラーメッセージとトレースバックを表示
- **EDINET API Key**: Cloud上では`st.secrets`の`edinet.api_key`で設定。未設定の場合はスキップされ株価のみ取得。`st.secrets`アクセスはtry-exceptで囲むこと（secrets.toml未設定時にクラッシュするため）
- **EDINET一括検索**: `fetch_companies_batch()`で全社まとめて1回の日付ループ。N社×日数→1×日数に削減
- **TDnet検索はオプション（デフォルトOFF）**: サイドバーのチェックボックスで有効化。1社あたり数分追加されるため、必要時のみONに
- **デフォルト検索期間90日**: 有報・半期報の提出は決算後3ヶ月以内が多いため90日で十分
- **sys.stdout書き換え禁止**: モジュールのトップレベルで`sys.stdout`を書き換えるとStreamlitのIO破壊でクラッシュ。`if __name__ == '__main__':`ガード必須
- **未使用.pyでもimportに注意**: Streamlit Cloudは全.pyを走査する場合がある。使わないモジュールでもimportエラーがあるとクラッシュ（auth.pyのgotrueで発生→try/except化）
- **yfinanceレート制限**: 複数銘柄連続取得でToo Many Requests。各社間に3秒ディレイ＋リトライ（5/10/15秒バックオフ）で対応済み

## 修正履歴
| 日付 | コミット | 内容 |
|------|---------|------|
| 2026/2/26 | 0392c01 | EDINET一括検索で高速化（N社×400日→1×90日）、TDnetをオプション化 |
| 2026/2/26 | d73ccfc | EDINET CSVパーサーにIFRS対応追加（6779空データ修正） |
| 2026/2/26 | 5cb98db | 手動補完UIに株価・株式数入力＋マルチプル自動再計算を追加 |
| 2026/2/26 | 31b775f | 手動補完UIの金額を整数（百万円）表示に変更 |
| 2026/2/26 | d25c30c | EDINET periodEnd=Noneでのソートクラッシュ修正 |
| 2026/2/26 | 8fc1b25 | yfinanceレート制限対策（リトライ+バックオフ+社間ディレイ） |
| 2026/2/26 | 9647a47 | auth.pyのgotrue importをtry/except化（Cloud未使用.pyクラッシュ対策） |
| 2026/2/26 | fbdfb58 | comps_generatorのsys.stdout書き換えをCLIのみに限定 |
| 2026/2/26 | baee8f1 | st.secretsアクセスをtry-exceptでラップ（secrets未設定対応） |
| 2026/2/26 | aa347aa | ログイン機能を無効化（auth.pyは残置、app.pyからの呼び出し・gotrueを削除） |
| 2026/2/26 | b7c90b4 | Cloud上で生成後に結果が表示されない問題を修正（st.rerun追加、try-except、メモリ削減） |
| 2026/2/25 | a21e95c | runtime.txt追加、type hint互換性修正 |
| 〜2026/2/25 | 初期構築 | EDINET/TDnet/yfinance連携、手動補完UI、Excel出力 |

## 対象企業の会計基準
| コード | 会社名 | 会計基準 | EDINET取得状況 |
|--------|--------|---------|---------------|
| 6763 | 帝国通信工業 | J-GAAP | OK |
| 6989 | 北陸電気工業 | J-GAAP | OK |
| 6768 | タムラ製作所 | J-GAAP | OK（TDnet予想なし） |
| 6779 | 日本電波工業 | IFRS | IFRS対応後OK（要確認） |

## 既知の課題・TODO
- LTM計算が通期値の近似になっている（前期H1自動取得未対応）
- TDnet決算短信PDFの業績予想抽出は正規表現ベースで、フォーマット差異に弱い（6768/6779で tanshin_count=0）
- EDINET APIは1日1リクエスト/秒のレート制限あり（`time.sleep(1)`で対応済み）
- TDnet検索のさらなる高速化（キャッシュ等）が今後の改善候補
- yfinanceレート制限は完全には回避できない→手動株価入力で代替可能
