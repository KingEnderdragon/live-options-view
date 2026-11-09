#!/usr/bin/env python3
"""Prove Clyde's offline catalogue and account-assignment core (issue #25).

Hermetic like verify_invariants.py: no network access, no real accounts.json.
All assignment/recovery demonstrations run on isolated tempfile fixtures.
Secrets are never printed; tests assert they survive assignment with
values and ordering preserved (serialization may reformat whitespace).

    python scripts/verify_clyde.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clyde  # noqa: E402  (scripts/ is sys.path[0] when run as a script)

# Windows consoles default to cp1252, which cannot encode the [✓]/[✗]
# markers the repo's verify scripts use; force UTF-8 stdout (no-op on
# POSIX, safe fallback on failure).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [✓] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [✗] {name}" + (f" -- {detail}" if detail else ""))


def fixture_accounts() -> dict:
    """Isolated accounts fixture: secrets, params, ordering, comments, active flags."""
    return {
        "_comment": "isolated fixture -- never a real accounts.json",
        "accounts": [
            {"alias": "Alice", "username": "crassus_alice",
             "password": "SECRET-ALICE-DO-NOT-PRINT",
             "strategy_id": "smoke_atm_roundtrip",
             "params": {"note": "keep me"}},
            {"alias": "Bob", "username": "crassus_bob",
             "password_env": "CRASSUS_PW_BOB",
             "strategy_id": "reddit_sentiment_qqq"},
            {"alias": "Carol", "username": "crassus_carol",
             "password_env": "CRASSUS_PW_CAROL",
             "strategy_id": "smoke_atm_roundtrip",
             "active": False},
        ],
    }


def write_fixture(tmp: Path, data: dict | None = None) -> Path:
    path = tmp / "accounts.json"
    path.write_text(json.dumps(data if data is not None else fixture_accounts(), indent=2) + "\n",
                    encoding="utf-8")
    return path


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_catalogue() -> None:
    entries = clyde.catalogue_entries()
    cats = {e["category"] for e in entries}
    check("catalogue has all four categories", cats == {"registered", "candidate", "overlay", "research"},
          f"found {sorted(cats)}")
    registered = [e for e in entries if e["category"] == "registered"]
    check("registered entries discovered from REGISTRY",
          [e["name"] for e in registered] == clyde.registered_strategy_ids(),
          f"{len(registered)} registered")
    check("registered includes smoke_atm_roundtrip",
          "smoke_atm_roundtrip" in clyde.registered_strategy_ids())
    non_assignable = [e for e in entries if e["category"] != "registered"]
    check("every non-assignable entry has a fit note",
          all(e.get("fit") for e in non_assignable), f"{len(non_assignable)} entries")
    check("candidate HourBasedStrategy present",
          clyde.catalogue_category("HourBasedStrategy") == "candidate")
    check("overlay BreakEven present",
          clyde.catalogue_category("BreakEven") == "overlay")
    check("research FX Carry Trade present",
          clyde.catalogue_category("FX Carry Trade") == "research")
    check("unknown name has no category",
          clyde.catalogue_category("no_such_strategy") is None)


def test_selection_and_assign(tmp: Path) -> None:
    path = write_fixture(tmp)
    data = clyde.load_accounts_file(path)
    # One account.
    new_data, changed = clyde.assign_strategy(data, "momentum_qqq", ["Alice"])
    check("one-account assign changes only that alias", changed == ["Alice"])
    check("one-account assign sets strategy_id",
          new_data["accounts"][0]["strategy_id"] == "momentum_qqq")
    check("one-account assign leaves others untouched",
          new_data["accounts"][1]["strategy_id"] == "reddit_sentiment_qqq")
    # Multiple accounts.
    new_data, changed = clyde.assign_strategy(data, "momentum_qqq", ["Alice", "Bob"])
    check("multi-account assign changes both", changed == ["Alice", "Bob"])
    # All accounts (including inactive Carol -- assignment is a config edit,
    # activation is a separate concern).
    new_data, changed = clyde.assign_strategy(data, "momentum_qqq", None, all_accounts=True)
    check("all-accounts assign changes all three", changed == ["Alice", "Bob", "Carol"])
    # Already assigned: no change reported.
    new_data, changed = clyde.assign_strategy(data, "smoke_atm_roundtrip", ["Alice"])
    check("already-assigned reports no change", changed == [])
    # Unknown alias rejected.
    try:
        clyde.assign_strategy(data, "momentum_qqq", ["Zed"])
        check("unknown alias rejected", False)
    except ValueError as exc:
        check("unknown alias rejected", "Unknown account alias" in str(exc))


def test_refusals_leave_file_unchanged(tmp: Path) -> None:
    for name in ("HourBasedStrategy", "BreakEven", "FX Carry Trade", "no_such_strategy"):
        path = write_fixture(tmp)
        before = path.read_bytes()
        data = clyde.load_accounts_file(path)
        try:
            clyde.assign_strategy(data, name, ["Alice"])
            check(f"refuse {name!r}", False)
        except ValueError:
            check(f"refuse {name!r}", path.read_bytes() == before,
                  "file unchanged")
    # Category-specific refusal message for a candidate.
    path = write_fixture(tmp)
    data = clyde.load_accounts_file(path)
    try:
        clyde.assign_strategy(data, "Supertrend", ["Alice"])
        check("candidate refusal names the category", False)
    except ValueError as exc:
        check("candidate refusal names the category", "candidate" in str(exc))


def test_malformed_leaves_file_unchanged(tmp: Path) -> None:
    # Bad JSON.
    path = tmp / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    before = path.read_bytes()
    try:
        clyde.load_accounts_file(path)
        check("bad JSON rejected", False)
    except ValueError:
        check("bad JSON rejected", path.read_bytes() == before, "file unchanged")
    # Missing accounts list.
    path = tmp / "noaccounts.json"
    path.write_text(json.dumps({"alias": "x"}) + "\n", encoding="utf-8")
    before = path.read_bytes()
    try:
        clyde.load_accounts_file(path)
        check("missing accounts list rejected", False)
    except ValueError:
        check("missing accounts list rejected", path.read_bytes() == before, "file unchanged")
    # Missing file.
    try:
        clyde.load_accounts_file(tmp / "does-not-exist.json")
        check("missing file rejected", False)
    except FileNotFoundError:
        check("missing file rejected", True)
    # Entry missing strategy_id fails validation (write refused).
    data = fixture_accounts()
    del data["accounts"][0]["strategy_id"]
    problems = clyde.validate_accounts(data)
    check("entry missing strategy_id fails validation", len(problems) > 0, str(problems))


def test_dry_run_validation_and_mutual_exclusion(tmp: Path) -> None:
    # Dry-run must run the SAME registry/selection validation as apply:
    # candidate/overlay/research/unknown strategies fail with exit 2 and
    # leave the file byte-unchanged.
    for name in ("HourBasedStrategy", "BreakEven", "FX Carry Trade", "no_such_strategy"):
        path = write_fixture(tmp)
        before = path.read_bytes()
        rc = clyde.main(["assign", "--strategy", name, "--account", "Alice",
                         "--accounts", str(path), "--dry-run"])
        check(f"dry-run refuse {name!r} (exit 2, unchanged)",
              rc == 2 and path.read_bytes() == before)
    # Dry-run with an unknown alias also fails.
    path = write_fixture(tmp)
    before = path.read_bytes()
    rc = clyde.main(["assign", "--strategy", "momentum_qqq", "--account", "Zed",
                     "--accounts", str(path), "--dry-run"])
    check("dry-run unknown alias refused (exit 2, unchanged)",
          rc == 2 and path.read_bytes() == before)
    # --account and --all are mutually exclusive: refused, no mutation.
    path = write_fixture(tmp)
    before = path.read_bytes()
    rc = clyde.main(["assign", "--strategy", "momentum_qqq", "--account", "Alice",
                     "--all", "--accounts", str(path)])
    check("--account + --all mutually exclusive (exit 2, unchanged)",
          rc == 2 and path.read_bytes() == before)
    # Same for dry-run with both flags.
    rc = clyde.main(["assign", "--strategy", "momentum_qqq", "--account", "Alice",
                     "--all", "--accounts", str(path), "--dry-run"])
    check("--account + --all + --dry-run refused (exit 2, unchanged)",
          rc == 2 and path.read_bytes() == before)


def test_typed_invalid_leaves_file_unchanged(tmp: Path) -> None:
    # Malformed configuration is refused even when the bad entry is
    # UNSELECTED: the file is replaced as a unit, so a typed-invalid
    # username/strategy_id anywhere blocks both apply and dry-run.
    cases = [
        ("username", 123),
        ("strategy_id", ["invalid"]),
        ("alias", 42),
        ("strategy_id", ""),
        ("username", None),
    ]
    for field, value in cases:
        for dry_run in (False, True):
            path = write_fixture(tmp)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["accounts"][1][field] = value
            path.write_text(json.dumps(data), encoding="utf-8")
            before = path.read_bytes()
            argv = ["assign", "--strategy", "momentum_qqq", "--account", "Alice",
                    "--accounts", str(path)]
            if dry_run:
                argv.append("--dry-run")
            rc = clyde.main(argv)
            check(f"typed-invalid {field}={value!r} "
                  f"{'dry-run' if dry_run else 'apply'} refused (exit 2, unchanged)",
                  rc == 2 and path.read_bytes() == before)
    # Duplicate identities are ambiguous and refused (alias and username).
    for dup_field in ("alias", "username"):
        path = write_fixture(tmp)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["accounts"][1][dup_field] = data["accounts"][0][dup_field]
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes()
        rc = clyde.main(["assign", "--strategy", "momentum_qqq",
                         "--account", "Alice", "--accounts", str(path)])
        check(f"duplicate {dup_field} refused (exit 2, unchanged)",
              rc == 2 and path.read_bytes() == before)


def test_preservation_and_atomicity(tmp: Path) -> None:
    path = write_fixture(tmp)
    before = read(path)
    backup = tmp / "accounts.json.bak"
    # Apply via the real write path (not just the pure function).
    data = clyde.load_accounts_file(path)
    new_data, changed = clyde.assign_strategy(data, "momentum_qqq", ["Alice", "Bob"])
    clyde.write_accounts_atomic(path, new_data, backup_path=backup)
    after = read(path)
    check("backup holds the pre-change bytes",
          read(backup) == before, "backup matches original")
    # Only strategy_id changed, for the selected accounts only.
    check("only strategy_id changed (Alice)",
          after["accounts"][0]["strategy_id"] == "momentum_qqq"
          and after["accounts"][0].get("password") == "SECRET-ALICE-DO-NOT-PRINT"
          and after["accounts"][0].get("params") == {"note": "keep me"})
    check("only strategy_id changed (Bob)",
          after["accounts"][1]["strategy_id"] == "momentum_qqq"
          and after["accounts"][1].get("password_env") == "CRASSUS_PW_BOB")
    check("unselected account untouched (Carol)",
          after["accounts"][2] == before["accounts"][2])
    check("top-level comment and ordering preserved",
          after.get("_comment") == before.get("_comment")
          and [a["alias"] for a in after["accounts"]] == ["Alice", "Bob", "Carol"])
    check("no temp files left behind",
          not list(tmp.glob(".*.tmp")), "temp cleaned up")
    # Dry-run preview prints changes but writes nothing.
    path2 = write_fixture(tmp)
    before2 = path2.read_bytes()
    data2 = clyde.load_accounts_file(path2)
    buf = io.StringIO()
    with redirect_stdout(buf):
        for line in clyde.preview_changes(data2, "momentum_qqq", ["Alice"]):
            print(line)
    out = buf.getvalue()
    check("dry-run preview names the change",
          "Alice" in out and "smoke_atm_roundtrip -> momentum_qqq" in out)
    check("dry-run preview prints no secret",
          "SECRET-ALICE-DO-NOT-PRINT" not in out)
    check("dry-run leaves the file unchanged", path2.read_bytes() == before2)
    # Interrupted write: fault-inject a failure at the atomic rename step.
    # (A read-only directory does not stop root, so inject the failure
    # directly.) The original must be untouched and no temp file left.
    path3 = write_fixture(tmp)
    before3 = path3.read_bytes()
    data3 = clyde.load_accounts_file(path3)
    new_data3, _ = clyde.assign_strategy(data3, "momentum_qqq", ["Alice"])
    real_replace = clyde.os.replace
    def failing_replace(src, dst):
        raise OSError("injected rename failure")
    clyde.os.replace = failing_replace
    try:
        clyde.write_accounts_atomic(path3, new_data3, backup_path=None)
        check("interrupted write leaves original intact", False)
    except OSError:
        check("interrupted write leaves original intact",
              path3.read_bytes() == before3)
    finally:
        clyde.os.replace = real_replace
    check("interrupted write leaves no temp file",
          not list(tmp.glob(".*.tmp")), "temp cleaned up")
    # Pre-rename failure: fault-inject fsync to raise (before the rename).
    # The handle must close before cleanup so the temp file is actually
    # removed (on Windows an open file cannot be deleted).
    path4 = write_fixture(tmp)
    before4 = path4.read_bytes()
    data4 = clyde.load_accounts_file(path4)
    new_data4, _ = clyde.assign_strategy(data4, "momentum_qqq", ["Alice"])
    real_fsync = clyde.os.fsync
    def failing_fsync(fd):
        raise OSError("injected fsync failure")
    clyde.os.fsync = failing_fsync
    try:
        clyde.write_accounts_atomic(path4, new_data4, backup_path=None)
        check("pre-rename fsync failure leaves original intact", False)
    except OSError:
        check("pre-rename fsync failure leaves original intact",
              path4.read_bytes() == before4)
    finally:
        clyde.os.fsync = real_fsync
    check("pre-rename failure leaves no temp file",
          not list(tmp.glob(".*.tmp")), "temp cleaned up")


def test_recovery(tmp: Path) -> None:
    path = write_fixture(tmp)
    original = read(path)
    backup = tmp / "accounts.json.bak"
    data = clyde.load_accounts_file(path)
    new_data, _ = clyde.assign_strategy(data, "momentum_qqq", ["Alice"])
    clyde.write_accounts_atomic(path, new_data, backup_path=backup)
    check("apply changed the file", read(path)["accounts"][0]["strategy_id"] == "momentum_qqq")
    clyde.recover_from_backup(backup, path)
    check("recovery restores the original", read(path) == original)
    # Recovery from a missing backup fails without touching the file.
    before = path.read_bytes()
    try:
        clyde.recover_from_backup(tmp / "nope.bak", path)
        check("missing backup rejected", False)
    except FileNotFoundError:
        check("missing backup rejected", path.read_bytes() == before, "file unchanged")


def test_cli_end_to_end(tmp: Path) -> None:
    path = write_fixture(tmp)
    # catalogue subcommand runs offline and lists categories.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = clyde.main(["catalogue", "--registered-only"])
    check("CLI catalogue --registered-only exits 0", rc == 0)
    check("CLI catalogue lists a registered id",
          "smoke_atm_roundtrip" in buf.getvalue())
    # accounts subcommand.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = clyde.main(["accounts", "--accounts", str(path)])
    check("CLI accounts exits 0 and lists aliases",
          rc == 0 and "Alice" in buf.getvalue() and "Bob" in buf.getvalue())
    # assign --dry-run writes nothing.
    before = path.read_bytes()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = clyde.main(["assign", "--strategy", "momentum_qqq", "--account", "Alice",
                         "--accounts", str(path), "--dry-run"])
    check("CLI assign --dry-run exits 0, file unchanged",
          rc == 0 and path.read_bytes() == before)
    # assign (real) writes, with backup.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = clyde.main(["assign", "--strategy", "momentum_qqq", "--account", "Alice",
                         "--accounts", str(path)])
    check("CLI assign exits 0 and changes the file",
          rc == 0 and read(path)["accounts"][0]["strategy_id"] == "momentum_qqq")
    check("CLI assign prints no secret",
          "SECRET-ALICE-DO-NOT-PRINT" not in buf.getvalue())
    # assign a research name is refused (exit 2, file unchanged).
    before = path.read_bytes()
    rc = clyde.main(["assign", "--strategy", "FX Carry Trade", "--account", "Alice",
                     "--accounts", str(path)])
    check("CLI assign research name refused (exit 2, file unchanged)",
          rc == 2 and path.read_bytes() == before)
    # recover restores.
    backup = path.with_suffix(".json.bak")
    rc = clyde.main(["recover", "--backup", str(backup), "--accounts", str(path)])
    check("CLI recover exits 0 and restores",
          rc == 0 and read(path)["accounts"][0]["strategy_id"] == "smoke_atm_roundtrip")


def main() -> int:
    print("verify_clyde: offline catalogue + assignment core (issue #25)")
    test_catalogue()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_selection_and_assign(tmp)
        test_refusals_leave_file_unchanged(tmp)
        test_malformed_leaves_file_unchanged(tmp)
        test_typed_invalid_leaves_file_unchanged(tmp)
        test_dry_run_validation_and_mutual_exclusion(tmp)
        test_preservation_and_atomicity(tmp)
        test_recovery(tmp)
        test_cli_end_to_end(tmp)
    print(f"\nverify_clyde: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
