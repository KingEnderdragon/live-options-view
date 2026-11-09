import unittest
from datetime import datetime, timezone, date
from unittest.mock import patch
from market_data.live_market import settlement_marks, quotes, chain

class Sink:
 def __init__(self): self.writes=[]
 def put_object(self,**kw): self.writes.append(kw)

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

if __name__=='__main__': unittest.main()
