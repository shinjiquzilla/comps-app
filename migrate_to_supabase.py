"""
migrate_to_supabase.py — ローカル data/ ディレクトリの全データを Supabase に移行。

Usage:
    py migrate_to_supabase.py
    py migrate_to_supabase.py --dry-run   # 件数確認のみ
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Streamlit secrets を非Streamlit環境で読み込むためのヘルパー
def _load_secrets():
    """secrets.toml から Supabase 接続情報を環境変数にセット。"""
    secrets_path = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if not secrets_path.exists():
        print(f"ERROR: {secrets_path} が見つかりません")
        sys.exit(1)
    # 簡易TOMLパーサー（supabase セクションのみ）
    in_supabase = False
    for line in secrets_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line == '[supabase]':
            in_supabase = True
            continue
        elif line.startswith('['):
            in_supabase = False
            continue
        if in_supabase and '=' in line:
            key, val = line.split('=', 1)
            key = key.strip()
            val = val.strip().strip('"')
            if key == 'url':
                os.environ['SUPABASE_URL'] = val
            elif key == 'anon_key':
                os.environ['SUPABASE_ANON_KEY'] = val


def main():
    dry_run = '--dry-run' in sys.argv

    _load_secrets()

    from supabase import create_client
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_ANON_KEY')
    if not url or not key:
        print("ERROR: Supabase URL/Key が設定されていません")
        sys.exit(1)

    sb = create_client(url, key)
    print(f"Supabase 接続OK: {url}")

    data_dir = Path(__file__).parent / "data"
    edinet_dir = data_dir / "edinet"
    stock_dir = data_dir / "stock"
    tanshin_dir = data_dir / "tanshin"
    forecasts_file = data_dir / "tanshin_forecasts.json"

    # ---------- Step 1: テーブル作成 ----------
    print("\n=== Step 1: テーブル作成 ===")
    schema_file = Path(__file__).parent / "schema.sql"
    if schema_file.exists():
        sql = schema_file.read_text(encoding='utf-8')
        # コメント行と空行を除去して個別のステートメントに分割
        statements = []
        current = []
        for line in sql.splitlines():
            stripped = line.strip()
            if stripped.startswith('--') or not stripped:
                continue
            current.append(line)
            if stripped.endswith(';'):
                statements.append('\n'.join(current))
                current = []

        for stmt in statements:
            if dry_run:
                print(f"  [DRY-RUN] SQL: {stmt[:80]}...")
            else:
                try:
                    sb.rpc('exec_sql', {'sql': stmt}).execute()
                except Exception:
                    # rpc が無い場合は postgrest 経由では実行不可
                    # → Supabase Dashboard で手動実行が必要
                    pass
        print(f"  {len(statements)} statements")
        print("  NOTE: テーブルが未作成の場合、Supabase Dashboard の SQL Editor で schema.sql を実行してください。")
    else:
        print("  schema.sql が見つかりません。Supabase Dashboard で手動作成してください。")

    # ---------- Step 2: EDINET データ ----------
    print("\n=== Step 2: EDINET データ移行 ===")
    edinet_count = 0
    financials_count = 0
    meta_count = 0

    if edinet_dir.exists():
        for code_dir in sorted(edinet_dir.iterdir()):
            if not code_dir.is_dir():
                continue
            code = code_dir.name
            if not code.isdigit() or len(code) != 4:
                continue

            # meta.json
            meta_path = code_dir / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
                company_name = meta.get('company_name', '')
                docs = meta.get('docs', [])
                last_searched = meta.get('last_searched')
                search_days = meta.get('search_days')

                if not dry_run:
                    # companies テーブル
                    try:
                        sb.table("companies").upsert(
                            {"code": code, "name": company_name},
                            on_conflict="code"
                        ).execute()
                    except Exception as e:
                        print(f"  WARN: companies upsert failed for {code}: {e}")

                    # edinet_meta テーブル
                    for doc in docs:
                        doc_id = doc.get('docID')
                        if not doc_id:
                            continue
                        dt_code = doc.get('docTypeCode', '')
                        doc_type = 'yuho' if dt_code in ('120', '130') else 'hanki'
                        period_end = (doc.get('periodEnd') or '').replace('/', '-')
                        if not period_end:
                            continue
                        try:
                            sb.table("edinet_meta").upsert({
                                "code": code,
                                "doc_id": doc_id,
                                "doc_type": doc_type,
                                "period_end": period_end,
                                "filer_name": doc.get('filerName'),
                                "last_searched": last_searched,
                                "search_days": search_days,
                                "raw_meta": doc,
                            }, on_conflict="doc_id").execute()
                            meta_count += 1
                        except Exception as e:
                            print(f"  WARN: edinet_meta upsert failed for {doc_id}: {e}")

                edinet_count += 1

            # yuho_parsed.json
            yuho_parsed_path = code_dir / "yuho_parsed.json"
            if yuho_parsed_path.exists():
                yuho_data = json.loads(yuho_parsed_path.read_text(encoding='utf-8'))

                # period_end を meta から取得
                period_end = None
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text(encoding='utf-8'))
                    for doc in meta.get('docs', []):
                        if doc.get('docTypeCode') in ('120', '130'):
                            period_end = (doc.get('periodEnd') or '').replace('/', '-')
                            break
                if not period_end:
                    period_end = date.today().isoformat()

                if not dry_run:
                    row = {
                        "code": code,
                        "doc_type": "yuho",
                        "period_end": period_end,
                        "raw_data": yuho_data,
                    }
                    # 既知カラムをマッピング
                    for col in ['revenue', 'operating_income', 'ordinary_income',
                                'net_income', 'depreciation', 'cash',
                                'investment_securities', 'short_term_debt',
                                'long_term_debt', 'bonds', 'current_long_term_debt',
                                'current_bonds', 'lease_debt_current',
                                'lease_debt_noncurrent', 'net_assets',
                                'shareholders_equity', 'equity_parent',
                                'equity_ratio', 'dps']:
                        if col in yuho_data:
                            row[col] = yuho_data[col]
                    try:
                        sb.table("financials").upsert(
                            row, on_conflict="code,doc_type,period_end"
                        ).execute()
                        financials_count += 1
                    except Exception as e:
                        print(f"  WARN: financials upsert failed for {code}/yuho: {e}")

            # hanki_parsed.json
            hanki_parsed_path = code_dir / "hanki_parsed.json"
            if hanki_parsed_path.exists():
                hanki_parsed = json.loads(hanki_parsed_path.read_text(encoding='utf-8'))
                current_data = hanki_parsed.get('current', {})
                prior_data = hanki_parsed.get('prior', {})

                # period_end を meta から取得
                period_end = None
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text(encoding='utf-8'))
                    for doc in meta.get('docs', []):
                        if doc.get('docTypeCode') in ('160', '170'):
                            period_end = (doc.get('periodEnd') or '').replace('/', '-')
                            break
                if not period_end:
                    period_end = date.today().isoformat()

                if not dry_run:
                    for dt_label, data in [('hanki_current', current_data),
                                            ('hanki_prior', prior_data)]:
                        if not data:
                            continue
                        row = {
                            "code": code,
                            "doc_type": dt_label,
                            "period_end": period_end,
                            "raw_data": data,
                        }
                        for col in ['revenue', 'operating_income', 'ordinary_income',
                                    'net_income', 'depreciation', 'cash',
                                    'investment_securities', 'short_term_debt',
                                    'long_term_debt', 'bonds', 'current_long_term_debt',
                                    'current_bonds', 'lease_debt_current',
                                    'lease_debt_noncurrent', 'net_assets',
                                    'shareholders_equity', 'equity_parent',
                                    'equity_ratio', 'dps']:
                            if col in data:
                                row[col] = data[col]
                        try:
                            sb.table("financials").upsert(
                                row, on_conflict="code,doc_type,period_end"
                            ).execute()
                            financials_count += 1
                        except Exception as e:
                            print(f"  WARN: financials upsert failed for {code}/{dt_label}: {e}")

    print(f"  企業: {edinet_count}, meta: {meta_count}, financials: {financials_count}")

    # ---------- Step 3: 株価データ ----------
    print("\n=== Step 3: 株価データ移行 ===")
    stock_count = 0

    if stock_dir.exists():
        for code_dir in sorted(stock_dir.iterdir()):
            if not code_dir.is_dir():
                continue
            code = code_dir.name
            stock_file = code_dir / "stock.json"
            if not stock_file.exists():
                continue

            stock_data = json.loads(stock_file.read_text(encoding='utf-8'))
            if stock_data.get('stock_price') is None:
                continue

            if not dry_run:
                # companies テーブルに name_en を追加
                name_en = stock_data.get('company_name_en', '')
                if name_en:
                    try:
                        sb.table("companies").upsert(
                            {"code": code, "name_en": name_en},
                            on_conflict="code"
                        ).execute()
                    except Exception:
                        pass

                fetched = stock_data.get('_fetched_date', date.today().isoformat())
                row = {
                    "code": code,
                    "stock_price": stock_data.get('stock_price'),
                    "shares_outstanding": stock_data.get('shares_outstanding'),
                    "market_cap": stock_data.get('market_cap'),
                    "company_name_en": name_en,
                    "fetched_date": fetched,
                }
                try:
                    sb.table("stock_data").upsert(
                        row, on_conflict="code,fetched_date"
                    ).execute()
                    stock_count += 1
                except Exception as e:
                    print(f"  WARN: stock_data upsert failed for {code}: {e}")

    print(f"  株価: {stock_count}件")

    # ---------- Step 4: 決算短信予想値 ----------
    print("\n=== Step 4: 決算短信予想値移行 ===")
    forecast_count = 0

    if forecasts_file.exists():
        forecasts = json.loads(forecasts_file.read_text(encoding='utf-8'))
        for code, data in forecasts.items():
            if not dry_run:
                # companies に存在しない場合は作成
                try:
                    sb.table("companies").upsert(
                        {"code": code},
                        on_conflict="code"
                    ).execute()
                except Exception:
                    pass

                # fy_month / period_type を決算短信PDFファイル名から推定
                fy_month = data.get('fy_month', 'unknown')
                period_type = data.get('period_type', 'unknown')
                if fy_month == 'unknown' or period_type == 'unknown':
                    code_tanshin_dir = tanshin_dir / code
                    if code_tanshin_dir.is_dir():
                        # tanshin_2026-03_Q1.pdf のようなファイル名からパース
                        for pdf_path in sorted(code_tanshin_dir.glob("tanshin_*.pdf"), reverse=True):
                            parts = pdf_path.stem.split('_')
                            if len(parts) >= 3:
                                fy_month = parts[1]   # '2026-03'
                                period_type = parts[2]  # 'Q1', 'FY' etc.
                                break

                row = {
                    "code": code,
                    "rev_forecast": data.get('rev_forecast'),
                    "op_forecast": data.get('op_forecast'),
                    "ni_forecast": data.get('ni_forecast'),
                    "fy_month": fy_month,
                    "period_type": period_type,
                }
                try:
                    sb.table("tanshin_forecasts").upsert(
                        row, on_conflict="code,fy_month,period_type"
                    ).execute()
                    forecast_count += 1
                except Exception as e:
                    print(f"  WARN: tanshin_forecasts upsert failed for {code}: {e}")

    print(f"  予想値: {forecast_count}件")

    # ---------- Step 5: 決算短信PDF → Storage ----------
    print("\n=== Step 5: 決算短信PDF → Storage ===")
    pdf_count = 0

    if tanshin_dir.exists():
        for code_dir in sorted(tanshin_dir.iterdir()):
            if not code_dir.is_dir():
                continue
            code = code_dir.name
            for pdf_path in code_dir.glob("*.pdf"):
                if not dry_run:
                    storage_path = f"{code}/{pdf_path.name}"
                    try:
                        sb.storage.from_("tanshin-pdfs").upload(
                            storage_path,
                            pdf_path.read_bytes(),
                            file_options={"content-type": "application/pdf", "upsert": "true"}
                        )
                        pdf_count += 1
                    except Exception as e:
                        err_msg = str(e)
                        if 'Duplicate' in err_msg or 'already exists' in err_msg:
                            pdf_count += 1  # 既にアップロード済み
                        else:
                            print(f"  WARN: PDF upload failed for {storage_path}: {e}")
                else:
                    pdf_count += 1

    print(f"  PDF: {pdf_count}件")

    # ---------- Summary ----------
    print("\n=== 移行完了 ===")
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"{prefix}企業: {edinet_count}")
    print(f"{prefix}EDINET meta: {meta_count}")
    print(f"{prefix}Financials: {financials_count}")
    print(f"{prefix}株価: {stock_count}")
    print(f"{prefix}予想値: {forecast_count}")
    print(f"{prefix}PDF: {pdf_count}")


if __name__ == '__main__':
    main()
