"""
Financial Calculator - LTM計算・EBITDA・EV・マルチプル算出。

Calendarize LTM対応:
- Q3パターン (H1ベース): LTM = yuho_FY - hanki_prior_H1 + hanki_current_H1
- Q2/Q4パターン (決算短信): LTM = yuho_FY - tanshin_prior_Q + tanshin_current_Q
- FY/Q1パターン: LTM = yuho_FY そのまま
- EBITDA = 営業利益 + D&A
- EV = 時価総額 + 有利子負債 - 現金
"""

from datetime import date


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
        'current_long_term_debt', 'current_bonds',
        'lease_debt_current', 'lease_debt_noncurrent',
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


def determine_calendarize_pattern(fy_end_month, today=None):
    """
    決算月と現在日付から最適なCalendarizeパターンを決定。

    45日ルール: 決算短信は四半期末から約45日後に公開
    90日ルール: 有報・半期報は期末から約90日後に公開

    Parameters:
        fy_end_month: 決算月 (1-12)
        today: 基準日 (default: 今日)

    Returns:
        str: 'Q4_PREV' / 'FY_TANSHIN' / 'Q1' / 'Q2' / 'Q3' / 'Q4'

    例（3月決算、各データ公開時期）:
        FY end 3/31 + 45日 = 5/15: FY通期短信公開
        FY end 3/31 + 90日 = 6/29: 有報公開
        Q1 end 6/30 + 45日 = 8/14: Q1短信公開
        H1 end 9/30 + 90日 = 12/29: 半期報公開
        Q3 end 12/31 + 45日 = 2/14: Q3短信公開

    タイムライン:
        4/1〜5/14  → Q4_PREV    (新データなし)
        5/15〜6/28 → FY_TANSHIN (通期短信あり)
        6/29〜8/13 → Q1         (有報あり)
        8/14〜12/28→ Q2         (Q1短信あり)
        12/29〜2/13→ Q3         (半期報あり)
        2/14〜3/31 → Q4         (Q3短信あり)
    """
    import calendar
    if today is None:
        today = date.today()

    m = fy_end_month

    def _quarter_end_date(base_month, offset_months):
        """基準月からoffset_months後の月末日を返す。"""
        target_month = ((base_month - 1 + offset_months) % 12) + 1
        # 年の計算: FY年度を求める
        fy_year = today.year if today.month > m else today.year - 1
        if today.month == m:
            fy_year = today.year - 1
        # offset_monthsから年をずらす
        target_year = fy_year + (base_month - 1 + offset_months) // 12
        last_day = calendar.monthrange(target_year, target_month)[1]
        return date(target_year, target_month, last_day)

    # 各四半期末日を計算
    fy_end = _quarter_end_date(m, 0)       # FY末
    q1_end = _quarter_end_date(m, 3)       # Q1末
    h1_end = _quarter_end_date(m, 6)       # H1末
    q3_end = _quarter_end_date(m, 9)       # Q3末

    # todayが前年度にいる場合の調整
    if today <= fy_end:
        # 前年度のQ3短信が使えるか確認
        prev_q3_end = _quarter_end_date(m, 9 - 12)  # 前年度Q3末
        days_since_prev_q3 = (today - prev_q3_end).days
        if days_since_prev_q3 >= 45:
            return 'Q4'
        return 'Q3'

    # FY末からの経過日数で判定
    days_since_fy = (today - fy_end).days
    days_since_q1 = (today - q1_end).days
    days_since_h1 = (today - h1_end).days
    days_since_q3 = (today - q3_end).days

    # 逆順に判定（最新パターンから）
    if days_since_q3 >= 45:
        return 'Q4'
    if days_since_h1 >= 90:
        return 'Q3'
    if days_since_q1 >= 45:
        return 'Q2'
    if days_since_fy >= 90:
        return 'Q1'
    if days_since_fy >= 45:
        return 'FY_TANSHIN'
    if days_since_fy >= 0:
        return 'Q4_PREV'

    return 'Q4'  # フォールバック


# フォールバックチェーン: Q4 → Q3 → Q2 → Q1 → FY_TANSHIN → yuho_FY
_FALLBACK_CHAIN = ['Q4', 'Q3', 'Q2', 'Q1', 'FY_TANSHIN', 'Q4_PREV']


def calc_ltm_calendarized(pattern, yuho, hanki, hanki_prior, tanshin_actuals):
    """
    Calendarizeパターンに応じたLTM計算。

    Parameters:
        pattern: str — determine_calendarize_pattern() の戻り値
        yuho: dict — 有報データ (revenue, operating_income, net_income)
        hanki: dict — 今期半期報データ
        hanki_prior: dict — 前期半期報データ
        tanshin_actuals: dict — 決算短信実績値
            {
                'Q3': {'rev_actual': ..., 'op_actual': ..., 'ni_actual': ...,
                       'rev_prior': ..., 'op_prior': ..., 'ni_prior': ...},
                'FY': {...},
                ...
            }

    Returns:
        (rev_ltm, op_ltm, ni_ltm, used_pattern) — used_patternは実際に使用したパターン
    """
    if tanshin_actuals is None:
        tanshin_actuals = {}

    fy_rev = yuho.get('revenue')
    fy_op = yuho.get('operating_income')
    fy_ni = yuho.get('net_income')

    # パターン実行関数
    def _try_pattern(p):
        """パターンpでLTMを計算。成功すれば(rev, op, ni)、失敗すればNone。"""
        if p == 'Q4_PREV':
            # 前年度のQ4 = 有報FYそのまま
            if fy_rev is not None:
                return fy_rev, fy_op, fy_ni
            return None

        if p == 'FY_TANSHIN':
            # 通期決算短信の実績値
            fy_data = tanshin_actuals.get('FY')
            if fy_data and fy_data.get('rev_actual') is not None:
                return (fy_data['rev_actual'],
                        fy_data.get('op_actual'),
                        fy_data.get('ni_actual'))
            return None

        if p == 'Q1':
            # 有報のFY値そのまま（有報提出後、Q1短信前）
            if fy_rev is not None:
                return fy_rev, fy_op, fy_ni
            return None

        if p == 'Q2':
            # LTM = yuho_FY + tanshin_Q1_cur - tanshin_Q1_prior
            q1 = tanshin_actuals.get('Q1')
            if q1 and fy_rev is not None and q1.get('rev_actual') is not None:
                rev = fy_rev - (q1.get('rev_prior') or 0) + q1['rev_actual']
                op = _ltm_calc(fy_op, q1.get('op_prior'), q1.get('op_actual'))
                ni = _ltm_calc(fy_ni, q1.get('ni_prior'), q1.get('ni_actual'))
                return rev, op, ni
            return None

        if p == 'Q3':
            # LTM = yuho_FY + hanki_H1_cur - hanki_H1_prior (現行ロジック)
            h1_rev = hanki.get('revenue')
            prior_h1_rev = hanki_prior.get('revenue')
            if fy_rev is not None and h1_rev is not None and prior_h1_rev is not None:
                rev = calc_ltm(fy_rev, prior_h1_rev, h1_rev)
                op = calc_ltm(fy_op, hanki_prior.get('operating_income'),
                              hanki.get('operating_income'))
                ni = calc_ltm(fy_ni, hanki_prior.get('net_income'),
                              hanki.get('net_income'))
                return rev, op, ni
            # フォールバック: 半期報がなければ決算短信Q2で代替
            q2 = tanshin_actuals.get('Q2')
            if q2 and fy_rev is not None and q2.get('rev_actual') is not None:
                rev = fy_rev - (q2.get('rev_prior') or 0) + q2['rev_actual']
                op = _ltm_calc(fy_op, q2.get('op_prior'), q2.get('op_actual'))
                ni = _ltm_calc(fy_ni, q2.get('ni_prior'), q2.get('ni_actual'))
                return rev, op, ni
            return None

        if p == 'Q4':
            # LTM = yuho_FY + tanshin_Q3_cur - tanshin_Q3_prior
            q3 = tanshin_actuals.get('Q3')
            if q3 and fy_rev is not None and q3.get('rev_actual') is not None:
                rev = fy_rev - (q3.get('rev_prior') or 0) + q3['rev_actual']
                op = _ltm_calc(fy_op, q3.get('op_prior'), q3.get('op_actual'))
                ni = _ltm_calc(fy_ni, q3.get('ni_prior'), q3.get('ni_actual'))
                return rev, op, ni
            return None

        return None

    def _ltm_calc(fy_val, prior_val, current_val):
        """単一項目のLTM = FY - prior + current。Noneセーフ。"""
        if fy_val is None or prior_val is None or current_val is None:
            return fy_val  # フォールバック: FY値
        return fy_val - prior_val + current_val

    # 指定パターンから試行、失敗したらフォールバック
    # フォールバックチェーンの中で pattern 以降を試す
    try:
        start_idx = _FALLBACK_CHAIN.index(pattern)
    except ValueError:
        start_idx = 0

    chain = _FALLBACK_CHAIN[start_idx:]

    for p in chain:
        result = _try_pattern(p)
        if result is not None:
            rev, op, ni = result
            return rev, op, ni, p

    # 全パターン失敗: 有報FY値をフォールバック
    return fy_rev, fy_op, fy_ni, 'yuho_FY'


def build_company_data(code_4, edinet_data, tdnet_data, stock_data,
                       tanshin_actuals=None, jquants_data=None):
    """
    各ソースのデータを統合して comps_generator.py が期待する形式に変換。

    Parameters:
        code_4: 証券コード（4桁）
        edinet_data: dict with keys 'yuho_data', 'hanki_data', 'company_name'
        tdnet_data: dict with key 'forecast'
        stock_data: dict from stock_fetcher
        tanshin_actuals: dict — 決算短信実績値 (Calendarize LTM用)
            {'Q3': {'rev_actual': ..., ...}, 'FY': {...}, ...}
            Noneの場合は従来のH1ベースLTMにフォールバック
        jquants_data: dict — J-Quants /v2/fins/summary の整理済みデータ
            Noneの場合は従来ロジック（EDINET+tanshin_actuals）

    Returns: dict compatible with comps_generator.py JSON config format
    """
    yuho = edinet_data.get('yuho_data', {})
    hanki = edinet_data.get('hanki_data', {})
    hanki_prior = edinet_data.get('hanki_prior_data', {})
    forecast = tdnet_data.get('forecast', {})
    company_name = edinet_data.get('company_name', '')

    # --- J-Quantsデータがある場合: P&L LTM・予想・株式数・純資産・DPSをJ-Quantsから取得 ---
    if jquants_data:
        from jquants_client import compute_ltm_from_jquants

        quarters = jquants_data.get('quarters', {})
        jq_forecast = jquants_data.get('forecast', {})
        jq_shares = jquants_data.get('shares', {})

        # 決算月・会計基準: J-Quantsから
        _fy_end_month_num = jquants_data.get('fy_end_month', 3)
        accounting_std = jquants_data.get('accounting', 'J-GAAP')

        # P&L LTM: J-Quantsの四半期データから直接計算
        rev_ltm, op_ltm, ni_ltm, used_pattern = compute_ltm_from_jquants(quarters)
        calendarize_pattern = 'jquants_' + used_pattern

        # 予想: J-Quantsから
        rev_fwd = jq_forecast.get('rev_forecast')
        op_fwd = jq_forecast.get('op_forecast')
        ni_fwd = jq_forecast.get('ni_forecast')
        eps_fwd = jq_forecast.get('eps_forecast')

        # DPS: J-Quants予想を優先、なければ有報実績
        dps = jq_forecast.get('dps_forecast')
        if dps is None:
            dps = yuho.get('dps')
        dps_source = 'jquants_forecast' if jq_forecast.get('dps_forecast') is not None else 'yuho'

        # 株式数: J-Quantsから（千株単位）
        shares = None
        _si = jq_shares.get('shares_issued')
        _ts = jq_shares.get('treasury_shares') or 0
        if _si is not None:
            shares = _si - _ts

        # 純資産: J-Quantsの最新四半期から
        equity = None
        equity_ratio = None
        # 最新四半期から取得（Q3 > 2Q > Q1 > FY）
        for _qk in ['Q3', '2Q', 'Q1', 'FY']:
            _qd = quarters.get(_qk)
            if _qd and _qd.get('equity') is not None:
                equity = _qd['equity']
                equity_ratio = _qd.get('equity_ratio')
                break

        if equity_ratio and equity_ratio > 1:
            equity_ratio = equity_ratio / 100

        # ---- 以下はEDINETから（J-Quantsにない） ----
        # cash, total_debt → EV計算用
        cash = hanki.get('cash') or yuho.get('cash')
        total_debt = calc_total_debt(hanki) or calc_total_debt(yuho)

        # depreciation → EBITDA計算用（D&Aは常にEDINETベース）
        fy_da = yuho.get('depreciation')
        h1_da = hanki.get('depreciation')
        prior_h1_da = hanki_prior.get('depreciation')
        da_ltm = calc_ltm(fy_da, prior_h1_da, h1_da)
        if da_ltm is None:
            da_ltm = fy_da

        # EBITDA
        ebitda_ltm = None
        if op_ltm is not None and da_ltm is not None:
            ebitda_ltm = op_ltm + da_ltm

        # 予想 EBITDA
        ebitda_fwd = None
        if op_fwd is not None and da_ltm is not None:
            ebitda_fwd = op_fwd + da_ltm

        # 株価情報
        stock_price = stock_data.get('stock_price')

        # stock_dataの株式数はフォールバック（J-Quantsで取れない場合）
        if shares is None:
            shares = stock_data.get('shares_outstanding')
        if shares is None:
            _shares_issued = hanki.get('shares_issued') or yuho.get('shares_issued')
            _treasury = hanki.get('treasury_shares') or yuho.get('treasury_shares') or 0
            if _shares_issued is not None:
                shares = _shares_issued - _treasury

        # 時価総額
        market_cap = stock_data.get('market_cap')
        if market_cap is None and stock_price is not None and shares is not None:
            market_cap = int(stock_price * shares / 1000)

        # EV
        ev = calc_ev(market_cap, total_debt, cash)

        # マルチプル
        multiples = calc_multiples(
            market_cap, ev, ebitda_ltm, ebitda_fwd,
            ni_fwd, equity, stock_price, dps
        )

        # BS日付・決算月
        bs_date = ""
        _month_map = {1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr', 5: 'May', 6: 'Jun',
                      7: 'Jul', 8: 'Aug', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'}
        fy_end_month = _month_map.get(_fy_end_month_num, 'Mar')
        hanki_doc = edinet_data.get('hanki_doc')
        if hanki_doc:
            pe = hanki_doc.get('periodEnd', '')
            if pe:
                bs_date = pe.replace('-', '/')

        # pl_history: 過去FYデータ（fy_historyがない旧キャッシュはquartersからフォールバック）
        pl_history = jquants_data.get('fy_history', [])
        if not pl_history and quarters.get('FY'):
            fy_q = quarters['FY']
            pl_history = [{
                'fy_year': fy_q.get('fy_year', ''),
                'revenue': fy_q.get('revenue'),
                'op': fy_q.get('op'),
                'ni': fy_q.get('ni'),
            }]

        # Supplement pl_history from Supabase financials table if fewer than 3 FY entries
        if len(pl_history) < 3 and code_4:
            try:
                from supabase_client import get_supabase
                sb = get_supabase()
                if sb:
                    existing_years = {h['fy_year'] for h in pl_history}
                    resp = (sb.table("financials")
                            .select("period_end, revenue, operating_income, net_income")
                            .eq("code", code_4)
                            .eq("doc_type", "yuho")
                            .order("period_end", desc=True)
                            .limit(5)
                            .execute())
                    if resp.data:
                        for row in resp.data:
                            pe = str(row.get("period_end", ""))
                            if pe and pe not in existing_years:
                                pl_history.append({
                                    'fy_year': pe,
                                    'revenue': row.get("revenue"),
                                    'op': row.get("operating_income"),
                                    'ni': row.get("net_income"),
                                })
                                existing_years.add(pe)
                        pl_history.sort(key=lambda x: x['fy_year'], reverse=True)
                        pl_history = pl_history[:3]
            except Exception:
                pass  # Supabase unavailable, continue with J-Quants data only

        # ltm_components: LTM計算内訳
        ltm_components = None
        if used_pattern in ('2Q', 'Q1', 'Q3'):
            fy_q = quarters.get('FY', {})
            sub_q = quarters.get(used_pattern + '_prior', {})
            add_q = quarters.get(used_pattern, {})
            ltm_components = {
                'base': {
                    'fy_year': fy_q.get('fy_year', ''),
                    'period': 'FY',
                    'revenue': fy_q.get('revenue'),
                    'op': fy_q.get('op'),
                    'ni': fy_q.get('ni'),
                },
                'subtract': {
                    'fy_year': sub_q.get('fy_year', ''),
                    'period': used_pattern,
                    'revenue': sub_q.get('revenue'),
                    'op': sub_q.get('op'),
                    'ni': sub_q.get('ni'),
                },
                'add': {
                    'fy_year': add_q.get('fy_year', ''),
                    'period': used_pattern,
                    'revenue': add_q.get('revenue'),
                    'op': add_q.get('op'),
                    'ni': add_q.get('ni'),
                },
                'result': {
                    'revenue': rev_ltm,
                    'op': op_ltm,
                    'ni': ni_ltm,
                },
            }
        elif used_pattern == 'FY':
            fy_q = quarters.get('FY', {})
            ltm_components = {
                'base': {
                    'fy_year': fy_q.get('fy_year', ''),
                    'period': 'FY',
                    'revenue': fy_q.get('revenue'),
                    'op': fy_q.get('op'),
                    'ni': fy_q.get('ni'),
                },
                'result': {
                    'revenue': rev_ltm,
                    'op': op_ltm,
                    'ni': ni_ltm,
                },
            }

        return {
            'code': code_4,
            'name': company_name,
            'sector': '',
            'accounting': accounting_std,
            'fy_end': fy_end_month,
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
            'rev_forecast': rev_fwd,
            'op_forecast': op_fwd,
            'ni_forecast': ni_fwd,
            'ebitda_forecast': ebitda_fwd,
            'dps': dps,
            'pl_history': pl_history,
            'ltm_components': ltm_components,
            '_ev': ev,
            '_multiples': multiples,
            '_calendarize_expected': calendarize_pattern,
            '_calendarize_used': 'jquants_' + used_pattern,
            '_data_source': 'jquants',
            '_dps_source': dps_source,
        }

    # --- 従来ロジック（J-Quantsなし: EDINET + tanshin_actuals） ---

    # --- LTM計算 (Calendarize対応) ---
    fy_da = yuho.get('depreciation')
    h1_da = hanki.get('depreciation')
    prior_h1_da = hanki_prior.get('depreciation')

    # 決算月を推定（後でCalendarizeパターン判定に使用）
    _fy_end_month_num = 3  # デフォルト3月
    yuho_doc = edinet_data.get('yuho_doc')
    hanki_doc = edinet_data.get('hanki_doc')
    for _doc in [yuho_doc, hanki_doc]:
        if _doc and _doc.get('periodEnd'):
            _pe = _doc['periodEnd'].replace('/', '-')
            _parts = _pe.split('-')
            if len(_parts) >= 2:
                _m = int(_parts[1])
                if _doc is yuho_doc:
                    _fy_end_month_num = _m
                    break
                else:
                    _fy_end_month_num = ((_m - 1 + 6) % 12) + 1

    # Calendarize: tanshin_actuals があれば最適パターンでLTM計算
    calendarize_pattern = determine_calendarize_pattern(_fy_end_month_num)
    used_pattern = None

    if tanshin_actuals:
        rev_ltm, op_ltm, ni_ltm, used_pattern = calc_ltm_calendarized(
            calendarize_pattern, yuho, hanki, hanki_prior, tanshin_actuals
        )
    else:
        # 従来ロジック: H1ベースLTM (後方互換)
        fy_revenue = yuho.get('revenue')
        fy_op = yuho.get('operating_income')
        fy_ni = yuho.get('net_income')

        h1_revenue = hanki.get('revenue')
        h1_op = hanki.get('operating_income')
        h1_ni = hanki.get('net_income')

        prior_h1_revenue = hanki_prior.get('revenue')
        prior_h1_op = hanki_prior.get('operating_income')
        prior_h1_ni = hanki_prior.get('net_income')

        rev_ltm = calc_ltm(fy_revenue, prior_h1_revenue, h1_revenue)
        if rev_ltm is None:
            rev_ltm = fy_revenue
        op_ltm = calc_ltm(fy_op, prior_h1_op, h1_op)
        if op_ltm is None:
            op_ltm = fy_op
        ni_ltm = calc_ltm(fy_ni, prior_h1_ni, h1_ni)
        if ni_ltm is None:
            ni_ltm = fy_ni
        used_pattern = 'Q3' if hanki.get('revenue') and hanki_prior.get('revenue') else 'yuho_FY'

    # D&Aは常にyuho/hankiベース（決算短信にD&Aがないため）
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
    equity = (hanki.get('equity_parent') or hanki.get('shareholders_equity')
              or hanki.get('net_assets') or hanki.get('equity')
              or yuho.get('equity_parent') or yuho.get('shareholders_equity')
              or yuho.get('net_assets') or yuho.get('equity'))

    # 自己資本比率
    equity_ratio = hanki.get('equity_ratio') or yuho.get('equity_ratio')
    if equity_ratio and equity_ratio > 1:
        equity_ratio = equity_ratio / 100  # %表記→小数

    # 株価情報
    stock_price = stock_data.get('stock_price')

    # 発行済株式数: stock_data（キャッシュ後方互換 + 手動補完）→ EDINET有報/半期報
    shares = stock_data.get('shares_outstanding')
    if shares is None:
        # EDINET経営指標等から算出: 発行済株式数 - 自己株式数（千株単位）
        _shares_issued = hanki.get('shares_issued') or yuho.get('shares_issued')
        _treasury = hanki.get('treasury_shares') or yuho.get('treasury_shares') or 0
        if _shares_issued is not None:
            shares = _shares_issued - _treasury  # 既に千株単位（edinet_client.pyで変換済み）

    # 時価総額: stock_data（キャッシュ後方互換）→ 株価×発行済株式数から算出
    market_cap = stock_data.get('market_cap')
    if market_cap is None and stock_price is not None and shares is not None:
        market_cap = int(stock_price * shares / 1000)  # 千株×円÷1000 = 百万円

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

    # BS日付・決算月
    bs_date = ""
    _month_map = {1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr', 5: 'May', 6: 'Jun',
                  7: 'Jul', 8: 'Aug', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'}
    fy_end_month = _month_map.get(_fy_end_month_num, 'Mar')
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
        'fy_end': fy_end_month,
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
        'pl_history': [],
        'ltm_components': None,
        # デバッグ・Calendarize用
        '_ev': ev,
        '_multiples': multiples,
        '_calendarize_expected': calendarize_pattern,
        '_calendarize_used': used_pattern,
    }
