#!/usr/bin/env python3
"""
Internal Webinar Lead Dashboard
================================
Fetches lead & opportunity data from Close CRM for Internal Webinar leads,
then generates a static index.html for GitHub Pages hosting.

Triggered by cron-job.org → GitHub Actions (workflow_dispatch).

To add a new webinar (e.g., April 2026), duplicate the entry in WEBINARS
and update utm_content + booked_on_or_after. Set active=True to display it.
"""

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

# ─── Constants ────────────────────────────────────────────────────────────────

PACIFIC  = ZoneInfo("America/Los_Angeles")
BASE_URL = "https://api.close.com/api/v1"

CLOSE_API_KEY = os.environ.get("CLOSE_API_KEY", "")
if not CLOSE_API_KEY:
    print("ERROR: CLOSE_API_KEY environment variable not set.", file=sys.stderr)
    sys.exit(1)

# Known custom field IDs (confirmed in Close)
CF_FUNNEL_NAME_DEAL           = "cf_xqDQE8fkPsWa0RNEve7hcaxKblCe6489XeZGRDzyPdX"
# CF_FIRST_SALES_CALL_BOOKED_DATE is discovered at runtime by field name (see main())
CF_FIRST_CALL_SHOW_UP         = "cf_OPyvpU45RdvjLqfm8V1VWwNxrGKogEH2IBJmfCj0Uhq"
CF_QUALIFIED                  = "cf_ZDx7NBQaDzV1yYrFcBMzt6cIYj81dAcswpNN0CQzCPS"

# Lead statuses — Close returns these with emoji prefixes stripped in status_label
STATUS_CANCELED    = "Canceled (by Lead)"
STATUS_OUTSIDE_US  = "Outside the US"
STATUS_LOST        = "Lost"
STATUS_CLOSED_WON  = "Closed/Won"
STATUS_NO_SHOW     = "No Show"          # excluded from open pipeline
EXCLUDED_FROM_BOOKED = {STATUS_CANCELED, STATUS_OUTSIDE_US}

THROTTLE = 0.35   # seconds between API calls (stay under Close's ~100 req/min)


# ─── Webinar Configuration ────────────────────────────────────────────────────
# Add a new dict here for each webinar event. Pattern for utm_content:
#   "mar24_end_cta" = March 24 end-of-webinar CTA
#   "apr24_end_cta" = April 24 end-of-webinar CTA
# Set active=True to show a webinar on the dashboard.

WEBINARS = [
    {
        "label":              "March 24, 2026 Webinar",
        "utm_content":        "mar24_end_cta",   # Contact-level utm_content field
        "booked_on_or_after": "2026-03-24",      # First Sales Call Booked Date >= this
        "booked_before":      "2026-04-24",      # exclusive upper bound (next webinar date)
        "active":             True,
    },
    # Uncomment when the April webinar runs:
    # {
    #     "label":              "April 24, 2026 Webinar",
    #     "utm_content":        "apr24_end_cta",
    #     "booked_on_or_after": "2026-04-24",
    #     "booked_before":      "2026-05-24",
    #     "active":             False,
    # },
]


# ─── API Helpers ──────────────────────────────────────────────────────────────

_sess = requests.Session()
_sess.auth = (CLOSE_API_KEY, "")
_sess.headers.update({"Accept": "application/json"})


def api_get(path: str, params: dict | None = None, retries: int = 3) -> dict:
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        try:
            r = _sess.get(url, params=params, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                print(f"  [429] Rate limited — waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            time.sleep(THROTTLE)
            return r.json()
        except requests.exceptions.RequestException as exc:
            if attempt == retries - 1:
                raise
            print(f"  [WARN] {exc} — retrying ({attempt + 1}/{retries})")
            time.sleep(2 ** attempt)
    return {}


def get_all_pages(path: str, params: dict | None = None) -> list:
    """Paginate through all results from an endpoint using _skip/_limit."""
    params = dict(params or {})
    params.setdefault("_limit", 100)
    results, skip = [], 0
    while True:
        params["_skip"] = skip
        data  = api_get(path, params)
        items = data.get("data", [])
        results.extend(items)
        if not data.get("has_more", False):
            break
        skip += len(items)
    return results


# ─── Custom Field Discovery ───────────────────────────────────────────────────

def discover_custom_fields() -> tuple[dict, dict]:
    """
    Fetch all custom field definitions from Close.
    Returns (lead_map, opp_map) — each is {field_name: field_id}.
    """
    print("Discovering custom fields...")
    lead_fields = get_all_pages("/custom_field/lead/")
    opp_fields  = get_all_pages("/custom_field/opportunity/")
    lead_map = {f["name"]: f["id"] for f in lead_fields}
    opp_map  = {f["name"]: f["id"] for f in opp_fields}
    print(f"  Lead fields: {sorted(lead_map.keys())}")
    print(f"  Opp fields:  {sorted(opp_map.keys())}")
    return lead_map, opp_map


def resolve_field(mapping: dict, *candidate_names: str) -> str | None:
    """Return the field ID for the first matching candidate name."""
    for name in candidate_names:
        if name in mapping:
            print(f"  ✓ Resolved '{name}' → {mapping[name]}")
            return mapping[name]
    print(f"  ✗ Could not resolve any of: {candidate_names}")
    return None


# ─── Lead Fetching ────────────────────────────────────────────────────────────

def fetch_internal_webinar_leads() -> list[dict]:
    """
    Fetch all leads where Funnel Name DEAL = 'Internal Webinar',
    including their opportunities (for show-up/qualified fields).
    """
    print("\nFetching Internal Webinar leads from Close...")
    query  = f'custom.{CF_FUNNEL_NAME_DEAL}:"Internal Webinar"'
    params = {
        "query":   query,
        "_fields": "id,display_name,status_label,custom,opportunities,contacts",
    }
    leads = get_all_pages("/lead/", params)
    print(f"  → {len(leads)} leads fetched with Funnel = Internal Webinar")
    return leads


# ─── Metrics Calculation ─────────────────────────────────────────────────────

def process_webinar(webinar: dict, all_leads: list[dict], field_ids: dict) -> dict:
    """
    Filter and categorize leads for a specific webinar config.
    Filtering logic:
      - First Sales Call Booked Date >= booked_on_or_after (and < booked_before if set)
      - Excludes Canceled (by Lead) and Outside the US from booked count
      - No Show is counted as booked but NOT as open pipeline
    """
    utm_value       = webinar.get("utm_content", "")       # Contact-level field value
    start_date      = webinar["booked_on_or_after"]        # "YYYY-MM-DD"
    end_date        = webinar.get("booked_before", "")     # optional upper bound
    sales_booked_cf = field_ids.get("sales_booked_cf_id")
    show_up_cf_id   = field_ids.get("show_up_cf_id")
    qualified_cf_id = field_ids.get("qualified_cf_id")

    counts = {
        "booked":                      0,
        "showed":                      0,
        "qualified":                   0,
        "closed_won":                  0,
        "open_pipeline":               0,
        "no_show":                     0,
        "lost":                        0,
        "excluded_cancelled_outside":  0,
    }

    for lead in all_leads:
        custom = lead.get("custom") or {}

        # ── 1a. Filter: utm_content on any contact must match ────────────────────
        if utm_value:
            contacts = lead.get("contacts") or []
            matched_utm = False
            for contact in contacts:
                contact_custom = contact.get("custom") or {}
                val = str(contact_custom.get(CF_UTM_CONTENT, "") or "").strip()
                if val == utm_value:
                    matched_utm = True
                    break
            if not matched_utm:
                continue

        # ── 1b. Filter: First Sales Call Booked Date in window ────────────────
        if sales_booked_cf:
            booked_raw = str(custom.get(sales_booked_cf, "") or "").strip()
        else:
            # Fallback: scan for any field matching the name
            booked_raw = ""
            for k, v in custom.items():
                if "first sales call booked" in k.lower():
                    booked_raw = str(v or "").strip()
                    break

        if not booked_raw:
            continue
        booked_date = booked_raw[:10]   # "YYYY-MM-DD"
        if booked_date < start_date:
            continue
        if end_date and booked_date >= end_date:
            continue

        # ── 2. Status-based categorization ────────────────────────────────────
        status_raw = (lead.get("status_label") or "").strip()
        # Close returns status_label with emoji prefix (e.g. "🔻 Canceled (by Lead)")
        # Strip leading emoji/symbols so matching is reliable
        status = status_raw.lstrip("🔻📄👻☎️🗓️📞🏆🕛💤💔 ").strip()

        # Check for excluded statuses (partial match to catch emoji variants)
        def status_is(label: str) -> bool:
            return label.lower() in status_raw.lower()

        if status_is("Canceled (by Lead)") or status_is("Outside the US"):
            counts["excluded_cancelled_outside"] += 1
            continue

        # This lead counts as a valid "booked" meeting
        counts["booked"] += 1

        # ── 3. Opportunity-level fields (show-up / qualified) ─────────────────
        showed    = False
        qualified = False
        for opp in (lead.get("opportunities") or []):
            opp_custom = opp.get("custom") or {}

            if show_up_cf_id and not showed:
                val = str(opp_custom.get(show_up_cf_id, "") or "").lower().strip()
                if val == "yes":
                    showed = True

            if qualified_cf_id and not qualified:
                val = str(opp_custom.get(qualified_cf_id, "") or "").lower().strip()
                if val == "yes":
                    qualified = True

        if showed:
            counts["showed"]    += 1
        if qualified:
            counts["qualified"] += 1

        # ── 4. Win / Loss / No Show / Open Pipeline ───────────────────────────
        if status_is("Closed / Won") or status_is("Closed/Won"):
            counts["closed_won"] += 1
        elif status_is("Lost"):
            counts["lost"] += 1
        elif status_is("No Show"):
            counts["no_show"] += 1
            # No Show = not open pipeline (attended their slot but didn't show)
        else:
            counts["open_pipeline"] += 1

    # ── Derived rates (% of booked) ───────────────────────────────────────────
    booked = counts["booked"]

    def pct(n: int) -> float:
        return round(n / booked * 100, 1) if booked else 0.0

    return {
        "label":          webinar["label"],
        "start_date":     start_date,
        "end_date":       end_date,
        "counts":         counts,
        "show_rate":      pct(counts["showed"]),
        "qualified_rate": pct(counts["qualified"]),
        "close_rate":     pct(counts["closed_won"]),
        "open_pct":       pct(counts["open_pipeline"]),
    }


# ─── HTML Generation ─────────────────────────────────────────────────────────

def pct_bar(pct: float, css_class: str) -> str:
    width = min(100.0, max(0.0, pct))
    return (
        f'<div class="bar-bg">'
        f'<div class="bar-fill {css_class}" style="width:{width:.1f}%"></div>'
        f'</div>'
    )


def funnel_row(name: str, desc: str, count: int, pct: float | None,
               num_cls: str, bar_cls: str) -> str:
    pct_html = ""
    bar_html = ""
    if pct is not None:
        pct_html = f'<span class="pct-badge">{pct}%</span>'
        bar_html = pct_bar(pct, bar_cls)

    return f"""
      <tr>
        <td>
          <div class="metric-name">{name}</div>
          <div class="metric-desc">{desc}</div>
        </td>
        <td class="num-cell">
          <span class="big-num {num_cls}">{count}</span>{pct_html}
        </td>
        <td class="bar-cell">{bar_html}</td>
      </tr>"""


def build_webinar_card(m: dict) -> str:
    c      = m["counts"]
    booked = c["booked"]

    if booked == 0:
        table_html = '<div class="no-data">No qualifying bookings found for this webinar configuration.</div>'
    else:
        rows  = funnel_row(
            "Total Booked",
            "Fresh first-call meetings — excludes Canceled by Lead &amp; Outside US",
            booked, None, "num-booked", ""
        )
        rows += funnel_row(
            "Showed",
            "First Call Show Up (Opp) = Yes",
            c["showed"], m["show_rate"], "num-showed", "bar-showed"
        )
        rows += funnel_row(
            "Qualified",
            "Qualified (Opp) = Yes",
            c["qualified"], m["qualified_rate"], "num-qualified", "bar-qualified"
        )
        rows += funnel_row(
            "Closed Won",
            "Lead status = Closed/Won",
            c["closed_won"], m["close_rate"], "num-won", "bar-won"
        )
        rows += funnel_row(
            "Open Pipeline",
            "Active — not Lost, not Closed Won, not Canceled/Outside US",
            c["open_pipeline"], m["open_pct"], "num-open", "bar-open"
        )

        excl = c["excluded_cancelled_outside"]
        lost = c["lost"]
        footnote = ""
        if excl > 0 or lost > 0:
            parts = []
            if excl > 0:
                parts.append(f"{excl} excluded (Canceled by Lead or Outside US)")
            if lost > 0:
                parts.append(f"{lost} Lost")
            footnote = f'<div class="footnote">&#9432; {" · ".join(parts)}</div>'

        table_html = f"""
  <table class="funnel-table">
    <thead>
      <tr>
        <th>Metric</th>
        <th class="num-th">Count / Rate</th>
        <th>% of Booked</th>
      </tr>
    </thead>
    <tbody>{rows}
    </tbody>
  </table>{footnote}"""

    return f"""
<section class="webinar-card">
  <div class="card-header">
    <span class="card-label">{m["label"]}</span>
    <span class="utm-badge">{m["utm_content"]}</span>
    <span class="card-meta">bookings on or after {m["start_date"]}</span>
  </div>
  {table_html}
</section>"""


def generate_html(all_metrics: list[dict]) -> str:
    now          = datetime.now(PACIFIC)
    today_str    = now.strftime("%A, %B %-d, %Y")
    updated_str  = now.strftime("%-I:%M %p PST")

    cards_html = "\n".join(build_webinar_card(m) for m in all_metrics)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Internal Webinar Dashboard</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #edf0ed;
      color: #1a1a1a;
      font-size: 13px;
    }}

    /* ── Header ─────────────────────────────────────────────────────── */
    .site-header {{
      background: #0f1f10;
      color: #d4e8c2;
      padding: 10px 20px 11px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .title-group h1 {{
      font-size: 15px;
      font-weight: 700;
      color: #e8f5e0;
      letter-spacing: 0.02em;
      display: flex;
      align-items: center;
      gap: 7px;
    }}
    .title-group h1 svg {{
      width: 16px; height: 16px; fill: #5ccc6e; flex-shrink: 0;
    }}
    .title-group .subtitle {{
      font-size: 11px;
      color: #7aaa68;
      margin-top: 3px;
      margin-left: 23px;
    }}
    .meta-right {{
      text-align: right;
      font-size: 11px;
      color: #7aaa68;
    }}
    .meta-right .date-line {{
      color: #a8d88a;
      font-weight: 600;
      font-size: 12px;
    }}
    .live-dot {{
      display: inline-block;
      width: 7px; height: 7px;
      background: #4ccc44;
      border-radius: 50%;
      margin-right: 5px;
      animation: blink 2s ease-in-out infinite;
    }}
    @keyframes blink {{
      0%, 100% {{ opacity: 1; }}
      50%       {{ opacity: 0.3; }}
    }}

    /* ── Layout ─────────────────────────────────────────────────────── */
    .main {{
      max-width: 900px;
      margin: 22px auto;
      padding: 0 16px;
    }}

    /* ── Webinar Card ────────────────────────────────────────────────── */
    .webinar-card {{
      background: #fff;
      border: 1px solid #cfd9cf;
      border-radius: 4px;
      margin-bottom: 22px;
      overflow: hidden;
    }}
    .card-header {{
      background: #f5f8f4;
      border-bottom: 2px solid #cfd9cf;
      padding: 9px 16px;
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .card-label {{
      font-size: 12px;
      font-weight: 700;
      color: #1d5c2e;
      text-transform: uppercase;
      letter-spacing: 0.09em;
    }}
    .utm-badge {{
      background: #e2f0e2;
      color: #1a5a2a;
      font-size: 10px;
      font-weight: 700;
      font-family: "SF Mono", "Fira Code", monospace;
      padding: 2px 8px;
      border-radius: 10px;
      border: 1px solid #a8cca8;
    }}
    .card-meta {{
      font-size: 11px;
      color: #8aaa88;
      margin-left: auto;
    }}

    /* ── Funnel Table ────────────────────────────────────────────────── */
    .funnel-table {{
      width: 100%;
      border-collapse: collapse;
    }}
    .funnel-table thead tr {{
      background: #f2f6f2;
    }}
    .funnel-table th {{
      padding: 7px 16px;
      font-size: 11px;
      font-weight: 700;
      color: #4a7050;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      border-bottom: 1px solid #d8e4d8;
      text-align: left;
    }}
    .funnel-table th.num-th {{ text-align: right; }}
    .funnel-table td {{
      padding: 10px 16px;
      border-bottom: 1px solid #edf0ed;
      vertical-align: middle;
    }}
    .funnel-table tbody tr:last-child td {{ border-bottom: none; }}
    .funnel-table tbody tr:hover {{ background: #fafbfa; }}

    .metric-name {{ font-weight: 600; color: #222; }}
    .metric-desc {{ font-size: 11px; color: #999; margin-top: 2px; }}

    .num-cell {{
      text-align: right;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }}
    .big-num {{
      font-size: 22px;
      font-weight: 700;
      line-height: 1;
    }}
    .num-booked    {{ color: #1a2e1a; }}
    .num-showed    {{ color: #1a7f3c; }}
    .num-qualified {{ color: #2255cc; }}
    .num-won       {{ color: #b84400; }}
    .num-open      {{ color: #4a7722; }}

    .pct-badge {{
      display: inline-block;
      margin-left: 8px;
      font-size: 11px;
      font-weight: 700;
      color: #fff;
      background: #3a8a4a;
      padding: 2px 8px;
      border-radius: 10px;
      vertical-align: middle;
    }}

    /* Bar */
    .bar-cell  {{ width: 160px; }}
    .bar-bg    {{
      height: 8px;
      background: #e4ece4;
      border-radius: 4px;
      overflow: hidden;
    }}
    .bar-fill       {{ height: 100%; border-radius: 4px; }}
    .bar-showed     {{ background: #3db554; }}
    .bar-qualified  {{ background: #4477dd; }}
    .bar-won        {{ background: #dd6622; }}
    .bar-open       {{ background: #88aa44; }}

    /* Footnote */
    .footnote {{
      padding: 7px 16px;
      font-size: 11px;
      color: #888;
      background: #fafafa;
      border-top: 1px solid #eee;
    }}

    /* No data */
    .no-data {{
      padding: 36px 16px;
      text-align: center;
      color: #aaa;
    }}

    /* Footer */
    footer {{
      text-align: center;
      padding: 18px;
      font-size: 11px;
      color: #9aa99a;
    }}
  </style>
</head>
<body>

<header class="site-header">
  <div class="title-group">
    <h1>
      <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
        <path d="M3 3h18v2H3V3zm0 4h18v2H3V7zm0 4h12v2H3v-2zm0 4h8v2H3v-2zM14 12l6 4-6 4V12z"/>
      </svg>
      Internal Webinar Dashboard
    </h1>
    <div class="subtitle">Funnel Name DEAL = Internal Webinar &nbsp;·&nbsp; First Sales Call Booked &ge; webinar date</div>
  </div>
  <div class="meta-right">
    <div class="date-line"><span class="live-dot"></span>{today_str}</div>
    <div>Last updated: {updated_str}</div>
  </div>
</header>

<main class="main">
  {cards_html}
</main>

<footer>
  Data source: Close CRM &nbsp;·&nbsp; Hosted on GitHub Pages &nbsp;·&nbsp; Updated via cron-job.org &rarr; GitHub Actions
</footer>

</body>
</html>"""


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ts = datetime.now(PACIFIC).strftime("%Y-%m-%d %H:%M PST")
    print(f"=== Internal Webinar Dashboard - {ts} ===\n")

    # Discover "First Sales Call Booked Date" field ID by name
    print("Discovering lead custom field IDs...")
    lead_fields = get_all_pages("/custom_field/lead/")
    lead_cf_map = {f["name"]: f["id"] for f in lead_fields}

    sales_booked_cf_id = None
    for candidate in ["First Sales Call Booked Date", "First Sales Call Booked",
                       "First Sales Booked Date"]:
        if candidate in lead_cf_map:
            sales_booked_cf_id = lead_cf_map[candidate]
            print(f"  Found '{candidate}' -> {sales_booked_cf_id}")
            break
    if not sales_booked_cf_id:
        print("  WARNING: 'First Sales Call Booked Date' not found by name.")
        print("  Available lead fields:", sorted(lead_cf_map.keys()))
        print("  Will attempt fallback scan on lead custom data.")

    field_ids = {
        "sales_booked_cf_id": sales_booked_cf_id,
        "show_up_cf_id":      CF_FIRST_CALL_SHOW_UP,
        "qualified_cf_id":    CF_QUALIFIED,
    }
    print("Field IDs in use:")
    for k, v in field_ids.items():
        print(f"  {k}: {v}")

    # ── Step 2: Fetch all Internal Webinar leads once ─────────────────────────
    all_leads = fetch_internal_webinar_leads()

    # ── Step 3: Process each active webinar config ────────────────────────────
    active_webinars = [w for w in WEBINARS if w.get("active", True)]
    all_metrics: list[dict] = []

    for webinar in active_webinars:
        m = process_webinar(webinar, all_leads, field_ids)
        all_metrics.append(m)

        c = m["counts"]
        print(f"\n{m['label']} ({m['utm_content']})")
        print(f"  Booked:        {c['booked']}")
        print(f"  Showed:        {c['showed']}  ({m['show_rate']}%)")
        print(f"  Qualified:     {c['qualified']}  ({m['qualified_rate']}%)")
        print(f"  Closed Won:    {c['closed_won']}  ({m['close_rate']}%)")
        print(f"  Open Pipeline: {c['open_pipeline']}  ({m['open_pct']}%)")
        print(f"  Lost:          {c['lost']}")
        print(f"  Excl (Cxl/OUS):{c['excluded_cancelled_outside']}")

    # ── Step 4: Generate HTML ─────────────────────────────────────────────────
    html = generate_html(all_metrics)
    out  = "index.html"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"\n✓ Dashboard written → {out}")


if __name__ == "__main__":
    main()
