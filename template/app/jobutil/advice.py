"""Turn utilization results into optimization hints.

Two layers:
  job_hints(job)   short tags for one job ("cores 8→2", "mem 50G→12G", ...)
  advise(...)      aggregated advice for the whole result set, sorted by how
                   many allocated hours could be recovered.

Every rule has a minimum run time or sample size so it does not fire on
noise, and every suggestion carries a caveat about what the data cannot
tell. Thresholds are module constants so a site can tune them in one place.
"""
import math
from collections import defaultdict

# --- thresholds -----------------------------------------------------------
MIN_ELAPSED_S = 600          # ignore jobs shorter than this for core/mem/time hints
CPU_LOW = 0.5                # CPU efficiency below this -> over-requested cores
CPU_MARGIN = 1.25            # recommended cores = used cores x margin
SERIAL_CORES = 1.3           # <= this many cores' worth of CPU time -> looks serial
MEM_LOW = 0.30               # memory efficiency below this -> over-requested memory
MEM_MIN_REQ_GIB = 4          # ...but only when the request was at least this big
MEM_MARGIN = 1.3             # recommended --mem = peak x margin
MEM_HIGH = 0.95              # above this the job is one input change away from OOM
WALL_LOW = 0.25              # used under a quarter of the time limit
WALL_MIN_LIMIT_S = 1800      # ...and the limit was at least 30 min
WALL_MARGIN = 1.5            # recommended --time = longest actual run x margin
GPU_IDLE = 30.0              # percent utilization below which a GPU is "idle"
SHORT_JOB_S = 300            # jobs shorter than this count as "short"
SHORT_JOB_MIN_COUNT = 20     # ...and we need this many to suggest bundling
FAILED_RATIO = 0.20          # failed share above this (with >= FAILED_MIN) gets a hint
FAILED_MIN = 5
CANCEL_LONG_S = 3600         # cancelled after running this long = wasted
GIB = 1024 ** 3


# --- helpers --------------------------------------------------------------

def _gib(b):
    return b / GIB


def _ceil_gib(b):
    return max(1, int(math.ceil(_gib(b))))


def _fmt_time(seconds):
    """Seconds -> Slurm --time string, rounded up to 15 minutes."""
    s = int(math.ceil(seconds / 900.0) * 900)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}-{h:02d}:{m:02d}:00" if d else f"{h:02d}:{m:02d}:00"


def _rec_cores(j):
    return max(1, int(math.ceil(j.cpu_eff * j.alloc_cpus * CPU_MARGIN)))


def _rec_mem_b(j):
    return max(GIB, j.mem_used_b * MEM_MARGIN)


def _fmt_hours(h):
    return f"{h:,.0f}" if h >= 100 else f"{h:,.1f}"


def _n(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# --- per-job --------------------------------------------------------------

def job_hints(j):
    """Short tags for the job table. Each is {'code', 'text'}."""
    hints = []
    long_enough = j.elapsed_s >= MIN_ELAPSED_S

    if long_enough and not j.gpus and j.alloc_cpus >= 2 and j.cpu_eff is not None and j.cpu_eff < CPU_LOW:
        rec = _rec_cores(j)
        if rec < j.alloc_cpus:
            serial = j.cpu_eff * j.alloc_cpus <= SERIAL_CORES
            hints.append({"code": f"cores {j.alloc_cpus}→{rec}",
                          "text": (f"Used {j.cpu_eff * j.alloc_cpus:.1f} of {j.alloc_cpus} cores on average"
                                   + (" — this looks like a serial program" if serial else "")
                                   + f". Try --cpus-per-task={rec}."
                                   + (" Multi-node jobs waiting on communication also look like this."
                                      if j.alloc_nodes > 1 else ""))})

    if j.state == "OUT_OF_MEMORY":
        hints.append({"code": "OOM", "text": f"Ran out of memory with {_ceil_gib(j.mem_req_b or 0)}G allocated; "
                      f"try about 1.5× that, or fewer tasks per node."})
    elif long_enough and j.mem_eff is not None:
        if j.mem_eff < MEM_LOW and j.mem_req_b >= MEM_MIN_REQ_GIB * GIB:
            rec = _ceil_gib(_rec_mem_b(j))
            if rec * GIB < j.mem_req_b * 0.7:
                approx = "≈" if j.mem_source == "maxrss" else ""
                hints.append({"code": f"mem {_ceil_gib(j.mem_req_b)}G→{approx}{rec}G",
                              "text": (f"Peak memory was {_gib(j.mem_used_b):.1f} GiB of {_gib(j.mem_req_b):.0f} GiB allocated. "
                                       f"Try --mem={rec}G (peak × {MEM_MARGIN})."
                                       + (" Peak is from MaxRSS, the largest single task, so multi-task steps "
                                          "may need more." if j.mem_source == "maxrss" else ""))})
        elif j.mem_over:
            hints.append({"code": "mem over", "text": f"Reported peak is {j.mem_eff * 100:.0f}% of the allocation. If "
                          f"memory is enforced here this is RSS double-counting across forked processes, not real "
                          f"use; if it is not enforced, the job really exceeded its request and should ask for more."})
        elif j.mem_eff > MEM_HIGH:
            hints.append({"code": "mem near limit", "text": f"Used {j.mem_eff * 100:.0f}% of allocated memory; a "
                          f"slightly larger input would be killed. Consider a little headroom."})

    if j.state == "TIMEOUT":
        hints.append({"code": "hit time limit", "text": "Killed at the time limit; all work since the last "
                      "checkpoint is lost. Raise --time, or checkpoint and use --signal to finish cleanly."})
    elif (long_enough and j.wall_eff is not None and j.timelimit_s >= WALL_MIN_LIMIT_S
          and j.wall_eff < WALL_LOW):
        hints.append({"code": f"time {_fmt_time(j.timelimit_s)}→{_fmt_time(j.elapsed_s * WALL_MARGIN)}",
                      "text": (f"Ran {_fmt_time(j.elapsed_s)} of a {_fmt_time(j.timelimit_s)} limit. A tighter "
                               f"--time lets the scheduler backfill this job into gaps and start it sooner.")})

    if j.gpus and j.gpu_util is not None and j.gpu_util < GPU_IDLE:
        if j.cpu_eff is not None and j.cpu_eff > 0.8:
            why = "CPU cores were busy, so the CPU side is probably the bottleneck: more --cpus-per-task per GPU or more data-loader workers."
        else:
            why = "CPU cores were not busy either, so look at I/O, small batch sizes, or time spent outside the GPU section."
        hints.append({"code": f"GPU idle {j.gpu_util:.0f}%", "text": f"GPU utilization averaged {j.gpu_util:.0f}%. {why}"})

    if j.state == "CANCELLED" and j.elapsed_s >= CANCEL_LONG_S:
        hints.append({"code": "cancelled late", "text": f"Cancelled after {_fmt_time(j.elapsed_s)}; "
                      f"{_fmt_hours(j.cpu_hours_alloc)} CPU-hours were spent for no result."})
    return hints


# --- aggregate ------------------------------------------------------------

def advise(jobs, scope, by_user=None):
    """Return advice cards sorted by recoverable allocated hours.

    Each card: {'id', 'title', 'impact', 'detail', 'action', 'caveat', 'jobs', 'savings_hours'}.
    `scope` changes the voice: 'mine' speaks to the job owner, otherwise to
    whoever is looking across users.
    """
    out = []
    n = len(jobs)
    if n == 0:
        return out
    you = scope == "mine"
    long_jobs = [j for j in jobs if j.elapsed_s >= MIN_ELAPSED_S]

    # Cores -------------------------------------------------------------
    cpu_jobs = [j for j in long_jobs if not j.gpus and j.alloc_cpus >= 2 and j.cpu_eff is not None
                and j.cpu_eff < CPU_LOW and _rec_cores(j) < j.alloc_cpus]
    if cpu_jobs:
        saving = sum((j.alloc_cpus - _rec_cores(j)) * j.elapsed_s * j.fraction / 3600 for j in cpu_jobs)
        serial = [j for j in cpu_jobs if j.cpu_eff * j.alloc_cpus <= SERIAL_CORES]
        multinode = [j for j in cpu_jobs if j.alloc_nodes > 1]
        typical = sorted(_rec_cores(j) for j in cpu_jobs)[len(cpu_jobs) // 2]
        detail = (f"{len(cpu_jobs)} of {len(long_jobs)} jobs over {MIN_ELAPSED_S // 60} min used less than half of "
                  f"their cores. ")
        if serial:
            detail += (f"{len(serial)} of them used about one core's worth of CPU time in total — the program is "
                       f"probably serial, or threads were never enabled (OMP_NUM_THREADS, --threads, -j). ")
        detail += "Requesting only the cores a job actually uses frees the rest for other jobs and usually shortens queue time."
        out.append({
            "id": "cores", "title": "Request fewer cores" if you else "Cores are being over-requested",
            "impact": f"about {_fmt_hours(saving)} CPU-hours idle across {_n(len(cpu_jobs), 'job')}",
            "detail": detail,
            "action": f"#SBATCH --cpus-per-task={typical}\n# and make the program use them:\nexport OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK",
            "caveat": ("GPU jobs are left out of this rule: their cores feed the GPU and are cheap by comparison. "
                       "CPU time is averaged over the whole job, so a job with a long serial setup phase and a short "
                       "parallel phase also scores low." + (f" {len(multinode)} of these are multi-node jobs, where "
                       "time spent waiting on communication looks like idle CPU." if multinode else "")),
            "jobs": len(cpu_jobs), "savings_hours": saving,
        })

    # Memory over-request -----------------------------------------------
    mem_jobs = [j for j in long_jobs if j.mem_eff is not None and j.mem_eff < MEM_LOW
                and j.mem_req_b >= MEM_MIN_REQ_GIB * GIB and _ceil_gib(_rec_mem_b(j)) * GIB < j.mem_req_b * 0.7]
    if mem_jobs:
        idle_gib_h = sum((j.mem_req_b - j.mem_used_b) / GIB * j.elapsed_s * j.fraction / 3600 for j in mem_jobs)
        peaks = sorted(j.mem_used_b for j in mem_jobs)
        safe = _ceil_gib(max(peaks) * MEM_MARGIN)
        median_req = _ceil_gib(sorted(j.mem_req_b for j in mem_jobs)[len(mem_jobs) // 2])
        maxrss = sum(1 for j in mem_jobs if j.mem_source == "maxrss")
        out.append({
            "id": "memory", "title": "Request less memory" if you else "Memory is being over-requested",
            "impact": f"about {_fmt_hours(idle_gib_h)} GiB-hours reserved but unused across {_n(len(mem_jobs), 'job')}",
            "detail": (f"These jobs peaked at {_gib(peaks[0]):.1f}–{_gib(peaks[-1]):.1f} GiB but were allocated a median of "
                       f"{median_req} GiB. Memory that is reserved but unused cannot be given to anyone else, and on "
                       f"shared nodes a large --mem can pin cores idle too. A request of about {MEM_MARGIN}× the "
                       f"observed peak is usually safe."),
            "action": f"#SBATCH --mem={safe}G   # {MEM_MARGIN}x the largest peak seen here",
            "caveat": ("Peak memory is sampled by Slurm every few seconds, so short spikes can be missed."
                       + (f" For {maxrss} of these jobs the peak comes from MaxRSS, the largest single task, "
                          "which understates multi-task steps." if maxrss else "")),
            "jobs": len(mem_jobs), "savings_hours": idle_gib_h / 8,   # rough: 8 GiB ≈ one core-equivalent
        })

    # Out of memory / near limit ------------------------------------------
    oom = [j for j in jobs if j.state == "OUT_OF_MEMORY"]
    near = [j for j in long_jobs if j.mem_eff is not None and MEM_HIGH < j.mem_eff <= 1.0 and j.state != "OUT_OF_MEMORY"]
    over = [j for j in long_jobs if j.mem_over and j.state != "OUT_OF_MEMORY"]
    if oom:
        wasted = sum(j.cpu_hours_alloc for j in oom)
        biggest = max(j.mem_req_b or 0 for j in oom)
        out.append({
            "id": "oom", "title": "Some jobs ran out of memory",
            "impact": f"{_n(len(oom), 'job')} killed, {_fmt_hours(wasted)} CPU-hours spent for no result",
            "detail": (f"Jobs that exceed their --mem are killed and everything they computed is lost. "
                       + (f"{len(near)} further jobs finished within 5% of their limit." if near else "")),
            "action": f"#SBATCH --mem={_ceil_gib(biggest * 1.5)}G   # 1.5x the largest request that failed",
            "caveat": "If memory grows with input size, scale the request with the input rather than fixing one number.",
            "jobs": len(oom), "savings_hours": wasted,
        })
    elif near:
        out.append({
            "id": "near-limit", "title": "Some jobs are close to their memory limit",
            "impact": f"{_n(len(near), 'job')} used over {int(MEM_HIGH * 100)}% of allocated memory",
            "detail": "They finished, but a slightly larger input or a different random seed could push them over "
                      "the limit and lose the whole run. A little headroom is cheap insurance.",
            "action": None, "caveat": None, "jobs": len(near), "savings_hours": 0,
        })

    if over:
        out.append({
            "id": "mem-over", "title": "Reported memory peaks exceed the allocation",
            "impact": f"{_n(len(over), 'job')} report up to {max(j.mem_eff for j in over) * 100:.0f}% of allocated memory",
            "detail": "Slurm adds up the RSS of every process in a step. Programs that fork workers (Python "
                      "multiprocessing, Airflow, R parallel, make -j) re-count the memory each child shares with "
                      "its parent, so the sum can be far above what the node ever held. If this cluster enforces "
                      "memory with cgroups, these numbers are artefacts and the jobs did not actually use more than "
                      "they were allocated. If it does not enforce memory, they really did, and should request more "
                      "before a neighbour's job is squeezed.",
            "action": ("# cluster side: accurate per-job peaks\nJobAcctGatherParams=UsePss        # or JobAcctGatherType=jobacct_gather/cgroup"
                       if not you else "# check whether the cluster enforces memory:\nscontrol show config | grep -i -e ConstrainRAMSpace -e JobAcctGather"),
            "caveat": "Aggregate memory efficiency caps each job at 100% so these jobs do not inflate the totals.",
            "jobs": len(over), "savings_hours": 0,
        })

    # Time limits ---------------------------------------------------------
    time_jobs = [j for j in long_jobs if j.wall_eff is not None and j.timelimit_s >= WALL_MIN_LIMIT_S
                 and j.wall_eff < WALL_LOW and j.state != "TIMEOUT"]
    if time_jobs:
        longest = max(j.elapsed_s for j in time_jobs)
        median_limit = sorted(j.timelimit_s for j in time_jobs)[len(time_jobs) // 2]
        out.append({
            "id": "time", "title": "Shorten time limits to start sooner",
            "impact": f"{_n(len(time_jobs), 'job')} used under {int(WALL_LOW * 100)}% of their time limit",
            "detail": (f"The longest of these actually ran {_fmt_time(longest)} against a median limit of "
                       f"{_fmt_time(median_limit)}. Slurm backfills short jobs into gaps ahead of the queue, so a "
                       f"realistic --time often starts a job hours earlier. It does not change what the job costs."),
            "action": f"#SBATCH --time={_fmt_time(longest * WALL_MARGIN)}   # {WALL_MARGIN}x the longest run seen here",
            "caveat": "A job that hits its limit is killed, so keep a margin and checkpoint anything long.",
            "jobs": len(time_jobs), "savings_hours": 0,
        })
    timeouts = [j for j in jobs if j.state == "TIMEOUT"]
    if timeouts:
        wasted = sum(j.cpu_hours_alloc for j in timeouts)
        out.append({
            "id": "timeout", "title": "Some jobs hit their time limit",
            "impact": f"{_n(len(timeouts), 'job')} killed at the limit, {_fmt_hours(wasted)} CPU-hours with no result",
            "detail": "Unless the program checkpoints, everything since it started is lost. Either raise --time with a "
                      "margin, or checkpoint periodically and ask Slurm for a warning signal before the limit so the "
                      "job can save and requeue itself.",
            "action": "#SBATCH --time=<longer>\n#SBATCH --signal=B:USR1@300   # warn 5 min before the limit\n# trap 'save_checkpoint; scontrol requeue $SLURM_JOB_ID' USR1",
            "caveat": "Requeueing needs the job script to resume from its checkpoint on restart.",
            "jobs": len(timeouts), "savings_hours": wasted,
        })

    # GPUs -------------------------------------------------------------------
    gpu_jobs = [j for j in jobs if j.gpus]
    idle_gpu = [j for j in gpu_jobs if j.gpu_util is not None and j.gpu_util < GPU_IDLE]
    if idle_gpu:
        gpu_h = sum(j.gpu_hours for j in idle_gpu)
        cpu_bound = [j for j in idle_gpu if j.cpu_eff is not None and j.cpu_eff > 0.8]
        detail = (f"{len(idle_gpu)} of {len(gpu_jobs)} GPU jobs kept the GPU under {int(GPU_IDLE)}% busy. ")
        if cpu_bound:
            detail += (f"{len(cpu_bound)} of them had busy CPU cores at the same time, which points at the CPU side "
                       f"(data loading, preprocessing, Python loops) as the bottleneck: more cores per GPU and more "
                       f"data-loader workers usually help. ")
        if len(cpu_bound) < len(idle_gpu):
            detail += ("The rest had idle CPUs too, so the GPU is waiting on something else — storage reads, tiny "
                       "batches, or long stretches of non-GPU code. ")
        detail += "GPUs are the scarcest resource on most clusters, so a half-idle GPU is the most expensive waste here."
        out.append({
            "id": "gpu", "title": "GPUs are sitting idle inside jobs",
            "impact": f"about {_fmt_hours(gpu_h)} GPU-hours at under {int(GPU_IDLE)}% utilization",
            "detail": detail,
            "action": "# profile first (nvidia-smi dmon, torch.profiler, nsys); typical fixes:\n#SBATCH --cpus-per-task=8      # per GPU, for data loading\n# larger batch size, mixed precision, prefetching / pinned memory",
            "caveat": "gres/gpuutil is averaged over the job, so a long CPU-only setup phase lowers it even if the "
                      "training loop itself keeps the GPU busy.",
            "jobs": len(idle_gpu), "savings_hours": gpu_h * 4,   # weight GPU-hours heavily in the ordering
        })
    elif gpu_jobs and all(j.gpu_util is None for j in gpu_jobs):
        out.append({
            "id": "gpu-unknown", "title": "GPU utilization is not being recorded",
            "impact": f"{_n(len(gpu_jobs), 'GPU job')}, no gres/gpuutil data",
            "detail": "Slurm only stores GPU utilization when gres/gpuutil is tracked in AccountingStorageTRES "
                      "(and the GPU plugin can talk to the driver), so the GPU-hours above cannot be judged "
                      "for efficiency here.",
            "action": ("# inside a job, sample it yourself:\nnvidia-smi dmon -s u -d 10 > gpu_util.log &"
                       if you else "# slurm.conf (slurmctld restart; gres/gpu plugin must be built with NVML/RSMI):\n"
                                   "AccountingStorageTRES=gres/gpu,gres/gpumem,gres/gpuutil\n"
                                   "JobAcctGatherType=jobacct_gather/cgroup   # or jobacct_gather/linux"),
            "caveat": None, "jobs": len(gpu_jobs), "savings_hours": 0,
        })

    # Short jobs ------------------------------------------------------------
    short = [j for j in jobs if j.elapsed_s < SHORT_JOB_S]
    if len(short) >= SHORT_JOB_MIN_COUNT:
        out.append({
            "id": "short", "title": "Bundle very short jobs",
            "impact": f"{_n(len(short), 'job')} ran under {SHORT_JOB_S // 60} minutes",
            "detail": "Each job pays scheduling, prolog/epilog and node setup overhead that can rival its own run "
                      "time, and thousands of tiny jobs slow the scheduler for everyone. Grouping work so each job "
                      "runs for at least 10–30 minutes keeps throughput and is kinder to the queue.",
            "action": "# one array task handles a chunk of inputs instead of one:\n#SBATCH --array=0-99\nfor i in $(seq $((SLURM_ARRAY_TASK_ID*50)) $((SLURM_ARRAY_TASK_ID*50+49))); do\n  ./process input_$i\ndone\n# or run several at once inside one job: GNU parallel / srun --exclusive -n1",
            "caveat": "Tasks in one bundle share a time limit, so size bundles for the slowest item.",
            "jobs": len(short), "savings_hours": len(short) * 0.05,
        })

    # Failures ---------------------------------------------------------------
    failed = [j for j in jobs if j.state in ("FAILED", "NODE_FAIL", "BOOT_FAIL")]
    if len(failed) >= FAILED_MIN and len(failed) / n >= FAILED_RATIO:
        wasted = sum(j.cpu_hours_alloc for j in failed)
        quick = sum(1 for j in failed if j.elapsed_s < SHORT_JOB_S)
        out.append({
            "id": "failed", "title": "A large share of jobs failed",
            "impact": f"{len(failed)} of {n} jobs ({len(failed) * 100 // n}%), {_fmt_hours(wasted)} CPU-hours",
            "detail": (f"{quick} failed within {SHORT_JOB_S // 60} minutes, which usually means a missing file, module "
                       f"or path rather than a problem with the computation. " if quick else "")
                      + "Running one small test case interactively or on a debug partition before submitting the "
                        "batch catches most of these for almost nothing.",
            "action": "sacct -j <jobid> -o JobID,State,ExitCode,DerivedExitCode   # 1:0 = program error, 0:9 = killed",
            "caveat": "Exit codes are in the job table; NODE_FAIL and BOOT_FAIL are hardware or system problems, not yours.",
            "jobs": len(failed), "savings_hours": wasted,
        })
    cancelled = [j for j in jobs if j.state == "CANCELLED" and j.elapsed_s >= CANCEL_LONG_S]
    if cancelled:
        wasted = sum(j.cpu_hours_alloc for j in cancelled)
        out.append({
            "id": "cancelled", "title": "Jobs cancelled after running for a long time",
            "impact": f"{_n(len(cancelled), 'job')}, {_fmt_hours(wasted)} CPU-hours",
            "detail": "A job cancelled hours in has usually been found to be misconfigured. Checking early output "
                      "(a quick sanity log in the first minutes) or running a scaled-down version first avoids this.",
            "action": None, "caveat": None, "jobs": len(cancelled), "savings_hours": wasted,
        })

    # Cross-user view -----------------------------------------------------------
    if not you and by_user and len(by_user) > 1:
        ranked = sorted(by_user, key=lambda u: u["cpu_hours_alloc"] - u["cpu_hours_used"], reverse=True)[:5]
        rows = [f"{u['name']}: {_fmt_hours(u['cpu_hours_alloc'] - u['cpu_hours_used'])} idle of "
                f"{_fmt_hours(u['cpu_hours_alloc'])} CPU-hours ({(u['cpu_eff_weighted'] or 0) * 100:.0f}% efficient)"
                for u in ranked if u["cpu_hours_alloc"] > 0]
        if rows:
            total_idle = sum(u["cpu_hours_alloc"] - u["cpu_hours_used"] for u in by_user)
            top_idle = sum(u["cpu_hours_alloc"] - u["cpu_hours_used"] for u in ranked)
            out.append({
                "id": "users", "title": "Where the idle CPU-hours are",
                "impact": f"{len(ranked)} users account for {top_idle / total_idle * 100:.0f}% of idle CPU-hours" if total_idle else "",
                "detail": "Switch the scope to one of these users' jobs (filter the table) to see which of the hints "
                          "above apply to them; a short note with the specific --cpus-per-task or --mem to use is "
                          "far more effective than a general reminder.",
                "action": "\n".join(rows), "caveat": None, "jobs": sum(u["jobs"] for u in ranked), "savings_hours": top_idle,
            })

    out.sort(key=lambda a: a["savings_hours"], reverse=True)
    if not out:
        out.append({
            "id": "ok", "title": "Nothing stands out",
            "impact": f"{_n(n, 'job')} checked",
            "detail": "No rule found over-requested cores, memory or time, idle GPUs, or wasted runs in this window. "
                      "The job table still shows per-job efficiencies if you want to look closer.",
            "action": None, "caveat": None, "jobs": n, "savings_hours": 0,
        })
    return out
