"""
Company Profile PPTX Generator — CLI エントリポイント

Usage:
    py generate_profile.py 6763 --comps 6989,6768,6779
    py generate_profile.py 6135 --comps 6989,6768,6779 --output profile.pptx
    py generate_profile.py 6763  # Compsなし（Overview + Directors のみ）

Options:
    code_4          対象企業の証券コード（4桁）
    --comps         Comps対象企業のコード（カンマ区切り）
    --output        出力ファイルパス（デフォルト: {code}_{name}_Company_Profile.pptx）
    --no-cache      キャッシュを使わず全データを再取得
    --no-web        Webデータ収集をスキップ（Wikipedia/Claude API）
"""

import argparse
import os
import sys
from pathlib import Path

# SSL検証バイパス（社内ネットワーク対応）
os.environ['CURL_CA_BUNDLE'] = ''
os.environ['REQUESTS_CA_BUNDLE'] = ''


def main():
    parser = argparse.ArgumentParser(
        description="Company Profile PPTX を自動生成"
    )
    parser.add_argument(
        "code", type=str,
        help="対象企業の証券コード（4桁）"
    )
    parser.add_argument(
        "--comps", type=str, default="",
        help="Comps対象企業のコード（カンマ区切り）"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="出力ファイルパス"
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="キャッシュを使わず全データを再取得"
    )
    parser.add_argument(
        "--no-web", action="store_true",
        help="Webデータ収集をスキップ"
    )

    args = parser.parse_args()

    code_4 = args.code.strip()
    use_cache = not args.no_cache
    comps_codes = [c.strip() for c in args.comps.split(",") if c.strip()]

    print(f"=== Company Profile Generator ===")
    print(f"対象企業: {code_4}")
    if comps_codes:
        print(f"Comps対象: {', '.join(comps_codes)}")
    print()

    # --- Step 1: プロファイルデータ収集 ---
    from profile_data_collector import collect_profile_data
    profile = collect_profile_data(code_4, use_cache=use_cache)

    if args.no_web:
        profile['web'] = {}

    # --- Step 2: Comps データ収集 ---
    comps_data = []
    if comps_codes:
        print(f"\n=== Comps データ収集 ===")

        # 全コード（対象企業含む）
        all_codes = [code_4] + [c for c in comps_codes if c != code_4]

        from edinet_client import fetch_companies_batch
        from stock_fetcher import fetch_stock_info
        from financial_calc import build_company_data

        # J-Quants 一括取得
        jquants_all = {}
        try:
            from jquants_client import fetch_fins_summary
            for c in all_codes:
                jq = fetch_fins_summary(c)
                if jq:
                    jquants_all[c] = jq
                    print(f"  [J-Quants] {c}: OK")
        except Exception as e:
            print(f"  [J-Quants] 一括取得失敗: {e}")

        # EDINET 一括取得
        edinet_all = fetch_companies_batch(all_codes, use_cache=use_cache)
        print(f"  [EDINET] {len(edinet_all)}社取得")

        # 株価 + build_company_data
        for c in all_codes:
            stock = fetch_stock_info(c)
            edinet = edinet_all.get(c, {})
            jq = jquants_all.get(c)

            # Supabase から tanshin_forecasts を取得
            forecasts = {}
            try:
                from supabase_client import load_forecasts
                all_forecasts = load_forecasts()
                if all_forecasts and c in all_forecasts:
                    forecasts = {'forecast': all_forecasts[c]}
            except Exception:
                pass

            try:
                cd = build_company_data(
                    c, edinet, forecasts, stock, jquants_data=jq
                )
                comps_data.append(cd)
                print(f"  [Comps] {c} ({cd.get('name', 'N/A')}): "
                      f"Rev LTM={cd.get('rev_ltm')}, EV/EBITDA={cd.get('_multiples', {}).get('ev_ebitda_ltm')}")
            except Exception as e:
                print(f"  [Comps] {c}: build_company_data 失敗: {e}")

    # --- Step 3: PPTX 生成 ---
    from profile_pptx_builder import build_profile_pptx
    output_path = build_profile_pptx(profile, comps_data, args.output)

    print(f"\n=== 完了 ===")
    print(f"出力: {output_path}")
    print(f"スライド数: {1 + max(0, (len(profile.get('directors', [])) + 5) // 6)}"
          f"{' + 2 (Comps)' if comps_data else ''} + 1 (Financial)")

    return output_path


if __name__ == "__main__":
    main()
