'use strict';
const test=require('node:test');const assert=require('node:assert/strict');
const {Consumer}=require('./consumer');const {Source,DataError,hash,LIMITS,permitted}=require('./source');
const {Protocol,tools}=require('./server');
const {gzipSync}=require('node:zlib');
const {fixtures,SESSION,NOW,CONTRACT}=require('./fixtures');
const BASE=`oa203/scanner/${SESSION}/`, ACTIVE=BASE+'inprogress/';
function setup(){
  const files=fixtures(), calls=[];
  const manifest=files[BASE+'sweeps/manifest.jsonl'].trim().split('\n').map(JSON.parse).slice(0,6)
    .map(m=>({...m,sha256:hash(files[BASE+'sweeps/'+m.file])}));
  delete files[BASE+'summary.json'];
  const prefix=ACTIVE+'sweep_0006/';
  files[prefix+'leaderboard.csv']=files[BASE+'leaderboard.csv'];
  files[prefix+'qualified_ask_bid_v1.csv']=files[BASE+'qualified_ask_bid_v1.csv'];
  files[prefix+'manifest.jsonl']=manifest.map(m=>JSON.stringify(m)).join('\n')+'\n';
  const members=Object.fromEntries(['leaderboard.csv','qualified_ask_bid_v1.csv','manifest.jsonl'].map(n=>[n,hash(files[prefix+n])]));
  const cutoff=manifest.at(-1).ended_ms;
  const summary={schema_version:1,trade_date:SESSION,final:false,state:'in_progress',cutoff_sweep:6,cutoff_ms:cutoff,
    session_open_ms:1790947800000,session_close_ms:1790971200000,contracts:2,
    assessment:{status:'partial',reasons:['session_in_progress'],completed_sweeps:6,truncated_sweeps:0,chain_status_counts:{ok:6}},
    return_policy:{version:'oa203-returns-v1'},qualified_policy:{version:'oa203-qualified-ask-bid-v1'},
    outputs:{leaderboard:'leaderboard.csv',qualified_ask_bid_v1:'qualified_ask_bid_v1.csv'},member_sha256:members,built_at:'2026-10-02T13:56:00Z'};
  files[prefix+'summary.json']=JSON.stringify(summary);
  const pointer={schema_version:1,trade_date:SESSION,state:'in_progress',sweep:6,cutoff_ms:cutoff,archive:prefix.slice(0,-1),
    members:{...members,'summary.json':hash(files[prefix+'summary.json'])},built_at:summary.built_at,published_at:'2026-10-02T13:56:10Z',expected_cadence_s:1800};
  files[ACTIVE+'latest.json']=JSON.stringify(pointer);
  const source=new Source({read:async key=>{calls.push(key);if(!Object.hasOwn(files,key))throw new DataError('missing_artifact','absent',key);return files[key];}});
  const consumer=new Consumer(source,{now:()=>NOW});
  return {files,calls,consumer,prefix,pointer,summary,manifest};
}
test('active all, clean, qualified and pinned detail survive pointer advance',async()=>{
  const f=setup();
  const all=await f.consumer.call('active_rankings',{session:SESSION,view:'all',limit:1});
  assert.equal(all.status,'in_progress');assert.equal(all.actual.sweep,6);assert.equal(all.rows[0].symbol,CONTRACT);
  assert.equal(all.freshness.status,'stale');
  const clean=await f.consumer.call('active_rankings',{session:SESSION,view:'clean'});
  assert.equal(clean.rows.length,1);assert.equal(clean.rows[0].clean_rank,'1');
  const qualified=await f.consumer.call('active_rankings',{session:SESSION,view:'qualified_ask_bid_v1'});
  assert.equal(qualified.rows[0].qualified_first_ask_to_later_bid_rank,'1');
  f.files[ACTIVE+'latest.json']='{}';
  const detail=await f.consumer.call('result_detail',{reference:all.rows[0].detail_reference,limit:6});
  assert.equal(detail.status,'in_progress');assert.equal(detail.actual.sweep,6);
  assert.equal(detail.quote_path.length,2);assert.ok(detail.quote_path.every(r=>r.sweep<=6));
  assert.equal(detail.pagination.next_offset,2);
  const last=await f.consumer.call('result_detail',{reference:all.rows[0].detail_reference,limit:6,offset:4});
  assert.equal(last.quote_path.length,2);assert.equal(last.quote_path.at(-1).sweep,6);
  assert.equal(last.pagination.next_offset,null);
  assert.ok(!f.calls.some(k=>k.includes('sweep_0007')));
});
test('active pointer and immutable member mismatches fail closed',async()=>{
  for(const mutate of [f=>f.pointer.members['summary.json']='0'.repeat(64),f=>f.files[f.prefix+'leaderboard.csv']+='\n']){
    const f=setup();mutate(f);f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
    const out=await f.consumer.call('active_rankings',{session:SESSION});
    assert.equal(out.status,'failed');assert.equal(out.errors.at(-1).code,'source_changed');
  }
});
test('pinned detail rejects changed source sweep and never mixes quote path',async()=>{
  const f=setup(),first=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  f.files[BASE+'sweeps/sweep_0001.jsonl.gz']=gzipSync('{"symbol":"'+CONTRACT+'"}\n');
  const out=await f.consumer.call('result_detail',{reference:first.rows[0].detail_reference,limit:2});
  assert.equal(out.status,'failed');assert.equal(out.errors[0].code,'source_changed');
  assert.equal(out.quote_path,undefined);
});
test('missing pinned source sweep is partial with an exact retry offset',async()=>{
  const f=setup(),first=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  delete f.files[BASE+'sweeps/sweep_0002.jsonl.gz'];
  const out=await f.consumer.call('result_detail',{reference:first.rows[0].detail_reference,limit:2});
  assert.equal(out.status,'partial');assert.equal(out.errors[0].code,'missing_artifact');
  assert.deepEqual(out.pagination.retry_offsets,[1]);assert.equal(out.quote_path.length,1);
});
test('active missing, before-open, holiday and invalid filters stay explicit',async()=>{
  const f=setup();delete f.files[ACTIVE+'latest.json'];
  assert.equal((await f.consumer.call('active_rankings',{session:SESSION})).status,'missing');
  f.consumer.now=()=>Date.parse('2026-10-02T12:00:00Z');
  assert.equal((await f.consumer.call('active_rankings',{session:'current'})).status,'before_open');
  assert.equal((await f.consumer.call('active_rankings',{session:'2026-10-03'})).status,'holiday');
  for(const a of [{session:'../secret'},{session:SESSION,sweep:10000},{session:SESSION,url:'https://example.com'},{session:SESSION,limit:51}]){
    const g=setup(),out=await g.consumer.call('active_rankings',a);
    assert.equal(out.status,'failed');assert.equal(g.calls.length,0);
  }
  assert.throws(()=>permitted(BASE+'inprogress/sweep_0006/secrets.json'),/allowlist/);
});
test('active detail and paging retain request and byte bounds',async()=>{
  const f=setup();const first=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  const detail=await f.consumer.call('result_detail',{reference:first.rows[0].detail_reference,limit:50,offset:3});
  assert.equal(detail.quote_path.length,2);assert.equal(detail.pagination.limit,2);
  assert.ok(f.calls.length<=LIMITS.requests*2);
});
test('newer failed attempt stays visible beside last successful snapshot',async()=>{
  const f=setup();f.files[ACTIVE+'latest-attempt.json']=JSON.stringify({schema_version:1,trade_date:SESSION,
    sweep:12,status:'failed',reason:'timeout',at:'2026-10-02T15:00:00Z'});
  const out=await f.consumer.call('active_rankings',{session:SESSION,limit:1});
  assert.equal(out.status,'in_progress');assert.equal(out.actual.sweep,6);
  assert.equal(out.attempt.status,'failed');assert.ok(out.warnings.some(w=>w.includes('timeout')));
});
test('a final daily result supersedes latest active only through the final producer path',async()=>{
  const f=setup();f.files[BASE+'summary.json']=fixtures()[BASE+'summary.json'];
  const out=await f.consumer.call('active_rankings',{session:SESSION,view:'clean',limit:1});
  assert.equal(out.status,'final');assert.equal(out.active_state,'final');assert.equal(out.rows.length,1);
  const detail=await f.consumer.call('result_detail',{reference:out.rows[0].detail_reference,limit:1});
  assert.equal(detail.status,'available');assert.equal(detail.actual.final,true);
});
test('early-close boundary marks a still-published snapshot as pending final',async()=>{
  const f=setup();f.summary.session_close_ms=Date.parse('2026-10-02T16:00:00Z');
  f.files[f.prefix+'summary.json']=JSON.stringify(f.summary);
  f.pointer.members['summary.json']=hash(f.files[f.prefix+'summary.json']);
  f.files[ACTIVE+'latest.json']=JSON.stringify(f.pointer);
  f.consumer.now=()=>Date.parse('2026-10-02T16:10:00Z');
  const out=await f.consumer.call('active_rankings',{session:SESSION});
  assert.equal(out.status,'closed_pending_final');assert.equal(out.active_state,'closed_pending_final');
});
test('MCP discovery, active query and pinned detail use read-only bounded schemas',async()=>{
  const f=setup(),p=new Protocol(f.consumer);
  assert.ok(tools.find(t=>t.name==='active_rankings').annotations.readOnlyHint);
  await p.handle({jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-11-25',
    capabilities:{},clientInfo:{name:'active-fixture',version:'1'}}});
  await p.handle({jsonrpc:'2.0',method:'notifications/initialized'});
  const call=async(name,args)=>(await p.handle({jsonrpc:'2.0',id:2,method:'tools/call',
    params:{name,arguments:args}})).result.structuredContent;
  const discovery=await call('discover_sources',{session:SESSION});
  assert.equal(discovery.capabilities.at(-1).dataset,'options_active');
  const ranking=await call('active_rankings',{session:SESSION,limit:1});
  const detail=await call('result_detail',{reference:ranking.rows[0].detail_reference,limit:1});
  assert.equal(detail.actual.archive,ranking.actual.archive);
  assert.equal(detail.quote_path[0].sweep,1);
});
