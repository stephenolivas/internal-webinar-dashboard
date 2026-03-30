# Internal Webinar Dashboard

A lightweight static dashboard that tracks Close CRM lead data for Internal Webinar funnel performance. Hosted on GitHub Pages. Updated on demand via cron-job.org → GitHub Actions.

**Live dashboard:** `https://YOUR_USERNAME.github.io/webinar-dashboard/`

---

## What It Shows

For each configured webinar event (identified by `utm_content`), the dashboard displays:

| Metric | Description |
|--------|-------------|
| **Total Booked** | Fresh first-call bookings — leads with `Funnel Name DEAL = Internal Webinar`, matching `utm_content`, and `First Call Booked Date ≥` webinar date. Excludes *Canceled by Lead* and *Outside the US*. |
| **Showed** | Of booked leads, those where `First Call Show Up (Opp) = Yes`. Includes show rate %. |
| **Qualified** | Of booked leads, those where `Qualified (Opp) = Yes`. Includes qualified rate %. |
| **Closed Won** | Of booked leads, those with lead status `Closed/Won`. Includes close rate %. |
| **Open Pipeline** | Active leads — not Lost, not Closed Won, not Canceled/Outside US. |

---

## Setup

### 1. Fork / Clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/webinar-dashboard.git
cd webinar-dashboard
```

### 2. Add your Close API key as a GitHub Secret

- Go to **Settings → Secrets and variables → Actions → New repository secret**
- Name: `CLOSE_API_KEY`
- Value: your Close CRM API key (found in Close under Settings → API Keys)

### 3. Enable GitHub Pages

- Go to **Settings → Pages**
- Source: **Deploy from a branch**
- Branch: `main`, folder: `/ (root)`
- Save

### 4. Set up cron-job.org

Create a new job in [cron-job.org](https://cron-job.org) with:

- **URL:**
  ```
  https://api.github.com/repos/YOUR_USERNAME/webinar-dashboard/actions/workflows/update.yml/dispatches
  ```
- **Method:** POST
- **Headers:**
  ```
  Authorization: Bearer YOUR_GITHUB_PAT
  Accept: application/vnd.github+json
  Content-Type: application/json
  ```
- **Body:**
  ```json
  { "ref": "main" }
  ```
- **Schedule:** Every hour (or as often as you want)

> **GitHub PAT:** Create a fine-grained personal access token at GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. Grant it **Actions: Read and Write** on this repository.

### 5. Run manually for the first time

In GitHub → Actions → **Update Webinar Dashboard** → **Run workflow**.

---

## Adding a New Webinar

When a new webinar runs (e.g., April 24, 2026):

1. Open `fetch_data.py`
2. Find the `WEBINARS` list near the top
3. Add a new entry (or uncomment the example):

```python
WEBINARS = [
    {
        "label":              "March 24, 2026 Webinar",
        "utm_content":        "mar24_end_cta",
        "booked_on_or_after": "2026-03-24",
        "active":             True,
    },
    {
        "label":              "April 24, 2026 Webinar",
        "utm_content":        "apr24_end_cta",
        "booked_on_or_after": "2026-04-24",
        "active":             True,      # ← set True when ready
    },
]
```

4. Commit and push — the next cron run will include the new webinar on the dashboard.

> The `utm_content` pattern follows the convention: `{mon}{day}_end_cta`
> e.g., March 24 → `mar24_end_cta`, April 24 → `apr24_end_cta`

---

## How It Works

```
cron-job.org (schedule)
    └── POST to GitHub Actions API
          └── workflow_dispatch triggers update.yml
                └── fetch_data.py runs
                      ├── Discovers custom field IDs from Close API
                      ├── Fetches all leads (Funnel = Internal Webinar)
                      ├── Filters by utm_content + booked date per webinar
                      ├── Categorizes by status + opp fields
                      └── Writes index.html
                └── Git commit + push index.html
                      └── GitHub Pages serves the updated dashboard
```

---

## Custom Fields Used

| Field | Object | Close CF ID | Notes |
|-------|--------|-------------|-------|
| Funnel Name DEAL | Lead | `cf_xqDQE8fkPsWa0RNEve7hcaxKblCe6489XeZGRDzyPdX` | Hardcoded — known stable |
| First Call Booked Date | Lead | `cf_JsJZIVh7QDcFQBXr4cTRBxf1AkREpLdsKiZB4AEJ8Xh` | Hardcoded — known stable |
| utm_content | Lead | Discovered at runtime by field name | |
| First Call Show Up (Opp) | Opportunity | Discovered at runtime by field name | |
| Qualified (Opp) | Opportunity | Discovered at runtime by field name | |

The script discovers runtime field IDs by fetching `/api/v1/custom_field/lead/` and `/api/v1/custom_field/opportunity/` and matching by known field names. If a field name changes in Close, update the candidate names in the `resolve_field()` calls in `fetch_data.py`.

---

## Status Definitions

| Status | Counted As |
|--------|------------|
| Canceled (by Lead) | Excluded from all counts |
| Outside the US | Excluded from all counts |
| Lost | Not in Booked or Open Pipeline; shown in footnote |
| Closed/Won | Counted in Booked + Closed Won; NOT in Open Pipeline |
| Everything else | Counted in Booked + Open Pipeline |

This follows the same exclusion logic as the Rep Dashboard (see dashboard-metrics-reference.md).

---

## Local Development

```bash
pip install requests
export CLOSE_API_KEY=your_key_here
python fetch_data.py
# → Writes index.html; open in browser to preview
```
