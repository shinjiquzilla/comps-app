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

        # 「通期」の業績予想を含む行を検出
        if re.search(r'通期.*予想|予想.*通期|通期.*業績', line_stripped):
            forecast_section = True
            forecast_lines = []
            continue

        # 「前期」「前年」が含まれる行は実績なのでスキップ
        if forecast_section and re.search(r'前期|前年|実績', line_stripped):
            continue

        if forecast_section:
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
    if forecast_lines:
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
        if re.search(r'通期.*予想|予想.*通期', ls):
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
