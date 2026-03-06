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
import unicodedata
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_WEB_CACHE_DIR = Path(__file__).parent / "data" / "web"


def _normalize_company_name(name):
    """全角英数字→半角に変換し、「株式会社」等を除去して検索用の名前を返す。"""
    # NFKC正規化: 全角英数字・記号 → 半角（例: ＳＥＭＩＴＥＣ → SEMITEC）
    normalized = unicodedata.normalize("NFKC", name)
    # 「株式会社」「有限会社」等を除去
    normalized = re.sub(r'(株式会社|有限会社|合同会社)', '', normalized).strip()
    return normalized


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

IMPORTANT:
- For main_business_ja and main_business_en: write a concise description of the company's actual products and services, target markets, and competitive strengths.
- PRIORITIZE the Company Website as the primary source for business description (it has the most accurate and up-to-date product/service information).
- Use Wikipedia for founding year, headquarters, and general context.
- Use EDINET for group structure and financial context.
- Do NOT copy generic Wikipedia definitions of product categories (e.g., do not describe what a "thermistor" is in general terms). Instead describe what THIS COMPANY specifically does.
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
    """Claude API が使えない場合の簡易抽出（Wikipedia + EDINET テキストから正規表現）。"""
    result = {}
    all_text = "\n".join(t for t in [wiki_text, edinet_overview] if t)

    if not all_text:
        return result

    # --- 設立年 ---
    # パターン1: "1944年...設立"
    m = re.search(r'(19\d{2}|20[0-2]\d)年.*?(設立|創立|創業|設置)', all_text)
    if m:
        result["founding_year"] = m.group(1)
    else:
        # パターン2: "設立: 1944年" / "創業 1958年"
        m = re.search(r'(?:設立|創立|創業|設置)[：:\s]*(\d{4})年', all_text)
        if m:
            result["founding_year"] = m.group(1)

    # --- 本社所在地 ---
    # パターン1: "〜に本社を置く" / "〜に本店を置く"（Wikipedia冒頭で最もよく出る形）
    _pref = (r'(?:東京都|北海道|(?:大阪|京都|兵庫|奈良|和歌山|滋賀|三重)府|'
             r'(?:青森|岩手|宮城|秋田|山形|福島|茨城|栃木|群馬|埼玉|千葉|神奈川|'
             r'新潟|富山|石川|福井|山梨|長野|岐阜|静岡|愛知|鳥取|島根|岡山|広島|'
             r'山口|徳島|香川|愛媛|高知|福岡|佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄)県)')
    m = re.search(
        r'(' + _pref + r'[^\n]{1,30}?)に本[社店]を置く',
        all_text,
    )
    if m:
        result["headquarters"] = m.group(1).strip()
    else:
        # パターン2: "本社所在地\n東京都..." / "本社 東京都..."
        m = re.search(
            r'(?:本社所在地|本店所在地)[：:\s\n]*'
            r'(' + _pref + r'[^\n]{2,30})',
            all_text,
        )
        if m:
            hq = m.group(1).strip().rstrip("。、．.")
            result["headquarters"] = hq

    # --- 英語社名 ---
    _en_suffix = r'(?:Co\.,?\s*Ltd\.?|Corp(?:oration)?|Inc\.?|Ltd\.?|Holdings|Group)'
    # パターン1: "英: KOA CORPORATION" / "英語: Tamura Corporation"
    m = re.search(
        r'(?:英[語文称]?[：:\s]+|英称[：:\s]+)([A-Z][A-Za-z\s&,.\'-]+' + _en_suffix + r'[A-Za-z.,\s]*)',
        all_text, re.IGNORECASE,
    )
    if m:
        result["company_name_en"] = m.group(1).strip().rstrip(".,）)")
    else:
        # パターン2: 括弧内の英語名: "（KOA Corporation）"
        m = re.search(
            r'[（(]\s*(?:英[：:\s]*)?([A-Z][A-Za-z\s&,.\'-]+' + _en_suffix + r'[A-Za-z.,\s]*)\s*[）)]',
            all_text, re.IGNORECASE,
        )
        if m:
            result["company_name_en"] = m.group(1).strip().rstrip(".,）)")

    # --- 事業概要（日本語、Wikipedia冒頭1-2文） ---
    if wiki_text:
        # 最初の句点まで（最大200文字）
        first_sentence = re.split(r'(?<=。)', wiki_text[:500])
        if first_sentence:
            desc = "".join(first_sentence[:2]).strip()
            if len(desc) > 20:
                result["main_business_ja"] = desc[:300]

    return result


def _guess_company_url(company_name_ja, code_4):
    """証券コードと会社名から企業サイトURLを推定。"""
    # 代表的なパターンを試す
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

    # 全角→半角正規化（ＳＥＭＩＴＥＣ → SEMITEC 等）
    search_name = _normalize_company_name(company_name_ja)
    print(f"  [Web] Wikipedia + 会社HP + Claude API で情報収集: {company_name_ja} (検索名: {search_name})")

    # 会社HPのURLが未指定の場合は推定
    if not company_url:
        company_url = _guess_company_url(company_name_ja, code_4)

    # Wikipedia取得（短い名前の場合は「(企業)」付きでも試行）
    wiki_text = fetch_wikipedia(search_name)

    def _is_company_article(text, target_code=None):
        """企業記事かどうかを簡易チェック。target_codeがあればコードの一致も検証。"""
        if not text or len(text) < 300:
            return False
        if "曖昧さ回避" in text or "以下の" in text[:100]:
            return False
        # 天体・地理等の非企業記事を検出（NGC, 星座, 銀河, etc.）
        non_company_markers = ["NGC ", "星座", "銀河", "天体", "球状星団", "惑星",
                               "スリランカ", "Sri Lanka", "Buddhism"]
        head = text[:500]
        if any(m in head for m in non_company_markers):
            return False
        # 証券コード検証: 記事内に別の証券コードが記載されている場合は不一致の可能性
        if target_code:
            # 記事冒頭1000文字でコードを探す
            head_1k = text[:1500]
            # 「証券コード: XXXX」「コード番号: XXXX」パターン
            code_match = re.search(r'(?:証券コード|コード番号|銘柄コード)[：:\s]*(\d{4})', head_1k)
            if code_match and code_match.group(1) != target_code:
                print(f"  [Wikipedia] 証券コード不一致: 記事={code_match.group(1)}, 対象={target_code}")
                return False
        return True

    if not _is_company_article(wiki_text, target_code=code_4):
        # 「(企業)」付きで再検索
        alt_text = fetch_wikipedia(f"{search_name} (企業)")
        if _is_company_article(alt_text, target_code=code_4):
            wiki_text = alt_text
        else:
            # 「株式会社」付きで再検索
            alt_text = fetch_wikipedia(f"株式会社{search_name}")
            if _is_company_article(alt_text, target_code=code_4):
                wiki_text = alt_text
            elif not _is_company_article(wiki_text, target_code=code_4):
                # 元の名前（正規化前）でも試行
                if search_name != company_name_ja:
                    alt_text = fetch_wikipedia(company_name_ja)
                    if _is_company_article(alt_text, target_code=code_4):
                        wiki_text = alt_text
                    else:
                        wiki_text = ""  # 全て不一致 → Wikipediaデータを使わない
                else:
                    wiki_text = ""
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
