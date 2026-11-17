"""Independent arithmetic checks; run only after measurements have finished."""
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_groups(rows, summaries, field):
    groups = defaultdict(list)
    for row in rows:
        assert not row.get("error"), row
        groups[(row["case"], row["phase"])].append(row[field])
    assert len(groups) == len(summaries)
    for summary in summaries:
        values = sorted(groups[(summary["case"], summary["phase"])])
        assert len(values) == summary["count"] == 20
        assert round(statistics.median(values), 3) == summary["median_ms"]
        assert round(values[math.ceil(len(values) * .95) - 1], 3) == summary["p95_ms"]
        assert round(max(values), 3) == summary["max_ms"]
    return len(groups)


def check_run(directory):
    run, summary, api, browser = (load(directory / name) for name in
                                  ("run.json", "summary.json", "api.json", "browser.json"))
    assert run["status"] == summary["status"] == browser["status"] == "completed"
    assert run["exit_code"] == 0
    assert run["run_id"] == summary["run_id"] == api["run_id"] == browser["run_id"]
    assert not summary["errors"] and not api["errors"] and not browser["errors"]
    assert len(api["results"]) == 240 and len(browser["results"]) == 180
    groups = check_groups(api["results"], summary["api"], "elapsed_ms")
    groups += check_groups(browser["results"], summary["pages_first_data"], "first_data_ms")
    groups += check_groups(browser["results"], summary["pages_complete"], "complete_ms")
    return run, summary, groups


def native(root):
    directory = root / "docs/optimization/baselines/T00-revision1/native-delivery"
    run, summary, groups = check_run(directory)
    env = load(directory / "environment.json")
    mismatches = [name for name, expected in env["input_sha256"].items()
                  if sha(root / name) != expected]
    assert not mismatches, mismatches
    assert env["git_head"]["stdout"] == "79d46dfbceff86fefec89c12db357ee03bfe1701"
    probe = directory.parent / "timeout-probe"
    prun, pbrowser, psummary = (load(probe / name) for name in ("run.json", "browser.json", "summary.json"))
    assert prun["status"] == pbrowser["status"] == psummary["status"] == "failed"
    assert prun["exit_code"] != 0 and len(pbrowser["results"]) > 0
    probe_env = load(probe / "environment.json")
    probe_files = ["tools/optimization/" + name for name in ("run_baseline.py", "processes.py", "browser_benchmark.mjs", "progress_report.mjs")]
    assert all(sha(root / name) == probe_env["input_sha256"][name] for name in probe_files)
    receipt = {"base": "024d9d5664fa60a49c4710d129de504bfffbce2c",
               "audited_head": "ca23f465843a327539d3f1360c39bb7975db03bd",
               "tool_head": env["git_head"]["stdout"], "native_run": run,
               "native_input_fingerprints_checked": len(env["input_sha256"]),
               "native_input_mismatches": mismatches, "statistics_groups_checked": groups,
               "native_api_samples": 240, "native_page_samples": 180,
               "timeout_probe_run": prun, "timeout_completed_samples_preserved": len(pbrowser["results"]),
               "timeout_tested_core_fingerprints_match": probe_files,
               "timeout_probe_tool_fingerprint": load(probe / "environment.json")["input_sha256"]["tools/optimization/run_baseline.py"],
               "native_docker_metrics": "unmeasured; Linux evidence is recorded separately"}
    (directory.parent / "verification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


def containers(root):
    receipts, envs, all_images, inspections = {}, {}, {}, {}
    for label in ("base", "candidate"):
        directory = root / f"container-{label}-delivery"
        run, summary, groups = check_run(directory)
        env = load(directory / "environment.json")
        images = load(directory / "images.json")
        assert summary["image_total_bytes"] == sum(i["size_bytes"] for i in images.values())
        assert not load(directory / "cleanup.json")["errors"]
        raw = [json.loads(line) for line in (directory / "cgroup-samples.jsonl").read_text().splitlines()]
        checks = 0
        for name in ("backend", "frontend"):
            for window, start_key, end_key in (("idle", "idle_start", "idle_end"), ("workload", "work_start", "work_end")):
                start, end = summary["windows"][start_key], summary["windows"][end_key]
                samples = [start[name]] + [r[name] for r in raw if start["at_monotonic"] < r["at_monotonic"] < end["at_monotonic"]] + [end[name]]
                wall = samples[-1]["at_monotonic"] - samples[0]["at_monotonic"]
                cpu = samples[-1]["cpu_seconds"] - samples[0]["cpu_seconds"]
                calculated = {"wall_seconds": wall, "cpu_seconds": cpu,
                              "one_core_cpu_percent": cpu / wall * 100,
                              "peak_working_set_bytes": max(r["working_set_bytes"] for r in samples),
                              "peak_memory_current_bytes": max(r["memory_current_bytes"] for r in samples),
                              "mean_working_set_bytes": sum(r["working_set_bytes"] for r in samples) / len(samples),
                              "samples": len(samples)}
                assert calculated == summary["resources"][name][window]
                checks += 1
        file_manifest = {str(p.relative_to(directory)): {"bytes": p.stat().st_size, "sha256": sha(p)}
                         for p in sorted(directory.rglob("*")) if p.is_file()}
        receipts[label] = {"run": run, "statistics_groups_checked": groups,
                           "resource_windows_checked": checks, "raw_cgroup_samples": len(raw),
                           "resources": summary["resources"], "image_total_bytes": summary["image_total_bytes"],
                           "pages_first_data": [{k: s[k] for k in ("case", "phase", "count", "median_ms", "p95_ms")} for s in summary["pages_first_data"]],
                           "pages_complete": [{k: s[k] for k in ("case", "phase", "count", "median_ms", "p95_ms")} for s in summary["pages_complete"]],
                           "database": summary["database"], "evidence_files": file_manifest}
        envs[label] = env
        all_images[label] = images
        inspections[label] = load(directory / "image-inspect.json")
    assert envs["base"]["production_sha256"] == envs["candidate"]["production_sha256"]
    assert envs["base"]["tool_sha256"] == envs["candidate"]["tool_sha256"]
    for name in ("backend", "frontend"):
        assert {k: v for k, v in all_images["base"][name].items() if k not in ("tag", "id")} == {k: v for k, v in all_images["candidate"][name].items() if k not in ("tag", "id")}
        for field in ("Architecture", "Os", "Config", "RootFS", "Size", "Created"):
            assert inspections["base"][name][field] == inspections["candidate"][name][field]
    for label in receipts:
        expected = "024d9d5664fa60a49c4710d129de504bfffbce2c" if label == "base" else "ca23f465843a327539d3f1360c39bb7975db03bd"
        assert envs[label]["source_commit"] == expected
    data = {"machine_id": "m_agqymokr64sggvjgxypiphnoke", "remote_root": str(root),
            "tool_head": "79d46dfbceff86fefec89c12db357ee03bfe1701", "runs": receipts,
            "paired_same_production_files": len(envs["base"]["production_sha256"]),
            "paired_same_tool_files": len(envs["base"]["tool_sha256"]),
            "paired_images": all_images,
            "paired_equal_image_runtime_fields": ["Architecture", "Os", "Config", "RootFS", "Size", "Created"],
            "environment": {k: v for k, v in envs["base"].items() if k not in ("production_sha256", "tool_sha256")},
            "tool_sha256": envs["base"]["tool_sha256"],
            "source_archives": {label: envs[label]["source_archive_sha256"] for label in envs}}
    path = root / "container-verification.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps({"receipt": str(path), "sha256": sha(path), "bytes": path.stat().st_size,
                      "statistics_groups_checked": 60, "resource_windows_checked": 8}))


if __name__ == "__main__":
    {"native": native, "containers": containers}[sys.argv[1]](Path(sys.argv[2]).resolve())
