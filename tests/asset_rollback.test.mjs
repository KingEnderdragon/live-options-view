import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { buildAssets, verifyArtifact, FLOOR } from '../scripts/build-assets.mjs';
import { createPreview, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
import { pathToFileURL } from 'node:url';
import { assetBinding } from '../scripts/preview-local.mjs';
import { prepareRelease } from '../scripts/prepare-asset-release.mjs';

function artifacts() {
  const parent=mkdtempSync(resolve(tmpdir(),'musef-rollback-'));
  const old=resolve(parent,'old'), current=resolve(parent,'current');
  // Two synthetic release IDs exercise the artifact protocol. They are not
  // claims that either source revision was deployed or checked out here.
  const before=buildAssets({out:old,revision:FLOOR,now:new Date('2026-11-11T22:00:00Z')});
  const after=buildAssets({out:current,revision:'a'.repeat(40),now:new Date('2026-11-11T22:01:00Z')});
  return {old,current,before,after};
}
test('asset-only rollback preserves API instance, account/session, schema and producer targets',async()=>{
  const {old,current,before,after}=artifacts();
  assert.deepEqual(verifyArtifact(old,after),before);
  const p=createPreview({assetRoot:resolve(current,'site'),revision:after.revision});
  let gateway=(await import(pathToFileURL(resolve(current,'frontend/worker.mjs')).href)).default;
  const call=(path,init)=>gateway.fetch(new Request(LOCAL_ORIGIN+path,init),p.env);
  try {
    const registered=await call('/api/register',{method:'POST',headers:{Origin:LOCAL_ORIGIN,'Content-Type':'application/json'},
      body:JSON.stringify({username:'rollback_reader',password:'disposable-password'})});
    assert.equal(registered.status,201);
    const cookie=registered.headers.get('Set-Cookie').split(';')[0];
    const api=p.env.API, db=p.apiEnv.DB;
    const head=await (await call('/')).text(); assert.match(head,/aaaaaaa-/);
    // This is the entire rollback action in local emulation: only asset binding
    // and observable asset revision change. No API deploy/schema command exists.
    p.env.ASSETS=assetBinding(resolve(old,'site')); p.env.ASSET_REVISION=before.revision;
    gateway=(await import(pathToFileURL(resolve(old,'frontend/worker.mjs')).href)).default;
    const restored=await (await call('/')).text(); assert.match(restored,/90cdc56/); assert.doesNotMatch(restored,/aaaaaaa-/);
    assert.equal(p.env.API,api); assert.equal(p.apiEnv.DB,db);
    assert.equal((await call('/api/me',{headers:{Cookie:cookie}})).status,200);
    assert.deepEqual(after.targets,before.targets); assert.deepEqual(before.targets.producers,[]);
    assert.deepEqual(before.schema,after.schema);
  } finally {p.close();}
});
for (const [label,mutate,pattern] of [
  ['legacy KV serving',m=>m.legacy_kv_serving=true,/rollback contract/],
  ['recovery serving',m=>m.recovery='serving',/rollback contract/],
  ['pre-D1 floor',m=>m.serving_rollback_floor='0'.repeat(40),/rollback contract/],
  ['API target drift',m=>m.targets.api='production-other',/rollback contract/],
  ['producer promotion',m=>m.targets.producers=['main-collector'],/rollback contract/],
  ['D1 schema mismatch',m=>m.schema[0].sha256='0'.repeat(64),/schema contract mismatch/],
]) test(`rollback refuses ${label}`,()=>{
  const {old,after}=artifacts(); const file=resolve(old,'release.json');
  const manifest=JSON.parse(readFileSync(file,'utf8'));mutate(manifest);writeFileSync(file,JSON.stringify(manifest));
  assert.throws(()=>verifyArtifact(old,after),pattern);
});
test('release/rollback staging changes only asset target and never executes a provider command',()=>{
  const {old,current,before}=artifacts();
  const stage=resolve(mkdtempSync(resolve(tmpdir(),'musef-stage-')),'release');
  const plan=prepareRelease({artifact:old,current,out:stage});
  assert.equal(plan.asset_revision,before.revision);assert.equal(plan.asset_target,'live-options-view');
  assert.equal(plan.unchanged_api_target,'live-options-api');assert.deepEqual(plan.producer_actions,[]);
  assert.deepEqual(plan.schema_actions,[]);assert.deepEqual(plan.recovery_actions,[]);
  assert.equal(plan.state,'prepared-not-approved-not-deployed');
  assert.deepEqual(verifyArtifact(resolve(stage,'payload')),before);
  const config=readFileSync(resolve(stage,'wrangler.assets.toml'),'utf8');
  assert.match(config,/run_worker_first = true/);assert.match(config,/payload\/frontend\/worker.mjs/);
  assert.doesNotMatch(config,/\[\[(r2_buckets|d1_databases|kv_namespaces)\]\]|\[triggers\]/);
  assert.throws(()=>prepareRelease({artifact:old,out:resolve(old,'nested')}),/outside artifact/);
});
test('rollback refuses tampered payload and unmanifested operational file',()=>{
  const {old,current}=artifacts();
  writeFileSync(resolve(old,'site/index.html'),'tampered');
  assert.throws(()=>verifyArtifact(old),/Hash mismatch/);
  writeFileSync(resolve(current,'collector.py'),'not an asset');
  assert.throws(()=>verifyArtifact(current),/inventory mismatch/);
});
