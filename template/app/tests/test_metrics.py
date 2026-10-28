import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import render_fixture                      # noqa: E402
from jobutil import metrics, parse                       # noqa: E402
from jobutil.slurm import SACCT_FIELDS                   # noqa: E402

NOW = datetime(2026, 10, 28, 12, 0, 0)


@pytest.fixture
def jobs():
    rows = parse.sacct_rows(render_fixture("sacct_sample.txt", NOW), SACCT_FIELDS)
    return {j.raw_id: j for j in metrics.build_jobs(rows)}


# ---------- unit parsing ----------

@pytest.mark.parametrize("text,expected", [
    ("4000M", 4000 * 1024 ** 2), ("16G", 16 * 1024 ** 3), ("12345K", 12345 * 1024),
    ("512", 512 * 1024 ** 2), ("", None), ("garbage", None),
])
def test_parse_mem(text, expected):
    assert parse.parse_mem(text)[0] == expected


def test_parse_mem_suffixes():
    assert parse.parse_mem("32Gn") == (32 * 1024 ** 3, "n")
    assert parse.parse_mem("2Gc") == (2 * 1024 ** 3, "c")


@pytest.mark.parametrize("text,expected", [
    ("05:20:00", 19200), ("20-00:00:00", 1728000), ("28:00.000", 1680),
    ("00:02.345", 2.345), ("UNLIMITED", None), ("", None),
])
def test_parse_duration(text, expected):
    assert parse.parse_duration(text) == expected


def test_jobname_with_delimiter_survives(jobs):
    assert jobs["1002"].name == "train model | run 7"


# ---------- per-job metrics ----------

def test_cpu_efficiency(jobs):
    assert jobs["1001"].cpu_eff == pytest.approx(19200 / (7200 * 4))
    assert jobs["1002"].cpu_eff == pytest.approx(36000 / (10800 * 8))


def test_memory_prefers_tres_usage_then_maxrss(jobs):
    j = jobs["1001"]
    assert j.mem_source == "tres"
    assert j.mem_used_b == 4_000_000 * 1024          # max over steps, extern ignored by size
    assert j.mem_eff == pytest.approx(4_000_000 * 1024 / (8000 * 1024 ** 2))
    b = jobs["1005"]
    assert b.mem_source == "maxrss"
    assert b.mem_eff == pytest.approx(500_000 * 1024 / (2 * 1024 ** 3))


def test_per_node_reqmem_is_multiplied(jobs):
    assert jobs["1003"].mem_req_b == 64 * 1024 ** 3


def test_gpu_fields(jobs):
    j = jobs["1002"]
    assert j.gpus == 2
    assert j.gpu_util == 72.0
    assert jobs["1001"].gpus == 0


def test_state_normalised_and_timelimit(jobs):
    assert jobs["1009"].state == "CANCELLED"
    assert jobs["1001"].timelimit_s == 240 * 60
    assert jobs["1001"].wall_eff == pytest.approx(0.5)


# ---------- window selection ----------

def _window(hours):
    return NOW - timedelta(hours=hours), NOW


def test_ended_mode_last_24h(jobs):
    start, end = _window(24)
    sel, info = metrics.select_jobs(list(jobs.values()), start, end, "ended",
                                    metrics.TERMINAL_STATES, False, NOW)
    ids = {j.raw_id for j in sel}
    assert ids == {"1001", "1002", "1003", "1005", "1006", "1009", "1011", "1012", "1013"}   # 1010 ended 28h ago; 1007 running; 1008 pending
    long_job = jobs["1003"]
    assert long_job.spans_start and long_job.multi_day and long_job.fraction == 1.0
    assert info["span_start"] == 1
    assert info["excluded_unfinished"] == 1                           # the running job
    assert info["excluded_unfinished_cpu_hours"] == pytest.approx(32 * 4)
    assert info["cpu_hours_before_window"] == pytest.approx(16 * 26)
    assert info["never_started"] == 1


def test_ended_mode_7d_includes_yesterdays_job(jobs):
    start, end = _window(24 * 7)
    sel, _ = metrics.select_jobs(list(jobs.values()), start, end, "ended",
                                 metrics.TERMINAL_STATES, False, NOW)
    assert "1010" in {j.raw_id for j in sel}


def test_completed_only_preset(jobs):
    start, end = _window(24)
    sel, _ = metrics.select_jobs(list(jobs.values()), start, end, "ended",
                                 metrics.STATE_PRESETS["completed"], False, NOW)
    assert {j.raw_id for j in sel} == {"1001", "1002", "1005", "1011", "1012", "1013"}


def test_prorate_mode_scales_consumption(jobs):
    start, end = _window(24)
    sel, info = metrics.select_jobs(list(jobs.values()), start, end, "prorate",
                                    metrics.TERMINAL_STATES, False, NOW)
    long_job = next(j for j in sel if j.raw_id == "1003")
    # 49.5 h long, 23.5 h of it inside the window
    assert long_job.fraction == pytest.approx(23.5 / 49.5)
    assert long_job.prorated
    assert long_job.cpu_hours_alloc == pytest.approx(16 * 23.5)
    assert long_job.cpu_eff == pytest.approx(1728000 / (178200 * 16))  # unchanged
    assert info["prorated"] == 1
    assert info["excluded_running"] == 1


def test_prorate_mode_can_include_running(jobs):
    start, end = _window(24)
    sel, _ = metrics.select_jobs(list(jobs.values()), start, end, "prorate",
                                 metrics.TERMINAL_STATES, True, NOW)
    run = next(j for j in sel if j.raw_id == "1007")
    assert run.spans_end and run.fraction == pytest.approx(1.0)


# ---------- aggregation ----------

def test_summary_totals(jobs):
    start, end = _window(24)
    sel, _ = metrics.select_jobs(list(jobs.values()), start, end, "ended",
                                 metrics.TERMINAL_STATES, False, NOW)
    s = metrics.summarize(sel)
    assert s["jobs"] == 9
    expected_alloc = (4 * 2 + 8 * 3 + 16 * 49.5 + 1 * .5 + 1 * .5 + 2 * 300 / 3600 + 8 * 1 + 8 * 1 + 1 * 1)
    assert s["cpu_hours_alloc"] == pytest.approx(expected_alloc)
    assert s["cpu_eff_weighted"] == pytest.approx(s["cpu_hours_used"] / s["cpu_hours_alloc"])
    assert s["gpu_jobs"] == 2 and s["gpu_hours"] == pytest.approx(7)
    assert s["gpu_util_weighted"] == pytest.approx((72 * 6 + 88 * 1) / 7)   # GPU-hour weighted
    assert sum(s["cpu_eff_hist"]) == 9
    assert s["mem_jobs_maxrss_fallback"] == 2
    assert s["states"] == {"CANCELLED": 1, "COMPLETED": 6, "FAILED": 1, "TIMEOUT": 1}


def test_group_by_partition(jobs):
    start, end = _window(24)
    sel, _ = metrics.select_jobs(list(jobs.values()), start, end, "ended",
                                 metrics.TERMINAL_STATES, False, NOW)
    groups = {g["name"]: g for g in metrics.group_summaries(sel, "partition")}
    assert set(groups) == {"cpu", "gpu"}
    assert groups["gpu"]["jobs"] == 2
    assert groups["cpu"]["jobs"] == 7


def test_sreport_table():
    text = open(os.path.join(os.path.dirname(__file__), "fixtures", "sreport_sample.txt")).read()
    rows = parse.sreport_table(text)
    assert rows[0]["Cluster"] == "demo" and rows[0]["Planned"] == "300000"


# ---------- typed GPUs (one-off H200 node) ----------

def test_h200_alloc_tres_is_recorded_by_type(jobs):
    j = jobs["1011"]
    assert j.gpus == 1
    assert j.gpu_types == {"h200": 1}
    assert j.gpu_type == "h200"
    assert j.gpu_types_consistent
    assert j.gpu_util == 88.0
    assert j.mem_req_b == 50 * 1024 ** 3
    # the A100 job in the fixture carries gres/gpu=2,gres/gpu:a100=2
    assert jobs["1002"].gpu_type == "a100"


def test_gpu_hours_by_type_and_breakdown(jobs):
    start, end = _window(24)
    sel, _ = metrics.select_jobs(list(jobs.values()), start, end, "ended",
                                 metrics.TERMINAL_STATES, False, NOW)
    s = metrics.summarize(sel)
    assert s["gpu_jobs"] == 2
    assert s["gpu_hours_by_type"] == {"a100": pytest.approx(6.0), "h200": pytest.approx(1.0)}
    assert s["gpu_hours"] == pytest.approx(7.0)
    assert s["gpu_jobs_inconsistent_type"] == 0
    by_type = {g["name"]: g for g in metrics.group_summaries(sel, "gpu_type")}
    assert set(by_type) == {"a100", "h200"}
    assert by_type["h200"]["gpu_util_weighted"] == 88.0
    assert by_type["h200"]["jobs"] == 1


def test_untyped_and_mixed_gpu_entries():
    assert parse.gpu_types(parse.parse_tres("gres/gpu=4")) == {"": 4}
    assert parse.gpu_type_label(parse.gpu_types(parse.parse_tres("gres/gpu=4"))) == "untyped"
    mixed = parse.gpu_types(parse.parse_tres("gres/gpu=3,gres/gpu:a100=2"))
    assert mixed == {"a100": 2, "": 1}
    assert parse.gpu_types(parse.parse_tres("gres/gpu:H200=2")) == {"h200": 2}


# ---------- memory efficiency above 100% ----------

def test_alloc_tres_mem_beats_per_cpu_reqmem_quirk(jobs):
    j = jobs["1012"]                     # ReqMem says 4G (per-CPU, no suffix); AllocTRES says 32G
    assert j.mem_alloc_source == "alloctres"
    assert j.mem_req_b == 32 * 1024 ** 3
    assert j.mem_eff == pytest.approx(7.0 / 32)        # 22%, not 175%
    assert not j.mem_over


def test_rss_overcount_is_flagged_and_capped_in_aggregates(jobs):
    j = jobs["1013"]                     # 7 GiB summed RSS against a 4 GiB allocation
    assert j.mem_over
    assert j.mem_eff == pytest.approx(1.75)             # raw value kept for the table
    assert j.mem_eff_capped == 1.0
    assert j.mem_gb_hours_used == pytest.approx(4.0)    # capped: 4 GiB x 1 h
    s = metrics.summarize([j])
    assert s["mem_eff_weighted"] == pytest.approx(1.0)
    assert s["mem_jobs_over"] == 1 and s["mem_over_max"] == pytest.approx(1.75)
    assert s["mem_eff_hist"][-1] == 1                   # lands in the 90-100% bin


def test_user_aggregate_cannot_exceed_100_percent(jobs):
    start, end = _window(24)
    sel, _ = metrics.select_jobs(list(jobs.values()), start, end, "ended",
                                 metrics.TERMINAL_STATES, False, NOW)
    for g in metrics.group_summaries(sel, "user"):
        assert g["mem_eff_weighted"] is None or g["mem_eff_weighted"] <= 1.0
    svc = next(g for g in metrics.group_summaries(sel, "user") if g["name"] == "svc_airflow")
    # (7 GiB of 32 + 4 of 4 capped) x 1 h each
    assert svc["mem_eff_weighted"] == pytest.approx((7 + 4) / (32 + 4))


def test_reqmem_fallback_when_alloctres_has_no_mem():
    rows = parse.sacct_rows("77|77|u|a|p|COMPLETED|2026-10-28T01:00:00|2026-10-28T02:00:00|3600|01:00:00|2|1|2Gc||cpu=2,node=1||||60|0:0|x\n", SACCT_FIELDS)
    j = metrics.build_jobs(rows)[0]
    assert j.mem_alloc_source == "reqmem" and j.mem_req_b == 4 * 1024 ** 3


# ---------- GPU utilization: parsing breadth and diagnosis ----------

def _gpu_rows(ave, mx, tot, steps=True):
    row = "900|900|u|a|gpu|COMPLETED|2026-10-28T01:00:00|2026-10-28T03:00:00|7200|02:00:00|4|1|16G||cpu=4,mem=16G,node=1,gres/gpu=1,gres/gpu:h200=1||||120|0:0|g\n"
    if steps:
        row += f"900.batch|900.batch|||  |COMPLETED|2026-10-28T01:00:00|2026-10-28T03:00:00|7200|02:00:00|4|1|16G|1000000K|cpu=4,mem=16G,node=1,gres/gpu=1|{tot}|{ave}|{mx}||0:0|batch\n"
    return parse.sacct_rows(row, SACCT_FIELDS)


def test_gpuutil_read_from_any_tresusage_field():
    assert metrics.build_jobs(_gpu_rows("cpu=01:00:00,mem=1000000K,gres/gpuutil=55", "", ""))[0].gpu_util == 55.0
    assert metrics.build_jobs(_gpu_rows("cpu=01:00:00,mem=1000000K", "gres/gpumem=1G,gres/gpuutil=61", ""))[0].gpu_util == 61.0
    assert metrics.build_jobs(_gpu_rows("", "", "gres/gpuutil=40"))[0].gpu_util == 40.0
    assert metrics.build_jobs(_gpu_rows("gres/gpuutil:h200=77", "", ""))[0].gpu_util == 77.0     # typed key


def test_gpu_util_diagnosis_identifies_untracked_tres():
    jobs = metrics.build_jobs(_gpu_rows("cpu=01:00:00,mem=1000000K,energy=0", "cpu=01:00:00,mem=1000000K", "cpu=01:00:00,mem=1000000K"))
    d = metrics.gpu_util_diagnosis(jobs)
    assert "gres/gpuutil is not among the tracked TRES" in d["verdict"]
    assert "AccountingStorageTRES" in d["fix"]
    assert d["job"] == "900" and d["sample"][0]["step"] == "900.batch"


def test_gpu_util_diagnosis_no_usage_and_no_steps():
    assert "not storing usage" in metrics.gpu_util_diagnosis(metrics.build_jobs(_gpu_rows("", "", "")))["verdict"]
    assert "no step records" in metrics.gpu_util_diagnosis(metrics.build_jobs(_gpu_rows("", "", "", steps=False)))["verdict"]


def test_gpu_util_diagnosis_is_none_when_util_exists(jobs):
    assert metrics.gpu_util_diagnosis(list(jobs.values())) is None            # fixture GPU jobs report 72 and 88
    assert metrics.gpu_util_diagnosis([jobs["1001"]]) is None                 # no GPU jobs at all


# ---------- GPU occupancy against available GPU-hours ----------

def test_gpu_occupancy_counts_in_window_overlap_for_all_gpu_jobs(jobs):
    start, end = _window(24)
    occ = metrics.gpu_occupancy(list(jobs.values()), start, end, NOW)
    # 1002: 2 GPUs x 3 h; 1011: 1 GPU x 1 h   (no running GPU jobs in the fixture)
    assert occ["jobs"] == 2
    assert occ["gpu_hours"] == pytest.approx(7.0)
    assert occ["by_type"] == {"a100": pytest.approx(6.0), "h200": pytest.approx(1.0)}


def test_gpu_occupancy_clips_to_window_and_includes_running():
    from datetime import timedelta
    j = metrics.Job(id="5", raw_id="5", name="n", user="u", account="a", partition="gpu", state="RUNNING",
                    start=NOW - timedelta(hours=30), end=None, elapsed_s=30 * 3600, alloc_cpus=1, alloc_nodes=1,
                    cpu_used_s=0, mem_req_b=None, mem_alloc_source=None, mem_used_b=None, mem_source=None,
                    gpus=4, gpu_types={"": 4}, gpu_util=None, timelimit_s=None, exit_code="")
    occ = metrics.gpu_occupancy([j], NOW - timedelta(hours=24), NOW, NOW)
    assert occ["gpu_hours"] == pytest.approx(4 * 24)         # only the 24 h inside the window
    assert occ["by_type"] == {"": pytest.approx(96.0)}


def test_parse_gres_gpus():
    from jobutil import slurm
    assert slurm.parse_gres_gpus("gpu:h200:1") == {"h200": 1}
    assert slurm.parse_gres_gpus("gpu:4(S:0-1)") == {"": 4}
    assert slurm.parse_gres_gpus("gpu:a100:4,shard:16") == {"a100": 4}
    assert slurm.parse_gres_gpus("(null)") == {}
