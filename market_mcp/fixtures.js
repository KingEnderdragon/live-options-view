'use strict';
// Synthetic examples of existing producer contracts. Never represented as
// acquired market data. Kept small so smoke/test runs need no network or SDK.
const fs=require('node:fs/promises');const path=require('node:path');const {gzipSync}=require('node:zlib');
const {hash}=require('./source');
const SESSION='2026-10-02', NOW=Date.parse('2026-10-02T16:10:00Z');
const RUN='00000000-0000-4000-8000-000000000001';const ARCHIVE=`squeeze-scanner/v1/scheduled/runs/${RUN}`;
const CONTRACT='QQQ261002C00600000';const PUT='QQQ261002P00600000';const SPY_CONTRACT='SPY261002C00670000';
function fixtures(){
  const files={};const json=(key,value)=>files[key]=JSON.stringify(value);
  const row={ticker:'QQQ',comparison_eligible:true,options_status:'valid',exclusion_reason:null,ranks:{combined:1,factor:2,options:1,momentum:3},scores:{combined:0.64,factor:0.78,options:0.44,momentum:25.43},components:{factor:{score:0.78},options:{score:0.44}},liquidity:{eligible:true,reason_code:'eligible',policy_version:'liquidity-v1',metrics:{total_open_interest:5278}},first_seen_at:'2026-10-02T13:01:00Z',first_seen_run_id:RUN,is_new:false};
  json('intraday/latest.json',{timestamp:'2026-10-02T16:09:30Z',date:SESSION,expiration:SESSION,underlying_price:600,snapshot_key:'intraday/20261002/snapshot_120930123456.csv',
    underlying_market:{symbol:'QQQ',spot:600,spot_ts:'2026-10-02T16:09:29Z',vwap:599,vwap_ts:'2026-10-02T16:09:29Z',vwap_partial_session:false,session_volume:1000000,session_volume_ts:'2026-10-02T16:09:29Z',rvol:{status:'ok',multiple:1.2,baseline_days_used:5,baseline_lookback_days:20},momentum:{status:'ok',return_pct:0.42,lookback_minutes:60,anchor_age_minutes:60,sample_count:200,direction:'up'},source:'dxlink',freshness:'live'},
    rows:[{OptionSymbol:CONTRACT,Type:'call',Strike:600,Expiration:SESSION,OpenInterest:0,Bid:1,Ask:1.2,IV:0.2},{OptionSymbol:PUT,Type:'put',Strike:600,Expiration:SESSION,OpenInterest:21,Bid:2,Ask:2.2}]});
  // SPY (#121): same collector contract, symbol-labeled, own archive tree.
  const spy=JSON.parse(files['intraday/latest.json']);
  Object.assign(spy,{symbol:'SPY',underlying_price:670,snapshot_key:'intraday/spy/20261002/snapshot_120930123456.csv',
    underlying_market:{...spy.underlying_market,symbol:'SPY',spot:670,vwap:669,rvol:{status:'insufficient_history',multiple:null,baseline_days_used:0}},
    rows:[{OptionSymbol:SPY_CONTRACT,Type:'call',Strike:670,Expiration:SESSION,OpenInterest:40,Bid:3,Ask:3.2,IV:0.18},{OptionSymbol:'SPY261002P00670000',Type:'put',Strike:670,Expiration:SESSION,OpenInterest:55,Bid:2.5,Ask:2.7}]});
  json('intraday/spy/latest.json',spy);
  json(ARCHIVE+'/results.json',[row,{ticker:'ABC',comparison_eligible:false,options_status:'no_options',exclusion_reason:'no listed expirations',ranks:{combined:null},scores:{combined:null}}]);
  json(ARCHIVE+'/inputs.json',[{ticker:'QQQ',factor_inputs:{short_float:0.3},options_inputs:{atm_iv:1.0059},options_status:'valid',liquidity:row.liquidity}]);
  json(ARCHIVE+'/manifest.json',{schema_version:1,run_id:RUN,scoring_version:'baseline-60-40-v1',coverage:{candidates:2,eligible:1,options_status:{valid:1,no_options:1}},files:Object.fromEntries(['results.json','inputs.json'].map(name=>[name,{sha256:hash(files[ARCHIVE+'/'+name]),bytes:Buffer.byteLength(files[ARCHIVE+'/'+name])}]))});
  const pointer={schema_version:1,display_schema_version:1,status:'complete',run_id:RUN,archive:ARCHIVE,manifest_sha256:hash(files[ARCHIVE+'/manifest.json']),sampling_mode:'scheduled',scheduled_for:'2026-10-02T16:00:00Z',started_at:'2026-10-02T16:00:03Z',finished_at:'2026-10-02T16:00:20Z',published_at:'2026-10-02T16:01:04Z',session_date:SESSION,scoring_version:'baseline-60-40-v1',candidates:[row],coverage:{eligible:1,candidates:2}};
  json('squeeze-scanner/v1/scheduled/latest.json',pointer);
  json('squeeze-scanner/v1/scheduled/latest-attempt.json',pointer);
  json('squeeze-scanner/v1/scheduled/latest-schedule.json',{status:'finished',scheduled_for:pointer.scheduled_for,run_id:RUN,acquisition_status:'complete',calendar:'XNYS',calendar_version:'4.13.2'});
  const prefix=`oa203/scanner/${SESSION}/`;
  json(prefix+'summary.json',{trade_date:SESSION,final:true,session_open_ms:1790947800000,session_close_ms:1790971200000,finalized_at:'2026-10-02T20:06:03Z',assessment:{status:'complete',reasons:[],chain_success_rate:1,universe_coverage:1},return_policy:{version:'oa203-returns-v1',min_entry_premium:0.05,stale_quote_s:1800,spike_ratio:3,min_coverage:0.5},qualified_policy:{version:'oa203-qualified-ask-bid-v1',max_relative_spread:0.3},measurement:'Sampled-return leaderboard',outputs:{contracts:'contracts.csv.gz',leaderboard:'leaderboard.csv',qualified_ask_bid_v1:'qualified_ask_bid_v1.csv'}});
  json(prefix+'universe.json',{selection_version:'oa203-universe-v1',selected:[{underlying:'QQQ',occ_rank:1}],skipped:[]});
  files[prefix+'leaderboard.csv']='symbol,underlying,option_type,rank,rank_in_type,clean_rank,clean_rank_in_type,clean,mid_first_to_max_pct,mid_first_to_max_entry,mid_first_to_max_exit,mid_first_to_max_entry_ms,mid_first_to_max_exit_ms,mid_first_to_max_flags,contract_flags\n'+
    `${CONTRACT},QQQ,call,1,1,,,False,3,0.02,0.08,1790947800000,1790948100000,tiny_entry,low_coverage\n`+
    `${PUT},QQQ,put,2,1,1,1,True,0.90909091,1.1,2.1,1790947800000,1790948100000,,\n`;
  files[prefix+'qualified_ask_bid_v1.csv']='symbol,underlying,option_type,rank,clean_rank,clean,qualified_first_ask_to_later_bid_rank,qualified_first_ask_to_later_bid_status,qualified_first_ask_to_later_bid_reasons,qualified_first_ask_to_later_bid_pct,qualified_first_ask_to_later_bid_entry_ms,qualified_first_ask_to_later_bid_exit_ms,qualified_first_ask_to_later_bid_entry_bid_size,qualified_first_ask_to_later_bid_exit_bid_size\n'+
    `${PUT},QQQ,put,2,1,True,1,eligible,,0.5,1790947800000,1790948100000,2,2\n`;
  const manifests=[];
  for(let sweep=1;sweep<=8;sweep++){
    const t=1790947800000+(sweep-1)*300000;const name=`sweep_${String(sweep).padStart(4,'0')}.jsonl.gz`;
    manifests.push({sweep,file:name,started_ms:t,ended_ms:t+100,chains:{QQQ:{status:'ok',fetched_ms:t,rows:2}},truncated:false});
    files[prefix+'sweeps/'+name]=gzipSync(JSON.stringify({symbol:CONTRACT,underlying:'QQQ',type:'call',t,sweep,bid:0.01*sweep,ask:0.03*sweep,bid_ms:t,ask_ms:t,oi:0,spot:600})+'\n'+JSON.stringify({symbol:PUT,underlying:'QQQ',type:'put',t,sweep,bid:1+0.1*sweep,ask:1.2+0.1*sweep})+'\n');
  }
  files[prefix+'sweeps/manifest.jsonl']=manifests.map(m=>JSON.stringify(m)).join('\n')+'\n';
  json('intraday/overview.json',overviewFixture());
  return files;
}
// Synthetic intraday/overview.json (schema 1, #132) in the shape market_overview.py's
// build_overview publishes. Values are invented; changes are precomputed by "the producer".
function overviewFixture(){
  const obs='2026-10-02T16:09:30Z',prev='2026-10-01T20:00:00Z';
  const f=(status,max=120,open=true)=>({status,age_seconds:30,warnings:[],market_open:open,max_age_seconds:max,evaluated_at:'2026-10-02T16:09:45Z'});
  const row=(id,o)=>({id,symbol:o.symbol??id,label:o.label??id,asset_class:o.asset_class??'equity',instrument_type:o.instrument_type??'stock',group:o.group??null,origin:o.origin??'baseline',unit:o.unit??'USD',
    provider:{source:o.source??'dxlink',symbol:o.provider_symbol??id,contract:o.contract??null},
    current:{value:o.cur,observed_at:o.observed_at===undefined?obs:o.observed_at,session:o.session??'regular'},
    reference:{value:o.ref,observed_at:o.ref_at??prev,basis:o.basis??'previous_regular_close',session:'regular'},
    change:o.change,freshness:o.freshness??f('fresh')});
  const ok=(abs,pct,unit='USD',bps=null)=>({status:'ok',reasons:[],absolute:abs,absolute_unit:unit,percent:pct,bps});
  return {schema_version:1,dataset:'cross_market_overview',universe_version:'2026-10-12.candidate-1',universe_status:'candidate',generated_at:'2026-10-02T16:09:45Z',
    cycle:{status:'complete',started_at:'2026-10-02T16:09:40Z',finished_at:'2026-10-02T16:09:45Z',newer_failed_attempt_at:null},
    groups:[{id:'technology',label:'Technology'},{id:'financials',label:'Financials'}],
    instruments:[
      row('AAPL',{group:'technology',cur:202,ref:200,change:ok(2,1)}),
      row('MSFT',{group:'technology',cur:398,ref:400,change:ok(-2,-0.5)}),
      row('NVDA',{group:'technology',cur:130,ref:null,change:{status:'unavailable',reasons:['missing_reference'],absolute:null,absolute_unit:null,percent:null,bps:null}}),
      row('JPM',{group:'financials',cur:257.5,ref:250,change:ok(7.5,3)}),
      row('PLTR',{group:'watchlist',origin:'watchlist',cur:120,ref:100,change:ok(20,20)}),
      row('etf.spy',{symbol:'SPY',label:'SPDR S&P 500 ETF (proxy, not the index)',asset_class:'etf',instrument_type:'etf',origin:'macro',provider_symbol:'SPY',cur:668,ref:670,change:ok(-2,-0.2985)}),
      row('idx.spx',{symbol:'SPX',label:'S&P 500',asset_class:'index',instrument_type:'cash_index',origin:'macro',unit:'index_points',source:'yfinance',provider_symbol:'^GSPC',observed_at:null,cur:6650,ref:6680,change:ok(-30,-0.4491,'index_points'),freshness:{...f('unknown_time'),age_seconds:null}}),
      row('yld.us10y',{symbol:'US10Y',asset_class:'rates',instrument_type:'yield',origin:'macro',unit:'percent',basis:'previous_observation',cur:4.05,ref:4,change:ok(0.05,null,'percent',5)}),
      row('fx.usdjpy',{symbol:'USD/JPY',asset_class:'fx',instrument_type:'fx_spot',origin:'macro',unit:'JPY_per_USD',basis:'previous_observation',cur:151.5,ref:150,change:ok(1.5,1,'JPY_per_USD')}),
      row('crypto.btcusd',{symbol:'BTC/USD',asset_class:'crypto',instrument_type:'crypto_spot',origin:'macro',unit:'USD_per_BTC',basis:'utc_day_open',session:'continuous',cur:82000,ref:80000,change:ok(2000,2.5)}),
    ],
    gaps:[{id:'yld.us02y',reason:'not_collected'},{id:'fut.si.front',reason:'not_collected'}],
    notes:['An as-of collection of asynchronous observations, not a simultaneous snapshot.']};
}
async function writeFixtures(root){for(const [key,value] of Object.entries(fixtures())){const target=path.join(root,key);await fs.mkdir(path.dirname(target),{recursive:true});await fs.writeFile(target,value);}}
module.exports={fixtures,writeFixtures,SESSION,NOW,RUN,ARCHIVE,CONTRACT,PUT,SPY_CONTRACT};


