import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync, cpSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { execFileSync } from 'node:child_process';
import gateway from '../frontend/worker.mjs';
import { createPreview, startPreview, assetBinding, LOCAL_ORIGIN } from '../scripts/preview-local.mjs';
import { buildAssets, verifyArtifact, ROOT, FLOOR } from '../scripts/build-assets.mjs';

const request=path=>new Request(LOCAL_ORIGIN+path);
test('preview transformation drops stale entity headers and requests uncompressed bytes',async()=>{
  const p=createPreview();
  try {
    p.env.ASSETS={fetch:async r=>{assert.equal(r.headers.get('Accept-Encoding'),'identity');
      return new Response('<html><body>short</body></html>',{headers:{'Content-Type':'text/html','Content-Length':'30',ETag:'old','Last-Modified':'yesterday'}});}};
    const r=await p.fetch(request('/'));
    assert.ok((await r.text()).length>30);
    for(const h of ['Content-Length','ETag','Last-Modified']) assert.equal(r.headers.get(h),null);
    p.env.ASSETS={fetch:async()=>new Response('compressed',{headers:{'Content-Type':'text/html','Content-Encoding':'gzip'}})};
    assert.equal((await p.fetch(request('/'))).status,502);
    p.env.ASSETS={fetch:async()=>new Response(null,{status:304,headers:{'Content-Type':'text/html'}})};
    assert.equal((await p.fetch(request('/'))).status,304);
  } finally {p.close();}
});
test('public fixture service cannot set browser cookies or redirects',async()=>{
  const p=createPreview();
  try {
    p.env.PUBLIC_DATA={fetch:async()=>new Response('redirect',{status:302,headers:{Location:'https://production.invalid/api/login','Set-Cookie':'bad=1','Content-Type':'text/plain'}})};
    const r=await p.fetch(request('/browser-data/manifest.json'));
    assert.equal(r.status,302);assert.equal(r.headers.get('Location'),null);assert.equal(r.headers.get('Set-Cookie'),null);
  }finally{p.close();}
});
test('static binding refuses junction escape, malformed encoding, documentation and traversal',async()=>{
  const root=mkdtempSync(resolve(tmpdir(),'musef-static-'));
  const publicRoot=resolve(root,'public'),outside=resolve(root,'outside');mkdirSync(publicRoot);mkdirSync(outside);
  writeFileSync(resolve(outside,'secret.txt'),'OUTSIDE');writeFileSync(resolve(publicRoot,'DEPLOYMENTS.md'),'private notes');
  // Windows directory junctions need no symlink privilege; Unix uses a directory link.
  symlinkSync(outside,resolve(publicRoot,'linked'),process.platform==='win32'?'junction':'dir');
  const assets=assetBinding(publicRoot);
  for(const path of ['/linked/secret.txt','/DEPLOYMENTS.md','/%2e%2e/outside/secret.txt']) assert.equal((await assets.fetch(request(path))).status,404);
  assert.equal((await assets.fetch(request('/bad%ZZ'))).status,400);
});
function localHttp(path,headers={},body=null){return new Promise((ok,fail)=>{
  const r=http.request({host:'127.0.0.1',port:8787,path,method:body?'POST':'GET',headers:{Host:'localhost:8787',...headers}},res=>{
    const chunks=[];res.on('data',b=>chunks.push(b));res.on('end',()=>ok({status:res.statusCode,headers:res.headers,body:Buffer.concat(chunks).toString()}));
  });r.on('error',fail);if(body)r.write(body);r.end();
});}
test('actual loopback adapter preserves multiple cookies and rejects wrong host/origin',async()=>{
  const p=await startPreview();
  try {
    const r=await localHttp('/__environment');assert.equal(r.status,200);assert.equal(JSON.parse(r.body).isolation,'disposable');
    p.env.API={fetch:async()=>new Response('cookies',{headers:[['Set-Cookie','a=1; Path=/; HttpOnly'],['Set-Cookie','b=; Max-Age=0; Path=/; HttpOnly']]})};
    const cookies=await localHttp('/api/me');assert.equal(cookies.headers['set-cookie'].length,2);
    assert.match(cookies.headers['set-cookie'][1],/Max-Age=0/);
    assert.equal((await localHttp('/',{Host:'production.invalid'})).status,403);
    assert.equal((await localHttp('/api/logout',{Origin:'https://options.moopertonic.net'},'{}')).status,403);
    assert.equal((await localHttp('/api/register',{Origin:LOCAL_ORIGIN},'x'.repeat(20000))).status,413);
  }finally{await p.stop();}
});
test('tracked inventory excludes Git-ignored files and rejects misleading inside-source output',()=>{
  const root=mkdtempSync(resolve(tmpdir(),'musef-tracked-'));
  mkdirSync(resolve(root,'site'));mkdirSync(resolve(root,'backend/migrations'),{recursive:true});
  cpSync(resolve(ROOT,'frontend'),resolve(root,'frontend'),{recursive:true});
  cpSync(resolve(ROOT,'backend/migrations'),resolve(root,'backend/migrations'),{recursive:true});
  writeFileSync(resolve(root,'site/index.html'),'const BUILD_ID = "__BUILD_ID__";\n');
  writeFileSync(resolve(root,'.gitignore'),'__pycache__/\n');
  execFileSync('git',['init','--quiet'],{cwd:root});execFileSync('git',['add','.'],{cwd:root});
  mkdirSync(resolve(root,'site/__pycache__'));writeFileSync(resolve(root,'site/__pycache__/secret.json'),'UNREVIEWED');
  const out=resolve(mkdtempSync(resolve(tmpdir(),'musef-out-')),'release');
  const manifest=buildAssets({root,out,revision:FLOOR});assert.equal(manifest.files['site/__pycache__/secret.json'],undefined);
  assert.throws(()=>buildAssets({root,out:resolve(root,'..release'),revision:FLOOR}),/outside source worktree/);
  const file=resolve(out,'release.json');manifest.schema=[];writeFileSync(file,JSON.stringify(manifest));
  assert.throws(()=>verifyArtifact(out,{...manifest,schema:[]}),/schema contract mismatch/);
});
