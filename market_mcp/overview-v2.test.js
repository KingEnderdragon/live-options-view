'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {Consumer} = require('./consumer');
const {Source,LIMITS,DataError} = require('./source');
const {fixtures,NOW} = require('./fixtures');
const overview = require('./overview');
const KEY = overview.KEY;
function setup(prices, {now=NOW, schema_version=2}={}) {
  const files=fixtures();
  files[KEY]=JSON.stringify({schema_version,universe_version:overview.universe.universe_version,timestamp:'2026-10-02T16:09:58Z',feed_stale:false,prices});
  let reads=0;
  const consumer=new Consumer(new Source({read:async key=>{reads++;if(!Object.hasOwn(files,key))throw new DataError('missing_artifact','absent',key);return files[key];}}),{now:()=>now});
  return {consumer,files,reads:()=>reads};
}
function event(label='AAPL', extra={}) {
  const i=overview.INSTRUMENTS[label];
  return {instrument_id:i.id,price:202,source:'dxlink',source_symbol:i.dxlink_symbol,price_basis:'last_trade',
    observed_at:'2026-10-02T16:09:50Z',observation_granularity:'event',observation_timezone:'America/New_York',session:'regular',adjustment_basis:'raw',
    collector_received_at:'2026-10-02T16:09:55Z',quote_ts:'2026-10-02T16:09:55Z',acquired_at:'2026-10-02T16:09:56Z',last_success_at:'2026-10-02T16:09:56Z',last_attempt_at:'2026-10-02T16:09:56Z',refresh_error:null,
    reference:{instrument_id:i.id,value:200,source:'dxlink',source_symbol:i.dxlink_symbol,price_basis:'previous_close',observed_at:'2026-10-01T20:00:00Z',observation_granularity:'event',observation_timezone:'America/New_York',session:'regular',adjustment_basis:'raw'},...extra};
}
async function one(s,label='AAPL'){const out=await s.consumer.call('market_overview',{symbols:[label]});assert.notEqual(out.status,'failed',JSON.stringify(out.errors));return out.rows[0];}

test('exact-100 registry, original aliases, enabled additions and full denominator survive two 50-row pages',async()=>{
  const s=setup({AAPL:event()});
  const a=await s.consumer.call('market_overview',{limit:50});
  const b=await s.consumer.call('market_overview',{limit:50,offset:50});
  assert.equal(a.rows.length,50);assert.equal(b.rows.length,50);assert.equal(new Set([...a.rows,...b.rows].map(r=>r.id)).size,100);
  assert.deepEqual([...a.rows,...b.rows].map(r=>r.label),overview.universe.instruments.map(i=>i.label));
  assert.equal(a.pagination.total,100);assert.equal(a.pagination.next_offset,50);assert.equal(b.pagination.next_offset,null);
  assert.equal(a.coverage.full_universe.available_prices,1);assert.equal(a.coverage.full_universe.missing_prices,99);
  assert.equal(a.coverage.full_universe.ok_changes,1);assert.equal(a.coverage.full_universe.unavailable_changes,99);
  assert.equal(a.actual.universe_version,'market-overview-100-v1');
  const missing=[...a.rows,...b.rows].find(r=>r.label==='MSFT');assert.equal(missing.enabled,true);assert.equal(missing.compact,false);assert.equal(missing.price,null);assert.equal(missing.coverage.reason,'not_published');
  assert.equal([...a.rows,...b.rows].filter(r=>r.compact).length,18);
  const original=['QQQ','SPY','SMH','IGV','USO','VIX','OVX','KOSPI','10Y','JPY/USD','BTC/USD','META','GOOGL','AMZN','TSLA','MU','AAPL','SPCX'];
  assert.deepEqual(overview.universe.instruments.slice(0,18).map(i=>i.label),original);
  assert.equal(LIMITS.rows,50);assert.equal(LIMITS.references,64);assert.equal(s.reads(),2);
});

test('100 published rows accepted but 101 rejected without changing artifact or transport budgets',async()=>{
  const prices=Object.fromEntries(overview.universe.instruments.map(i=>[i.label,{instrument_id:i.id,price:null}]));
  let s=setup(prices);assert.notEqual((await s.consumer.call('market_overview',{limit:50})).status,'failed');
  prices.EXTRA={price:1};s=setup(prices);const out=await s.consumer.call('market_overview');assert.equal(out.errors[0].code,'excessive_or_malformed_artifact');
  assert.equal(LIMITS.artifact_bytes,4*1024*1024);assert.equal(LIMITS.requests,12);assert.equal(LIMITS.timeout_ms,15000);assert.equal(LIMITS.output_bytes,256*1024);
});

test('v2 event timestamps, acquisition, exact reference and formulas are retained in detail',async()=>{
  const s=setup({AAPL:event()});const out=await s.consumer.call('market_overview',{symbols:['AAPL']});const r=out.rows[0];
  assert.equal(r.change.status,'ok');assert.equal(r.change.absolute,2);assert.equal(r.change.percent,1);assert.equal(r.freshness.age_seconds,10);assert.equal(r.freshness.live,true);
  s.files[KEY]=JSON.stringify({timestamp:'2026-10-02T16:10:00Z',feed_stale:false,prices:{AAPL:event('AAPL',{price:999})}});
  const reads=s.reads();const detail=await s.consumer.call('result_detail',{reference:r.detail_reference});
  assert.equal(s.reads(),reads);assert.equal(detail.row.price,202);assert.deepEqual(detail.row.reference,r.reference);assert.equal(detail.producer_times.observation,r.observed_at);assert.equal(detail.actual.payload_sha256,out.actual.payload_sha256);
});

test('positive negative unchanged and zero-reference arithmetic uses normalized reference values',async()=>{
  for(const [price,absolute,percent] of [[202,2,1],[198,-2,-1],[200,0,0]]){
    const r=await one(setup({AAPL:event('AAPL',{price})}));assert.equal(r.change.absolute,absolute);assert.equal(r.change.percent,percent);
  }
  const r=event();r.reference.value=0;const zero=await one(setup({AAPL:r}));assert.equal(zero.change.absolute,202);assert.equal(zero.change.percent,null);assert.equal(zero.change.status,'partial');
  const y=event('10Y',{price:40.5});y.reference.value=40;const yieldRow=await one(setup({'10Y':y}),'10Y');assert.equal(yieldRow.price,4.05);assert.equal(yieldRow.previous_close,4);assert.equal(yieldRow.change.bps,5);assert.equal(yieldRow.change.percent,null);
});

test('partial numeric changes and genuinely unavailable changes are counted separately before pagination',async()=>{
  const partial=event();partial.reference.observed_at=null;partial.reference.observation_granularity=null;
  const s=setup({AAPL:partial,SPY:event('SPY',{reference:null})});const out=await s.consumer.call('market_overview',{symbols:['AAPL','SPY'],limit:1});
  assert.equal(out.coverage.partial_changes,1);assert.equal(out.coverage.unavailable_changes,1);assert.equal(out.coverage.numeric_changes,1);assert.equal(out.coverage.matched_rows,2);assert.equal(out.coverage.full_universe.rows,100);
});

test('mixed source, instrument, symbol, adjustments and future reference dates cannot produce comparable moves',async()=>{
  for(const [field,value,reason] of [['source','yfinance','mixed_reference_source'],['instrument_id','MSFT','reference_identity_mismatch'],['source_symbol','MSFT','reference_symbol_mismatch'],['adjustment_basis','split_adjusted','adjustment_basis_mismatch'],['observed_at','2026-10-03T20:00:00Z','invalid_reference_date']]){
    const r=event();r.reference[field]=value;const row=await one(setup({AAPL:r}));assert.equal(row.change.status,'unavailable');assert.equal(row.change.absolute,null);assert.ok(row.change.reasons.includes(reason));
  }
  const wrong=event('AAPL',{instrument_id:'MSFT'});assert.equal((await setup({AAPL:wrong}).consumer.call('market_overview')).status,'failed');
});

test('old provider event fetched now, failed newer refresh and small future times never become live',async()=>{
  let r=await one(setup({AAPL:event('AAPL',{observed_at:'2026-10-01T16:09:50Z',refresh_error:'timeout'})}));assert.equal(r.freshness.status,'stale');assert.equal(r.freshness.live,false);assert.equal(r.last_attempt_at,'2026-10-02T16:09:56Z');
  for(const field of ['observed_at','collector_received_at','acquired_at','last_attempt_at']){
    const e=event('AAPL',{[field]:'2026-10-02T16:10:01Z'});if(field==='collector_received_at')e.quote_ts=e.collector_received_at;
    r=await one(setup({AAPL:e}));assert.equal(r.freshness.status,'invalid_time');assert.equal(r.freshness.live,false);assert.equal(r.change.status,'unavailable');
  }
});

test('dated Yahoo daily bars retain date/timezone without manufacturing intraday observation time',async()=>{
  const daily=event('AAPL',{source:'yfinance',source_symbol:'AAPL',price_basis:'daily_bar_close',observed_at:null,observation_date:'2026-10-01',observation_timezone:'America/New_York',observation_granularity:'day',session:'daily',collector_received_at:null,quote_ts:null});
  daily.reference={...daily.reference,source:'yfinance',observed_at:null,observation_date:'2026-09-30',observation_granularity:'day',session:'daily'};
  let r=await one(setup({AAPL:daily}));assert.equal(r.observed_at,null);assert.equal(r.observation_date,'2026-10-01');assert.equal(r.freshness.status,'dated_close');assert.equal(r.freshness.age_seconds,null);assert.equal(r.freshness.live,false);assert.equal(r.change.percent,1);
  assert.equal(r.change.status,'partial');assert.ok(r.change.reasons.includes('daily_bar_observation_not_final'));
  r=await one(setup({AAPL:{...daily,observation_date:'2026-09-25'}}));assert.equal(r.freshness.status,'stale');
  for(const override of [{observation_date:'2026-02-30'},{observation_timezone:'Mars/Olympus'},{observed_at:'2026-10-01T20:00:00Z'},{observation_date:'tomorrow'}])assert.equal((await setup({AAPL:{...daily,...override}}).consumer.call('market_overview')).status,'failed');
});

test('a quote midpoint without a reported session, or an event outside any session, is never live',async()=>{
  const mid=event('SPY',{price_basis:'bid_ask_midpoint',session:null});
  let r=await one(setup({SPY:mid}),'SPY');
  assert.equal(r.freshness.status,'fresh');assert.equal(r.freshness.observation_window,'regular');assert.equal(r.freshness.live,false);
  assert.ok(r.change.reasons.includes('observation_and_reference_session_unverified'));
  r=await one(setup({SPY:{...mid,session:'regular'}}),'SPY');assert.equal(r.freshness.live,true);
  const now=Date.parse('2026-10-02T02:00:10Z');
  r=await one(setup({AAPL:event('AAPL',{observed_at:'2026-10-02T02:00:00Z'})},{now}),'AAPL');
  assert.equal(r.freshness.observation_window,'closed');assert.equal(r.freshness.live,false);
});

test('an activated addition published by the producer is usable, and bad session labels fail closed',async()=>{
  const injected=await setup({MSFT:event('MSFT')}).consumer.call('market_overview',{symbols:['MSFT']});
  assert.notEqual(injected.status,'failed');const row=injected.rows[0];
  assert.equal(row.enabled,true);assert.equal(row.price,202);
  assert.equal(row.change.status,'ok');assert.equal(row.freshness.live,true);assert.equal(row.raw_price,202);
  for(const override of [{session:'banana'},{reference:{...event('AAPL').reference,session:'banana'}}]){
    const out=await setup({AAPL:event('AAPL',override)}).consumer.call('market_overview');assert.equal(out.status,'failed');assert.equal(out.errors[0].code,'incompatible_schema');
  }
});

test('a reference more than one exchange session old cannot produce a previous-close move',async()=>{
  const stale=event('AAPL');stale.reference={...stale.reference,observed_at:'2026-09-01T20:00:00Z'};
  const r=await one(setup({AAPL:stale}),'AAPL');
  assert.equal(r.change.status,'unavailable');assert.equal(r.change.absolute,null);assert.ok(r.change.reasons.includes('reference_not_previous_session'));
  // Crypto's cut is the previous UTC day exactly; a missing day is a multi-day move.
  const btc=event('BTC/USD');btc.reference={...btc.reference,observed_at:'2026-09-30T00:00:00Z',observation_timezone:'UTC'};
  let c=await one(setup({'BTC/USD':btc}),'BTC/USD');
  assert.equal(c.change.status,'unavailable');assert.ok(c.change.reasons.includes('reference_not_previous_session'));
  btc.reference={...btc.reference,observed_at:'2026-10-01T00:00:00Z'};
  c=await one(setup({'BTC/USD':btc}),'BTC/USD');assert.notEqual(c.change.status,'unavailable');
});

test('a mathematically finite input that overflows an intermediate yields no number',async()=>{
  const extreme=event('AAPL',{price:1});extreme.reference={...extreme.reference,value:1e308};
  const r=await one(setup({AAPL:extreme}),'AAPL');
  assert.equal(r.change.status,'unavailable');assert.equal(r.change.percent,null);assert.equal(r.change.absolute,null);
  assert.ok(r.change.reasons.includes('nonfinite_arithmetic'));
  assert.doesNotThrow(()=>JSON.stringify(r));
});

test('a reference basis or session that contradicts the instrument cannot produce a move',async()=>{
  for(const [override,reason] of [[{price_basis:'settlement'},'reference_basis_not_valid_for_instrument'],
                                  [{price_basis:'previous_close',session:'extended'},'reference_session_basis_conflict']]){
    const e=event('AAPL');e.reference={...e.reference,...override};
    const r=await one(setup({AAPL:e}),'AAPL');
    assert.equal(r.change.status,'unavailable',JSON.stringify(override));
    assert.equal(r.change.absolute,null);
    assert.ok(r.change.reasons.includes(reason),JSON.stringify(r.change.reasons));
  }
  const clean=await one(setup({AAPL:event()}),'AAPL');assert.equal(clean.change.status,'ok');
});

test('oversized provenance strings are rejected before they can bloat a response',async()=>{
  for(const override of [{observation_time_reason:'x'.repeat(300)},{instrument_id:'A'.repeat(200)},{refresh_error:'y'.repeat(600)}]){
    const out=await setup({AAPL:event('AAPL',override)}).consumer.call('market_overview');
    assert.equal(out.status,'failed');assert.equal(out.errors[0].code,'incompatible_schema');
  }
});

test('unverified calendars stay unknown; crypto can be continuous and equity holiday is closed',async()=>{  let r=await one(setup({KOSPI:event('KOSPI')}),'KOSPI');assert.equal(r.freshness.market_state,'unknown');assert.equal(r.freshness.live,false);
  r=await one(setup({'BTC/USD':event('BTC/USD')}),'BTC/USD');assert.equal(r.freshness.market_state,'continuous_market');assert.equal(r.freshness.live,true);
  const now=Date.parse('2026-11-26T16:10:00Z');r=await one(setup({AAPL:event('AAPL',{observed_at:'2026-11-26T16:09:50Z'})},{now}));assert.equal(r.freshness.market_state,'closed');assert.equal(r.freshness.live,false);
});

test('negative commodity futures allowed only for a matched resolved contract; unresolved and roll remain explicit',async()=>{
  const i=overview.universe.instruments.find(i=>i.id==='fut.cl');assert.ok(i);
  const c={symbol:'/CLZ26:XNYM',expiry:'2026-11-20',root:'CL',exchange:'XNYM',selection_at:'2026-10-02T15:00:00Z',roll_rule:'explicit dated contract'};
  const e=event(i.label,{price:-20,source_symbol:c.symbol,resolved_contract:c});e.reference={...e.reference,value:-10,source_symbol:c.symbol,resolved_contract:c};
  // Activated futures retain contract-aware arithmetic.
  let r=await one(setup({[i.label]:e}),i.label);assert.equal(r.price,-20);assert.equal(r.change.absolute,-10);
  // Its contract arithmetic still holds once that slot is activated.
  const activated={...i,enabled:true};
  const payload={schema_version:2,timestamp:'2026-10-02T16:09:58Z',feed_stale:false,prices:{[i.label]:e}};
  let out=overview.rows(payload,NOW,[activated]).known[0];
  assert.equal(out.price,-20);assert.equal(out.change.absolute,-10);assert.equal(out.change.percent,100);
  e.reference.resolved_contract={...c,symbol:'/CLF27:XNYM'};
  out=overview.rows(payload,NOW,[activated]).known[0];
  assert.equal(out.change.status,'unavailable');assert.equal(out.price,null);assert.ok(out.change.reasons.includes('contract_unresolved_or_mismatch'));
});

test('both percent orders page the full universe with deterministic missing-value ties and bounded output',async()=>{
  const s=setup({AAPL:event(),SPY:event('SPY',{price:198}),QQQ:event('QQQ',{price:200})});
  for(const sort of ['percent_asc','percent_desc']){
    const first=await s.consumer.call('market_overview',{sort,limit:50});
    const second=await s.consumer.call('market_overview',{sort,limit:50,offset:50});
    assert.equal(first.rows.length,50);assert.equal(second.rows.length,50);
    assert.equal(new Set([...first.rows,...second.rows].map(r=>r.label)).size,100);
    assert.deepEqual(first.rows.slice(0,3).map(r=>r.label),sort==='percent_asc'?['SPY','QQQ','AAPL']:['AAPL','QQQ','SPY']);
    assert.equal(first.coverage.full_universe.rows,100);assert.equal(first.actual.payload_sha256,second.actual.payload_sha256);
    assert.ok(Buffer.byteLength(JSON.stringify(first))<=LIMITS.output_bytes);assert.ok(Buffer.byteLength(JSON.stringify(second))<=LIMITS.output_bytes);
  }
});

test('v2 malformed provenance fails closed and unknown Summary precision remains partial',async()=>{
  for(const override of [{reference:{source:'scraped'}},{reference:[]},{resolved_contract:'CLZ26'},{session:[]},{quote_ts:'2026-02-30T10:00:00Z'},{collector_received_at:'2026-10-02T16:09:54Z'},{observation_time_reason:'x'.repeat(300)}]){
    const out=await setup({AAPL:event('AAPL',override)}).consumer.call('market_overview');assert.equal(out.status,'failed',JSON.stringify(override));assert.equal(out.errors[0].code,'incompatible_schema');
  }
  const e=event('AAPL',{adjustment_basis:'unknown'});e.reference={...e.reference,observed_at:null,observation_date:'2026-10-01',observation_timezone:null,observation_granularity:null,adjustment_basis:'unknown',session:null};
  const r=await one(setup({AAPL:e}));assert.equal(r.change.status,'partial');assert.equal(r.change.absolute,2);assert.ok(r.change.reasons.includes('reference_precision_or_timezone_unverified'));assert.ok(r.change.reasons.includes('adjustment_basis_unverified'));
});

test('discovery advertises all100 even absent publication; query filters do not hide universe denominator',async()=>{
  const s=setup({AAPL:event()});delete s.files[KEY];let discovery=await s.consumer.call('discover_sources');const c=discovery.capabilities.find(c=>c.dataset==='cross_market_overview');assert.equal(c.instruments.length,100);assert.equal(c.universe_size,100);assert.equal(c.availability,'missing_publication');
  s.files[KEY]=JSON.stringify({schema_version:2,timestamp:'2026-10-02T16:09:58Z',feed_stale:false,prices:{AAPL:event()}});
  const out=await s.consumer.call('market_overview',{asset_class:'equity',symbols:['AAPL','etf.spy']});assert.deepEqual(out.rows.map(r=>r.label),['AAPL']);assert.equal(out.coverage.full_universe.rows,100);
});

test('official Treasury daily yields are normalized in percent with basis-point change',async()=>{
  const r=event('UST2Y',{price:4.51,source:'treasury',source_symbol:'BC_2YEAR',price_basis:'daily_close',observed_at:null,observation_date:'2026-10-02',observation_timezone:'America/New_York',observation_granularity:'day',session:'daily',quote_ts:null,collector_received_at:null,reference:{instrument_id:'yld.us02y.cmt',value:4.46,source:'treasury',source_symbol:'BC_2YEAR',price_basis:'daily_close',observed_at:null,observation_date:'2026-10-01',observation_timezone:'America/New_York',observation_granularity:'day',session:'daily',adjustment_basis:'raw'}});
  const out=await one(setup({UST2Y:r}),'UST2Y');
  assert.equal(out.price,4.51);assert.equal(out.change.bps,5);assert.equal(out.freshness.live,false);
});

test('#178 crypto rolling 24h change is bounded, same-instrument and never zero-filled',async()=>{
  const rolling=overrides=>({instrument_id:'crypto.btcusd',value:80000,source:'rolling_history',source_symbol:'BTC/USD:CXTALP',price_basis:'rolling_24h',observed_at:'2026-10-01T16:10:00Z',observation_granularity:'event',observation_timezone:'UTC',session:'continuous',adjustment_basis:'unadjusted',sample_clock:'provider_event_clock',...overrides});
  const base=()=>event('BTC/USD',{price:88000,market_session:'continuous',rolling_reference:rolling({})});
  const ok=await one(setup({'BTC/USD':base()}),'BTC/USD');
  assert.equal(ok.market_session,'continuous');assert.equal(ok.change_24h.status,'ok');
  assert.equal(ok.change_24h.percent,10);assert.equal(ok.change_24h.absolute,8000);assert.equal(ok.change_24h.reference_basis,'rolling_24h');
  assert.equal(await one(setup({'BTC/USD':event('BTC/USD',{price:88000})}),'BTC/USD').then(r=>r.change_24h.status),'unavailable');
  const stale=base();stale.rolling_reference.observed_at='2026-09-30T16:10:00Z';
  assert.equal((await one(setup({'BTC/USD':stale}),'BTC/USD')).change_24h.reasons[0],'rolling_reference_outside_24h_window');
  const mismatch=base();mismatch.rolling_reference.instrument_id='crypto.ethusd';
  assert.equal((await one(setup({'BTC/USD':mismatch}),'BTC/USD')).change_24h.status,'unavailable');
  const zero=base();zero.rolling_reference.value=0;
  const out=await setup({'BTC/USD':zero}).consumer.call('market_overview');assert.equal(out.status,'failed');
  const missing=base();missing.rolling_reference=null;
  assert.equal((await one(setup({'BTC/USD':missing}),'BTC/USD')).change_24h.status,'unavailable');
});
