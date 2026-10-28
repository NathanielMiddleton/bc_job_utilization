import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import render_fixture                      # noqa: E402
from jobutil import advice, metrics, parse               # noqa: E402
from jobutil.slurm import SACCT_FIELDS                   # noqa: E402

NOW = datetime(2026, 10, 28, 12, 0, 0)


def _job(**kw):
    base = dict(id="1", raw_id="1", name="j", user="alice", account="a", partition="cpu", state="COMPLETED",
                start=NOW - timedelta(hours=2), end=NOW - timedelta(hours=1), elapsed_s=3600.0,
                alloc_cpus=1, alloc_nodes=1, cpu_used_s=3600.0, mem_req_b=None, mem_alloc_source="alloctres", mem_used_b=None,
                mem_source=None, gpus=0, gpu_types={}, gpu_util=None, timelimit_s=None, exit_code="0:0")
    base.update(kw)
    return metrics.Job(**base)


@pytest.fixture
def selected():
    rows = parse.sacct_rows(render_fixture("sacct_sample.txt", NOW), SACCT_FIELDS)
    jobs = metrics.build_jobs(rows)
    sel, _ = metrics.select_jobs(jobs, NOW - timedelta(hours=24), NOW, "ended",
                                 metrics.TERMINAL_STATES, False, NOW)
    return {j.raw_id: j for j in sel}


def codes(j):
    return [h["code"] for h in advice.job_hints(j)]


# ---------- per-job hints ----------

def test_serial_program_on_many_cores():
    j = _job(alloc_cpus=8, cpu_used_s=3600.0)          # one core's worth over an hour
    c = codes(j)
    assert "cores 8→2" in c
    assert "serial" in advice.job_hints(j)[0]["text"]


def test_gpu_jobs_do_not_get_core_advice():
    j = _job(alloc_cpus=8, cpu_used_s=3600.0, gpus=1, gpu_types={"h200": 1}, gpu_util=80.0)
    assert not any(c.startswith("cores") for c in codes(j))


def test_short_jobs_get_no_sizing_hints():
    j = _job(alloc_cpus=16, cpu_used_s=10.0, elapsed_s=120.0, mem_req_b=64 * advice.GIB, mem_used_b=advice.GIB,
             mem_source="tres", timelimit_s=86400.0)
    assert codes(j) == []


def test_memory_hint_uses_margin_and_flags_maxrss(selected):
    j = selected["1011"]                                 # 50G requested, ~11.4 GiB peak
    c = codes(j)
    assert "mem 50G→15G" in c
    j2 = _job(mem_req_b=32 * advice.GIB, mem_used_b=2 * advice.GIB, mem_source="maxrss")
    assert "mem 32G→≈3G" in codes(j2)


def test_small_memory_requests_are_left_alone(selected):
    assert not any(c.startswith("mem") for c in codes(selected["1005"]))   # 2G request


def test_oom_and_near_limit():
    assert "OOM" in codes(_job(state="OUT_OF_MEMORY", mem_req_b=8 * advice.GIB))
    assert "mem near limit" in codes(_job(mem_req_b=8 * advice.GIB, mem_used_b=7.8 * advice.GIB, mem_source="tres"))


def test_mem_over_hint_and_card(selected):
    j = selected["1013"]
    assert "mem over" in codes(j)
    assert "mem near limit" not in codes(j)
    cards = advice.advise(list(selected.values()), "all")
    card = next(c for c in cards if c["id"] == "mem-over")
    assert "175%" in card["impact"] and "UsePss" in card["action"]
    mine = next(c for c in advice.advise(list(selected.values()), "mine") if c["id"] == "mem-over")
    assert "ConstrainRAMSpace" in mine["action"]


def test_time_limit_hints(selected):
    assert "hit time limit" in codes(selected["1003"])
    assert "time 08:00:00→01:30:00" in codes(selected["1011"])      # ran 1h of an 8h limit
    assert advice._fmt_time(100000) == "1-04:00:00"   # 1d 03:46:40 rounds up to the quarter hour


def test_gpu_idle_hint_distinguishes_cpu_bound():
    busy_cpu = _job(gpus=1, gpu_types={"a100": 1}, gpu_util=12.0, alloc_cpus=4, cpu_used_s=4 * 3600 * 0.9)
    idle_cpu = _job(gpus=1, gpu_types={"a100": 1}, gpu_util=12.0, alloc_cpus=4, cpu_used_s=3600.0)
    assert "GPU idle 12%" in codes(busy_cpu)
    assert "CPU side" in advice.job_hints(busy_cpu)[0]["text"]
    assert "I/O" in advice.job_hints(idle_cpu)[0]["text"]


# ---------- aggregate advice ----------

def ids(cards):
    return [c["id"] for c in cards]


def test_fixture_advice_for_owner(selected):
    cards = advice.advise(list(selected.values()), "mine")
    got = ids(cards)
    assert "timeout" in got and "memory" in got and "time" in got
    assert "users" not in got                              # owner view has no cross-user card
    assert "gpu" not in got                                # fixture GPUs are 72% and 88% busy
    tc = next(c for c in cards if c["id"] == "timeout")
    assert tc["jobs"] == 1 and "--signal" in tc["action"]
    assert cards == sorted(cards, key=lambda c: c["savings_hours"], reverse=True)


def test_cross_user_card_only_for_wider_scopes(selected):
    jobs = list(selected.values())
    by_user = metrics.group_summaries(jobs, "user")
    cards = advice.advise(jobs, "all", by_user)
    users = next(c for c in cards if c["id"] == "users")
    assert "alice" in users["action"]
    assert users["jobs"] == len(jobs)


def test_short_job_bundling_rule():
    jobs = [_job(id=str(i), raw_id=str(i), elapsed_s=60.0, cpu_used_s=55.0) for i in range(25)]
    cards = advice.advise(jobs, "mine")
    assert "short" in ids(cards)
    assert "--array" in next(c for c in cards if c["id"] == "short")["action"]


def test_failed_share_rule_needs_sample_size():
    few = [_job(state="FAILED") for _ in range(3)]
    assert "failed" not in ids(advice.advise(few, "mine"))
    many = [_job(state="FAILED", elapsed_s=30.0) for _ in range(6)] + [_job() for _ in range(10)]
    card = next(c for c in advice.advise(many, "mine") if c["id"] == "failed")
    assert "6 of 16" in card["impact"] and "missing file" in card["detail"]


def test_gpu_unknown_card_differs_by_scope():
    jobs = [_job(gpus=2, gpu_types={"": 2})]
    assert "nvidia-smi" in next(c for c in advice.advise(jobs, "mine") if c["id"] == "gpu-unknown")["action"]
    assert "AccountingStorageTRES" in next(c for c in advice.advise(jobs, "all") if c["id"] == "gpu-unknown")["action"]


def test_nothing_stands_out():
    cards = advice.advise([_job(alloc_cpus=4, cpu_used_s=4 * 3600 * 0.9, mem_req_b=8 * advice.GIB,
                                mem_used_b=6 * advice.GIB, mem_source="tres", timelimit_s=7200.0)], "mine")
    assert ids(cards) == ["ok"]
    assert advice.advise([], "mine") == []
