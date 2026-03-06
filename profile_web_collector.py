"""
Web Collector — 会社HP + Claude API で企業プロファイル情報を構造化抽出。

取得情報:
  - 設立年、本社所在地、事業概要（英語/日本語）
  - グローバル拠点、グループ会社数
  - 企業ヘッドライン（英語2-3文）

データソース優先順位:
  1. 会社HP（証券コードでJPXマスタから社名確定 → URL推定 → LLMで構造化抽出）
  2. EDINET有報の事業内容（フォールバック）

キャッシュ: data/web/{code}/profile_web.json
"""

import json
import os
import re
import unicodedata
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_WEB_CACHE_DIR = Path(__file__).parent / "data" / "web"


def _normalize_company_name(name):
    """全角英数字→半角に変換し、「株式会社」等を除去して検索用の名前を返す。"""
    normalized = unicodedata.normalize("NFKC", name)
    normalized = re.sub(r'(株式会社|有限会社|合同会社)', '', normalized).strip()
    return normalized


def _get_anthropic_key():
    """Anthropic API Keyを取得。環境変数 → .streamlit/secrets.toml の順。"""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    secrets_path = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if secrets_path.exists():
        try:
            text = secrets_path.read_text(encoding="utf-8")
            for line in text.split("\n"):
                if "ANTHROPIC_API_KEY" in line.upper() and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except Exception:
            pass
    return ""


def fetch_company_website(url):
    """
    会社HPからテキストを取得。

    Parameters:
        url: 会社ページのURL

    Returns:
        str: ページテキスト（最大5000文字）。失敗時は空文字。
    """
    if not url:
        return ""
    try:
        from bs4 import BeautifulSoup
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=15, verify=False)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        return text[:5000]
    except Exception as e:
        print(f"  [会社HP] 取得失敗: {e}")
        return ""


def extract_company_info_with_llm(hp_text="", edinet_overview=""):
    """
    Claude API で構造化データを抽出。

    Parameters:
        hp_text: 会社HPテキスト
        edinet_overview: EDINET有報の事業内容テキスト

    Returns:
        dict: 構造化された企業情報
    """
    api_key = _get_anthropic_key()
    if not api_key:
        print("  [Claude API] API Key未設定。スキップします。")
        return _fallback_extraction(edinet_overview)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        source_text = ""
        if hp_text:
            source_text += f"=== Company Website ===\n{hp_text}\n\n"
        if edinet_overview:
            source_text += f"=== EDINET Annual Report (Business Description) ===\n{edinet_overview}\n\n"

        if not source_text.strip():
            return {}

        prompt = """From the following company information sources, extract structured data in JSON format.

IMPORTANT:
- For main_business_ja and main_business_en: write a concise description of the company's actual products and services, target markets, and competitive strengths.
- PRIORITIZE the Company Website as the primary source for business description.
- Use EDINET for group structure and financial context.
- Keep descriptions factual and specific to the company, suitable for investment banking presentations.

Return ONLY valid JSON with these fields (use null if information is not available):

{
  "company_name_en": "Official English company name (e.g., 'Teikoku Tsushin Kogyo Co., Ltd.')",
  "founding_year": "Year founded (e.g., '1944')",
  "headquarters": "Headquarters location in Japanese (e.g., '東京都渋谷区')",
  "headquarters_en": "Headquarters location in English (e.g., 'Shibuya-ku, Tokyo, Japan')",
  "main_business_en": "2-3 sentence description of main business in English, based primarily on the company website",
  "main_business_ja": "事業内容の要約（日本語2-3文、会社HPの情報を優先）",
  "business_desc_source": "URL of the primary source used for business description (company website URL if available)",
  "headline_en": "2-3 sentence company overview headline suitable for an investor presentation, in English. Focus on market position, key products/services, and scale.",
  "global_footprint": "List of countries/regions where the company operates (e.g., 'Japan, China, Singapore, USA, Germany')",
  "group_companies": "Number and description of group companies (e.g., '16 consolidated subsidiaries')"
}

Sources:
""" + source_text

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()

        return json.loads(response_text)

    except Exception as e:
        print(f"  [Claude API] エラー: {e}")
        return _fallback_extraction(edinet_overview)


def _fallback_extraction(edinet_overview=""):
    """Claude API が使えない場合の簡易抽出（EDINETテキストから正規表現）。"""
    result = {}
    if not edinet_overview:
        return result

    # --- 設立年 ---
    m = re.search(r'(19\d{2}|20[0-2]\d)年.*?(設立|創立|創業|設置)', edinet_overview)
    if m:
        result["founding_year"] = m.group(1)
    else:
        m = re.search(r'(?:設立|創立|創業|設置)[：:\s]*(\d{4})年', edinet_overview)
        if m:
            result["founding_year"] = m.group(1)

    # --- 本社所在地 ---
    _pref = (r'(?:東京都|北海道|(?:大阪|京都|兵庫|奈良|和歌山|滋賀|三重)府|'
             r'(?:青森|岩手|宮城|秋田|山形|福島|茨城|栃木|群馬|埼玉|千葉|神奈川|'
             r'新潟|富山|石川|福井|山梨|長野|岐阜|静岡|愛知|鳥取|島根|岡山|広島|'
             r'山口|徳島|香川|愛媛|高知|福岡|佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄)県)')
    m = re.search(
        r'(' + _pref + r'[^\n]{1,30}?)に本[社店]を置く',
        edinet_overview,
    )
    if m:
        result["headquarters"] = m.group(1).strip()
    else:
        m = re.search(
            r'(?:本社所在地|本店所在地)[：:\s\n]*'
            r'(' + _pref + r'[^\n]{2,30})',
            edinet_overview,
        )
        if m:
            hq = m.group(1).strip().rstrip("。、．.")
            result["headquarters"] = hq

    # --- 事業概要（EDINET冒頭1-2文） ---
    first_sentence = re.split(r'(?<=。)', edinet_overview[:500])
    if first_sentence:
        desc = "".join(first_sentence[:2]).strip()
        if len(desc) > 20:
            result["main_business_ja"] = desc[:300]

    return result


def _guess_company_url(company_name_ja, code_4):
    """証券コードと会社名から企業サイトURLを推定。"""
    search_name = _normalize_company_name(company_name_ja).lower().replace(" ", "")
    candidates = [
        f"https://www.{search_name}.co.jp",
        f"https://www.{search_name}.com",
        f"https://{search_name}.co.jp",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    for url in candidates:
        try:
            resp = requests.head(url, headers=headers, timeout=5, verify=False,
                                 allow_redirects=True)
            if resp.status_code < 400:
                print(f"  [会社HP] URL推定成功: {url}")
                return url
        except Exception:
            continue
    return ""


def collect_web_data(code_4, company_name_ja, company_url="", edinet_overview="",
                     use_cache=True):
    """
    会社HP + Claude API から企業プロファイル情報を収集。

    データソース:
      1. 会社HP（URL推定 → LLMで構造化抽出）
      2. EDINET有報の事業内容（フォールバック）

    Parameters:
        code_4: 証券コード
        company_name_ja: 日本語会社名
        company_url: 会社HP URL（オプション）
        edinet_overview: EDINET事業内容テキスト（オプション）
        use_cache: キャッシュ使用フラグ

    Returns:
        dict: 構造化された企業情報
    """
    cache_dir = _WEB_CACHE_DIR / str(code_4)
    cache_file = cache_dir / "profile_web.json"

    # キャッシュ確認
    if use_cache and cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data:
                print(f"  [Web] キャッシュヒット: {cache_file}")
                return data
        except Exception:
            pass

    print(f"  [Web] 会社HP + Claude API で情報収集: {company_name_ja} ({code_4})")

    # 会社HPのURLが未指定の場合は推定
    if not company_url:
        company_url = _guess_company_url(company_name_ja, code_4)

    # 会社HP取得
    hp_text = fetch_company_website(company_url) if company_url else ""
    if hp_text:
        print(f"  [会社HP] {len(hp_text)}文字取得")
    else:
        print(f"  [会社HP] 取得できず。EDINETフォールバックを使用")

    # Claude APIで構造化
    result = extract_company_info_with_llm(hp_text, edinet_overview)

    # キャッシュ保存
    if result:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [Web] キャッシュ保存: {cache_file}")

    return result


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "6763"
    name = sys.argv[2] if len(sys.argv) > 2 else "帝国通信工業"
    data = collect_web_data(code, name, use_cache=False)
    print(json.dumps(data, ensure_ascii=False, indent=2))
