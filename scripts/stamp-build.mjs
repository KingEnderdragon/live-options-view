import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Shared packaging step for Actions and the provider Build command.
// This script does not deploy. A fresh source checkout is required each time.
export function stampHtml(html, revision, now = new Date()) {
  if (!/^[0-9a-f]{40}$/.test(revision)) throw new Error('Expected full Git commit SHA');
  if (!(now instanceof Date) || !Number.isFinite(now.getTime())) throw new Error('Invalid build time');
  const declarations = [...html.matchAll(/^const BUILD_ID = "([^"]*)";\r?$/gm)];
  if (declarations.length !== 1 || declarations[0][1] !== '__BUILD_ID__' ||
      html.split('__BUILD_ID__').length !== 2) {
    throw new Error('Expected exactly one unstamped BUILD_ID declaration in fresh source');
  }
  const buildId = `${revision.slice(0, 7)}-${now.toISOString().slice(0, 19).replace(/\D/g, '')}`;
  const output = html.replace('const BUILD_ID = "__BUILD_ID__";', `const BUILD_ID = "${buildId}";`);
  const stamped = [...output.matchAll(/^const BUILD_ID = "([^"]*)";\r?$/gm)];
  if (output.includes('__BUILD_ID__') || stamped.length !== 1 || stamped[0][1] !== buildId) {
    throw new Error('Build ID verification failed');
  }
  return { html: output, receipt: { commit: revision, build_id: buildId,
    asset: 'site/index.html', bytes: Buffer.byteLength(output),
    sha256: createHash('sha256').update(output).digest('hex') } };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const revision = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
  const target = resolve(root, 'site/index.html');
  const { html, receipt } = stampHtml(readFileSync(target, 'utf8'), revision);
  writeFileSync(target, html);
  if (createHash('sha256').update(readFileSync(target)).digest('hex') !== receipt.sha256) {
    throw new Error('Written asset does not match packaging receipt');
  }
  console.log(JSON.stringify(receipt));
}
