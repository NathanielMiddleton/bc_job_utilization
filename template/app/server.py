"""Job Utilization — a dependency-free WSGI app (Python 3.8+ standard library only).

It runs two ways with the same code:
  * standalone, as a Batch Connect job on a compute node:
        python3 server.py --host 0.0.0.0 --port 8080
  * under Phusion Passenger as an OnDemand system app (passenger_wsgi.py
    imports `application`).

Routes
  GET  /                 the single-page UI
  GET  /api/jobs         per-job records + statistics for a window and scope
  GET  /api/cluster      sreport cluster-wide allocated/idle/down totals
  GET  /api/partitions   partition names for the scope selector
  POST /login            token login (only when JOBUTIL_TOKEN is set)
  GET  /static/<file>

Environment
  JOBUTIL_TOKEN       when set, every request needs the session cookie that
                      POST /login issues for the right token (Batch Connect).
  JOBUTIL_URL_PREFIX  path prefix the reverse proxy keeps on requests, e.g.
                      /node/<host>/<port>; stripped into SCRIPT_NAME so links
                      and static files resolve under the proxy.
"""
import argparse
import getpass
import hashlib
import hmac
import json
import mimetypes
import os
import re
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from string import Template
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIServer, make_server
from socketserver import ThreadingMixIn

import threading
import time

from jobutil import advice, capacity, config, metrics, slurm

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_ROOT, "static")
TEMPLATE_PATH = os.path.join(APP_ROOT, "templates", "index.html")
CFG = config.load()

TOKEN = os.environ.get("JOBUTIL_TOKEN") or None
URL_PREFIX = os.environ.get("JOBUTIL_URL_PREFIX", "").rstrip("/")
_SECRET = hashlib.sha256(("jobutil:" + (TOKEN or "")).encode()).digest()
_COOKIE = "jobutil_session"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]{1,64}$")


# ---------------------------------------------------------------------------
# Tiny request/response plumbing
# ---------------------------------------------------------------------------

class BadRequest(Exception):
    pass


class Request:
    def __init__(self, environ):
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET")
        self.path = environ.get("PATH_INFO", "") or "/"
        self.script_name = environ.get("SCRIPT_NAME", "")
        self.args = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        self.cookies = SimpleCookie(environ.get("HTTP_COOKIE", ""))
        self._form = None

    @property
    def base(self):
        return self.script_name + "/"

    @property
    def form(self):
        if self._form is None:
            try:
                n = int(self.environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                n = 0
            raw = self.environ["wsgi.input"].read(n).decode("utf-8", "replace") if n else ""
            self._form = {k: v[0] for k, v in parse_qs(raw).items()}
        return self._form


class Response:
    def __init__(self, body=b"", status=HTTPStatus.OK, content_type="text/html; charset=utf-8", headers=None):
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status
        self.headers = [("Content-Type", content_type), ("Content-Length", str(len(self.body))),
                        ("X-Content-Type-Options", "nosniff")] + (headers or [])


def json_response(obj, status=HTTPStatus.OK):
    return Response(json.dumps(obj), status, "application/json")


def error(message, status):
    return json_response({"error": str(message)}, status)


def redirect(location):
    return Response(b"", HTTPStatus.SEE_OTHER, headers=[("Location", location)])


# ---------------------------------------------------------------------------
# Auth (Batch Connect mode only)
# ---------------------------------------------------------------------------

def _session_value():
    return hmac.new(_SECRET, b"authenticated", hashlib.sha256).hexdigest()


def is_authenticated(req):
    if not TOKEN:
        return True                      # Passenger mode: the PUN already authenticated the user
    morsel = req.cookies.get(_COOKIE)
    return bool(morsel) and hmac.compare_digest(morsel.value, _session_value())


def login(req):
    if not TOKEN:
        return redirect(req.base)
    token = req.form.get("token") or req.args.get("token") or ""
    if not hmac.compare_digest(token, TOKEN):
        return Response("<h1>Not authorized</h1><p>This dashboard belongs to another session. "
                        "Launch your own from the OnDemand interactive apps page.</p>", HTTPStatus.FORBIDDEN)
    cookie = f"{_COOKIE}={_session_value()}; Path={req.script_name or '/'}; HttpOnly; SameSite=Lax"
    r = redirect(req.base)
    r.headers.append(("Set-Cookie", cookie))
    return r


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def index(req):
    with open(TEMPLATE_PATH, encoding="utf-8") as fh:
        tpl = Template(fh.read())
    user = getpass.getuser()
    name = CFG["cluster_name"]
    scope_options = '<option value="mine">me</option>'
    if CFG["allow_all_users"]:
        scope_options += '<option value="all">everyone</option><option value="partition">partition</option>'
    html = tpl.safe_substitute(
        base=req.base, user=_esc(user),
        title_suffix=(" · " + _esc(name)) if name else "",
        cluster_badge=f' <span class="cluster">{_esc(name)}</span>' if name else "",
        scope_options=scope_options,
        dashboard_url=_esc(CFG["dashboard_url"]),
        default_hours=CFG["default_hours"], max_days=CFG["max_days"],
        allow_all="true" if CFG["allow_all_users"] else "false",
    )
    return Response(html)


def static_file(req, rel):
    path = os.path.normpath(os.path.join(STATIC_DIR, rel))
    if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
        return Response("Not found", HTTPStatus.NOT_FOUND, "text/plain")
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        return Response(fh.read(), content_type=ctype, headers=[("Cache-Control", "public, max-age=3600")])


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _parse_window(args):
    """Resolve start/end from the query string; defaults to the last N hours."""
    now = datetime.now().replace(microsecond=0)
    end_s, start_s = args.get("end"), args.get("start")
    try:
        end = datetime.fromisoformat(end_s) if end_s else now
        start = datetime.fromisoformat(start_s) if start_s else end - timedelta(hours=CFG["default_hours"])
    except ValueError:
        raise BadRequest("start and end must be ISO 8601 timestamps (YYYY-MM-DDTHH:MM).")
    if start >= end:
        raise BadRequest("The start of the range must be before its end.")
    if end - start > timedelta(days=CFG["max_days"]):
        raise BadRequest(f"Ranges longer than {CFG['max_days']} days are not allowed on this site.")
    return start, end, now


def _parse_scope(args):
    scope = args.get("scope", "mine")
    partition = args.get("partition") or None
    user = args.get("user") or None
    if scope not in ("mine", "all", "partition"):
        raise BadRequest("scope must be mine, all or partition.")
    if scope != "mine" and not CFG["allow_all_users"]:
        raise BadRequest("Viewing other users' jobs is disabled on this site.")
    if scope == "partition" and not partition:
        raise BadRequest("Choose a partition.")
    for v in (partition, user):
        if v and not _NAME_RE.match(v):
            raise BadRequest("Invalid partition or user name.")
    if scope == "mine":
        user = getpass.getuser()
    return scope, partition, user


def api_partitions(req):
    return json_response({"partitions": slurm.partitions(CFG, CFG["cluster"])})


def api_jobs(req):
    try:
        start, end, now = _parse_window(req.args)
        scope, partition, user = _parse_scope(req.args)
        mode = req.args.get("mode", "ended")
        if mode not in ("ended", "prorate"):
            raise BadRequest("mode must be ended or prorate.")
        preset = req.args.get("states", "finished")
        if preset not in metrics.STATE_PRESETS:
            raise BadRequest("Unknown state preset.")
        include_running = req.args.get("running") == "1" and mode == "prorate"
    except BadRequest as e:
        return error(e, HTTPStatus.BAD_REQUEST)

    try:
        rows = slurm.sacct(
            CFG, start, end,
            user=None if scope in ("all", "partition") else user,
            all_users=(scope in ("all", "partition")),
            partitions=[partition] if partition else None,
            cluster=CFG["cluster"],
        )
    except slurm.SlurmError as e:
        return error(e, HTTPStatus.BAD_GATEWAY)

    all_jobs = metrics.build_jobs(rows)
    if user and scope != "mine":
        all_jobs = [j for j in all_jobs if j.user == user]
    jobs, info = metrics.select_jobs(all_jobs, start, end, mode,
                                     metrics.STATE_PRESETS[preset], include_running, now)
    warnings = _build_warnings(scope, mode, jobs, info, all_jobs, start, end, now)
    gpu_capacity = _gpu_capacity(scope, partition, all_jobs, start, end, now, warnings)
    summary = metrics.summarize(jobs)
    jobs_sorted = sorted(jobs, key=lambda j: j.end or now, reverse=True)
    cap = CFG["max_jobs_in_table"]
    by_user = metrics.group_summaries(jobs, "user") if scope != "mine" else []
    records = []
    for j in jobs_sorted[:cap]:
        rec = j.to_record()
        rec["hints"] = advice.job_hints(j) if CFG["hints"] else []
        records.append(rec)
    return json_response({
        "window": {"start": start.isoformat(), "end": end.isoformat(), "generated": now.isoformat(),
                   "mode": mode, "scope": scope, "partition": partition,
                   "user": user if scope == "mine" else None, "states": preset,
                   "include_running": include_running},
        "summary": summary,
        "info": info,
        "by_partition": metrics.group_summaries(jobs, "partition"),
        "by_user": by_user,
        "by_account": metrics.group_summaries(jobs, "account"),
        "by_gpu_type": metrics.group_summaries(jobs, "gpu_type"),
        "jobs": records,
        "advice": advice.advise(jobs, scope, by_user) if CFG["hints"] else [],
        "gpu_util_diagnosis": metrics.gpu_util_diagnosis(jobs),
        "gpu_capacity": gpu_capacity,
        "table_truncated": len(jobs) > cap,
        "warnings": warnings,
    })


def _gpu_capacity(scope, partition, all_jobs, start, end, now, warnings):
    """GPU-hours held by jobs in the window against the GPU-hours that existed.

    Capacity = GPUs configured (sinfo) x hours in the window, for the whole
    cluster or for the chosen partition. The numerator comes from every job
    that held GPUs during the window, not just the ones selected above, so
    running jobs count for the hours they have used so far.
    """
    occ = metrics.gpu_occupancy(all_jobs, start, end, now)
    nodes = _cached(("nodes", CFG["cluster"]), 120, lambda: slurm.node_inventory(CFG, CFG["cluster"]))
    inv = slurm.gpu_inventory(CFG, CFG["cluster"], nodes=nodes)
    hours = (min(end, now) - start).total_seconds() / 3600
    if hours <= 0 or not occ["jobs"] and not (inv and inv["total"]):
        return None
    if inv is None:
        if CFG.get("gpu_total"):
            inv = {"total": {"": int(CFG["gpu_total"])}, "partitions": {}, "nodes": None}
            source = "config"
        else:
            warnings.append("GPU occupancy cannot be computed because sinfo did not answer and no gpu_total is "
                            "set in config.yml; GPU-hours held by jobs are still shown.")
            return {"occupied_gpu_hours": occ["gpu_hours"], "occupied_by_type": occ["by_type"],
                    "jobs": occ["jobs"], "available_gpu_hours": None, "ratio": None, "by_type": {}, "scope": None}
    else:
        source = "sinfo"
    if scope == "partition" and partition:
        gpus = inv["partitions"].get(partition, {})
        label = f"partition {partition}"
        if not gpus:
            warnings.append(f"sinfo lists no GPUs in partition {partition}, so GPU occupancy is measured against the "
                            f"whole cluster's GPUs instead.")
            gpus, label = inv["total"], "the cluster"
    else:
        gpus, label = inv["total"], "the cluster"
    total_gpus = sum(gpus.values())
    available = total_gpus * hours
    by_type = {}
    for t in sorted(set(gpus) | set(occ["by_type"])):
        cap = gpus.get(t, 0) * hours
        held = occ["by_type"].get(t, 0.0)
        by_type[t or "untyped"] = {"gpus": gpus.get(t, 0), "available": cap, "occupied": held,
                                  "ratio": (held / cap) if cap else None}
    ratio = (occ["gpu_hours"] / available) if available else None
    if ratio is not None and ratio > 1.02:
        warnings.append(f"Jobs held {occ['gpu_hours']:,.0f} GPU-hours but sinfo only accounts for "
                        f"{available:,.0f} in this window ({total_gpus} GPUs). Either GPUs were removed from the "
                        f"cluster since those jobs ran, or jobs were oversubscribed; the ratio is shown as reported.")
    if source == "sinfo":
        warnings.append("GPU occupancy is measured against the GPUs sinfo lists right now times the hours in the "
                        "window. It does not subtract GPUs that were down or drained during the window; the "
                        "cluster-wide panel from sreport does, where gres/gpu is tracked.")
    return {"occupied_gpu_hours": occ["gpu_hours"], "occupied_by_type": occ["by_type"], "jobs": occ["jobs"],
            "available_gpu_hours": available, "gpus": total_gpus, "hours": hours, "ratio": ratio,
            "by_type": by_type, "scope": label, "source": source}


def _build_warnings(scope, mode, jobs, info, all_jobs, start, end, now):
    """Plain-language notes about what the numbers do and do not include."""
    w = []
    hours = (end - start).total_seconds() / 3600

    if scope != "mine":
        users = {j.user for j in all_jobs}
        if users and users <= {getpass.getuser()}:
            w.append("Only your own jobs came back from Slurm. On this cluster slurmdbd's "
                     "PrivateData setting probably hides other users' jobs unless you are an "
                     "operator or administrator, so the \"everyone\" and partition views may be incomplete.")

    if mode == "ended":
        if info["span_start"]:
            w.append(f"{info['span_start']} job(s) started before this window and finished inside it. "
                     f"Their full consumption is counted here, including about "
                     f"{info['cpu_hours_before_window']:,.0f} CPU-hours spent before the window opened. "
                     f"Switch the query to jobs that \"ran during\" the window to count only the in-window share.")
        if info["excluded_unfinished"]:
            w.append(f"{info['excluded_unfinished']} job(s) ran during this window but are not counted "
                     f"because they finished later or are still running. They used roughly "
                     f"{info['excluded_unfinished_cpu_hours']:,.0f} CPU-hours inside the window.")
    else:
        if info["prorated"]:
            w.append(f"{info['prorated']} job(s) crossed a window boundary; their CPU-, node-, GPU- and "
                     f"memory-hours are scaled to the share of their run time inside the window. "
                     f"Efficiency percentages are always for the whole job.")
        if info["excluded_running"]:
            w.append(f"{info['excluded_running']} job(s) are still running and were left out. "
                     f"Tick \"include running jobs\" to count their in-window usage (their "
                     f"efficiency reflects usage so far, not the final result).")
    if info["multi_day"] and hours <= 24:
        w.append(f"{info['multi_day']} job(s) ran for more than a day, which is longer than this window. "
                 f"Day-by-day comparisons are only meaningful in \"ran during\" mode for such jobs.")

    s = metrics.summarize(jobs) if jobs else None
    if s and s["mem_jobs_maxrss_fallback"]:
        w.append(f"Memory usage for {s['mem_jobs_maxrss_fallback']} job(s) comes from MaxRSS (largest "
                 f"single task) because TRESUsageInTot was not recorded; multi-task steps are "
                 f"under-reported for these jobs.")
    if s and s["mem_jobs_over"]:
        w.append(f"{s['mem_jobs_over']} job(s) report a peak memory above what they were allocated (up to "
                 f"{s['mem_over_max'] * 100:.0f}%). Slurm sums the RSS of every process in a step, so forked "
                 f"workers each re-count the pages they share with their parent; or memory is not enforced "
                 f"with cgroups and the job really exceeded its request. Aggregate memory efficiency caps each "
                 f"job at 100%; the job table shows the raw value. If memory is enforced on this cluster, "
                 f"JobAcctGatherParams=UsePss or jobacct_gather/cgroup gives accurate peaks.")
    if s and s["mem_jobs_reqmem_fallback"]:
        w.append(f"{s['mem_jobs_reqmem_fallback']} job(s) have no mem= in AllocTRES, so their allocation comes "
                 f"from ReqMem, which some Slurm versions report per CPU for --mem-per-cpu jobs.")
    if s and s["jobs"] and s["mem_jobs_measured"] < s["jobs"]:
        w.append(f"{s['jobs'] - s['mem_jobs_measured']} job(s) have no memory measurement at all "
                 f"(typically jobs with no steps, or job accounting gather disabled).")
    if s and s["gpu_jobs"] and s["gpu_util_jobs"] == 0:
        diag = metrics.gpu_util_diagnosis(jobs)
        if diag:
            w.append(f"GPU utilization is missing for all {s['gpu_jobs']} GPU job(s). {diag['verdict']} "
                     f"Fix: {diag['fix']} GPU-hours are unaffected.")
    elif s and s["gpu_jobs"] and s["gpu_util_jobs"] < s["gpu_jobs"]:
        w.append(f"GPU utilization is recorded for {s['gpu_util_jobs']} of {s['gpu_jobs']} GPU job(s); the rest "
                 f"have no gres/gpuutil in their step records (typically jobs that finished before GPU "
                 f"accounting was enabled, or whose steps were not sampled). GPU-hours are always reported.")
    if s and s["gpu_jobs_inconsistent_type"]:
        w.append(f"{s['gpu_jobs_inconsistent_type']} GPU job(s) have typed GPU entries (gres/gpu:<type>) "
                 f"that do not add up to gres/gpu; the total is used for GPU-hours and the types are "
                 f"shown as recorded.")
    if info["never_started"]:
        w.append(f"{info['never_started']} job(s) in this range never started (pending or cancelled "
                 f"before launch) and are excluded.")
    if TOKEN:
        w.append("This dashboard is itself running as a Slurm job, so it will appear in your own "
                 "job list once it finishes.")
    if end > now:
        w.append("The window ends in the future; results will change as jobs finish.")
    return w


def api_cluster(req):
    try:
        start, end, now = _parse_window(req.args)
    except BadRequest as e:
        return error(e, HTTPStatus.BAD_REQUEST)
    out = {"window": {"start": start.isoformat(), "end": end.isoformat()}, "tres": [], "warnings": []}
    tres_list = CFG["cluster_tres"] or ["cpu"]
    for i, tres in enumerate(tres_list):
        try:
            out["tres"] += slurm.sreport_utilization(CFG, start, end, tres, CFG["cluster"])
        except slurm.SlurmError as e:
            if i == 0:
                return error(e, HTTPStatus.BAD_GATEWAY)
            out["warnings"].append(f"sreport has no data for {tres}; it is probably not listed in "
                                   f"AccountingStorageTRES, so it is left out of the capacity panel.")
    seen, unique = set(), []
    for t in out["tres"]:
        if (t["cluster"], t["tres"]) not in seen:
            seen.add((t["cluster"], t["tres"]))
            unique.append(t)
    out["tres"] = unique
    if not out["tres"]:
        out["warnings"].append("sreport returned no rows for this range. It works from hourly "
                               "roll-ups, so windows shorter than an hour, or the most recent "
                               "hour, usually have no data yet.")
    else:
        out["warnings"].append("Cluster capacity figures come from sreport, which rounds the "
                               "window to whole hours and lags behind by up to an hour.")
    return json_response(out)


_cache = {}
_cache_lock = threading.Lock()


def _cached(key, ttl, fn):
    """Memoise expensive Slurm queries briefly so clicking around the capacity panel is cheap."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (now + ttl, value)
        if len(_cache) > 64:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:32]:
                _cache.pop(k, None)
    return value


def api_capacity_breakdown(req):
    """Behind a segment of the cluster-wide bars: who/what the hours belong to."""
    try:
        start, end, now = _parse_window(req.args)
    except BadRequest as e:
        return error(e, HTTPStatus.BAD_REQUEST)
    tres = req.args.get("tres", "cpu")
    if tres != "cpu" and not tres.startswith("gres/gpu"):
        return error("tres must be cpu or gres/gpu[:type].", HTTPStatus.BAD_REQUEST)
    gpu_type = tres.split(":", 1)[1].lower() if ":" in tres else None

    key = ("sacct_all", start.isoformat(), end.isoformat(), CFG["cluster"])
    try:
        rows = _cached(key, 120, lambda: slurm.sacct(CFG, start, end, all_users=True, cluster=CFG["cluster"]))
    except slurm.SlurmError as e:
        return error(e, HTTPStatus.BAD_GATEWAY)
    jobs = metrics.build_jobs(rows)
    nodes = _cached(("nodes", CFG["cluster"]), 120, lambda: slurm.node_inventory(CFG, CFG["cluster"]))
    if gpu_type:
        # A typed TRES bar: restrict to jobs and nodes of that GPU type.
        nodes = [n for n in (nodes or []) if gpu_type in n["gpus"]]
        jobs = [j for j in jobs if gpu_type in j.gpu_types]
    bd = capacity.breakdown(nodes, jobs, start, end, "gres/gpu" if gpu_type else tres, now)
    res = _cached(("reservations", CFG["cluster"]), 120, lambda: slurm.reservations(CFG, CFG["cluster"]))

    warnings = []
    me = getpass.getuser()
    users = {j.user for j in jobs}
    if users and users <= {me}:
        warnings.append("Only your own jobs came back from sacct, so the allocated hours here cover just your jobs "
                        "while the capacity covers the whole cluster (slurmdbd PrivateData).")
    if nodes is None:
        warnings.append("sinfo did not answer, so capacity, idle and down figures are unavailable; allocated hours "
                        "by partition, user and account are still shown.")
    warnings.append("These breakdowns join job records to today's node inventory, so they differ slightly from the "
                    "sreport totals above: sreport uses hourly roll-ups and subtracts historical downtime, this uses "
                    "exact job times and the nodes as they are configured now. Down nodes and reservations are "
                    "current, not historical.")
    return json_response({
        "window": {"start": start.isoformat(), "end": end.isoformat()}, "tres": tres,
        "hours": bd["hours"], "total_capacity": bd["total_capacity"], "total_allocated": bd["total_allocated"],
        "groups": bd["groups"], "down_nodes": capacity.down_nodes(nodes, "gres/gpu" if gpu_type else tres),
        "reservations": res, "warnings": warnings,
    })


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def handle(req):
    path = req.path
    if req.method == "POST" and path == "/login":
        return login(req)
    if path.startswith("/static/"):
        return static_file(req, path[len("/static/"):])
    if not is_authenticated(req):
        if path.startswith("/api/"):
            return error("Not authorized. Reopen the dashboard from the OnDemand session card.", HTTPStatus.FORBIDDEN)
        return Response("<h1>Not authorized</h1><p>Open this dashboard from its session card on the "
                        "OnDemand interactive apps page.</p>", HTTPStatus.FORBIDDEN)
    if req.method != "GET":
        return Response("Method not allowed", HTTPStatus.METHOD_NOT_ALLOWED, "text/plain")
    if path in ("", "/"):
        return index(req)
    if path == "/api/jobs":
        return api_jobs(req)
    if path == "/api/cluster":
        return api_cluster(req)
    if path == "/api/partitions":
        return api_partitions(req)
    if path == "/api/capacity_breakdown":
        return api_capacity_breakdown(req)
    return Response("Not found", HTTPStatus.NOT_FOUND, "text/plain")


def application(environ, start_response):
    # Behind OnDemand's /node/<host>/<port> proxy the prefix stays on the path;
    # move it into SCRIPT_NAME so the app behaves as if mounted there.
    if URL_PREFIX:
        p = environ.get("PATH_INFO", "")
        if p == URL_PREFIX or p.startswith(URL_PREFIX + "/"):
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + URL_PREFIX
            environ["PATH_INFO"] = p[len(URL_PREFIX):] or "/"
    req = Request(environ)
    try:
        resp = handle(req)
    except Exception as e:  # keep the server alive; report the failure
        resp = error(f"Internal error: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)
    start_response(f"{resp.status.value} {resp.status.phrase}", resp.headers)
    return [resp.body]


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="Serve Job Utilization standalone.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    httpd = make_server(a.host, a.port, application, server_class=ThreadingWSGIServer)
    print(f"Job Utilization listening on http://{a.host}:{a.port}{URL_PREFIX}/"
          + (" (token auth on)" if TOKEN else ""), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
