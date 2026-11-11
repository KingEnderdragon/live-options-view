// Purely local staging/plan generation. No provider SDK, deploy, migration or restart.
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, relative, sep, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ROOT, verifyArtifact } from './build-assets.mjs';
export function prepareRelease({artifact,out,current=null}) {
  const manifest=verifyArtifact(artifact,current?verifyArtifact(current):null);
  const staging=resolve(out);
  for (const base of [resolve(artifact),ROOT]) {
    const rel=relative(base,staging);
    if(!isAbsolute(rel)&&rel!=='..'&&!rel.startsWith('..'+sep))throw new Error('Stage outside artifact and source');
  }
  if(existsSync(staging))throw new Error('Staging directory must be new');
  mkdirSync(staging,{recursive:true});
  cpSync(resolve(artifact),resolve(staging,'payload'),{recursive:true});
  verifyArtifact(resolve(staging,'payload'),manifest);
  const config=readFileSync(resolve(ROOT,'patches/musef-225/wrangler.assets.toml.proposed'),'utf8');
  writeFileSync(resolve(staging,'wrangler.assets.toml'),config);
  const plan={state:'prepared-not-approved-not-deployed',asset_revision:manifest.revision,
    artifact:resolve(staging,'payload'),asset_target:manifest.targets.assets,
    unchanged_api_target:manifest.targets.api,producer_actions:[],schema_actions:[],
    recovery_actions:[],requires:'Nova C2 topology/trigger/resource review and existing production authority',
    proposed_command:`wrangler deploy --config "${resolve(staging,'wrangler.assets.toml')}" --var ASSET_REVISION:${manifest.revision}`};
  writeFileSync(resolve(staging,'plan.json'),JSON.stringify(plan,null,2)+'\n');
  return plan;
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const args=process.argv.slice(2),get=k=>args[args.indexOf(k)+1];
  if(!args.includes('--artifact')||!args.includes('--out'))throw new Error('Usage: --artifact VERIFIED_ARTIFACT --out NEW_STAGING [--current CURRENT_ARTIFACT]');
  console.log(JSON.stringify(prepareRelease({artifact:get('--artifact'),out:get('--out'),current:args.includes('--current')?get('--current'):null})));
}
