import unittest
from datetime import datetime, timezone, date
from unittest.mock import patch
from market_data.live_market import settlement_marks, quotes, chain
from market_data.live_market import market as mkt
from market_data.live_market.state import Counters, SnapshotTracker, UnderlyingSession

class Sink:
 def __init__(self): self.writes=[]
 def put_object(self,**kw): self.writes.append(kw)

class ConflictSink(Sink):
 """Enforces IfNoneMatch='*' like R2: a second put to the same key 409s."""
 def __init__(self): super().__init__(); self.keys=set()
 def put_object(self, **kw):
  if kw.get("IfNoneMatch") == "*" and kw["Key"] in self.keys:
   exc = Exception("Precondition Failed")
   exc.response = {"ResponseMetadata": {"HTTPStatusCode": 412}}
   raise exc
  self.keys.add(kw["Key"]); self.writes.append(kw)

class FixedDatetime(datetime):
 """Pins take_snapshot's now to 2026-11-09T21:00:40Z (16:00:40 ET)."""
 @classmethod
 def now(cls, tz=None):
  fixed = datetime(2026, 11, 9, 21, 0, 40, tzinfo=timezone.utc)
  return fixed if tz is None else fixed.astimezone(tz)

def _snapshot_settlement_writes(quote, symbol="SPY", sink=None):
 sink = sink or Sink()
 class Feed:
  def get_state(self): return {symbol: quote}
 us = UnderlyingSession(symbol)
 strikes = [{"strike": 670, "call_sym": "SPY261109C00670000", "call_occ": "SPY261109C00670000",
             "put_sym": "SPY261109P00670000", "put_occ": "SPY261109P00670000"}]
 with patch.object(mkt, "datetime", FixedDatetime):
  mkt.take_snapshot(sink, Feed(), strikes, "2026-11-09", "tier", date(2026, 11, 9),
                    Counters(), SnapshotTracker(), us)
 return [w for w in sink.writes if w["Key"].startswith("settlement/")]

# Chej's reproduction: fresh receipt, provider time UNKNOWN on both sides.
UNKNOWN_QUOTE = {"bid": 670, "ask": 670.2,
                 "bid_ts": "2026-11-09T21:00:30+00:00", "ask_ts": "2026-11-09T21:00:31+00:00",
                 "bid_observed_at": None, "ask_observed_at": None}
# Valid post-close positive: both sides provider-observed in window, fresh.
KNOWN_QUOTE = dict(UNKNOWN_QUOTE, bid_observed_at="2026-11-09T21:00:30+00:00",
                   ask_observed_at="2026-11-09T21:00:31+00:00")

class MultiUnderlying(unittest.TestCase):
 def test_mark_requires_expiry_own_timed_post_close_spot(self):
  sink=Sink();now=datetime(2026,11,6,21,0,30,tzinfo=timezone.utc)
  for symbol in ('SPY','IBIT'):
   self.assertTrue(settlement_marks.publish_settlement_mark(sink,symbol,'2026-11-06',50,'2026-11-06T21:00:00+00:00',now))
  for stamp,exp in [(None,'2026-11-06'),('2026-11-06T20:59:00+00:00','2026-11-06'),('2026-11-06T21:00:00+00:00','2026-11-13'),('2026-11-06T21:01:00+00:00','2026-11-06')]:
   self.assertFalse(settlement_marks.publish_settlement_mark(sink,'IBIT',exp,50,stamp,now))
  self.assertEqual(len(sink.writes),2)
  self.assertTrue(all(w['IfNoneMatch']=='*' for w in sink.writes))
 def test_ibit_nearest_expiry(self):
  response=type('Response',(),{'raise_for_status':lambda s:None,'json':lambda s:{'data':{'items':[{'expirations':[{'expiration-date':'2026-11-13','strikes':[]}]}]}}})()
  with patch.object(chain.requests,'get',return_value=response):
   self.assertEqual(chain.load_chain('fixture',date(2026,11,9),'IBIT',False)[1],'2026-11-13')
   with self.assertRaises(RuntimeError): chain.load_chain('fixture',date(2026,11,9),'SPY',False)
 def test_held_contract_interest_is_bounded_and_no_extra_connection(self):
  class Feed:
   def __init__(self): self.added=[]
   def add_option_subscriptions(self,symbols): self.added+=symbols
   def get_state(self): return {}
   def get_health(self): return {'last_feed_event_time':None,'connected':True,'authorized':True,'channel_open':True}
  feed=Feed(); registry=quotes.LiveQuoteRegistry();registry.set_session(feed,{})
  with patch.object(quotes,'COLLECT_IBIT',True):
   registry.quote_payload([f'IBIT291221C{n*1000:08}' for n in range(1,151)])
   self.assertEqual(len(feed.added),100)
   registry.quote_payload(['IBIT291221C00001000']);self.assertEqual(len(feed.added),100)
  registry.set_session(feed,{})
  with patch.object(quotes,'COLLECT_IBIT',False): registry.quote_payload(['IBIT291221C00001000'])
  self.assertEqual(len(feed.added),100)

 def test_provider_observed_pairs_price_with_its_own_event_time(self):
  # Unknown provider time: price is computable but there is no provider
  # event to stamp it with -- must not publish.
  self.assertEqual(quotes._provider_observed_spy_price(UNKNOWN_QUOTE), (670.1, None, "bid_ask_midpoint"))
  # Both sides known: the OLDER side wins (a fresh half must not make an
  # old half look observed).
  self.assertEqual(quotes._provider_observed_spy_price(KNOWN_QUOTE),
                   (670.1, "2026-11-09T21:00:30+00:00", "bid_ask_midpoint"))
  # One side unknown: no provider time at all.
  self.assertEqual(quotes._provider_observed_spy_price(
   dict(KNOWN_QUOTE, ask_observed_at=None))[1], None)
  # Last trade: its own event time; unknown stays None.
  self.assertEqual(quotes._provider_observed_spy_price(
   {"last": 670.1, "last_ts": "2026-11-09T21:00:30+00:00",
    "last_observed_at": "2026-11-09T21:00:30+00:00"}),
   (670.1, "2026-11-09T21:00:30+00:00", "last_trade"))
  self.assertEqual(quotes._provider_observed_spy_price(
   {"last": 670.1, "last_ts": "2026-11-09T21:00:30+00:00", "last_observed_at": None})[1], None)
 def test_take_snapshot_unknown_provider_time_does_not_publish(self):
  # G3 regression (Chej review): SPY bid 670/ask 670.2, receipt 21:00:30/31,
  # provider time None on both sides, now 21:00:40. The receipt time falls
  # in the post-close window, but the provider observation is unknown, so
  # no immutable mark may be written.
  self.assertEqual(_snapshot_settlement_writes(UNKNOWN_QUOTE), [])
 def test_take_snapshot_valid_postclose_publishes(self):
  writes = _snapshot_settlement_writes(KNOWN_QUOTE)
  self.assertEqual([w["Key"] for w in writes], ["settlement/spy/2026-11-09.json"])
  self.assertEqual(writes[0]["IfNoneMatch"], "*")
 def test_take_snapshot_rejects_preclose_stale_future_mixed_side(self):
  base = dict(KNOWN_QUOTE)
  cases = {
   "preclose": dict(base, bid_observed_at="2026-11-09T20:59:30+00:00",
                    ask_observed_at="2026-11-09T20:59:31+00:00"),
   "stale": dict(base, bid_observed_at="2026-11-09T20:50:30+00:00",
                 ask_observed_at="2026-11-09T20:50:31+00:00"),
   "future": dict(base, bid_observed_at="2026-11-09T21:01:00+00:00",
                  ask_observed_at="2026-11-09T21:01:01+00:00"),
   # Mixed side: bid post-close, ask pre-close -- the older (pre-close)
   # side is the provider time, so the mark must not publish.
   "mixed": dict(base, bid_observed_at="2026-11-09T21:00:30+00:00",
                 ask_observed_at="2026-11-09T20:59:00+00:00"),
  }
  for name, quote in cases.items():
   with self.subTest(name):
    self.assertEqual(_snapshot_settlement_writes(quote), [])
 def test_take_snapshot_last_trade_and_ibit_provider_time(self):
  known_last = {"last": 670.1, "last_ts": "2026-11-09T21:00:30+00:00",
                "last_observed_at": "2026-11-09T21:00:30+00:00"}
  self.assertEqual([w["Key"] for w in _snapshot_settlement_writes(known_last)],
                   ["settlement/spy/2026-11-09.json"])
  self.assertEqual(_snapshot_settlement_writes(
   dict(known_last, last_observed_at=None)), [])
  self.assertEqual([w["Key"] for w in _snapshot_settlement_writes(KNOWN_QUOTE, "IBIT")],
                   ["settlement/ibit/2026-11-09.json"])
  self.assertEqual(_snapshot_settlement_writes(UNKNOWN_QUOTE, "IBIT"), [])
 def test_take_snapshot_future_contributing_leg_vetoes(self):
  # The provider time is the OLDEST contributing leg, which proves the
  # oldest leg isn't stale but hides a future newer leg: a valid bid at
  # 21:00:30 plus a FUTURE ask at 21:01:01 (now 21:00:40) must not publish,
  # for either root and either bid/ask permutation. Every contributing
  # provider timestamp is validated against the window and now.
  for symbol in ("SPY", "IBIT"):
   for leg in ("bid_observed_at", "ask_observed_at"):
    with self.subTest(msg=f"{symbol} {leg}"):
     self.assertEqual(_snapshot_settlement_writes(
      dict(KNOWN_QUOTE, **{leg: "2026-11-09T21:01:01+00:00"}), symbol), [])
  # Last trade with a future event time is vetoed the same way; the valid
  # and unknown cases stay green.
  self.assertEqual(_snapshot_settlement_writes(
   {"last": 670.1, "last_ts": "2026-11-09T21:00:30+00:00",
    "last_observed_at": "2026-11-09T21:01:00+00:00"}), [])
  self.assertEqual([w["Key"] for w in _snapshot_settlement_writes(KNOWN_QUOTE)],
                   ["settlement/spy/2026-11-09.json"])
  self.assertEqual(_snapshot_settlement_writes(UNKNOWN_QUOTE), [])
 def test_settlement_mark_is_immutable_and_repeated_publish_fails(self):
  sink = ConflictSink(); now = datetime(2026, 11, 9, 21, 0, 40, tzinfo=timezone.utc)
  self.assertTrue(settlement_marks.publish_settlement_mark(
   sink, "SPY", "2026-11-09", 670.1, "2026-11-09T21:00:30+00:00", now))
  # A second publish of the same mark is rejected (IfNoneMatch conflict)
  # and does not overwrite the first.
  self.assertFalse(settlement_marks.publish_settlement_mark(
   sink, "SPY", "2026-11-09", 670.2, "2026-11-09T21:00:31+00:00", now))
  self.assertEqual(len(sink.writes), 1)
  self.assertEqual(sink.writes[0]["Body"], sink.writes[0]["Body"])

if __name__=='__main__': unittest.main()
