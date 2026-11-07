// Shipped panel/CSS and Worker route, synthetic production-origin interception.
// O2_EVIDENCE_DIR must be a new directory; O2_BASELINE=1 records pre-fix behavior.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const APP = 'https://live-options-view.otmooper12.workers.dev';
const html = fs.readFileSync(path.join(__dirname,'../docs/index.html'),'utf8');
const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];
const panel = html.match(/<div id="steo-panel"[\s\S]*?<\/div>/)[0];
const from = html.indexOf('async function fetchSteoCalibration()');
const source = html.slice(from,html.indexOf('// ── short-squeeze scanner panel',from));
const routing = html.slice(html.indexOf('const R2 ='),html.indexOf('const REFRESH'));
const shared = fs.readFileSync(path.join(__dirname,'../docs/shared.js'),'utf8');
const fixture = {timestamp:'2026-10-09T10:00:00Z',current_release:'2026-10',prior_release:'2026-09',
 points:[{period:'2026-10',brent:72,wti:67,balance:-0.5},{period:'2026-11',brent:73,wti:68,balance:-0.7}],
 revisions:[{period:'2026-10',brent_delta:2,wti_delta:-1,balance_delta:-0.5}],ovx:{value:40,regime:'elevated'}};
const cases = [
 ['available',200,fixture,true],['absent',404,{},false],['recovered',200,fixture,true],
 ['empty',200,{points:[]},false],['malformed-json',200,'{',false],
 ['malformed-points',200,{points:{length:1}},false],
 ['malformed-value',200,{...fixture,points:[{period:'2026-10',brent:'bad'}]},false],
 ['missing-release',200,{...fixture,current_release:undefined},false],
 ['http-failure',503,{},false],['fetch-failure',0,{},false],
 ['recovery-new-values',200,{...fixture,points:[{period:'2026-10',brent:81,wti:76,balance:0.3}]},true],
 ];
(async()=>{
 const evidence=process.env.O2_EVIDENCE_DIR; assert.ok(evidence); fs.mkdirSync(evidence,{recursive:false});
 const baseline=process.env.O2_BASELINE==='1';
 const {default:worker}=await import('../worker.js');
 let mode; const calls=[], unexpected=[], sockets=[], warnings=[], errors=[],results=[];
 const originalFetch=global.fetch;
 global.fetch=async(url,options)=>{
  assert.equal(new URL(url).pathname,'/macro/eia_steo.json');
  assert.equal(options.credentials,'omit'); assert.equal(options.redirect,'manual');
  calls.push({url,options});
  return new Response(typeof mode[2]==='string'?mode[2]:JSON.stringify(mode[2]),{status:mode[1],headers:{'Content-Type':'application/json'}});
 };
 const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_CHROMIUM_PATH});
 try {
  for(const width of [1280,390,320]) {
   const context=await browser.newContext({viewport:{width,height:650},serviceWorkers:'block',acceptDownloads:false,locale:'en-US',timezoneId:'America/Los_Angeles'});
   await context.routeWebSocket('**/*',ws=>{sockets.push(ws.url());ws.close();});
   await context.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(route.request().method()==='GET'&&url.href===APP+'/') return route.fulfill({contentType:'text/html',body:'<!doctype html><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>'+css+'</style><h1>Macro · synthetic fixture</h1>'+panel+'<p id="following">Following content</p>'});
    if(url.origin===APP&&url.pathname==='/browser-data/macro/eia_steo.json'&&route.request().method()==='GET') {
     if(mode[1]===0) return route.abort('failed');
     const r=await worker.fetch(new Request(url),new Proxy({},{get(){throw Error('No bindings allowed');}}));
     return route.fulfill({status:r.status,body:Buffer.from(await r.arrayBuffer()),headers:Object.fromEntries(r.headers)});
    }
    unexpected.push(url.href);return route.abort();
   });
   const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='warning')warnings.push(m.text());});
   await page.clock.install({time:new Date('2026-10-09T12:00:00Z')});
   await page.goto(APP+'/');await page.addScriptTag({content:shared});await page.addScriptTag({content:routing+'\n'+source});
   for(mode of cases) {
    await page.evaluate(()=>fetchSteoCalibration());
    const state=await page.evaluate(()=>{
     const p=document.getElementById('steo-panel'),r=p.getBoundingClientRect();
     return {display:getComputedStyle(p).display,height:r.height,hiddenClass:p.classList.contains('hidden'),text:p.innerText,
      brent:document.getElementById('steo-brent').textContent,overflow:document.documentElement.scrollWidth>innerWidth,
      followingY:document.getElementById('following').getBoundingClientRect().y};
    });
    results.push({width,case:mode[0],expectedVisible:mode[3],...state});
    if(['available','absent','missing-release','recovery-new-values'].includes(mode[0]))await page.screenshot({path:path.join(evidence,`${mode[0]}-${width}.png`),fullPage:true});
    if(!baseline){
     assert.equal(state.display!=='none',mode[3],`${width} ${mode[0]} computed visibility`);
     assert.equal(state.overflow,false,`${width} ${mode[0]} overflow`);
     if(!mode[3])assert.equal(state.height,0,'no empty strip');
     if(mode[0]==='available'){assert.equal(state.brent,'$72.00');assert.match(state.text,/EIA STEO/);assert.match(state.text,/FORECAST\s*2026-10/);}
     if(mode[0]==='recovery-new-values')assert.equal(state.brent,'$81.00');
    }
   }
   await context.close();
  }
  assert.deepEqual(unexpected,[]);assert.deepEqual(sockets,[]);assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(evidence,'results.json'),JSON.stringify({baseline,results,calls,warnings,unexpected,sockets,errors},null,2));
  console.log(JSON.stringify({baseline,cases:results.length,unexpected,sockets,errors,results:results.filter(r=>r.width===320)}));
 } finally {await browser.close();global.fetch=originalFetch;}
})().catch(e=>{console.error(e);process.exitCode=1;});
