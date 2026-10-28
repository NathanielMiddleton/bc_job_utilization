"""Break the cluster's capacity down behind the sreport bars.

sreport only knows totals per TRES. To say *which* partition, GPU type,
node feature set, user or account the allocated and idle hours belong to,
jobs (sacct, with their node lists) are joined to the node inventory
(sinfo). The totals here are therefore computed independently of sreport
and will differ slightly from it: sreport works from hourly roll-ups and
subtracts historical downtime, this works from job records and today's
inventory.
"""
from collections import defaultdict
from datetime import datetime

from . import slurm

DIMENSIONS = ("partition", "gpu_type", "feature", "user", "account")


def _feature_key(node):
    return ",".join(node["features"]) if node and node["features"] else "(no features)"


def _units(node, tres):
    return node["cpus"] if tres == "cpu" else sum(node["gpus"].values())


def breakdown(nodes, jobs, wstart, wend, tres, now=None):
    """Return per-dimension groups for one TRES ('cpu' or 'gres/gpu').

    Each group: {name, allocated, capacity (or None), idle (or None), ratio (or None), jobs}.
    Allocated hours come from every job that held the TRES during the window
    (running jobs included, clipped to the window). Capacity is the node
    inventory times the window hours. A node in several partitions counts
    toward each partition's capacity, as sinfo itself reports it.
    """
    now = now or datetime.now()
    hours = (min(wend, now) - wstart).total_seconds() / 3600
    if hours <= 0:
        hours = 0.0
    by_name = {n["name"]: n for n in (nodes or [])}
    is_gpu = tres != "cpu"

    cap = {d: defaultdict(float) for d in DIMENSIONS}
    alloc = {d: defaultdict(float) for d in DIMENSIONS}
    njobs = {d: defaultdict(int) for d in DIMENSIONS}

    # ---- capacity from the inventory
    for n in by_name.values():
        u = _units(n, tres)
        if not u:
            continue
        for p in n["partitions"]:
            cap["partition"][p] += u * hours
        cap["feature"][_feature_key(n)] += u * hours
        if is_gpu:
            for t, c in n["gpus"].items():
                cap["gpu_type"][t or "untyped"] += c * hours
        else:
            # For CPUs the "GPU type" view means: cores on nodes carrying that GPU type.
            if n["gpus"]:
                for t in n["gpus"]:
                    cap["gpu_type"][t or "untyped"] += u * hours
            else:
                cap["gpu_type"]["no GPU"] += u * hours

    # ---- allocation from jobs
    for j in jobs:
        units_total = j.alloc_cpus if not is_gpu else j.gpus
        if not units_total or j.start is None:
            continue
        jend = j.end if (j.end and j.state != "RUNNING") else now
        overlap = (min(jend, wend) - max(j.start, wstart)).total_seconds()
        if overlap <= 0:
            continue
        h = overlap / 3600
        total_h = units_total * h
        job_nodes = slurm.expand_hostlist(j.nodelist) or [None]
        per_node = total_h / len(job_nodes)

        def add(dim, key, amount):
            alloc[dim][key] += amount
            njobs[dim][key] += 1   # counts once per (job, group); a job spanning two feature sets counts in both

        add("partition", j.partition or "(none)", total_h)
        add("user", j.user or "(none)", total_h)
        add("account", j.account or "(none)", total_h)

        seen_feat = set()
        for name in job_nodes:
            node = by_name.get(name)
            fk = _feature_key(node) if node else "(unknown node)"
            alloc["feature"][fk] += per_node
            if fk not in seen_feat:
                njobs["feature"][fk] += 1
                seen_feat.add(fk)

        if is_gpu:
            typed = {t: c for t, c in j.gpu_types.items() if t}
            untyped = j.gpu_types.get("", 0)
            for t, c in typed.items():
                add("gpu_type", t, c * h)
            if untyped:
                # Infer the type from the node when it carries exactly one GPU type.
                per_node_untyped = untyped * h / len(job_nodes)
                seen_keys = set()
                for name in job_nodes:
                    node = by_name.get(name)
                    types = [t for t in (node["gpus"] if node else {}) if t]
                    key = types[0] if len(types) == 1 else "untyped"
                    alloc["gpu_type"][key] += per_node_untyped
                    if key not in seen_keys:
                        njobs["gpu_type"][key] += 1
                        seen_keys.add(key)
        else:
            seen_t = set()
            for name in job_nodes:
                node = by_name.get(name)
                if node and node["gpus"]:
                    for t in node["gpus"]:
                        key = t or "untyped"
                        alloc["gpu_type"][key] += per_node / len(node["gpus"])
                        if key not in seen_t:
                            njobs["gpu_type"][key] += 1
                            seen_t.add(key)
                else:
                    key = "no GPU" if node else "(unknown node)"
                    alloc["gpu_type"][key] += per_node
                    if key not in seen_t:
                        njobs["gpu_type"][key] += 1
                        seen_t.add(key)

    out = {}
    for d in DIMENSIONS:
        names = set(cap[d]) | set(alloc[d])
        rows = []
        for name in names:
            a = alloc[d][name]
            c = cap[d].get(name) if d in ("partition", "gpu_type", "feature") else None
            rows.append({
                "name": name, "allocated": a, "capacity": c,
                "idle": (max(c - a, 0.0) if c is not None else None),
                "ratio": (a / c if c else None),
                "jobs": njobs[d][name],
            })
        rows.sort(key=lambda r: (r["capacity"] or r["allocated"]), reverse=True)
        out[d] = rows
    total_cap = sum(_units(n, tres) for n in by_name.values()) * hours
    total_alloc = sum(alloc["partition"].values())
    return {"groups": out, "hours": hours, "total_capacity": total_cap, "total_allocated": total_alloc,
            "nodes_known": len(by_name)}


def down_nodes(nodes, tres):
    """Nodes currently down/drained, with their reason and the TRES they take out of service."""
    out = []
    for n in nodes or []:
        if not n.get("down"):
            continue
        u = _units(n, tres)
        if not u:
            continue
        out.append({"name": n["name"], "state": n["state"], "reason": n["reason"], "cpus": n["cpus"],
                    "gpus": sum(n["gpus"].values()), "gpu_types": {t or "untyped": c for t, c in n["gpus"].items()},
                    "partitions": sorted(n["partitions"]), "features": n["features"]})
    out.sort(key=lambda r: (r["gpus"], r["cpus"]), reverse=True)
    return out
