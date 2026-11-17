# Keys and accounts setup (human tasks H1–H5)

Everything goes into **one file: `.env` in the repo root** (copy `.env.example`). It is gitignored. Agents cannot read it (the sandbox blocks `.env`), and you should never paste keys into chat.
When `.env` is filled in, upload it to GitHub Actions in one command:

```bash
gh secret set -f .env -R allenvarghese05/desk-brief
```

Time needed: ~30–40 minutes. Do them in this order; 1–4 unblock the first scheduled run.

---

## 1. Alpaca (stocks/ETFs: IEX real-time + 15-min delayed SIP) → `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`
1. Sign up at alpaca.markets (individual account; you do **not** need to fund a live brokerage account).
2. In the dashboard, switch to the **Paper Trading** account (top-left account switcher).
3. On the paper dashboard's right side, **API Keys → Generate New Keys**.
4. Copy **Key ID** and **Secret Key** (the secret is shown once). Market data on the free "Basic" plan works with paper keys.

## 2. Twelve Data (FX) → `TWELVEDATA_API_KEY`
1. Sign up at twelvedata.com (free Basic plan).
2. Dashboard → **API Keys** → copy the key.

## 3. FRED (official rates, oil, macro) → `FRED_API_KEY`
1. Create an account at fredaccount.stlouisfed.org.
2. **My Account → API Keys → Request API Key**. Purpose: "personal data pipeline project".
3. Copy the 32-character key.

## 4. Cloudflare R2 (storage) → `R2_*`
1. Sign up at dash.cloudflare.com. Open **R2 Object Storage** and enable it (asks for a card; the free tier is 10 GB, no egress fees).
2. **Create bucket** → name `deskbrief-data`, location Automatic, storage class Standard. Leave **public access off**.
3. Note your **Account ID** (R2 overview page, right side) → `R2_ACCOUNT_ID`.
4. **Manage R2 API Tokens → Create API token**:
   - Name `deskbrief-ci-writer`, permission **Object Read & Write**, **Apply to specific bucket: deskbrief-data**, TTL Forever → copy **Access Key ID** → `R2_ACCESS_KEY_ID`, **Secret Access Key** → `R2_SECRET_ACCESS_KEY`.
   - Again: name `deskbrief-dashboard-reader`, permission **Object Read only**, same bucket → `R2_READER_ACCESS_KEY_ID`, `R2_READER_SECRET_ACCESS_KEY`.
5. Admin access = your Cloudflare login. No admin token needed.

## 5. healthchecks.io (dead-man's switch) → `HEALTHCHECK_URL_MORNING`, `HEALTHCHECK_URL_EOD`
1. Sign up at healthchecks.io (free).
2. **Add Check** `deskbrief-morning`: Schedule = Cron, expression `15 6 * * 1-5`, time zone `America/New_York`, grace 45 minutes. Copy its ping URL.
3. **Add Check** `deskbrief-eod`: Cron `30 22 * * 1-5`, time zone `UTC`, grace 60 minutes. Copy its ping URL.
4. Integrations → email (default) is enough.

## 6. Email (Gmail SMTP) → `SMTP_USER`, `SMTP_APP_PASSWORD`, `REPORT_RECIPIENTS`
1. Google Account → **Security** → turn on **2-Step Verification** (required for app passwords).
2. Search "App passwords" in the account settings → create one named `deskbrief` → copy the 16-character password (no spaces) → `SMTP_APP_PASSWORD`.
3. `SMTP_USER` = your Gmail address. `REPORT_RECIPIENTS` = comma-separated list (start with just yourself).
4. Optional: `SMTP_HOST` and `SMTP_PORT` (default `smtp.gmail.com` and `465`, implicit TLS; any other port uses STARTTLS). Leave them unset for Gmail.

---

## Later (not needed tonight)
- **H6** Excel macro project (`vbaProject.bin`) — after T33's `.bas` files exist.
- **H7** Streamlit Community Cloud private app — after T34.
- **H9** Provider display-terms check before hosted mode shows market data.

## Checklist
- [ ] `.env` filled (all lines above except the "Later" items)
- [ ] `gh secret set -f .env -R allenvarghese05/desk-brief` ran successfully (`gh secret list -R allenvarghese05/desk-brief` shows the names)
- [ ] Tell the orchestrator "keys are in" — it will trigger the first live probes and the morning workflow dispatch
