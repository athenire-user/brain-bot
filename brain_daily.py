"""
WorldQuant BRAIN daily bot (backup runner for GitHub Actions or any computer).

- Logs in with BRAIN_EMAIL / BRAIN_PASSWORD environment variables
  (or a local .brain_credentials file: email on line 1, password on line 2).
- Tops up today's simulations to TARGET_SIMS (default 250), counting what
  already ran today (e.g. from the Claude desktop run), so nothing doubles up.
- Submits the best passing alphas up to the daily quota (2/day until
  Nov 21 2026, then 1/day), minus anything already submitted today.
- Never submits an alpha that fails any BRAIN check.
- Writes results_<date>.csv.
"""
import csv, datetime as dt, itertools, os, random, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
import requests

API = "https://api.worldquantbrain.com"
HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_SIMS = int(os.environ.get("TARGET_SIMS", 250))
WORKERS = int(os.environ.get("WORKERS", 3))
NY = ZoneInfo("America/New_York")      # BRAIN's day boundary
IST = ZoneInfo("Asia/Kolkata")
TODAY_NY = dt.datetime.now(NY).date()
SPRINT_END = dt.date(2026, 11, 21)
QUOTA = 2 if dt.datetime.now(IST).date() <= SPRINT_END else 1

# Weekday (Mon=0) -> datasets, least crowded first
THEMES = {0: ["model16", "model51"], 1: ["news18", "socialmedia12"], 2: ["option9", "option8"],
          3: ["fundamental2", "model16"], 4: ["pv13", "news18"], 5: ["model51", "socialmedia8"],
          6: ["socialmedia8", "model51"]}
HELPERS = ["unsystematic_risk_last_90_days", "beta_last_90_days_spy", "snt_social_volume"]

TEMPLATES = [
    "rank(-ts_delta({f}, 5))", "rank(ts_delta({f}, 20))",
    "-ts_rank({f}, 20)", "ts_rank({f}, 60)",
    "group_rank(-{f}, subindustry)", "group_rank({f}, subindustry)",
    "rank(-ts_zscore({f}, 60))", "rank(ts_zscore({f}, 20))",
    "-rank(ts_mean({f}, 5) - ts_mean({f}, 60))",
    "group_rank(ts_backfill({f}, 20), industry)", "-group_rank(ts_backfill({f}, 20), industry)",
]
PAIR_TEMPLATES = [
    "rank(ts_zscore({f}, 20)) * rank({h})", "-rank(ts_zscore({f}, 20)) * rank({h})",
    "group_rank(ts_rank({f}, 60) - ts_rank({h}, 60), subindustry)",
]
# Ideas that have actually passed or come close on this account (fundamental/analyst value & quality)
VALUE_IDEAS = [
    "group_rank(ts_rank(operating_income/assets, 252), subindustry)", "rank(ts_rank(cashflow_op/assets, 252))",
    "group_rank(ts_rank(income/equity, 252), subindustry)", "group_rank(cashflow_op/enterprise_value, subindustry)",
    "group_rank(ts_rank(ebitda/enterprise_value, 252), subindustry)", "group_rank(-ts_rank(liabilities/assets, 252), subindustry)",
    "group_rank(ts_rank((cashflow_op - capex)/assets, 252), industry)", "group_rank(ts_rank(sales/assets, 252), subindustry)",
    "rank(ts_rank(operating_income/assets, 252)) * rank(-unsystematic_risk_last_90_days)",
    "group_rank(ts_rank(est_eps/close, 252), subindustry)", "group_rank(ts_delta(est_eps, 60)/close, subindustry)",
]
SETTINGS = [(4, "SUBINDUSTRY"), (10, "INDUSTRY"), (0, "SUBINDUSTRY")]
lock = threading.Lock()


def log(*a):
    with lock:
        print(*a, flush=True)


def wait(s, url):
    """Poll a BRAIN progress URL until Retry-After disappears."""
    for _ in range(600):
        r = s.get(url)
        ra = float(r.headers.get("Retry-After", 0) or 0)
        if ra == 0:
            return r
        time.sleep(max(2, ra))
    return r


def login():
    email, pw = os.environ.get("BRAIN_EMAIL"), os.environ.get("BRAIN_PASSWORD")
    if not (email and pw):
        with open(os.path.join(HERE, ".brain_credentials")) as fh:
            email, pw = [l.strip() for l in fh.read().splitlines()[:2]]
    s = requests.Session()
    r = s.post(API + "/authentication", auth=(email, pw))
    if r.status_code == 401 and "persona" in r.headers.get("WWW-Authenticate", "").lower():
        log("BRAIN wants biometric verification for this login. Open this link on your phone, "
            "complete it, then re-run the workflow:")
        log(API + r.headers.get("Location", ""))
        sys.exit(2)
    if r.status_code not in (200, 201):
        log("Login failed:", r.status_code, r.text[:300])
        sys.exit(1)
    return s


def sims_today(s):
    try:
        d = s.get(API + "/users/self/activities/simulations").json()
        for date, val in d.get("records", {}).get("records", []):
            if date == str(TODAY_NY):
                return int(val)
    except Exception:
        pass
    return 0


def submitted_today(s):
    d = s.get(API + "/users/self/alphas", params={"status": "ACTIVE", "order": "-dateSubmitted", "limit": 30}).json()
    n = 0
    for a in d.get("results", []):
        ds = a.get("dateSubmitted")
        if ds and dt.datetime.fromisoformat(ds).astimezone(NY).date() == TODAY_NY:
            n += 1
    return n


def fields_for(s, dataset):
    out, off = [], 0
    while True:
        r = s.get(API + "/data-fields", params={"instrumentType": "EQUITY", "region": "USA", "delay": 1,
                  "universe": "TOP3000", "dataset.id": dataset, "limit": 50, "offset": off}).json()
        res = r.get("results", [])
        out += [x["id"] for x in res if x.get("type") == "MATRIX"]
        off += 50
        if off >= r.get("count", 0) or not res:
            return out


def simulate(s, job):
    expr, decay, neut = job
    body = {"type": "REGULAR", "regular": expr, "settings": {
        "instrumentType": "EQUITY", "region": "USA", "universe": "TOP3000", "delay": 1,
        "decay": decay, "neutralization": neut, "truncation": 0.08, "pasteurization": "ON",
        "unitHandling": "VERIFY", "nanHandling": "ON", "language": "FASTEXPR", "visualization": False}}
    for _ in range(40):
        r = s.post(API + "/simulations", json=body)
        if r.status_code == 201:
            break
        time.sleep(15)
    else:
        return None
    p = wait(s, r.headers["Location"]).json()
    aid = p.get("alpha")
    if not aid:
        return {"expr": expr, "decay": decay, "neut": neut, "error": p.get("message", p.get("status", "failed"))}
    a = s.get(f"{API}/alphas/{aid}").json().get("is", {})
    fails = [c["name"] for c in a.get("checks", []) if c["result"] == "FAIL"]
    row = {"id": aid, "expr": expr, "decay": decay, "neut": neut, "sharpe": a.get("sharpe"),
           "fitness": a.get("fitness"), "turnover": a.get("turnover"), "fails": "|".join(fails)}
    log(("PASS " if not fails else "fail ") + f"{row['sharpe']}\t{row['fitness']}\t{expr}")
    return row


def self_corr_ok(s, aid):
    r = wait(s, f"{API}/alphas/{aid}/check").json()
    checks = r.get("is", {}).get("checks", [])
    bad = [c["name"] for c in checks if c.get("result") in ("FAIL", "ERROR")]
    return not bad, bad


def submit(s, aid):
    r = s.post(f"{API}/alphas/{aid}/submit")
    if r.status_code not in (200, 201):
        return False, f"{r.status_code} {r.text[:200]}"
    if r.headers.get("Location") or r.headers.get("Retry-After"):
        wait(s, r.headers.get("Location") or f"{API}/alphas/{aid}/submit")
    st = s.get(f"{API}/alphas/{aid}").json().get("status")
    return st == "ACTIVE", st


def main():
    s = login()
    done = sims_today(s)
    already = submitted_today(s)
    todo = max(0, TARGET_SIMS - done)
    want = max(0, QUOTA - already)
    log(f"BRAIN day {TODAY_NY}: {done} sims already, {already} submitted already. "
        f"Running {todo} sims, will submit up to {want}.")

    rows = []
    if todo:
        dsets = THEMES[dt.datetime.now(IST).weekday()]
        fields = []
        for d in dsets:
            f = fields_for(s, d)
            log(f"{d}: {len(f)} fields")
            fields += f
        random.shuffle(fields)
        fields = fields[:40]
        jobs = [(t.format(f=f), d, n) for (d, n), f, t in itertools.product(SETTINGS, fields, TEMPLATES)]
        jobs += [(t.format(f=f, h=h), 4, "SUBINDUSTRY") for f, h, t in itertools.product(fields[:10], HELPERS, PAIR_TEMPLATES) if f != h]
        value = [(e, 0, n) for e in VALUE_IDEAS for n in ("INDUSTRY", "SUBINDUSTRY")]
        random.shuffle(jobs)
        jobs = value + jobs          # run the proven-style ideas first
        with ThreadPoolExecutor(WORKERS) as ex:
            rows = [r for r in ex.map(lambda j: simulate(s, j), jobs[:todo]) if r]

    # Candidates: today's passing results + any older unsubmitted passing alphas
    old = s.get(API + "/users/self/alphas", params={"status": "UNSUBMITTED", "order": "-is.fitness", "limit": 50}).json()
    for a in old.get("results", []):
        fails = [c["name"] for c in a.get("is", {}).get("checks", []) if c["result"] == "FAIL"]
        if not fails and all(r.get("id") != a["id"] for r in rows):
            rows.append({"id": a["id"], "expr": a["regular"]["code"], "sharpe": a["is"].get("sharpe"),
                         "fitness": a["is"].get("fitness"), "turnover": a["is"].get("turnover"), "fails": ""})

    passing = sorted([r for r in rows if r.get("id") and not r.get("fails") and "error" not in r],
                     key=lambda r: -(r.get("fitness") or 0))
    submitted, used_exprs = [], set()
    for r in passing:
        if len(submitted) >= want:
            break
        core = r["expr"].replace("-", "")[:25]          # skip near-duplicates of the same idea
        if core in used_exprs:
            continue
        ok, bad = self_corr_ok(s, r["id"])
        if not ok:
            log("skip (checks):", bad, r["expr"])
            continue
        ok, st = submit(s, r["id"])
        log(("SUBMITTED " if ok else "submit failed ") + f"{st}: {r['expr']}")
        if ok:
            submitted.append(r)
            used_exprs.add(core)

    out = os.path.join(HERE, f"results_{TODAY_NY}.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "expr", "decay", "neut", "sharpe", "fitness", "turnover", "fails", "error"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log(f"\nSummary: ran {len([r for r in rows if 'decay' in r])} sims, {len(passing)} passing, "
        f"submitted {len(submitted)} (target {want}).")
    for r in submitted:
        log(f"  {r['expr']}  sharpe={r['sharpe']} fitness={r['fitness']} turnover={r['turnover']}")


if __name__ == "__main__":
    main()
