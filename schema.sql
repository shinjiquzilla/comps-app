-- Supabase PostgreSQL Schema for Comps App
-- Run this in Supabase SQL Editor (Dashboard > SQL Editor)

-- 1. companies — 企業マスター
CREATE TABLE IF NOT EXISTS companies (
  code TEXT PRIMARY KEY,
  name TEXT,
  name_en TEXT,
  sector TEXT,
  accounting TEXT,
  fy_end TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- 2. edinet_meta — EDINET書類メタデータ
CREATE TABLE IF NOT EXISTS edinet_meta (
  id SERIAL PRIMARY KEY,
  code TEXT REFERENCES companies(code),
  doc_id TEXT UNIQUE,
  doc_type TEXT,
  period_end DATE,
  filer_name TEXT,
  last_searched DATE,
  search_days INT,
  raw_meta JSONB
);

-- 3. financials — パース済み財務データ
CREATE TABLE IF NOT EXISTS financials (
  id SERIAL PRIMARY KEY,
  code TEXT REFERENCES companies(code),
  doc_type TEXT NOT NULL,
  period_end DATE,
  revenue NUMERIC,
  operating_income NUMERIC,
  ordinary_income NUMERIC,
  net_income NUMERIC,
  depreciation NUMERIC,
  cash NUMERIC,
  investment_securities NUMERIC,
  short_term_debt NUMERIC,
  long_term_debt NUMERIC,
  bonds NUMERIC,
  current_long_term_debt NUMERIC,
  current_bonds NUMERIC,
  lease_debt_current NUMERIC,
  lease_debt_noncurrent NUMERIC,
  net_assets NUMERIC,
  shareholders_equity NUMERIC,
  equity_parent NUMERIC,
  equity_ratio NUMERIC,
  dps NUMERIC,
  goodwill_amortization NUMERIC,
  raw_data JSONB,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(code, doc_type, period_end)
);

-- 4. stock_data — 株価データ
CREATE TABLE IF NOT EXISTS stock_data (
  id SERIAL PRIMARY KEY,
  code TEXT REFERENCES companies(code),
  stock_price NUMERIC,
  shares_outstanding INT,
  market_cap NUMERIC,
  company_name_en TEXT,
  fetched_date DATE,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(code, fetched_date)
);

-- 5. tanshin_forecasts — 決算短信業績予想（履歴保持）
-- 同一企業でも決算期(fy_month)×四半期(period_type)ごとに別レコード
-- 例: 6763, 2027-03, Q1 = 2027年3月期Q1時点の通期予想
--     6763, 2027-03, Q2 = 2027年3月期Q2時点の通期予想（修正あれば異なる値）
--     6763, 2028-03, Q1 = 翌年度のQ1時点予想
CREATE TABLE IF NOT EXISTS tanshin_forecasts (
  id SERIAL PRIMARY KEY,
  code TEXT REFERENCES companies(code),
  rev_forecast NUMERIC,
  op_forecast NUMERIC,
  ni_forecast NUMERIC,
  period_type TEXT NOT NULL,      -- 'FY' / 'Q1' / 'Q2' / 'Q3'
  fy_month TEXT NOT NULL,         -- '2027-03' (予想対象の決算期)
  pdf_storage_path TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(code, fy_month, period_type)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_financials_code ON financials(code);
CREATE INDEX IF NOT EXISTS idx_edinet_meta_code ON edinet_meta(code);
CREATE INDEX IF NOT EXISTS idx_stock_data_code ON stock_data(code);
