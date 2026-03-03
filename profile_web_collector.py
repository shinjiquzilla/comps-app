"""
Web Collector — Wikipedia + 会社HP + Claude API で企業プロファイル情報を構造化抽出。

取得情報:
  - 設立年、本社所在地、事業概要（英語/日本語）
  - グローバル拠点、グループ会社数
  - 企業ヘッドライン（英語2-3文）

キャッシュ: data/web/{code}/profile_web.json
"""

import json
import os
import re
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_WEB_CACHE_DIR = Path(__file__).parent / "data" / "web"


def _get_anthropic_key():
    """Anthropic API Keyを取得。環境変数 → .streamlit/secrets.toml の順。"""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    # .streamlit/secrets.toml を直接パース
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


def fetch_wikipedia(company_name_ja):
    """
    Wikipedia日本語版からテキストを取得（MediaWiki API）。

    Parameters:
        company_name_ja: 日本語の会社名（例: "帝国通信工業"）

    Returns:
        str: Wikipedia記事テキスト（最大5000文字）。見つからなければ空文字。
    """
    try:
        # MediaWiki API: 検索 → 本文取得
        search_url = "https://ja.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": company_name_ja,
            "srlimit": 3,
            "format": "json",
        }
        headers = {
            "User-Agent": "CompsApp/1.0 (Company Profile Generator; contact@quzilla.co.jp)"
        }
        resp = requests.get(search_url, params=params, headers=headers,
                            timeout=15, verify=False)
        data = resp.json()
        results = data.get("query", {}).get("search", [])
        if not results:
            return ""

        # 最も関連性の高い記事を選択
        page_title = results[0]["title"]

        # 本文テキスト取得
        params = {
            "action": "query",
            "titles": page_title,
            "prop": "extracts",
            "explaintext": True,
            "exlimit": 1,
            "format": "json",
        }
        resp = requests.get(search_url, params=params, headers=headers,
                            timeout=15, verify=False)
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            text = page.get("extract", "")
            if text:
                return text[:5000]
        return ""
    except Exception as e:
        print(f"  [Wikipedia] 取得失敗: {e}")
        return ""


def fetch_company_website(url):
    """
    会社HPの会社概要ページからテキストを取得。

    Parameters:
        url: 会社概要ページのURL

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

        # scriptとstyleを除去
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        return text[:5000]
    except Exception as e:
        print(f"  [会社HP] 取得失敗: {e}")
        return ""


def extract_company_info_with_llm(wiki_text, hp_text="", edinet_overview=""):
    """
    Claude API で構造化データを抽出。

    Parameters:
        wiki_text: Wikipedia記事テキスト
        hp_text: 会社HPテキスト
        edinet_overview: EDINET有報の事業内容テキスト

    Returns:
        dict: 構造化された企業情報
    """
    api_key = _get_anthropic_key()
    if not api_key:
        print("  [Claude API] API Key未設定。スキップします。")
        return _fallback_extraction(wiki_text, edinet_overview)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        source_text = ""
        if wiki_text:
            source_text += f"=== Wikipedia ===\n{wiki_text}\n\n"
        if hp_text:
            source_text += f"=== Company Website ===\n{hp_text}\n\n"
        if edinet_overview:
            source_text += f"=== EDINET Annual Report (Business Description) ===\n{edinet_overview}\n\n"

        if not source_text.strip():
            return {}

        prompt = """From the following company information sources, extract structured data in JSON format.
Return ONLY valid JSON with these fields (use null if information is not available):

{
  "company_name_en": "Official English company name (e.g., 'Teikoku Tsushin Kogyo Co., Ltd.')",
  "founding_year": "Year founded (e.g., '1944')",
  "headquarters": "Headquarters location in English (e.g., 'Meguro-ku, Tokyo, Japan')",
  "main_business_en": "2-3 sentence description of main business in English",
  "main_business_ja": "事業内容の要約（日本語2-3文）",
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
        # JSONブロック抽出
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()

        return json.loads(response_text)

    except Exception as e:
        print(f"  [Claude API] エラー: {e}")
        return _fallback_extraction(wiki_text, edinet_overview)


def _fallback_extraction(wiki_text, edinet_overview=""):
    """Claude API が使えない場合の簡易抽出。"""
    result = {}

    # 設立年を正規表現で抽出
    for text in [wiki_text, edinet_overview]:
        if not text:
            continue
        m = re.search(r'(19\d{2}|20[0-2]\d)年.*?(設立|創立|創業|設置)', text)
        if m:
            result["founding_year"] = m.group(1)
            break

    return result


def collect_web_data(code_4, company_name_ja, company_url="", edinet_overview="",
                     use_cache=True):
    """
    Webソースから企業プロファイル情報を収集。

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

    print(f"  [Web] Wikipedia + Claude API で情報収集: {company_name_ja}")

    # Wikipedia取得
    wiki_text = fetch_wikipedia(company_name_ja)
    if wiki_text:
        print(f"  [Wikipedia] {len(wiki_text)}文字取得")

    # 会社HP取得
    hp_text = fetch_company_website(company_url) if company_url else ""
    if hp_text:
        print(f"  [会社HP] {len(hp_text)}文字取得")

    # Claude APIで構造化
    result = extract_company_info_with_llm(wiki_text, hp_text, edinet_overview)

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
