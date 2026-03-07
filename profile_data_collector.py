"""
Profile Data Collector — EDINET有報からプロファイルデータを抽出し、全ソースを統合。

取得情報:
  - 役員リスト（氏名・役職・経歴・生年月日）
  - 株主Top10（株主名・持株比率）
  - 会社概要（代表者名・従業員数・事業内容・沿革）
  - 財務データ（既存モジュール再利用）
  - Webデータ（Wikipedia + 会社HP + Claude API）
  - 株価1年分日次データ（チャート用）

キャッシュ: data/edinet/{code}/profile_parsed.json
"""

import io
import json
import os
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path

from edinet_client import (
    CACHE_BASE,
    get_cache_dir,
    load_cached_meta,
    parse_csv_lines,
)

# ---------------------------------------------------------------------------
# Company name lookup from DealPortal companies master
# ---------------------------------------------------------------------------

def _lookup_company_name(code_4: str) -> str:
    """Look up Japanese company name from DealPortal companies master table."""
    try:
        from dealportal_supabase import get_dealportal_supabase
        sb = get_dealportal_supabase()
        if sb:
            resp = (
                sb.table("companies")
                .select("name_ja")
                .eq("ticker", code_4)
                .limit(1)
                .execute()
            )
            if resp.data and resp.data[0].get("name_ja"):
                name = resp.data[0]["name_ja"]
                print(f"  [Companies Master] {code_4} → {name}")
                return name
    except Exception as e:
        print(f"  [Companies Master] lookup failed: {e}")
    return ""


# ---------------------------------------------------------------------------
# EDINET Profile Elements
# ---------------------------------------------------------------------------

PROFILE_ELEMENTS = {
    # 代表者
    'jpcrp_cor:TitleAndNameOfRepresentativeCoverPage': 'representative',
    # 事業内容
    'jpcrp_cor:DescriptionOfBusinessTextBlock': 'business_description',
    # 沿革
    'jpcrp_cor:CompanyHistoryTextBlock': 'company_history',
    # 従業員数
    'jpcrp_cor:NumberOfEmployees': 'num_employees',
    # 株主名
    'jpcrp_cor:NameMajorShareholders': 'shareholder_name',
    # 持株比率
    'jpcrp_cor:ShareholdingRatio': 'shareholder_ratio',
    # 株主住所
    'jpcrp_cor:AddressMajorShareholders': 'shareholder_address',
    # 役員氏名
    'jpcrp_cor:NameInformationAboutDirectorsAndCorporateAuditors': 'director_name',
    # 役員役職
    'jpcrp_cor:OfficialTitleOrPositionInformationAboutDirectorsAndCorporateAuditors': 'director_title',
    # 役員経歴
    'jpcrp_cor:CareerSummaryInformationAboutDirectorsAndCorporateAuditors'
    'TextBlock': 'director_career',
    # 役員生年月日
    'jpcrp_cor:DateOfBirthInformationAboutDirectorsAndCorporateAuditors': 'director_dob',
}


def _extract_member_order(context_id):
    """
    コンテキストIDから並び順を抽出。

    株主: 'CurrentYearInstant_No3MajorShareholdersMember' → 3
    役員: 'FilingDateInstant_jpcrp030000-asr_E01782-000HanyuMasuoMember' → member名
    """
    # 株主: No{N}MajorShareholdersMember
    m = re.search(r'No(\d+)MajorShareholdersMember', context_id)
    if m:
        return int(m.group(1))

    # 役員: member名を返す
    m = re.search(r'(?:asr|ssr)_\w+-\d+(\w+Member)$', context_id)
    if m:
        return m.group(1)

    return context_id


def extract_profile_from_edinet(code_4, use_cache=True):
    """
    既存の有報ZIP（data/edinet/{code}/）からプロファイルデータを抽出。

    Returns:
        dict: {
            'representative': str,
            'num_employees': int,
            'business_description': str,
            'company_history': str,
            'shareholders': [{'rank': 1, 'name': ..., 'ratio': ...}, ...],
            'directors': [{'name': ..., 'title': ..., 'career': ..., 'dob': ...}, ...],
        }
    """
    cache_dir = CACHE_BASE / str(code_4)
    profile_cache = cache_dir / "profile_parsed.json"

    # キャッシュ確認
    if use_cache and profile_cache.exists():
        try:
            data = json.loads(profile_cache.read_text(encoding="utf-8"))
            if data:
                print(f"  [EDINET Profile] キャッシュヒット: {profile_cache}")
                return data
        except Exception:
            pass

    # 有報ZIPを探す
    yuho_zip = None
    if cache_dir.exists():
        for f in sorted(cache_dir.glob("yuho_*.zip"), reverse=True):
            yuho_zip = f
            break

    if not yuho_zip:
        # Playwright で EDINET から自動ダウンロード
        try:
            from edinet_scraper import search_and_download
            from playwright.sync_api import sync_playwright as _pw_check
            print(f"  [EDINET Profile] Playwright で有報DL中: {code_4}")
            cache_dir.mkdir(parents=True, exist_ok=True)
            docs = search_and_download(code_4, period="1year", cache_dir=cache_dir)
            for doc in docs:
                if doc['doc_type'] == 'yuho':
                    yuho_zip = doc['zip_path']
                    break
        except Exception as e:
            print(f"  [EDINET Profile] Playwright DL失敗: {e}")

    if not yuho_zip:
        print(f"  [EDINET Profile] 有報ZIPが見つかりません: {code_4}")
        return {}

    print(f"  [EDINET Profile] 有報パース中: {yuho_zip.name}")
    zip_bytes = yuho_zip.read_bytes()

    # CSV行を取得
    lines = parse_csv_lines(zip_bytes)
    if not lines:
        return {}

    # プロファイルデータを抽出
    result = {
        'representative': '',
        'num_employees': None,
        'business_description': '',
        'company_history': '',
        'shareholders': [],
        'directors': [],
    }

    # 一時格納
    shareholder_names = {}   # {rank: name}
    shareholder_ratios = {}  # {rank: ratio}
    director_data = {}       # {member_key: {name, title, career, dob}}

    # 当期のみ対象
    current_periods = ('当期', '当期末', '提出日現在')

    for line in lines[1:]:
        parts = line.split('\t')
        if len(parts) < 9:
            continue

        def clean(s):
            return s.strip().strip('"').strip()

        element_id = clean(parts[0])
        context_id = clean(parts[2])
        relative_year = clean(parts[3])
        value_str = clean(parts[8])

        if not value_str or value_str in ('', '－', '-', '―'):
            continue

        # 代表者（表紙）
        if element_id == 'jpcrp_cor:TitleAndNameOfRepresentativeCoverPage':
            result['representative'] = value_str

        # 事業内容
        elif element_id == 'jpcrp_cor:DescriptionOfBusinessTextBlock':
            if relative_year in ('提出日現在', '当期') or not result['business_description']:
                result['business_description'] = value_str[:3000]

        # 沿革
        elif element_id == 'jpcrp_cor:CompanyHistoryTextBlock':
            result['company_history'] = value_str[:3000]

        # 従業員数（連結・当期の最初の値）
        elif element_id == 'jpcrp_cor:NumberOfEmployees':
            consolidated = clean(parts[4]) if len(parts) > 4 else ''
            if consolidated in ('連結', 'その他') and result['num_employees'] is None:
                try:
                    result['num_employees'] = int(float(value_str.replace(',', '')))
                except ValueError:
                    pass

        # 株主名
        elif element_id == 'jpcrp_cor:NameMajorShareholders':
            rank = _extract_member_order(context_id)
            if isinstance(rank, int):
                shareholder_names[rank] = value_str

        # 持株比率
        elif element_id == 'jpcrp_cor:ShareholdingRatio':
            rank = _extract_member_order(context_id)
            if isinstance(rank, int):
                try:
                    shareholder_ratios[rank] = float(value_str)
                except ValueError:
                    pass

        # 役員氏名
        elif element_id == 'jpcrp_cor:NameInformationAboutDirectorsAndCorporateAuditors':
            member_key = _extract_member_order(context_id)
            director_data.setdefault(member_key, {})['name'] = value_str

        # 役員役職
        elif ('OfficialTitleOrPosition' in element_id
              and 'DirectorsAndCorporateAuditors' in element_id):
            member_key = _extract_member_order(context_id)
            director_data.setdefault(member_key, {})['title'] = value_str.strip()

        # 役員経歴
        elif ('CareerSummary' in element_id
              and 'DirectorsAndCorporateAuditors' in element_id):
            member_key = _extract_member_order(context_id)
            director_data.setdefault(member_key, {})['career'] = value_str[:2000]

        # 役員生年月日
        elif element_id == 'jpcrp_cor:DateOfBirthInformationAboutDirectorsAndCorporateAuditors':
            member_key = _extract_member_order(context_id)
            director_data.setdefault(member_key, {})['dob'] = value_str

    # 株主Top10を整理
    for rank in sorted(shareholder_names.keys()):
        result['shareholders'].append({
            'rank': rank,
            'name': shareholder_names[rank],
            'ratio': shareholder_ratios.get(rank),
        })

    # 役員リストを整理（コンテキストID出現順を保持）
    for member_key, data in director_data.items():
        if data.get('name'):
            result['directors'].append({
                'name': data.get('name', ''),
                'title': data.get('title', ''),
                'career': data.get('career', ''),
                'dob': data.get('dob', ''),
            })

    print(f"  [EDINET Profile] 株主{len(result['shareholders'])}名, 役員{len(result['directors'])}名")

    # キャッシュ保存
    if use_cache:
        try:
            profile_cache.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    return result


def fetch_stock_history(code_4, days=365):
    """
    株価1年分の日次データを取得（チャート用）。J-Quants API を使用。

    Returns:
        list of (date_str, close_price) or empty list
    """
    try:
        import requests as req
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # J-Quants API key を取得
        api_key = None
        try:
            secrets_path = Path(__file__).parent / ".streamlit" / "secrets.toml"
            if secrets_path.exists():
                text = secrets_path.read_text(encoding="utf-8")
                in_jquants = False
                for line in text.split("\n"):
                    if line.strip() == "[jquants]":
                        in_jquants = True
                        continue
                    if line.strip().startswith("[") and in_jquants:
                        break
                    if in_jquants and "api_key" in line and "=" in line:
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass

        if not api_key:
            api_key = os.environ.get("JQUANTS_API_KEY", "")

        if not api_key:
            print("  [株価履歴] J-Quants API key なし")
            return []

        code_5 = code_4 + "0"
        end = date.today()
        start = end - timedelta(days=days)

        headers = {"x-api-key": api_key}
        url = "https://api.jquants.com/v2/equities/bars/daily"
        params = {
            "code": code_5,
            "from": start.strftime("%Y-%m-%d"),
            "to": end.strftime("%Y-%m-%d"),
        }
        resp = req.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        bars = data.get("daily_quotes") or data.get("data") or []
        if not bars:
            return []

        result = []
        for bar in bars:
            d = bar.get("Date", "")
            close = bar.get("AdjustmentClose") or bar.get("AdjC") or bar.get("Close") or bar.get("C")
            if d and close is not None:
                result.append((d, float(close)))

        return result
    except Exception as e:
        print(f"  [株価履歴] 取得失敗: {e}")
        return []


def collect_profile_data(code_4, company_name_ja=None, company_url="", use_cache=True):
    """
    全ソースを統合してプロファイルデータを収集。

    Parameters:
        code_4: 証券コード
        company_name_ja: 日本語会社名（Noneの場合EDINET metaから取得）
        company_url: 会社HP URL（オプション）
        use_cache: キャッシュ使用フラグ

    Returns:
        dict: 統合プロファイルデータ
    """
    print(f"\n=== プロファイルデータ収集: {code_4} ===")

    # 1. 会社名を取得
    if company_name_ja is None:
        meta = load_cached_meta(code_4)
        if meta:
            company_name_ja = meta.get('company_name', '')
        # Fallback: DealPortal companies master table (JPX 4000+ companies)
        if not company_name_ja:
            company_name_ja = _lookup_company_name(code_4)
        if not company_name_ja:
            company_name_ja = code_4

    # 2. EDINETプロファイル
    edinet_profile = extract_profile_from_edinet(code_4, use_cache=use_cache)

    # 2b. EDINET有報がない場合（Render等）、DBから既存データをフォールバック取得
    if not edinet_profile.get('directors') and not edinet_profile.get('shareholders'):
        try:
            from dealportal_supabase import load_directors_shareholders, get_existing_profile_data
            db_directors, db_shareholders = load_directors_shareholders(code_4)
            if db_directors:
                edinet_profile['directors'] = db_directors
                print(f"  [DB Fallback] 役員 {len(db_directors)}名取得")
            if db_shareholders:
                edinet_profile['shareholders'] = db_shareholders
                print(f"  [DB Fallback] 株主 {len(db_shareholders)}名取得")

            # 代表者・事業概要も既存プロフィールから取得
            if not edinet_profile.get('representative') or not edinet_profile.get('business_description'):
                existing = get_existing_profile_data(code_4)
                if existing:
                    if not edinet_profile.get('representative') and existing.get('representative'):
                        edinet_profile['representative'] = existing['representative']
                    if not edinet_profile.get('business_description') and existing.get('business_description'):
                        edinet_profile['business_description'] = existing['business_description']
                    if not edinet_profile.get('num_employees') and existing.get('num_employees'):
                        edinet_profile['num_employees'] = existing['num_employees']
                    print(f"  [DB Fallback] 既存プロフィールデータを再利用")
        except Exception as e:
            print(f"  [DB Fallback] 失敗: {e}")

    # 3. Webデータ（Wikipedia + Claude API）
    from profile_web_collector import collect_web_data
    web_data = collect_web_data(
        code_4,
        company_name_ja,
        company_url=company_url,
        edinet_overview=edinet_profile.get('business_description', ''),
        use_cache=use_cache,
    )

    # 4. J-Quantsデータ（既存）
    jquants_data = None
    try:
        from jquants_client import fetch_fins_summary
        jquants_data = fetch_fins_summary(code_4)
        if jquants_data:
            print(f"  [J-Quants] データ取得成功")
    except Exception as e:
        print(f"  [J-Quants] 取得失敗: {e}")

    # 5. 株価（既存）
    stock_data = {}
    try:
        from stock_fetcher import fetch_stock_info
        stock_data = fetch_stock_info(code_4)
        if stock_data.get('stock_price'):
            print(f"  [株価] {stock_data['stock_price']}円")
    except Exception as e:
        print(f"  [株価] 取得失敗: {e}")

    # 6. EDINETデータ（既存 — BS, D&A）
    # fetch_companies_batch を使用（Supabaseフォールバック対応）
    edinet_financial = {}
    try:
        from edinet_client import fetch_companies_batch
        batch_result = fetch_companies_batch([code_4])
        edinet_financial = batch_result.get(code_4, {})
        if edinet_financial.get('yuho_data') or edinet_financial.get('hanki_data'):
            print(f"  [EDINET Financial] データ取得成功 (yuho: {bool(edinet_financial.get('yuho_data'))}, hanki: {bool(edinet_financial.get('hanki_data'))})")
        else:
            print(f"  [EDINET Financial] データなし")
    except Exception as e:
        print(f"  [EDINET Financial] 取得失敗: {e}")

    # 7. build_company_data で統合計算
    company_data = None
    try:
        from financial_calc import build_company_data
        company_data = build_company_data(
            code_4,
            edinet_financial,
            {'forecast': {}},
            stock_data,
            jquants_data=jquants_data,
        )
        print(f"  [Financial] build_company_data 成功")
    except Exception as e:
        print(f"  [Financial] build_company_data 失敗: {e}")

    # 8. 株価履歴（チャート用）
    stock_history = fetch_stock_history(code_4)
    if stock_history:
        print(f"  [株価履歴] {len(stock_history)}日分取得")

    # 9. 統合
    # 英語社名: JPX master → stock_data → web_data → 日本語名にフォールバック
    name_en = ''
    try:
        from dealportal_supabase import lookup_company_name_en
        name_en = lookup_company_name_en(code_4)
        if name_en:
            print(f"  [英語社名] JPXマスタから取得: {name_en}")
    except Exception:
        pass
    if not name_en:
        name_en = stock_data.get('company_name_en', '')
    if not name_en and web_data:
        name_en = web_data.get('company_name_en', '')
    if not name_en:
        name_en = company_name_ja  # 最終フォールバック

    profile = {
        'code': code_4,
        'company_name': company_name_ja,
        'company_name_en': name_en,

        # EDINET Profile
        'representative': edinet_profile.get('representative', ''),
        'num_employees': edinet_profile.get('num_employees'),
        'business_description': edinet_profile.get('business_description', ''),
        'company_history': edinet_profile.get('company_history', ''),
        'shareholders': edinet_profile.get('shareholders', []),
        'directors': edinet_profile.get('directors', []),

        # Web data (Claude API)
        'web': web_data,

        # Financial data
        'financial': company_data or {},

        # Stock history for chart
        'stock_history': stock_history,
    }

    return profile


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "6763"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    data = collect_profile_data(code, name)
    # Print summary
    print(f"\n=== 結果サマリー ===")
    print(f"会社名: {data['company_name']}")
    print(f"代表者: {data['representative']}")
    print(f"従業員数: {data['num_employees']}")
    print(f"株主: {len(data['shareholders'])}名")
    print(f"役員: {len(data['directors'])}名")
    print(f"株価履歴: {len(data['stock_history'])}日")
    if data['financial']:
        f = data['financial']
        print(f"株価: {f.get('stock_price')}円, 時価総額: {f.get('market_cap')}百万円")
