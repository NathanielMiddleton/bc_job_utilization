"""Thin wrappers around the Slurm command-line tools.

Commands are run with an argument list (never a shell) and a timeout, and
SLURM_TIME_FORMAT is forced to the ISO 'standard' format so parsing is
independent of the site's defaults.
"""
import os
import subprocess

from . import parse

SACCT_FIELDS = [
    "JobIDRaw", "JobID", "User", "Account", "Partition", "State", "Start", "End",
    "ElapsedRaw", "TotalCPU", "AllocCPUS", "AllocNodes", "ReqMem", "MaxRSS",
    "AllocTRES", "TRESUsageInTot", "TRESUsageInAve", "TRESUsageInMax", "TimelimitRaw", "ExitCode",
    "JobName",   # keep last: job names may contain the delimiter
]
DELIM = "|"
TIME_FMT = "%Y-%m-%dT%H:%M:%S"


class SlurmError(Exception):
    pass


def _env():
    env = dict(os.environ)
    env["SLURM_TIME_FORMAT"] = "standard"
    env["LC_ALL"] = "C"
    return env


def _run(args, timeout):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=_env())
    except FileNotFoundError:
        raise SlurmError(f"{args[0]} was not found. Set the binary path in config.yml.")
    except subprocess.TimeoutExpired:
        raise SlurmError(f"{os.path.basename(args[0])} did not answer within {timeout}s. "
                         "Try a shorter time range.")
    if proc.returncode != 0:
        msg = proc.stderr.strip() or f"exit status {proc.returncode}"
        raise SlurmError(f"{os.path.basename(args[0])} failed: {msg}")
    return proc.stdout


def sacct(cfg, start, end, user=None, all_users=False, partitions=None, cluster=None):
    """Return raw rows (dicts) for every job *and step* touching [start, end].

    No --state filter is passed on purpose: sacct interprets --state together
    with -S/-E as "was in this state during the window", which drops jobs that
    were still running inside the window and finished afterwards. Filtering by
    state is done in metrics.select_jobs instead, where the semantics are
    explicit.
    """
    args = [cfg["sacct"], "-P", "-n", "--delimiter", DELIM,
            "--format", ",".join(SACCT_FIELDS),
            "-S", start.strftime(TIME_FMT), "-E", end.strftime(TIME_FMT)]
    if all_users:
        args.append("-a")
    elif user:
        args += ["-u", user]
    if partitions:
        args += ["-r", ",".join(partitions)]
    if cluster:
        args += ["-M", cluster]
    out = _run(args, cfg["command_timeout"])
    return parse.sacct_rows(out, SACCT_FIELDS, DELIM)


def sreport_utilization(cfg, start, end, tres="cpu", cluster=None):
    """Cluster-wide allocated / idle / down totals from sreport, in TRES-seconds.

    sreport works from hourly roll-ups, so the window is effectively rounded
    to whole hours and the most recent hour is usually not yet available.
    """
    args = [cfg["sreport"], "-P", "-t", "Seconds", "cluster", "utilization",
            f"start={start.strftime('%Y-%m-%dT%H:%M')}",
            f"end={end.strftime('%Y-%m-%dT%H:%M')}", f"--tres={tres}"]
    if cluster:
        args += ["-M", cluster]
    rows = parse.sreport_table(_run(args, cfg["command_timeout"]))
    result = []
    for r in rows:
        def num(*names):
            for n in names:
                if n in r:
                    return parse._to_int(r[n])
            return 0
        allocated = num("Allocated")
        reported = num("Reported")
        result.append({
            "cluster": r.get("Cluster", ""),
            "tres": r.get("TRES Name", tres).lower(),
            "allocated": allocated,
            "down": num("Down"),
            "planned_down": num("PLND Down", "Planned Down", "PlannedDown"),
            "idle": num("Idle"),
            "planned": num("Planned", "Reserved"),
            "reported": reported,
            "utilization": (allocated / reported) if reported else None,
        })
    return result


def partitions(cfg, cluster=None):
    args = [cfg["sinfo"], "-h", "-o", "%R"]
    if cluster:
        args += ["-M", cluster]
    try:
        out = _run(args, cfg["command_timeout"])
    except SlurmError:
        return []
    names = set()
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("CLUSTER:"):
            names.add(line)
    return sorted(names)
