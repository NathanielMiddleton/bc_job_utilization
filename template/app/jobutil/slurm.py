"""Thin wrappers around the Slurm command-line tools.

Commands are run with an argument list (never a shell) and a timeout, and
SLURM_TIME_FORMAT is forced to the ISO 'standard' format so parsing is
independent of the site's defaults.
"""
import os
import re
import subprocess

from . import parse

SACCT_FIELDS = [
    "JobIDRaw", "JobID", "User", "Account", "Partition", "State", "Start", "End",
    "ElapsedRaw", "TotalCPU", "AllocCPUS", "AllocNodes", "ReqMem", "MaxRSS",
    "AllocTRES", "TRESUsageInTot", "TRESUsageInAve", "TRESUsageInMax", "TimelimitRaw", "ExitCode",
    "NodeList",
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


_GRES_GPU = re.compile(r"^gpu(?::([^:(]+?))?:(\d+)")


def parse_gres_gpus(gres):
    """'gpu:h200:1,shard:4' -> {'h200': 1}; 'gpu:4(S:0-1)' -> {'': 4}; '(null)' -> {}."""
    out = {}
    for item in (gres or "").split(","):
        m = _GRES_GPU.match(item.strip())
        if m:
            typ = (m.group(1) or "").lower()
            out[typ] = out.get(typ, 0) + int(m.group(2))
    return out


def expand_hostlist(text):
    """'gpu[01-03,07],cpu05' -> ['gpu01', 'gpu02', 'gpu03', 'gpu07', 'cpu05']. Nested brackets are handled."""
    text = (text or "").strip()
    if not text or text in ("None assigned", "(null)"):
        return []
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    out = []
    for part in parts:
        m = re.match(r"^(.*?)\[([^\]]+)\](.*)$", part)
        if not m:
            if part:
                out.append(part)
            continue
        pre, body, post = m.groups()
        for rng in body.split(","):
            if "-" in rng:
                a, b = rng.split("-", 1)
                for i in range(int(a), int(b) + 1):
                    out.extend(expand_hostlist(f"{pre}{str(i).zfill(len(a))}{post}"))
            else:
                out.extend(expand_hostlist(f"{pre}{rng}{post}"))
    return out


_DOWN_STATES = ("down", "drain", "drng", "fail", "maint", "not_responding", "no_respond", "inval", "unknown")


def node_inventory(cfg, cluster=None):
    """One record per node from `sinfo -N`:
    {name, partitions:set, gpus:{type:n}, features:[...], cpus, mem_mb, state, reason, down:bool}
    or None when sinfo is unavailable. Current state only."""
    args = [cfg["sinfo"], "-h", "-N", "-o", "%N|%P|%G|%f|%c|%m|%T|%E"]
    if cluster:
        args += ["-M", cluster]
    try:
        out = _run(args, cfg["command_timeout"])
    except SlurmError:
        return None
    nodes = {}
    for line in out.splitlines():
        if "|" not in line or line.startswith("CLUSTER:"):
            continue
        cols = (line.split("|") + [""] * 8)[:8]
        name, part, gres, feats, cpus, mem, state, reason = [c.strip() for c in cols]
        if not name:
            continue
        n = nodes.setdefault(name, {
            "name": name, "partitions": set(), "gpus": parse_gres_gpus(gres),
            "features": sorted(f for f in feats.split(",") if f and f != "(null)"),
            "cpus": parse._to_int(cpus), "mem_mb": parse._to_int(mem),
            "state": state.lower(), "reason": "" if reason in ("none", "(null)") else reason,
        })
        n["partitions"].add(part.rstrip("*"))
        n["down"] = any(n["state"].startswith(d) or d in n["state"] for d in _DOWN_STATES)
    return list(nodes.values())


def gpu_inventory(cfg, cluster=None, nodes=None):
    """GPUs currently configured, cluster-wide and per partition, by type:
    {'total': {type: n}, 'partitions': {name: {type: n}}, 'nodes': n_gpu_nodes} or None."""
    if nodes is None:
        nodes = node_inventory(cfg, cluster)
    if nodes is None:
        return None
    total, parts, count = {}, {}, 0
    for n in nodes:
        if not n["gpus"]:
            continue
        count += 1
        for t, c in n["gpus"].items():
            total[t] = total.get(t, 0) + c
            for p in n["partitions"]:
                pp = parts.setdefault(p, {})
                pp[t] = pp.get(t, 0) + c
    return {"total": total, "partitions": parts, "nodes": count}


def reservations(cfg, cluster=None):
    """Current and future reservations from `scontrol show reservations`, as dicts of its key=value pairs."""
    args = [cfg["scontrol"], "show", "reservations"]
    if cluster:
        args += ["-M", cluster]
    try:
        out = _run(args, cfg["command_timeout"])
    except SlurmError:
        return []
    res = []
    for block in re.split(r"\n\s*\n", out):
        kv = dict(re.findall(r"(\w+)=(\S*)", block))
        if "ReservationName" in kv:
            res.append({
                "name": kv["ReservationName"], "start": kv.get("StartTime", ""), "end": kv.get("EndTime", ""),
                "nodes": kv.get("Nodes", ""), "node_count": parse._to_int(kv.get("NodeCnt")),
                "cores": parse._to_int(kv.get("CoreCnt")), "tres": kv.get("TRES", ""),
                "users": kv.get("Users", ""), "accounts": kv.get("Accounts", ""), "flags": kv.get("Flags", ""),
            })
    return res
