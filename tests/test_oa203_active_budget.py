import sys
import unittest
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
import market_data.options_scanner.active_budget as b

NOW = datetime(2026, 12, 10, 16, tzinfo=timezone.utc)
DAY = NOW.date()
ROOT = b.PREFIX + DAY.isoformat() + "/inprogress/"


class Conflict(Exception):
    response = {"ResponseMetadata": {"HTTPStatusCode": 412}}


class Store:
    def __init__(self):
        self.items = {}
        self.deleted = []
        self.fail_delete = False
    def list_objects_v2(self, Bucket, Prefix, MaxKeys, Delimiter=None):
        rows = [dict(Key=k, **v) for k, v in self.items.items() if k.startswith(Prefix)]
        if Delimiter:
            prefixes = {Prefix + r["Key"][len(Prefix):].split("/")[0] + "/" for r in rows}
            return {"CommonPrefixes": [{"Prefix": p} for p in sorted(prefixes)]}
        return {"Contents": rows[:MaxKeys], "IsTruncated": len(rows) > MaxKeys}
    def put_object(self, Bucket, Key, Body, **kw):
        assert kw["IfNoneMatch"] == "*"
        if Key in self.items: raise Conflict()
        self.items[Key] = {"Size": len(Body), "LastModified": NOW}
    def delete_objects(self, Bucket, Delete):
        if self.fail_delete: return {"Errors": [{"Code": "AccessDenied"}]}
        for x in Delete["Objects"]:
            self.deleted.append(x["Key"])
            self.items.pop(x["Key"])
        return {}


class BudgetTests(unittest.TestCase):
    def test_restart_cannot_reset_remote_attempts(self):
        c = Store()
        for expected in (1, 2, 3):
            self.assertEqual(b.clean_and_reserve(c, "b", DAY, 6, NOW)["attempt"], expected)
        with self.assertRaisesRegex(ValueError, "retry budget"):
            b.clean_and_reserve(c, "b", DAY, 6, NOW)
    def test_cutoff_and_global_limits(self):
        c = Store()
        for sweep in range(1, 17): b.clean_and_reserve(c, "b", DAY, sweep, NOW)
        with self.assertRaisesRegex(ValueError, "cutoff budget"):
            b.clean_and_reserve(c, "b", DAY, 17, NOW)
        c.items[ROOT+"latest.json"] = {"Size": b.MAX_BYTES, "LastModified": NOW}
        with self.assertRaisesRegex(ValueError, "storage budget"):
            b.clean_and_reserve(c, "b", DAY, 6, NOW)
    def test_cleanup_preserves_raw_final_legacy_and_recent_revision(self):
        c = Store()
        old = b.PREFIX+"2026-11-09/"
        for name in ("summary.json", "sweeps/sweep_0001.jsonl.gz", "inprogress/latest.json"):
            c.items[old+name] = {"Size": 5, "LastModified": NOW-timedelta(days=31)}
        legacy = b.PREFIX+"2026-11-08/inprogress/latest.json"
        recent = old+"inprogress/sweep_0006.json"
        c.items[legacy] = {"Size": 5, "LastModified": NOW-timedelta(days=32)}
        c.items[recent] = {"Size": 5, "LastModified": NOW-timedelta(days=1)}
        result = b.clean_and_reserve(c, "b", DAY, 6, NOW)
        self.assertEqual(c.deleted, [old+"inprogress/latest.json"])
        self.assertIn(old+"summary.json", c.items)
        self.assertIn(old+"sweeps/sweep_0001.jsonl.gz", c.items)
        self.assertIn(legacy, c.items)
        self.assertIn(recent, c.items)
        self.assertEqual(result["expired_objects"], 1)
    def test_cleanup_failure_stops_publication(self):
        c=Store(); c.fail_delete=True
        c.items[b.PREFIX+"2026-11-09/inprogress/latest.json"]={"Size":5,"LastModified":NOW-timedelta(days=31)}
        with self.assertRaisesRegex(ValueError, "cleanup failed"):
            b.clean_and_reserve(c,"b",DAY,6,NOW)
        self.assertFalse(any("/budget/" in k for k in c.items))
    def test_unknown_key_and_historical_build_fail_closed(self):
        c=Store(); c.items[ROOT+"unknown"]={"Size":5,"LastModified":NOW}
        with self.assertRaisesRegex(ValueError,"unrecognized"):
            b.clean_and_reserve(c,"b",DAY,6,NOW)
        with self.assertRaisesRegex(ValueError,"current policy"):
            b.clean_and_reserve(Store(),"b",date(2026,11,9),6,NOW)
    def test_truncated_inventory_fails_closed(self):
        c=Store()
        for n in range(1001):
            c.items[ROOT+f"sweep_{n:04d}.json"]={"Size":5,"LastModified":NOW}
        with self.assertRaisesRegex(ValueError,"inventory"):
            b.clean_and_reserve(c,"b",DAY,6,NOW)

    def test_daily_attempt_limit_survives_spool_loss(self):
        c=Store()
        for sweep in range(1, 6):
            for attempt in range(3): b.clean_and_reserve(c, "b", DAY, sweep, NOW)
        b.clean_and_reserve(c, "b", DAY, 6, NOW)
        with self.assertRaisesRegex(ValueError, "daily attempt"):
            b.clean_and_reserve(c, "b", DAY, 6, NOW)

    @unittest.skipUnless(sys.platform == "linux", "Linux deployment limits")
    def test_child_limits_are_applied_without_limiting_parent(self):
        import subprocess, json, resource, os
        before=resource.getrlimit(resource.RLIMIT_AS)
        code="import resource,os,json;print(json.dumps([resource.getrlimit(resource.RLIMIT_AS),resource.getrlimit(resource.RLIMIT_CPU),len(os.sched_getaffinity(0)),os.nice(0)]))"
        r=subprocess.run([sys.executable,"-c",code],capture_output=True,text=True,check=True,preexec_fn=b.constrain_builder)
        limits=json.loads(r.stdout)
        self.assertEqual(limits[0],[2*1024**3]*2)
        self.assertEqual(limits[1],[230,230])
        self.assertEqual(limits[2],1)
        self.assertGreaterEqual(limits[3],10)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_AS),before)

if __name__ == "__main__": unittest.main()
