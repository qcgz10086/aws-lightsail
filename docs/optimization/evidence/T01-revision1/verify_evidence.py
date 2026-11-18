"""Verify the local T01 repair evidence offline. Run from an ordinary Git checkout."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


EVIDENCE = Path(__file__).resolve().parent
ROOT = EVIDENCE.parents[3]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(name):
    return json.loads((EVIDENCE / name).read_bytes())


def git(*args):
    return subprocess.check_output(["git", "-c", f"safe.directory={ROOT.as_posix()}", *args], cwd=ROOT)


def check(ok, description):
    if not ok:
        raise ValueError(description)


def check_probe(platform, phase):
    rows = {r["probe"]: r for r in load(f"{platform}-boundaries-{phase}.json")["observations"]}
    wal, fk, seq, ix, ledger = [rows[k] for k in ("snapshot_output_sidecar", "custom_inline_foreign_key",
        "custom_autoincrement", "legacy_explicit_unique_index", "ledger_required_columns")]
    before = phase == "before"
    allow_second_credential = not before
    check(all(r["accepted"] is before for r in (wal, fk, seq, ledger)), f"{platform} {phase}: refusal")
    check(ix["accepted"] and ix["second_credential_same_instance_accepted"] is allow_second_credential
          and ix["second_credential_same_traffic_accepted"] is allow_second_credential, f"{platform} {phase}: writes")
    check(seq["sequence_before"] == [["aws_credentials", 100]] and (fk["before"] == fk["after"]) != before,
          f"{platform} {phase}: schema")
    if before:
        check(wal["cli_exit_code"] == 0 and wal["source_rows"] == [[1, "trusted"]]
              and wal["snapshot_rows_on_open"] == [[1, "unrelated-old-data"]]
              and not wal["hash_still_matches_after_open"] and seq["new_id"] == 8
              and fk["unapproved_label_accepted"], f"{platform}: original defects")
    else:
        check("sidecar" in wal["error"] and "custom foreign key" in fk["cause"]
              and "manual review" in seq["cause"] and "ledger schema" in ledger["cause"],
              f"{platform}: candidate error causes")


def main():
    index = load("index.json")
    for name, info in index["files"].items():
        data = (EVIDENCE / name).read_bytes()
        check(len(data) == info["bytes"] and sha(data) == info["sha256"], name)
    code = index["implementation_head"]
    check(git("rev-parse", code + "^").decode().strip() == index["repair_base"], "repair BASE")
    check(not git("diff", "--name-only", code, "HEAD", "--", "backend", "frontend"), "candidate source at HEAD")
    manifest = load("source-manifest.json")
    paths = git("ls-tree", "-r", "--name-only", code, "--", "backend/app", "backend/tests").decode().splitlines()
    check(set(manifest) == {p for p in paths if p.endswith(".py")}, "source file set")
    for path, digest in manifest.items():
        check(sha(git("cat-file", "blob", f"{code}:{path}")) == digest, path)
    driver = "docs/optimization/evidence/T01/validate_cli.py"
    check(sha(git("cat-file", "blob", f"HEAD:{driver}")) == index["cli_driver_sha256"], driver)
    with tarfile.open(EVIDENCE / "linux-evidence.tar.gz", "r:gz") as archive:
        expected = index["linux_archive_members"]
        check(len(archive.getnames()) == len(expected) and set(archive.getnames()) == set(expected), "archive list")
        linux = {}
        for name, info in expected.items():
            data = archive.extractfile(name).read()
            check(len(data) == info["bytes"] and sha(data) == info["sha256"], name)
            if (EVIDENCE / name).is_file():
                check((EVIDENCE / name).read_bytes() == data, name + " local copy")
            linux[name] = data
    for platform in ("windows", "linux"):
        runs = load(platform + "-runs.json")
        runs = runs["runs"] if platform == "windows" else runs
        for suite, count in (("migrations", 34), ("tests", 169)):
            name = f"{platform}-{suite}"
            data = (EVIDENCE / (name + ".txt")).read_bytes() if platform == "windows" else linux[name + ".txt"]
            check(runs[name]["exit"] == 0 and runs[name]["tests"] == count and sha(data) == runs[name]["sha256"]
                  and f"Ran {count} tests".encode() in data and data.rstrip().endswith(b"OK"), name)
        cli = load(platform + "-cli.json")["cli_smoke"]
        check([r["exit_code"] for r in cli["commands"]] == [r["expected_exit_code"] for r in cli["commands"]]
              == [0, 0, 1, 1, 0, 0] and cli["restored_committed_wal"] and cli["preserved_pre_restore_data"],
              platform + " CLI")
        for phase in ("before", "after"):
            check_probe(platform, phase)
    print(f"Verified {len(index['files'])} evidence files, {len(manifest)} Git source blobs, "
          f"{len(linux)} Linux archive members, and both platforms' recorded runs and probe observations.")


if __name__ == "__main__":
    main()
