"""Recompute paired page, image and cgroup acceptance metrics.

Input: two container result folders using T00-compatible raw report fields.
Timing validity (new-data revision / scope completion) requires separate review.
Adapted from the independent audit arithmetic checker; this copy is an
implementation check and never substitutes for independent acceptance.
"""
import argparse
import json
import math
from pathlib import Path
import statistics

def load(path): return json.loads(path.read_text(encoding="utf-8"))

def groups(rows, field):
    grouped={}
    for row in rows:
        if row.get("error"):
            raise ValueError("Failed raw sample cannot enter successful statistics")
        value=row.get(field)
        if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError(f"Missing/nonpositive {field} raw sample")
        grouped.setdefault((row["case"],row["phase"]),[]).append((row["sample"],value))
    result={}
    for key, values in grouped.items():
        if len(values)!=20 or sorted(i for i,_ in values)!=list(range(20)):
            raise ValueError(f"Exactly 20 distinct samples required for {key}")
        ordered=sorted(value for _,value in values)
        result[key]={"samples":len(values),"median_ms":statistics.median(ordered),"p95_ms":ordered[math.ceil(0.95*len(ordered))-1],"raw_ms":[value for _,value in sorted(values)]}
    return result

def resource(folder, service, window):
    summary=load(folder/"summary.json")
    if window=="work" and "active_start" in summary["windows"]:
        window="active"
    start=summary["windows"][window+"_start"]
    end=summary["windows"][window+"_end"]
    raw_path=folder/("cgroup-samples.jsonl" if (folder/"cgroup-samples.jsonl").exists() else "cgroup.jsonl")
    raw=[json.loads(line) for line in raw_path.read_text().splitlines() if line.strip()]
    stamp=lambda row:row["at_monotonic"] if "at_monotonic" in row else row["time"]
    rows=[start[service]]+[row[service] for row in raw if stamp(start)<stamp(row)<stamp(end)]+[end[service]]
    times=[row["at_monotonic"] for row in rows]
    if len(rows)<2 or any(right<=left for left,right in zip(times,times[1:])):
        raise ValueError("Resource timestamps must be strictly increasing")
    values=[]
    for row in rows:
        if "inactive_file_bytes" in row:
            value=max(0,row["memory_current_bytes"]-row["inactive_file_bytes"])
            if value!=row["working_set_bytes"]: raise ValueError("Working-set arithmetic mismatch")
        else:
            value=row["working_set_bytes"]
        if not 0<=value<=row["memory_current_bytes"]:raise ValueError("Invalid resource reading")
        values.append(value)
    wall=times[-1]-times[0]
    cpu=rows[-1]["cpu_seconds"]-rows[0]["cpu_seconds"]
    if cpu<0:raise ValueError("CPU counter moved backwards")
    return {"samples":len(rows),"seconds":wall,"mean_working_set_bytes":statistics.mean(values),
        "peak_working_set_bytes":max(values),"cpu_one_core_percent":cpu/wall*100}

def read_run(folder):
    run=load(folder/"run.json")
    browser=load(folder/"browser.json")
    summary=load(folder/"summary.json")
    environment=load(folder/"environment.json")
    if run["status"]!="completed" or run.get("exit_code")!=0 or browser["status"]!="completed" or browser.get("errors") or summary.get("errors"):
        raise ValueError("Run must be complete with zero recorded errors")
    if not run["run_id"]==browser["run_id"]==summary["run_id"]==environment["run_id"]:
        raise ValueError("Evidence run identities differ")
    rows=browser["results"]
    for row in rows:
        if row.get("violations") or row.get("failures") or row.get("page_errors"):
            raise ValueError("Sample contains errors or isolation violations")
    first,complete=groups(rows,"first_data_ms"),groups(rows,"complete_ms")
    expected={(case,phase) for case in ["dashboard","instances","billing"] for phase in ["first_visit","repeat_visit","refresh_button","spa_return"]}
    if expected != set(first):raise ValueError("Expected exactly the 12 required page scenarios")
    image_total=sum(image["size_bytes"] for image in load(folder/"images.json").values())
    if len(load(folder/"images.json"))!=2:raise ValueError("Expected exactly two production images")
    return {"run":run,"environment":environment,"browser":browser,"first":first,"complete":complete,
        "image_total":image_total,"idle":resource(folder,"backend","idle"),"workload":resource(folder,"backend","work")}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("base",type=Path);parser.add_argument("candidate",type=Path);parser.add_argument("output",type=Path)
    args=parser.parse_args()
    base,candidate=(read_run(path) for path in [args.base,args.candidate])
    fixture_hash=lambda run:run["browser"].get("fixture_sha256") or run["browser"]["fixture"]["fixture_sha256"]
    checks={"same_fixture":fixture_hash(base)==fixture_hash(candidate),
        "same_browser":base["browser"]["browser"]==candidate["browser"]["browser"],
        "same_base_images":load(args.base/"base-images.json")==load(args.candidate/"base-images.json"),
        "same_tool_fingerprints":base["environment"]["tool_sha256"]==candidate["environment"]["tool_sha256"],
        "original_project_base":base["run"]["source_commit"]=="024d9d5664fa60a49c4710d129de504bfffbce2c",
        "image_total_not_increased":candidate["image_total"]<=base["image_total"],
        "idle_delta_le_32_mib":candidate["idle"]["mean_working_set_bytes"]-base["idle"]["mean_working_set_bytes"]<=32*1024*1024,
        "peak_delta_le_64_mib":candidate["workload"]["peak_working_set_bytes"]-base["workload"]["peak_working_set_bytes"]<=64*1024*1024}
    ttl_rows=[r for r in candidate['browser']['results'] if r['phase'] in ['repeat_visit','spa_return']]
    checks['ttl_same_scope_aws_zero']=all(not r.get('aws_events') and sum(r['aws_calls'].values())==0 for r in ttl_rows)
    checks['aws_idle_at_phase_completion']=all(r.get('active_aws_calls')==0 for r in candidate['browser']['results'])
    pages=[]
    for key,old in base["first"].items():
        new=candidate["first"][key]
        row={"case":key[0],"phase":key[1],"base_first":old,"candidate_first":new,
            "base_complete":base["complete"][key],"candidate_complete":candidate["complete"][key]}
        row["complete_not_slower"]=row["candidate_complete"]["median_ms"]<=row["base_complete"]["median_ms"]
        if key[1]=="first_visit":row["cold_target"]=new["median_ms"]<=old["median_ms"]*(0.7 if old["median_ms"]>=1000 else 1)
        if key[1] in ["repeat_visit","spa_return"]:row["hot_p95_le_1s"]=new["p95_ms"]<=1000
        pages.append(row)
    checks["page_targets"]=all(row[name] for row in pages for name in ["complete_not_slower","cold_target","hot_p95_le_1s"] if name in row)
    report={"base":base["run"]["source_commit"],"candidate":candidate["run"]["source_commit"],"checks":checks,"pages":pages,
        "resources":{label:{key:run[key] for key in ["image_total","idle","workload"]} for label,run in [("base",base),("candidate",candidate)]},
        "metrics_passed":all(checks.values()),"boundary":"Arithmetic only. Semantic new-data readiness, zero same-scope TTL AWS, workload equivalence and image contents must be independently reviewed."}
    args.output.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({"checks":checks,"metrics_passed":report["metrics_passed"]},indent=2))
    return 0 if report["metrics_passed"] else 1

if __name__=="__main__":raise SystemExit(main())
