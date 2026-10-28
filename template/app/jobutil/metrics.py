"""Turn sacct rows into per-job records and aggregate them into statistics."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from . import parse

# States that mean a job is finished and will not consume anything more.
TERMINAL_STATES = [
    "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY",
    "NODE_FAIL", "PREEMPTED", "DEADLINE", "BOOT_FAIL", "REVOKED",
]
STATE_PRESETS = {
    "finished": TERMINAL_STATES,
    "completed": ["COMPLETED"],
    "unsuccessful": [s for s in TERMINAL_STATES if s != "COMPLETED"],
}

GIB = 1024 ** 3
HIST_BINS = 10  # 0-10%, 10-20%, ... 90-100%


@dataclass
class Job:
    id: str
    raw_id: str
    name: str
    user: str
    account: str
    partition: str
    state: str
    start: Optional[datetime]
    end: Optional[datetime]
    elapsed_s: float
    alloc_cpus: int
    alloc_nodes: int
    cpu_used_s: float
    mem_req_b: Optional[float]     # memory allocated to the job (AllocTRES mem, else ReqMem)
    mem_alloc_source: Optional[str]  # 'alloctres' or 'reqmem'
    mem_used_b: Optional[float]
    mem_source: Optional[str]      # 'tres' (TRESUsageInTot), 'maxrss', or None
    gpus: int
    gpu_types: Dict[str, int]      # {'h200': 1}; '' is the untyped remainder
    gpu_util: Optional[float]      # percent, from gres/gpuutil when accounting is on
    timelimit_s: Optional[float]
    exit_code: str
    nodelist: str = ""
    # Window-dependent fields filled in by select_jobs()
    fraction: float = 1.0          # share of the job's wall time that falls inside the window
    prorated: bool = False
    multi_day: bool = False
    spans_start: bool = False      # started before the window opened
    spans_end: bool = False        # finished after the window closed (or still running)
    gpu_raw: list = field(default_factory=list)   # raw TRESUsageIn* per step, GPU jobs only (diagnostics)

    # ----- efficiency ratios (window-independent) -----
    @property
    def cpu_eff(self):
        denom = self.elapsed_s * self.alloc_cpus
        return self.cpu_used_s / denom if denom > 0 else None

    @property
    def mem_eff(self):
        if self.mem_req_b and self.mem_used_b is not None:
            return self.mem_used_b / self.mem_req_b
        return None

    @property
    def mem_over(self):
        """Measured peak exceeded the allocation: RSS double-counting or no enforcement."""
        return bool(self.mem_req_b and self.mem_used_b is not None and self.mem_used_b > self.mem_req_b)

    @property
    def mem_eff_capped(self):
        e = self.mem_eff
        return None if e is None else min(e, 1.0)

    @property
    def wall_eff(self):
        if self.timelimit_s:
            return self.elapsed_s / self.timelimit_s
        return None

    # ----- consumption (scaled by fraction when prorating) -----
    @property
    def cpu_hours_alloc(self):
        return self.alloc_cpus * self.elapsed_s * self.fraction / 3600

    @property
    def cpu_hours_used(self):
        return self.cpu_used_s * self.fraction / 3600

    @property
    def node_hours(self):
        return self.alloc_nodes * self.elapsed_s * self.fraction / 3600

    @property
    def gpu_hours(self):
        return self.gpus * self.elapsed_s * self.fraction / 3600

    @property
    def gpu_type(self):
        return parse.gpu_type_label(self.gpu_types)

    @property
    def gpu_types_consistent(self):
        """False when typed entries claim more GPUs than gres/gpu says."""
        return sum(self.gpu_types.values()) == self.gpus

    @property
    def mem_gb_hours_req(self):
        return (self.mem_req_b or 0) / GIB * self.elapsed_s * self.fraction / 3600

    @property
    def mem_gb_hours_used(self):
        """Used memory-hours, capped at the allocation: memory a job used beyond
        what it was allocated was never reserved for it, so it cannot count as
        utilization of the allocation (and over-counts are usually artefacts)."""
        used = min(self.mem_used_b or 0, self.mem_req_b or float("inf"))
        return used / GIB * self.elapsed_s * self.fraction / 3600

    def to_record(self):
        return {
            "id": self.id, "name": self.name, "user": self.user, "account": self.account,
            "partition": self.partition, "state": self.state, "exit_code": self.exit_code,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "elapsed_s": self.elapsed_s, "alloc_cpus": self.alloc_cpus,
            "alloc_nodes": self.alloc_nodes, "gpus": self.gpus, "gpu_type": self.gpu_type,
            "cpu_eff": self.cpu_eff, "mem_eff": self.mem_eff, "wall_eff": self.wall_eff,
            "gpu_util": self.gpu_util, "mem_req_b": self.mem_req_b,
            "mem_used_b": self.mem_used_b, "mem_source": self.mem_source,
            "mem_alloc_source": self.mem_alloc_source, "mem_over": self.mem_over,
            "timelimit_s": self.timelimit_s,
            "cpu_hours_alloc": self.cpu_hours_alloc, "cpu_hours_used": self.cpu_hours_used,
            "node_hours": self.node_hours, "gpu_hours": self.gpu_hours,
            "mem_gb_hours_req": self.mem_gb_hours_req,
            "fraction": self.fraction, "prorated": self.prorated, "multi_day": self.multi_day,
            "spans_start": self.spans_start, "spans_end": self.spans_end,
        }


# ---------------------------------------------------------------------------
# Building jobs from sacct rows
# ---------------------------------------------------------------------------

def build_jobs(rows: List[dict]) -> List[Job]:
    """Group allocation + step rows (by JobIDRaw) into Job objects."""
    groups: Dict[str, dict] = {}
    order = []
    for row in rows:
        raw = row.get("JobIDRaw", "")
        base = raw.split(".", 1)[0]
        if base not in groups:
            groups[base] = {"job": None, "steps": []}
            order.append(base)
        if "." in raw:
            groups[base]["steps"].append(row)
        else:
            groups[base]["job"] = row

    jobs = []
    for base in order:
        g = groups[base]
        row = g["job"]
        if row is None:          # steps without an allocation row: ignore
            continue
        jobs.append(_make_job(row, g["steps"]))
    return jobs


def _gpu_util_from_step(step):
    """GPU utilization (percent) recorded on a step, or None.

    Slurm stores it as gres/gpuutil in the TRESUsageIn* fields of each step.
    The average across tasks is preferred, then the maximum; the total is a
    sum over tasks so it is only trusted for single-task steps. Typed keys
    (gres/gpuutil:h200) are accepted in case a site's TRES list uses them.
    """
    for fld in ("TRESUsageInAve", "TRESUsageInMax", "TRESUsageInTot"):
        tres = parse.parse_tres(step.get(fld))
        for k, v in tres.items():
            if k == "gres/gpuutil" or k.startswith("gres/gpuutil:"):
                try:
                    val = float(v)
                except ValueError:
                    continue
                if fld == "TRESUsageInTot" and parse._to_int(step.get("NTasks"), 1) > 1:
                    continue
                return val
    return None


def _make_job(row, steps) -> Job:
    alloc_cpus = parse._to_int(row.get("AllocCPUS"))
    alloc_nodes = parse._to_int(row.get("AllocNodes"))
    alloc_tres = parse.parse_tres(row.get("AllocTRES"))

    # Memory the job was allocated. AllocTRES mem= is the total actually
    # allocated and is unambiguous across Slurm versions, so it is preferred.
    # ReqMem is the fallback: it is per job on Slurm >= 21.08, older versions
    # append 'n' (per node) or 'c' (per CPU), and some 21.08-22.x releases
    # show the per-CPU value for --mem-per-cpu jobs with no suffix at all,
    # which makes efficiencies of several hundred percent.
    alloc_mem, _ = parse.parse_mem(alloc_tres.get("mem"), bare_unit="M")
    mem_req, per = parse.parse_mem(row.get("ReqMem"), bare_unit="M")
    if mem_req:
        if per == "n":
            mem_req *= max(alloc_nodes, 1)
        elif per == "c":
            mem_req *= max(alloc_cpus, 1)
    if alloc_mem:
        mem_alloc, mem_alloc_source = alloc_mem, "alloctres"
    elif mem_req:
        mem_alloc, mem_alloc_source = mem_req, "reqmem"
    else:
        mem_alloc, mem_alloc_source = None, None

    # Used memory: prefer TRESUsageInTot mem (summed over tasks of a step),
    # else MaxRSS (largest single task; underestimates multi-task steps).
    mem_used, mem_source = None, None
    maxrss = None
    gpu_utils = []
    gpu_raw = []
    for s in steps:
        tot = parse.parse_tres(s.get("TRESUsageInTot"))
        v, _ = parse.parse_mem(tot.get("mem"), bare_unit="K")
        if v is not None:
            mem_used = v if mem_used is None else max(mem_used, v)
        r, _ = parse.parse_mem(s.get("MaxRSS"), bare_unit="K")
        if r is not None:
            maxrss = r if maxrss is None else max(maxrss, r)
        if not s.get("JobIDRaw", "").endswith(".extern"):
            util = _gpu_util_from_step(s)
            if util is not None:
                gpu_utils.append(util)
            gpu_raw.append({"step": s.get("JobID", ""), "TRESUsageInAve": s.get("TRESUsageInAve", ""),
                            "TRESUsageInMax": s.get("TRESUsageInMax", ""), "TRESUsageInTot": s.get("TRESUsageInTot", "")})
    if mem_used is not None:
        mem_source = "tres"
    elif maxrss is not None:
        mem_used, mem_source = maxrss, "maxrss"

    tl_min = row.get("TimelimitRaw", "")
    timelimit_s = float(tl_min) * 60 if tl_min.replace(".", "", 1).isdigit() else None

    return Job(
        id=row.get("JobID", ""), raw_id=row.get("JobIDRaw", ""),
        name=row.get("JobName", ""), user=row.get("User", ""),
        account=row.get("Account", ""), partition=row.get("Partition", ""),
        state=parse.parse_state(row.get("State")),
        start=parse.parse_time(row.get("Start")), end=parse.parse_time(row.get("End")),
        elapsed_s=float(parse._to_int(row.get("ElapsedRaw"))),
        alloc_cpus=alloc_cpus, alloc_nodes=alloc_nodes,
        cpu_used_s=parse.parse_duration(row.get("TotalCPU")) or 0.0,
        mem_req_b=mem_alloc, mem_alloc_source=mem_alloc_source, mem_used_b=mem_used, mem_source=mem_source,
        gpus=parse.gpu_count(alloc_tres), gpu_types=parse.gpu_types(alloc_tres),
        gpu_util=(sum(gpu_utils) / len(gpu_utils)) if gpu_utils else None,
        timelimit_s=timelimit_s, exit_code=row.get("ExitCode", ""),
        gpu_raw=gpu_raw if parse.gpu_count(alloc_tres) else [],
        nodelist=row.get("NodeList", ""),
    )


# ---------------------------------------------------------------------------
# Window selection
# ---------------------------------------------------------------------------

def select_jobs(jobs, wstart, wend, mode, states, include_running, now=None):
    """Pick the jobs that belong to [wstart, wend] and annotate them.

    mode='ended'   : a job belongs to the window if it finished inside it, and
                     its whole consumption is attributed to the window.
    mode='prorate' : any job that overlapped the window is included, and its
                     consumption is scaled by the share of its wall time that
                     fell inside the window (efficiency ratios are unchanged).

    Returns (selected_jobs, info) where info carries the numbers needed for
    the multi-day / boundary disclaimers.
    """
    now = now or datetime.now()
    states = set(states)
    info = {
        "never_started": 0,
        "excluded_unfinished": 0,            # ran in window, finished later / still running
        "excluded_unfinished_cpu_hours": 0.0,  # ... and how much they used inside the window
        "excluded_running": 0,
        "cpu_hours_before_window": 0.0,      # ended mode: consumption attributed here but spent earlier
        "span_start": 0, "span_end": 0, "multi_day": 0, "prorated": 0,
    }
    selected = []
    for j in jobs:
        if j.start is None or j.elapsed_s <= 0:
            info["never_started"] += 1
            continue
        running = j.state == "RUNNING"
        jend = j.end if (j.end and not running) else now
        overlap = (min(jend, wend) - max(j.start, wstart)).total_seconds()
        if overlap <= 0:
            continue
        j.multi_day = j.elapsed_s > 86400
        j.spans_start = j.start < wstart
        j.spans_end = running or jend > wend

        if mode == "ended":
            if running or j.end is None or j.end > wend:
                info["excluded_unfinished"] += 1
                info["excluded_unfinished_cpu_hours"] += j.alloc_cpus * overlap / 3600
                continue
            if j.state not in states:
                continue
            j.fraction = 1.0
            if j.spans_start:
                info["cpu_hours_before_window"] += j.alloc_cpus * (wstart - j.start).total_seconds() / 3600
        else:
            if running:
                if not include_running:
                    info["excluded_running"] += 1
                    continue
            elif j.state not in states:
                continue
            total = (jend - j.start).total_seconds()
            j.fraction = min(1.0, overlap / total) if total > 0 else 0.0
            j.prorated = j.fraction < 0.9995
            if j.prorated:
                info["prorated"] += 1

        info["span_start"] += j.spans_start
        info["span_end"] += j.spans_end
        info["multi_day"] += j.multi_day
        selected.append(j)
    return selected, info


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _hist(values):
    bins = [0] * HIST_BINS
    for v in values:
        if v is None:
            continue
        idx = min(int(max(v, 0) * HIST_BINS), HIST_BINS - 1)
        bins[idx] += 1
    return bins


def _safe_div(a, b):
    return a / b if b else None


def summarize(jobs: List[Job]) -> dict:
    cpu_alloc = sum(j.cpu_hours_alloc for j in jobs)
    cpu_used = sum(j.cpu_hours_used for j in jobs)
    mem_jobs = [j for j in jobs if j.mem_eff is not None]
    mem_req = sum(j.mem_gb_hours_req for j in mem_jobs)
    mem_used = sum(j.mem_gb_hours_used for j in mem_jobs)
    gpu_jobs = [j for j in jobs if j.gpus > 0]
    gpu_util_jobs = [j for j in gpu_jobs if j.gpu_util is not None]
    gpu_util_hours = sum(j.gpu_hours for j in gpu_util_jobs)
    wall_jobs = [j for j in jobs if j.wall_eff is not None]
    states = defaultdict(int)
    for j in jobs:
        states[j.state] += 1
    gpu_hours_by_type = defaultdict(float)
    for j in gpu_jobs:
        share = j.elapsed_s * j.fraction / 3600
        for name, n in j.gpu_types.items():
            gpu_hours_by_type[name or "untyped"] += n * share
    cpu_effs = [j.cpu_eff for j in jobs if j.cpu_eff is not None]
    mem_effs = [j.mem_eff_capped for j in mem_jobs]
    mem_over = [j for j in mem_jobs if j.mem_over]
    return {
        "jobs": len(jobs),
        "cpu_hours_alloc": cpu_alloc,
        "cpu_hours_used": cpu_used,
        "cpu_eff_weighted": _safe_div(cpu_used, cpu_alloc),
        "cpu_eff_mean": _safe_div(sum(cpu_effs), len(cpu_effs)),
        "mem_gb_hours_req": mem_req,
        "mem_gb_hours_used": mem_used,
        "mem_eff_weighted": _safe_div(mem_used, mem_req),
        "mem_eff_mean": _safe_div(sum(mem_effs), len(mem_effs)),
        "mem_jobs_measured": len(mem_jobs),
        "mem_jobs_maxrss_fallback": sum(1 for j in mem_jobs if j.mem_source == "maxrss"),
        "mem_jobs_over": len(mem_over),
        "mem_over_max": max((j.mem_eff for j in mem_over), default=None),
        "mem_jobs_reqmem_fallback": sum(1 for j in mem_jobs if j.mem_alloc_source == "reqmem"),
        "gpu_hours": sum(j.gpu_hours for j in gpu_jobs),
        "gpu_jobs": len(gpu_jobs),
        "gpu_util_weighted": _safe_div(sum(j.gpu_util * j.gpu_hours for j in gpu_util_jobs), gpu_util_hours),
        "gpu_util_jobs": len(gpu_util_jobs),
        "gpu_hours_by_type": dict(sorted(gpu_hours_by_type.items(), key=lambda kv: -kv[1])),
        "gpu_jobs_inconsistent_type": sum(1 for j in gpu_jobs if not j.gpu_types_consistent),
        "node_hours": sum(j.node_hours for j in jobs),
        "wall_hours": sum(j.elapsed_s * j.fraction for j in jobs) / 3600,
        "wall_eff_weighted": _safe_div(sum(j.elapsed_s for j in wall_jobs),
                                       sum(j.timelimit_s for j in wall_jobs)),
        "states": dict(sorted(states.items())),
        "cpu_eff_hist": _hist(cpu_effs),
        "mem_eff_hist": _hist(mem_effs),
    }


def group_summaries(jobs: List[Job], key: str) -> List[dict]:
    groups = defaultdict(list)
    for j in jobs:
        if key == "gpu_type" and not j.gpus:
            continue                       # GPU-type breakdown covers GPU jobs only
        groups[getattr(j, key) or "(none)"].append(j)
    out = []
    for name, js in groups.items():
        s = summarize(js)
        s["name"] = name
        out.append(s)
    out.sort(key=lambda s: s["cpu_hours_alloc"], reverse=True)
    return out


def gpu_util_diagnosis(jobs):
    """Explain why no GPU job in `jobs` has a utilization figure.

    Returns None when there is nothing to explain, else a dict with
    'verdict' (one sentence), 'fix' (what to change) and 'sample' (the raw
    TRESUsageIn* fields of one GPU job's steps) so the person can see exactly
    what slurmdbd returned.
    """
    gpu_jobs = [j for j in jobs if j.gpus]
    if not gpu_jobs or any(j.gpu_util is not None for j in gpu_jobs):
        return None
    # Prefer a job that ran long enough to have been sampled, with steps.
    sample = max(gpu_jobs, key=lambda j: (bool(j.gpu_raw), j.elapsed_s))
    steps = sample.gpu_raw
    any_usage = any(st["TRESUsageInAve"] or st["TRESUsageInMax"] or st["TRESUsageInTot"] for st in steps)
    keys = set()
    for st in steps:
        for f in ("TRESUsageInAve", "TRESUsageInMax", "TRESUsageInTot"):
            keys.update(parse.parse_tres(st[f]).keys())
    if not steps:
        verdict = ("The GPU jobs have no step records at all, so there is nothing to read utilization from. "
                   "sacct returns steps only for work launched as a step (sbatch script = .batch step, srun = "
                   "numbered steps); jobs created with salloc that never ran srun have none.")
        fix = "Run GPU work inside the batch script or via srun so Slurm records a step."
    elif not any_usage:
        verdict = ("Step records exist but carry no TRES usage of any kind (no cpu, mem or gpu entries), so job "
                   "accounting gathering is not storing usage on this cluster.")
        fix = "slurm.conf: JobAcctGatherType=jobacct_gather/cgroup (or linux) and a non-zero JobAcctGatherFrequency."
    elif not any(k.startswith("gres/gpuutil") for k in keys):
        verdict = ("TRES usage is recorded (" + ", ".join(sorted(keys)) + ") but gres/gpuutil is not among the "
                   "tracked TRES, so slurmdbd never stores GPU utilization.")
        fix = ("slurm.conf: AccountingStorageTRES=gres/gpu,gres/gpumem,gres/gpuutil (then restart slurmctld; "
               "the gres/gpu plugin must be built against NVML/RSMI and gres.conf should use AutoDetect). Only "
               "jobs that finish after the change will have it.")
    else:
        verdict = "gres/gpuutil appears in the step records but could not be parsed; the raw values are shown below."
        fix = "Please report the sample so the parser can be adjusted."
    return {"verdict": verdict, "fix": fix, "job": sample.id, "sample": steps[:4]}


def gpu_occupancy(jobs, wstart, wend, now=None):
    """GPU-hours held inside [wstart, wend] by every job that had GPUs.

    Deliberately independent of the completed-job selection: a GPU was busy
    for the hours a job held it whether or not that job has finished yet, so
    running jobs and jobs that ended after the window count for their overlap.
    Returns {'gpu_hours', 'by_type', 'jobs'}; untyped GPUs are under ''.
    """
    now = now or datetime.now()
    total, by_type, n = 0.0, {}, 0
    for j in jobs:
        if not j.gpus or j.start is None:
            continue
        jend = j.end if (j.end and j.state != "RUNNING") else now
        overlap = (min(jend, wend) - max(j.start, wstart)).total_seconds()
        if overlap <= 0:
            continue
        n += 1
        hours = overlap / 3600
        total += j.gpus * hours
        for t, cnt in j.gpu_types.items():
            by_type[t] = by_type.get(t, 0.0) + cnt * hours
    return {"gpu_hours": total, "by_type": by_type, "jobs": n}
