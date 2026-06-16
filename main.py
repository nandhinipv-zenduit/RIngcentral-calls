"""
rc_pull_calls.py
────────────────
Pulls user-level call data from RingCentral Analytics API
and uploads to Zoho Analytics (truncate + append pattern).

Setup:
    pip install requests pandas

Credentials pulled from environment variables.
RC auth uses JWT flow — no refresh token needed.

Usage (PyCharm — run directly, no CLI args needed):
    Just hit Run. Adjust START_DATE / END_DATE in CONFIG.
"""

import os
import requests
import base64
import time
import json
import pandas as pd
from datetime import datetime, timedelta, timezone

# ==============================================================
# CONFIG — edit here
# ==============================================================

# RingCentral — env vars preferred; fallback to hardcode for local dev
RC_CLIENT_ID     = os.environ.get("RC_CLIENT_ID")
RC_CLIENT_SECRET = os.environ.get("RC_CLIENT_SECRET")
RC_JWT_TOKEN     = os.environ.get("RC_JWT")           # Set this in GitHub Secrets / local env

RC_BASE_URL = "https://platform.ringcentral.com"

# Date range to pull
START_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)
END_DATE   = datetime(2026, 6, 16, tzinfo=timezone.utc)

# Zoho Analytics — uses same "uni" credentials as your existing auth module
ZOHO_CLIENT_ID     = os.environ.get("ZOHO_CLIENT_ID_UNI")
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET_UNI")
ZOHO_REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN_UNI")

ZOHO_ACCOUNTS_URL  = "https://accounts.zoho.com"
ZOHO_API_DOMAIN    = "https://analyticsapi.zoho.com/restapi/v2"
ZOHO_ORG_ID        = "67409019"
ZOHO_WORKSPACE_ID  = "953790000013364003"
ZOHO_VIEW_ID       = "953790000055404002"   # RC Calls table (Ringcentral calls)

ZOHO_MAX_BYTES     = 14 * 1024 * 1024       # 14 MB per chunk

# ==============================================================
# RINGCENTRAL AUTH — JWT FLOW
# ==============================================================

_rc_access_token: str | None = None


def rc_refresh() -> str:
    """Exchange JWT assertion for a fresh RC access token."""
    global _rc_access_token

    if not RC_JWT_TOKEN:
        raise RuntimeError(
            "RC_JWT env var is not set. "
            "Create a JWT credential at developers.ringcentral.com and set RC_JWT."
        )

    auth = base64.b64encode(
        f"{RC_CLIENT_ID}:{RC_CLIENT_SECRET}".encode()
    ).decode()

    res = requests.post(
        f"{RC_BASE_URL}/restapi/oauth/token",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion":  RC_JWT_TOKEN,
        },
        timeout=30,
    )

    data = res.json()

    if "access_token" not in data:
        raise RuntimeError(f"RC JWT auth failed: {json.dumps(data, indent=2)}")

    _rc_access_token = data["access_token"]
    print("[RC auth] Access token obtained via JWT ✅")
    return _rc_access_token


def rc_token() -> str:
    global _rc_access_token
    if not _rc_access_token:
        rc_refresh()
    return _rc_access_token


# ==============================================================
# RINGCENTRAL API WRAPPERS
# ==============================================================

def rc_get(path: str, params: dict | None = None) -> dict | None:
    url  = f"{RC_BASE_URL}{path}"
    wait = 2

    for attempt in range(5):
        res = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {rc_token()}"},
            timeout=30,
        )
        if res.status_code == 200:
            return res.json()
        if res.status_code == 401:
            print("[RC auth] 401 — refreshing token via JWT")
            rc_refresh()
            continue
        if res.status_code == 429:
            print(f"[RC rate-limit] waiting {wait}s")
            time.sleep(wait)
            wait *= 2
            continue
        print(f"[RC error] GET {path} → {res.status_code}: {res.text[:200]}")
        return None

    return None


def rc_post(path: str, body: dict) -> dict | None:
    url  = f"{RC_BASE_URL}{path}"
    wait = 2

    for attempt in range(5):
        res = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {rc_token()}"},
            timeout=30,
        )
        if res.status_code == 200:
            return res.json()
        if res.status_code == 401:
            print("[RC auth] 401 — refreshing token via JWT")
            rc_refresh()
            continue
        if res.status_code == 429:
            print(f"[RC rate-limit] waiting {wait}s")
            time.sleep(wait)
            wait *= 2
            continue
        print(f"[RC error] POST {path} → {res.status_code}: {res.text[:200]}")
        return None

    return None


# ==============================================================
# USER DIRECTORY
# ==============================================================

def get_user_map() -> dict[str, dict]:
    user_map = {}
    page     = 1

    while True:
        data = rc_get("/restapi/v1.0/account/~/extension", {
            "page":    page,
            "perPage": 100,
            "type":    "User",
            "status":  "Enabled",
        })

        if not data or "records" not in data:
            break

        for u in data["records"]:
            uid = str(u["id"])
            user_map[uid] = {
                "name":       u.get("name", "Unknown"),
                "ext":        u.get("extensionNumber", ""),
                "email":      u.get("contact", {}).get("email", ""),
                "department": (
                        u.get("contact", {}).get("department")
                        or u.get("department")
                        or (u.get("site") or {}).get("name")
                        or u.get("jobTitle")
                        or "No Group"
                ),
            }
            

        paging      = data.get("paging", {})
        total_pages = paging.get("totalPages", 1)
        print(f"[users] Page {page}/{total_pages} — {len(data['records'])} users")

        if page >= total_pages:
            break
        page += 1

    print(f"[users] Total mapped: {len(user_map)}")
    return user_map


# ==============================================================
# DAILY CALL FETCH
# ==============================================================

def fetch_daily_calls(date: datetime, user_map: dict) -> list[dict]:
    day_start = date.replace(hour=0,  minute=0,  second=0,  microsecond=0, tzinfo=timezone.utc)
    day_end   = min(
        date.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=timezone.utc),
        datetime.now(timezone.utc)
    )

    body = {
        "grouping": {"groupBy": "Users"},
        "timeSettings": {
            "timeZone": "UTC",
            "timeRange": {
                "timeFrom": day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeTo":   day_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        },
        "responseOptions": {
            "counters": {
                "allCalls":        {"aggregationType": "Sum"},
                "callsByDirection":{"aggregationType": "Sum"},
                "callsByResult":   {"aggregationType": "Sum"},
                "callsByOrigin":   {"aggregationType": "Sum"},
            },
            "timers": {
                "allCallsDuration":         {"aggregationType": "Sum"},
                "callsDurationByDirection": {"aggregationType": "Sum"},
            },
        },
    }

    result = rc_post("/analytics/calls/v1/accounts/~/aggregation/fetch", body)
    rows   = []

    if not result:
        return rows

    records = (result.get("data") or {}).get("records") or result.get("records") or []

    for r in records:
        key      = str(r.get("key", ""))
        info     = r.get("info", {})
        counters = r.get("counters", {})
        timers   = r.get("timers", {})

        user_info = user_map.get(key, {})
        name      = info.get("name")              or user_info.get("name",       "Unknown")
        ext       = info.get("extensionNumber")   or user_info.get("ext",        "")
        dept      = user_info.get("department",   "No Group")
        email     = user_info.get("email",        "")

        all_calls    = (counters.get("allCalls")         or {}).get("values", 0)
        direction    = (counters.get("callsByDirection") or {}).get("values", {})
        inbound      = direction.get("inbound",  0)
        outbound     = direction.get("outbound", 0)

        result_vals  = (counters.get("callsByResult") or {}).get("values", {})
        missed       = result_vals.get("missed",    0)
        answered     = result_vals.get("accepted",  0)
        voicemail    = result_vals.get("voiceMail", 0)

        origin_vals  = (counters.get("callsByOrigin") or {}).get("values", {})
        queue_calls  = origin_vals.get("queue",  0)
        direct_calls = origin_vals.get("direct", 0)

        all_dur    = (timers.get("allCallsDuration")                           or {}).get("values", 0)
        in_dur     = ((timers.get("callsDurationByDirection") or {}).get("values") or {}).get("inbound",  0)
        out_dur    = ((timers.get("callsDurationByDirection") or {}).get("values") or {}).get("outbound", 0)
        avg_handle = round(all_dur / all_calls, 1) if all_calls else 0

        rows.append({
            "date":               day_start.strftime("%Y-%m-%d"),
            "year":               day_start.year,
            "month":              day_start.month,
            "week":               day_start.isocalendar()[1],
            "user_id":            key,
            "name":               name,
            "ext":                ext,
            "email":              email,
            "department":         dept,
            "total_calls":        all_calls,
            "inbound":            inbound,
            "outbound":           outbound,
            "answered":           answered,
            "missed":             missed,
            "voicemail":          voicemail,
            "queue_calls":        queue_calls,
            "direct_calls":       direct_calls,
            "total_duration_s":   all_dur,
            "inbound_duration_s": in_dur,
            "outbound_duration_s":out_dur,
            "avg_handle_time_s":  avg_handle,
        })

    return rows


# ==============================================================
# BUILD DATAFRAME
# ==============================================================

def build_dataframe(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        print("[df] No rows returned.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    df["date"]  = pd.to_datetime(df["date"])
    df["year"]  = df["year"].astype(int)
    df["month"] = df["month"].astype(int)
    df["week"]  = df["week"].astype(int)

    for col in [
        "total_calls", "inbound", "outbound", "answered",
        "missed", "voicemail", "queue_calls", "direct_calls",
        "total_duration_s", "inbound_duration_s", "outbound_duration_s",
    ]:
        df[col] = df[col].astype(int)

    df["avg_handle_time_s"] = df["avg_handle_time_s"].astype(float)
    df = df.sort_values(["date", "name"]).reset_index(drop=True)

    print(f"[df] {df.shape[0]} rows × {df.shape[1]} cols")
    return df


# ==============================================================
# ZOHO ANALYTICS — AUTH
# ==============================================================

def zoho_get_access_token() -> str:
    res = requests.post(
        f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token",
        data={
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id":     ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type":    "refresh_token",
        },
        timeout=30,
    )
    res.raise_for_status()
    data = res.json()

    if "access_token" not in data:
        raise RuntimeError(f"Zoho token failed: {data}")

    print("[Zoho auth] Token OK")
    return data["access_token"]


# ==============================================================
# ZOHO ANALYTICS — IMPORT
# ==============================================================

def _zoho_import_chunk(csv_bytes: bytes, import_type: str, access_token: str):
    url = (
        f"{ZOHO_API_DOMAIN}"
        f"/workspaces/{ZOHO_WORKSPACE_ID}"
        f"/views/{ZOHO_VIEW_ID}/data"
    )

    res = requests.post(
        url,
        headers={
            "Authorization":    f"Zoho-oauthtoken {access_token}",
            "ZANALYTICS-ORGID": ZOHO_ORG_ID,
        },
        data={
            "CONFIG": json.dumps({
                "importType":   import_type,
                "fileType":     "csv",
                "autoIdentify": "true",
                "onError":      "setcolumnempty",
            })
        },
        files={
            "FILE": ("rc_calls.csv", csv_bytes, "text/csv")
        },
        timeout=300,
    )

    print(f"[Zoho import] {import_type} → {res.status_code}")

    if res.status_code != 200:
        print(res.text[:500])

    res.raise_for_status()
    return res.json()


def zoho_upload(df: pd.DataFrame, access_token: str):
    if df.empty:
        print("[Zoho] Empty DataFrame — skipping upload.")
        return

    df = df.copy()
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    header_bytes   = len(df.iloc[0:0].to_csv(index=False).encode("utf-8"))
    full_bytes     = len(df.to_csv(index=False).encode("utf-8"))
    avg_row        = max(1, (full_bytes - header_bytes) // max(1, len(df)))
    rows_per_chunk = max(1, (ZOHO_MAX_BYTES - header_bytes) // avg_row)

    total = len(df)
    print(f"\n[Zoho] Uploading {total:,} rows in chunks of {rows_per_chunk:,}")

    for i in range(0, total, rows_per_chunk):
        chunk       = df.iloc[i : i + rows_per_chunk]
        csv_bytes   = chunk.to_csv(index=False).encode("utf-8")
        import_type = "truncateadd" if i == 0 else "append"

        _zoho_import_chunk(csv_bytes, import_type, access_token)
        print(f"[Zoho] ✔ {min(i + len(chunk), total):,}/{total:,} rows uploaded")

    print("[Zoho] ✅ Upload complete")


# ==============================================================
# MAIN
# ==============================================================

def main():
    print("=" * 60)
    print("RingCentral → Zoho Analytics")
    print(f"Range: {START_DATE.date()} → {END_DATE.date()}")
    print("=" * 60)

    # ── Step 1: RC auth via JWT
    rc_refresh()

    # ── Step 2: User directory
    user_map = get_user_map()

    # ── Step 3: Pull daily call data
    all_rows   = []
    current    = START_DATE
    total_days = (END_DATE - START_DATE).days + 1
    day_num    = 0

    while current <= END_DATE:
        day_num += 1
        print(f"[fetch] Day {day_num}/{total_days}: {current.date()}", end="  ")
        rows = fetch_daily_calls(current, user_map)
        print(f"→ {len(rows)} records")
        all_rows.extend(rows)
        time.sleep(0.3)
        current += timedelta(days=1)

    print(f"\n[done] Total rows collected: {len(all_rows):,}")

    # ── Step 4: Build DataFrame
    df = build_dataframe(all_rows)

    if df.empty:
        print("No data — exiting.")
        return

    print("\n── Preview (5 rows) ───────────────────────────────")
    print(df.head(5).to_string())

    # ── Step 5: Zoho upload
    zoho_token = zoho_get_access_token()
    zoho_upload(df, zoho_token)

    print(f"\n🚀 Done. {len(df):,} rows pushed to Zoho Analytics view {ZOHO_VIEW_ID}")


if __name__ == "__main__":
    main()
