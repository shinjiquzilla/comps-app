"""
Financial Calculator - LTM計算・EBITDA・EV・マルチプル算出。

CLAUDE.md の計算ロジック準拠:
- LTM = 通期(FY) - H1(前期) + H1(今期)
- EBITDA = 営業利益 + D&A
- EV = 時価総額 + 有利子負債 - 現金
"""


def safe_div(a, b):
    """ゼロ除算・None安全な除算。"""
    if b is None or b == 0 or a is None:
        return None
    return a / b


def calc_ltm(fy_full, fy_h1, current_h1):
    """
    LTM (Last Twelve Months) を計算。
    LTM = 通期実績 - 前期H1累計 + 今期H1累計

    いずれかがNoneの場合はNoneを返す。
    """
    if any(v is None for v in [fy_full, fy_h1, current_h1]):
        return None
    return fy_full - fy_h1 + current_h1


def calc_total_debt(financials):
    """
    有利子負債合計を計算。
    = 短期借入金 + 長期借入金 + 社債 + 1年内返済長期借入金 + 1年内償還社債 + リース債務
    """
    keys = [
        'short_term_debt', 'long_term_debt', 'bonds',
        'current_long_term_debt', 'current_bonds', 'lease_debt'
    ]
    total = 0
    found_any = False
    for k in keys:
        v = financials.get(k)
        if v is not None:
            total += v
            found_any = True
    return total if found_any else None


def calc_ev(market_cap, total_debt, cash):
    """EV = 時価総額 + 有利子負債 - 現金。"""
    if market_cap is None:
        return None
    debt = total_debt or 0
    c = cash or 0
    return market_cap + debt - c


def calc_multiples(market_cap, ev, ebitda_ltm, ebitda_fwd, ni_fwd, equity, stock_price, dps):
    """
    バリュエーション・マルチプルを計算。

    Returns: dict with keys:
        ev_ebitda_ltm, ev_ebitda_fwd, per_fwd, pbr, div_yield
    """
    return {
        'ev_ebitda_ltm': safe_div(ev, ebitda_ltm),
        'ev_ebitda_fwd': safe_div(ev, ebitda_fwd),
        'per_fwd': safe_div(market_cap, ni_fwd),
        'pbr': safe_div(market_cap, equity),
        'div_yield': safe_div(dps, stock_price),
    }


def calc_margins(revenue_ltm, op_ltm, ebitda_ltm):
    """営業利益率・EBITDAマージンを計算。"""
    return {
        'op_margin': safe_div(op_ltm, revenue_ltm),
        'ebitda_margin': safe_div(ebitda_ltm, revenue_ltm),
    }


def build_company_data(code_4, edinet_data, tdnet_data, stock_data):
    """
    各ソースのデータを統合して comps_generator.py が期待する形式に変換。

    Parameters:
        code_4: 証券コード（4桁）
        edinet_data: dict with keys 'yuho_data', 'hanki_data', 'company_name'
        tdnet_data: dict with key 'forecast'
        stock_data: dict from stock_fetcher

    Returns: dict compatible with comps_generator.py JSON config format
    """
    yuho = edinet_data.get('yuho_data', {})
    hanki = edinet_data.get('hanki_data', {})
    hanki_prior = edinet_data.get('hanki_prior_data', {})
    forecast = tdnet_data.get('forecast', {})
    company_name = edinet_data.get('company_name', '')

    # --- LTM計算 ---
    # 有報 = FY通期、半期報 = 今期H1、hanki_prior = 前期H1
    # 正確なLTM = FY通期 − 前期H1 + 今期H1
    # フォールバック: 前期H1が取れなければ通期値をそのまま使用（近似）

    fy_revenue = yuho.get('revenue')
    fy_op = yuho.get('operating_income')
    fy_ni = yuho.get('net_income')
    fy_da = yuho.get('depreciation')

    h1_revenue = hanki.get('revenue')
    h1_op = hanki.get('operating_income')
    h1_ni = hanki.get('net_income')
    h1_da = hanki.get('depreciation')

    prior_h1_revenue = hanki_prior.get('revenue')
    prior_h1_op = hanki_prior.get('operating_income')
    prior_h1_ni = hanki_prior.get('net_income')
    prior_h1_da = hanki_prior.get('depreciation')

    # 正確なLTM（前期H1が取れた場合）、フォールバックは通期値
    rev_ltm = calc_ltm(fy_revenue, prior_h1_revenue, h1_revenue)
    if rev_ltm is None:
        rev_ltm = fy_revenue
    op_ltm = calc_ltm(fy_op, prior_h1_op, h1_op)
    if op_ltm is None:
        op_ltm = fy_op
    ni_ltm = calc_ltm(fy_ni, prior_h1_ni, h1_ni)
    if ni_ltm is None:
        ni_ltm = fy_ni
    da_ltm = calc_ltm(fy_da, prior_h1_da, h1_da)
    if da_ltm is None:
        da_ltm = fy_da

    # EBITDA
    ebitda_ltm = None
    if op_ltm is not None and da_ltm is not None:
        ebitda_ltm = op_ltm + da_ltm

    # 予想 EBITDA
    ebitda_fwd = None
    op_fwd = forecast.get('op_forecast')
    if op_fwd is not None and da_ltm is not None:
        ebitda_fwd = op_fwd + da_ltm  # D&Aは直近実績をそのまま使用

    # BS values（半期報告書が最新なので優先）
    cash = hanki.get('cash') or yuho.get('cash')
    total_debt = calc_total_debt(hanki) or calc_total_debt(yuho)
    equity = hanki.get('net_assets') or hanki.get('equity') or yuho.get('net_assets') or yuho.get('equity')

    # 自己資本比率
    equity_ratio = hanki.get('equity_ratio') or yuho.get('equity_ratio')
    if equity_ratio and equity_ratio > 1:
        equity_ratio = equity_ratio / 100  # %表記→小数

    # 株価情報
    stock_price = stock_data.get('stock_price')
    shares = stock_data.get('shares_outstanding')
    market_cap = stock_data.get('market_cap')

    # EV
    ev = calc_ev(market_cap, total_debt, cash)

    # DPS: 有報記載の直近終了フル年度の実績配当（予想ではなく実績）
    dps_actual = yuho.get('dps')

    # マルチプル
    multiples = calc_multiples(
        market_cap, ev, ebitda_ltm,
        ebitda_fwd,
        forecast.get('ni_forecast'),
        equity, stock_price,
        dps_actual
    )

    # BS日付
    hanki_doc = edinet_data.get('hanki_doc')
    bs_date = ""
    if hanki_doc:
        pe = hanki_doc.get('periodEnd', '')
        if pe:
            bs_date = pe.replace('-', '/')

    # comps_generator.py 互換形式
    return {
        'code': code_4,
        'name': company_name,
        'sector': '',
        'accounting': 'J-GAAP',
        'fy_end': 'Mar',
        'stock_price': stock_price,
        'shares_outstanding': shares,
        'market_cap': market_cap,
        'bs_date': bs_date,
        'cash': cash,
        'total_debt': total_debt,
        'equity_parent': equity,
        'equity_ratio': equity_ratio,
        'rev_ltm': rev_ltm,
        'op_ltm': op_ltm,
        'ni_ltm': ni_ltm,
        'da_ltm': da_ltm,
        'ebitda_ltm': ebitda_ltm,
        'rev_forecast': forecast.get('rev_forecast'),
        'op_forecast': op_fwd,
        'ni_forecast': forecast.get('ni_forecast'),
        'ebitda_forecast': ebitda_fwd,
        'dps': dps_actual,
        # デバッグ用
        '_ev': ev,
        '_multiples': multiples,
    }
