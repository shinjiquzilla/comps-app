"""
決算短信PDFパーサー — 業績予想の自動抽出。

東証規定の決算短信フォーマットから業績予想テーブルをパースし、
売上高・営業利益・経常利益・純利益・DPSを抽出する。

パース失敗時は空dictを返し、手動入力にフォールバック。
"""

import re

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


def parse_tanshin_pdf(pdf_bytes):
    """
    決算短信PDFから業績予想値を抽出。

    Parameters:
        pdf_bytes: PDFファイルのバイト列

    Returns:
        dict with keys (百万円単位、DPSは円):
            rev_forecast: 売上高予想
            op_forecast: 営業利益予想
            ni_forecast: 純利益予想（親会社株主帰属）
            dps: 配当予想（円）
        パース失敗時は空dict
    """
    if fitz is None:
        return {}

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return {}

    # 最初の3ページからテキストを抽出（業績予想は通常1-2ページ目）
    full_text = ""
    for page_num in range(min(3, len(doc))):
        full_text += doc[page_num].get_text() + "\n"
    doc.close()

    if not full_text.strip():
        return {}

    result = {}

    # --- 通期業績予想の抽出 ---
    result.update(_extract_forecast(full_text))

    # --- 配当予想の抽出 ---
    dps = _extract_dps(full_text)
    if dps is not None:
        result['dps'] = dps

    return result


def _parse_amount(s):
    """
    金額文字列をパース。百万円単位で返す。
    決算短信の金額は百万円単位が標準。
    """
    s = s.strip().replace(',', '').replace('，', '').replace(' ', '')
    s = s.replace('△', '-').replace('▲', '-')
    if not s or s in ('－', '-', '―', '—'):
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def _extract_forecast(text):
    """
    通期業績予想セクションから数値を抽出。

    決算短信の典型的な業績予想テーブル（通期）:
        売上高    営業利益    経常利益    親会社株主に帰属する当期純利益
        16,800    1,300       1,400       1,200

    数値は百万円単位。
    """
    result = {}

    # 「通期」を含む業績予想セクションを探す
    # パターン: "通期" の後に数値行が続く
    lines = text.split('\n')

    # 業績予想の見出し行を探す
    forecast_section = False
    forecast_lines = []

    for i, line in enumerate(lines):
        line_stripped = line.strip()

        # 業績予想セクションの見出し行を検出
        # 「通期」「業績予想」「連結業績予想」等にマッチ
        # ただし四半期累計の予想は除外（「第X四半期」を含む行）
        if re.search(r'業績予想|通期.*業績', line_stripped):
            # 四半期累計の予想セクションはスキップ
            if re.search(r'第[1-3１-３]四半期', line_stripped):
                continue
            forecast_section = True
            forecast_lines = []
            continue

        # 「前期」「前年」が含まれる行は実績なのでスキップ
        if forecast_section and re.search(r'前期|前年|実績', line_stripped):
            continue

        if forecast_section:
            # 「通期」ラベル付きデータ行の特別処理
            # 例: "通期 25,000 4.4 800 — 800 — 550 — 51.88"
            # 百万円（整数）と増減率/EPS（小数）が混在 → 整数のみ抽出
            if re.search(r'通期', line_stripped):
                all_nums = re.findall(r'[△▲\-]?[\d,]+\.?\d*', line_stripped)
                int_values = []
                for n in all_nums:
                    if '.' in n:
                        continue  # 小数 = 増減率 or EPS → スキップ
                    val = _parse_amount(n)
                    if val is not None:
                        int_values.append(val)
                if len(int_values) >= 4:
                    result['rev_forecast'] = int_values[0]
                    result['op_forecast'] = int_values[1]
                    # int_values[2] = 経常利益（スキップ）
                    result['ni_forecast'] = int_values[3]
                    break

            # 増減率の行（%を含む）はスキップ
            if '%' in line_stripped or '％' in line_stripped:
                continue
            # 数値が含まれる行を収集
            nums = re.findall(r'[△▲\-]?[\d,]+', line_stripped)
            if len(nums) >= 3:
                forecast_lines.append(nums)
                # 十分な数値行が取れたら終了
                if len(forecast_lines) >= 2:
                    break
            elif line_stripped and not nums:
                # 数値がない行が来たらセクション終了
                if forecast_lines:
                    break

    # 数値行をパース
    # 典型パターン: 売上高, 営業利益, 経常利益, 純利益 の順に4つ以上の数値
    if not result and forecast_lines:
        for nums in forecast_lines:
            if len(nums) >= 4:
                rev = _parse_amount(nums[0])
                op = _parse_amount(nums[1])
                # nums[2] は経常利益（スキップ）
                ni = _parse_amount(nums[3])
                if rev is not None:
                    result['rev_forecast'] = rev
                if op is not None:
                    result['op_forecast'] = op
                if ni is not None:
                    result['ni_forecast'] = ni
                break

    # フォールバック: テーブル形式でない場合、個別の数値を探す
    if not result:
        result.update(_extract_forecast_by_label(text))

    return result


def _extract_forecast_by_label(text):
    """
    ラベル＋数値のパターンで業績予想を抽出（フォールバック）。
    例: "売上高  16,800百万円" or "売上高 16,800"
    """
    result = {}
    lines = text.split('\n')

    # 通期予想セクション内かどうか
    in_forecast = False
    for line in lines:
        ls = line.strip()
        if re.search(r'業績予想|通期.*業績', ls):
            # 四半期累計の予想セクションはスキップ
            if re.search(r'第[1-3１-３]四半期', ls):
                continue
            in_forecast = True
            continue
        if not in_forecast:
            continue

        # 売上高
        m = re.search(r'売上[高收].*?([△▲\-]?[\d,]+)', ls)
        if m and 'rev_forecast' not in result:
            val = _parse_amount(m.group(1))
            if val is not None:
                result['rev_forecast'] = val

        # 営業利益
        m = re.search(r'営業利益.*?([△▲\-]?[\d,]+)', ls)
        if m and 'op_forecast' not in result:
            val = _parse_amount(m.group(1))
            if val is not None:
                result['op_forecast'] = val

        # 純利益
        m = re.search(r'(?:当期|親会社).*?純利益.*?([△▲\-]?[\d,]+)', ls)
        if m and 'ni_forecast' not in result:
            val = _parse_amount(m.group(1))
            if val is not None:
                result['ni_forecast'] = val

    return result


def _extract_dps(text):
    """
    配当予想を抽出。決算短信の配当セクションから年間配当を取得。

    パターン:
    - "年間 XX.XX円" or "年間配当 XX円"
    - 中間＋期末の合計
    """
    # 年間配当を直接探す
    m = re.search(r'年間[配当金額\s]*?(\d+[\.\d]*)\s*円?', text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    # 配当テーブルから合計行を探す
    lines = text.split('\n')
    for i, line in enumerate(lines):
        ls = line.strip()
        if '合計' in ls or '年間' in ls:
            m = re.search(r'(\d+[\.\d]*)\s*円?', ls)
            if m:
                try:
                    val = float(m.group(1))
                    if 1 <= val <= 10000:  # 配当として妥当な範囲
                        return val
                except ValueError:
                    pass

    return None


def identify_tanshin_pdf(pdf_bytes, candidate_codes):
    """
    PDF内容から証券コード・決算期・期間種別を自動判定。

    Parameters:
        pdf_bytes: PDFバイト列
        candidate_codes: list of str — 入力済み証券コード（例: ['6763', '6989', ...]）

    Returns:
        {
            'code_4': '6763' or None,
            'company_name': '帝国通信工業' or '',
            'fy_end': '2026-03' or '',        # 決算期（YYYY-MM）
            'period_type': 'FY' or 'Q2',
            'suggested_filename': 'tanshin_2026-03_FY.pdf' or '',
        }
    """
    result = {
        'code_4': None,
        'company_name': '',
        'fy_end': '',
        'period_type': 'FY',
        'suggested_filename': '',
    }

    if fitz is None:
        return result

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return result

    if len(doc) == 0:
        doc.close()
        return result

    text = doc[0].get_text()
    doc.close()

    if not text.strip():
        return result

    # --- 証券コード検出 ---
    # 決算短信1ページ目: 「コード番号 6763」「証券コード：6763」等
    code_patterns = [
        r'コード[番号\s:：]*(\d{4})',
        r'証券コード[\s:：]*(\d{4})',
        r'[\(（](\d{4})[\)）]',  # (6763) のようなパターン
    ]
    detected_code = None
    for pat in code_patterns:
        m = re.search(pat, text)
        if m:
            code = m.group(1)
            if code in candidate_codes:
                detected_code = code
                break

    if detected_code is None:
        # フォールバック: candidate_codesの中でテキストに含まれるものを探す
        for code in candidate_codes:
            if code in text:
                detected_code = code
                break

    result['code_4'] = detected_code

    # --- 会社名検出（1ページ目の最初数行から） ---
    lines = text.split('\n')
    for line in lines[:15]:
        ls = line.strip()
        # 「○○株式会社」または「株式会社○○」のパターン
        m = re.search(r'((?:\S+)?株式会社(?:\S+)?)', ls)
        if m:
            name = m.group(1)
            # ノイズ除去: 東京証券取引所等は除外
            if '証券取引所' not in name and '監査法人' not in name:
                result['company_name'] = name
                break

    # --- 決算期検出 ---
    # 「2026年3月期」「令和8年3月期」等
    m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*期', text)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        result['fy_end'] = f"{year}-{month:02d}"

    # --- 期間種別検出 ---
    # 「第X四半期」「第Ｘ四半期」を検出
    if re.search(r'第[1１]四半期', text):
        result['period_type'] = 'Q1'
    elif re.search(r'第[2２]四半期', text):
        result['period_type'] = 'Q2'
    elif re.search(r'第[3３]四半期', text):
        result['period_type'] = 'Q3'
    else:
        result['period_type'] = 'FY'

    # --- ファイル名生成 ---
    if result['fy_end']:
        result['suggested_filename'] = f"tanshin_{result['fy_end']}_{result['period_type']}.pdf"

    return result


def save_tanshin_pdf(pdf_bytes, code_4, filename, base_dir=None):
    """
    アップロードされた決算短信PDFをローカルに保存。

    保存先: data/tanshin/{code_4}/{filename}
    """
    from pathlib import Path
    if base_dir is None:
        base_dir = Path(__file__).parent / "data" / "tanshin"
    else:
        base_dir = Path(base_dir)

    save_dir = base_dir / str(code_4)
    save_dir.mkdir(parents=True, exist_ok=True)

    save_path = save_dir / filename
    save_path.write_bytes(pdf_bytes)
    return str(save_path)
