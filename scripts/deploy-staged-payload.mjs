// scripts/deploy-staged-payload.mjs — G3b staged-payload promotion gate.
// Local only. Default is --dry-run: print the plan's command and exit 0 only
// if every guard passes; nothing executes. --execute requires an authorization
// record for the exact revision and rechecks every guard immediately before
// executing, under a shared release lock, so an old candidate A cannot
// overwrite a newer authorized candidate B. The workflow that calls this is
// a local proposal until the C2 packet is approved; this script never
// deploys on its own, never merges, and never touches a collector.
//
// B1 — Authorization is EXTERNAL to the immutable candidate. The record
// lives in DEPLOY_RELEASES_DIR, which defaults to a sibling
// `.deploy-releases` directory OUTSIDE the candidate tree (never
// docs/releases inside it). Authorizing therefore never moves HEAD, so the
// HEAD == record == plan == manifest check is satisfiable: the record binds
// the revision AND its tree SHA (head_tree), and the gate re-verifies both
// at deploy time. A record inside the tree is a paradox (commit it and HEAD
// moves: old revision -> Revision mismatch, new HEAD -> No record), so the
// gate refuses any record found inside the tree. The applied-source asset
// config is a C2 precondition: it must exist at the repo root before any
// real cutover; tests use a fixture applied config, never a faked real one.
//
// B2 — This gate is the ONLY deploy mechanism. The old automatic
// deploy.yml push-deploy is removed in the local proposal; both workflows
// (deploy-staged.yml for assets, deploy.yml for the API) are
// workflow_dispatch-only, share the deploy-live-options-view concurrency
// group, and call only this script. The API lane is a separate explicit
// dispatch (--target api) with its own authorization record; it deploys
// only the API service (wrangler.api.toml) and has no producer effects.
// It must never deploy the combined public worker (wrangler.toml): the
// api target is bound to wrangler.api.toml by configPath, command and
// recorded hash, and a deploy against any other config is refused.
//
// C2 repair (2026-11-11) — deployment order and rollback. The asset
// service's API binding points at the live-options-api service, so the API
// service must exist before any asset authorize or deploy; the gate
// refuses an asset step until an API deploy receipt or an operator-recorded
// API-service verification exists for the revision. The combined public
// worker is REPLACED by the asset service (same name live-options-view);
// it is not kept alongside. Rollback is a separate lane (--target
// rollback) that deploys only wrangler.toml to restore the combined
// worker, and only under a rollback authorization with a recorded reason.
// Receipts are per-target so an asset receipt cannot overwrite the API
// receipt that proves the API service was deployed.
//
// B3 — One shared release lock for the whole release target, held in the
// external releases dir — not one lock per stage. authorize, supersede and
// deploy all take the same lock, so supersession cannot interleave with
// execution. Cross-runner serialization is the workflow concurrency group;
// the shared lock is the on-machine authority. The final recheck
// (recheckBeforeExecute) re-reads the authorization record, the plan and
// HEAD immediately before the executor runs, inside the lock; a stale
// candidate A never executes after B supersedes it.
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ROOT, verifyArtifact } from './build-assets.mjs';
import { transitionPlanState, verifyStage } from './prepare-asset-release.mjs';

// External to the candidate tree by default. DEPLOY_RELEASES_DIR (tests/CI)
// wins; otherwise a sibling directory next to the checkout. Never inside
// the tree: authorizing must not move HEAD (B1).
const releasesDir = () => process.env.DEPLOY_RELEASES_DIR || resolve(ROOT, '..', '.deploy-releases');
// Which checkout the gate verifies. Tests point it at an isolated clone.
const repoRoot = () => process.env.DEPLOY_REPO_ROOT || ROOT;
// The API lane deploys ONLY wrangler.api.toml (the separate API service).
// The combined public worker (wrangler.toml) is never an api target: the
// config is read and hashed here, and every later check re-reads it.
export const API_CONFIG_FILE = 'wrangler.api.toml';
export function apiConfig() {
  const path = resolve(repoRoot(), API_CONFIG_FILE);
  if (!existsSync(path)) throw new Error(`API config missing: ${API_CONFIG_FILE} must exist at the repo root (C2 precondition)`);
  return { path, sha256: createHash('sha256').update(readFileSync(path)).digest('hex') };
}
// The rollback lane deploys ONLY wrangler.toml (the combined public
// worker), and only as a restoration (target 'rollback'). It is the single
// lane that may deploy the combined config; the api lane can never deploy
// it, and the asset lane deploys only the staged asset config. A rollback
// record is a separate authorization bound to the combined config hash
// and the recorded combined code revision.
export const ROLLBACK_CONFIG_FILE = 'wrangler.toml';
export function rollbackConfig() {
  const path = resolve(repoRoot(), ROLLBACK_CONFIG_FILE);
  if (!existsSync(path)) throw new Error(`Rollback config missing: ${ROLLBACK_CONFIG_FILE} must exist at the repo root`);
  return { path, sha256: createHash('sha256').update(readFileSync(path)).digest('hex') };
}
const authorizationPath = revision => resolve(releasesDir(), `${revision}.authorization.json`);
// Receipts are per-target: an api receipt and an asset receipt for the same
// revision are different evidence and must not overwrite each other (the
// asset receipt used to overwrite the api receipt, which destroyed the
// API-readiness evidence the asset order check needs).
const receiptPath = (revision, target) => resolve(releasesDir(), `${revision}.${target}.deploy-receipt.json`);
const apiServiceVerifiedPath = revision => resolve(releasesDir(), `${revision}.api-service-verified.json`);
// Deployment order (C2 repair): the asset service's API binding points at
// the live-options-api service, so the API service must exist before any
// asset authorize or deploy. Existence is recorded one of two ways: an API
// deploy receipt for the same revision (the api lane ran through this
// gate), or an operator-recorded verification (the operator read back the
// live service list and recorded it). There is no provider call here —
// the gate cannot test the provider locally, so the operator's readback
// is the evidence and it is recorded, not assumed.
export function apiServiceReady(revision) {
  // Existence is a property of the NAMED service, not of the revision:
  // an API deploy receipt or operator verification for ANY revision proves
  // the service was created. A per-revision check would wrongly refuse a
  // new asset candidate while the API service from an earlier candidate
  // still exists (the binding references the service name, not a code
  // revision). If the service was deleted after being created, the receipt
  // is stale — the operator re-records a fresh verification in that case.
  if (!existsSync(releasesDir())) return false;
  for (const name of readdirSync(releasesDir())) {
    if (name.endsWith('.api.deploy-receipt.json') || name.endsWith('.api-service-verified.json')) return true;
  }
  return false;
}
export function assertApiServiceReady(revision) {
  if (!apiServiceReady(revision)) {
    throw new Error('API service not ready: deploy the API service first (--target api) or record an operator verification that live-options-api exists; an asset deploy now would break the API service binding');
  }
}
export function recordApiServiceVerified({ revision, verifier }) {
  if (!/^[a-f0-9]{40}$/.test(revision)) throw new Error('Full reviewed revision required');
  if (!verifier) throw new Error('API service verification requires --authorizer (the verifying operator)');
  const lock = acquireReleaseLock();
  try {
    const record = { revision, service: 'live-options-api', verified_at: new Date().toISOString(), verifier, verification_kind: 'operator-readback' };
    mkdirSync(releasesDir(), { recursive: true });
    writeFileSync(apiServiceVerifiedPath(revision), JSON.stringify(record, null, 2) + '\n');
    return record;
  } finally {
    lock.release();
  }
}
const STALE_LOCK_MS = 10 * 60 * 1000;

function parseCheck(value, name) {
  const i = value.lastIndexOf(':');
  if (i <= 0 || i === value.length - 1) throw new Error(`${name} must be <run-id>:<status>`);
  return { runId: value.slice(0, i), status: value.slice(i + 1) };
}
function loadAuthorization(revision) {
  const path = authorizationPath(revision);
  if (!existsSync(path)) throw new Error(`No authorization record for revision ${revision}`);
  return JSON.parse(readFileSync(path, 'utf8'));
}
function saveAuthorization(record) {
  mkdirSync(releasesDir(), { recursive: true });
  writeFileSync(authorizationPath(record.revision), JSON.stringify(record, null, 2) + '\n');
  return record;
}
function headRevision() {
  return execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoRoot(), encoding: 'utf8' }).trim();
}
function headTree() {
  return execFileSync('git', ['rev-parse', 'HEAD^{tree}'], { cwd: repoRoot(), encoding: 'utf8' }).trim();
}
function assertChecksMatch(record, webCheck, apiCheck) {
  // A rollback record waives checks by decision (recorded with the reason);
  // a supplied check on a rollback record must still match.
  if (record.web_check_status !== 'waived-rollback') {
    if (webCheck) {
      const web = parseCheck(webCheck, '--web-check');
      if (web.runId !== record.web_check_run_id || web.status !== 'success') {
        throw new Error('Web check evidence does not match the authorization record');
      }
    } else if (record.web_check_status !== 'success') throw new Error('Web check did not succeed for this revision');
  }
  if (record.api_check_status !== 'waived-rollback') {
    if (apiCheck) {
      const api = parseCheck(apiCheck, '--api-check');
      if (api.runId !== record.api_check_run_id || api.status !== 'success') {
        throw new Error('API check evidence does not match the authorization record');
      }
    } else if (record.api_check_status !== 'success') throw new Error('API check did not succeed for this revision');
  }
}
function assertHeadMatches(record) {
  if (headRevision() !== record.revision) {
    throw new Error('Revision mismatch: HEAD, authorization, plan and manifest must be the same exact revision');
  }
  if (record.head_tree && headTree() !== record.head_tree) {
    throw new Error('Tree mismatch: HEAD tree does not match the authorization record');
  }
}

// One shared lock for the whole release target (B3). It lives in the
// external releases dir, so every stage and both targets contend for the
// same lock. A per-stage lock (<stage>.lock) allowed A and B to be held
// simultaneously — that is the hole this replaces.
export function releaseLockPath() {
  return resolve(releasesDir(), '.release.lock');
}
export function acquireReleaseLock() {
  const lockPath = releaseLockPath();
  const tryOnce = () => {
    // The releases dir may not exist yet (first authorize in a fresh dir);
    // without this the lock mkdir fails ENOENT (Brian slice 8 residual).
    mkdirSync(releasesDir(), { recursive: true });
    mkdirSync(lockPath);
    writeFileSync(resolve(lockPath, 'owner.json'), JSON.stringify({ pid: process.pid, startedAt: Date.now() }) + '\n');
    return { lockPath, release() { rmSync(lockPath, { recursive: true, force: true }); } };
  };
  try {
    return tryOnce();
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    let stale = false;
    try {
      const owner = JSON.parse(readFileSync(resolve(lockPath, 'owner.json'), 'utf8'));
      if (Date.now() - owner.startedAt > STALE_LOCK_MS) stale = true;
      else try { process.kill(owner.pid, 0); } catch { stale = true; }
    } catch { stale = true; }
    if (!stale) throw new Error('Deploy lock held for the release target');
    rmSync(lockPath, { recursive: true, force: true });
    return tryOnce();
  }
}

// Authorize one candidate. target 'asset' requires an applied-source stage;
// target 'api' is the separately explicit API lane (no asset stage, deploys
// wrangler.api.toml only, no producer effects). target 'rollback' restores
// the combined public worker only (deploys wrangler.toml, no stage, no
// producer effects) and requires a recorded rollback reason; its web/API
// checks are optional and, if omitted, are recorded as waived with the
// reason. The record is written to the EXTERNAL releases dir (B1);
// authorizing never touches the candidate tree.
// A newer authorization supersedes every older one, so at most one
// candidate is deployable at a time. All record mutations run under the
// shared release lock, so supersession cannot interleave with a deploy.
export function authorize({ target = 'asset', stage, revision, webCheck, apiCheck, authorizer, rollbackReason = null }) {
  if (target !== 'asset' && target !== 'api' && target !== 'rollback') throw new Error("target must be 'asset', 'api' or 'rollback'");
  if (!/^[a-f0-9]{40}$/.test(revision)) throw new Error('Full reviewed revision required');
  if (!authorizer) throw new Error('Authorization requires --authorizer');
  // Web/API checks are required for asset and api targets. A rollback
  // restores a known-good combined build under an emergency decision, so
  // its checks are optional: if supplied they must be success; if omitted
  // they are recorded as waived with the rollback reason.
  let web = null, api = null;
  if (target === 'rollback') {
    if (webCheck) {
      web = parseCheck(webCheck, '--web-check');
      if (web.status !== 'success') throw new Error('Authorization requires successful web and API checks for the exact revision');
    }
    if (apiCheck) {
      api = parseCheck(apiCheck, '--api-check');
      if (api.status !== 'success') throw new Error('Authorization requires successful web and API checks for the exact revision');
    }
    if (!rollbackReason) throw new Error('Rollback authorization requires --rollback-reason');
  } else {
    web = parseCheck(webCheck, '--web-check');
    api = parseCheck(apiCheck, '--api-check');
    if (web.status !== 'success' || api.status !== 'success') {
      throw new Error('Authorization requires successful web and API checks for the exact revision');
    }
  }
  let plan = null;
  let apiCfg = null;
  let rbCfg = null;
  if (target === 'asset') {
    if (!stage) throw new Error('Asset authorization requires --stage');
    // Deployment order: the asset service's API binding points at the
    // live-options-api service. It must exist before the asset can be
    // authorized (and again before it deploys); otherwise the first asset
    // deploy breaks the binding.
    assertApiServiceReady(revision);
    plan = verifyStage(stage);
    if (plan.asset_revision !== revision) throw new Error('Stage revision does not match --revision');
    if (plan.config_state !== 'applied-source') {
      throw new Error('Proposal-only stages can never deploy; an applied root config is required');
    }
  } else if (target === 'api') {
    // The api lane is bound to wrangler.api.toml at authorize time; the
    // hash is re-checked at deploy so a changed config cannot deploy.
    apiCfg = apiConfig();
  } else {
    // The rollback lane restores the combined public worker only. It is
    // bound to wrangler.toml at authorize time; the hash is re-checked at
    // deploy so a changed config cannot restore.
    rbCfg = rollbackConfig();
  }
  const lock = acquireReleaseLock();
  try {
    if (existsSync(releasesDir())) {
      for (const name of readdirSync(releasesDir())) {
        if (!name.endsWith('.authorization.json')) continue;
        const older = JSON.parse(readFileSync(resolve(releasesDir(), name), 'utf8'));
        if (older.revision === revision || older.superseded_by) continue;
        older.superseded_by = revision;
        saveAuthorization(older);
        if (older.stage_path) {
          try {
            const olderPlan = JSON.parse(readFileSync(resolve(older.stage_path, 'plan.json'), 'utf8'));
            if (olderPlan.state === 'authorized') transitionPlanState(older.stage_path, ['authorized'], 'superseded');
          } catch { /* stage already gone; the record is the authority */ }
        }
      }
    }
    const record = {
      revision, head_tree: headTree(), target,
      stage_path: target === 'asset' ? resolve(stage) : null,
      config_source: plan ? plan.config_source : (target === 'api' ? API_CONFIG_FILE : ROLLBACK_CONFIG_FILE),
      config_sha256: plan ? plan.config_sha256 : (target === 'api' ? apiCfg.sha256 : rbCfg.sha256),
      web_check_run_id: web ? web.runId : null, web_check_status: web ? web.status : 'waived-rollback',
      api_check_run_id: api ? api.runId : null, api_check_status: api ? api.status : 'waived-rollback',
      authorizer, authorized_at: new Date().toISOString(), superseded_by: null,
      authorization_kind: target === 'rollback' ? 'rollback-restoration' : 'external-artifact',
      ...(target === 'rollback' ? { rollback_reason: rollbackReason } : {}),
    };
    saveAuthorization(record);
    if (target === 'asset') transitionPlanState(stage, ['prepared-not-approved-not-deployed'], 'authorized');
    return record;
  } finally {
    lock.release();
  }
}
export function authorizeStage(args) {
  return authorize({ ...args, target: 'asset' });
}
export function authorizeApi(args) {
  return authorize({ ...args, target: 'api' });
}
export function authorizeRollback(args) {
  return authorize({ ...args, target: 'rollback' });
}

export function supersede({ revision, supersededBy }) {
  const lock = acquireReleaseLock();
  try {
    const record = loadAuthorization(revision);
    if (record.superseded_by) throw new Error(`Revision ${revision} is already superseded by ${record.superseded_by}`);
    record.superseded_by = supersededBy;
    saveAuthorization(record);
    if (record.stage_path) {
      try {
        const plan = JSON.parse(readFileSync(resolve(record.stage_path, 'plan.json'), 'utf8'));
        if (plan.state === 'authorized') transitionPlanState(record.stage_path, ['authorized'], 'superseded');
      } catch { /* stage already gone; the record is the authority */ }
    }
    return record;
  } finally {
    lock.release();
  }
}
export function supersedeStage(args) {
  return supersede(args);
}

// Final guard, called immediately before the executor runs, inside the
// shared lock (B3). Re-reads the authorization record, the plan (asset
// only) and HEAD from scratch, so a supersession that landed after the
// first checks is caught here and the executor never runs for a stale
// candidate.
export function recheckBeforeExecute({ stage, revision, record, webCheck = null, apiCheck = null }) {
  const fresh = loadAuthorization(revision);
  if (fresh.superseded_by) throw new Error(`Revision ${revision} is superseded by ${fresh.superseded_by}`);
  assertHeadMatches(fresh);
  assertChecksMatch(fresh, webCheck, apiCheck);
  if (fresh.target === 'asset') {
    assertApiServiceReady(revision);
    const freshPlan = verifyStage(stage, ['authorized']);
    if (freshPlan.config_sha256 !== fresh.config_sha256 || freshPlan.config_state !== 'applied-source') {
      throw new Error('Stage config drifted while waiting for the lock');
    }
    return freshPlan;
  }
  if (fresh.target === 'rollback') {
    // The rollback lane re-reads wrangler.toml and compares its hash, so a
    // config changed after authorization cannot restore.
    const cfg = rollbackConfig();
    if (cfg.sha256 !== fresh.config_sha256 || fresh.config_source !== ROLLBACK_CONFIG_FILE) {
      throw new Error('Rollback config drifted while waiting for the lock');
    }
    return null;
  }
  // The api lane re-reads wrangler.api.toml and compares its hash, so a
  // config changed after authorization cannot deploy.
  const cfg = apiConfig();
  if (cfg.sha256 !== fresh.config_sha256 || fresh.config_source !== API_CONFIG_FILE) {
    throw new Error('API config drifted while waiting for the lock');
  }
  return null;
}

// Default executor runs wrangler from explicit fields as an argv array —
// never by shell-parsing the PowerShell-literal proposed_command string,
// which is review material for the Windows operating procedure, not a
// shell command on this machine.
export function defaultExecutor(command, { configPath, revision, target }) {
  if (target === 'api' || target === 'rollback') {
    execFileSync('wrangler', ['deploy', '--config', configPath], { stdio: 'inherit' });
  } else {
    execFileSync('wrangler', ['deploy', '--config', configPath, '--var', `ASSET_REVISION:${revision}`], { stdio: 'inherit' });
  }
  return command;
}

export function deploy({ target = 'asset', stage, revision, execute = false, webCheck = null, apiCheck = null, executor = defaultExecutor }) {
  if (target !== 'asset' && target !== 'api' && target !== 'rollback') throw new Error("target must be 'asset', 'api' or 'rollback'");
  if (target === 'asset' && !stage) throw new Error('Asset deploy requires --stage');
  // The shared release lock is taken FIRST, before any state is read, and
  // held through the executor and the receipt write. Authorization and
  // supersession take the same lock, so no state change can interleave.
  const lock = acquireReleaseLock();
  try {
    const record = loadAuthorization(revision);
    if (record.target !== target) throw new Error(`Authorization record for ${revision} is for target '${record.target}', not '${target}'`);
    if (record.superseded_by) throw new Error(`Revision ${revision} is superseded by ${record.superseded_by}`);
    assertHeadMatches(record);
    let plan = null;
    let manifest = null;
    if (target === 'asset') {
      // Deployment order, enforced at deploy time as well as authorize
      // time: the API service must exist before the asset deploy.
      assertApiServiceReady(revision);
      plan = verifyStage(stage, ['authorized']);
      manifest = verifyArtifact(resolve(stage, 'payload'));
      if (plan.asset_revision !== revision || manifest.revision !== revision) {
        throw new Error('Revision mismatch: HEAD, authorization, plan and manifest must be the same exact revision');
      }
      if (plan.config_sha256 !== record.config_sha256 || plan.config_source !== record.config_source ||
          plan.config_state !== 'applied-source') {
        throw new Error('Config hash/source mismatch or proposal-only stage');
      }
    }
    assertChecksMatch(record, webCheck, apiCheck);
    // Recheck immediately before deploy, under the lock (B3).
    const freshPlan = recheckBeforeExecute({ stage, revision, record, webCheck, apiCheck });
    const dryCommand = target === 'asset' ? freshPlan.proposed_command
      : target === 'api' ? 'wrangler deploy --config wrangler.api.toml'
      : 'wrangler deploy --config wrangler.toml';
    if (!execute) {
      return { dryRun: true, command: dryCommand, revision };
    }
    const configPath = target === 'asset' ? resolve(stage, 'wrangler.assets.toml')
      : target === 'api' ? resolve(repoRoot(), API_CONFIG_FILE)
      : resolve(repoRoot(), ROLLBACK_CONFIG_FILE);
    const command = dryCommand;
    executor(command, { configPath, revision, target });
    if (target === 'asset') transitionPlanState(stage, ['authorized'], 'deployed');
    const receipt = {
      revision, target, command, executed_at: new Date().toISOString(),
      web_check_run_id: record.web_check_run_id, api_check_run_id: record.api_check_run_id,
    };
    mkdirSync(releasesDir(), { recursive: true });
    writeFileSync(receiptPath(revision, target), JSON.stringify(receipt, null, 2) + '\n');
    return { deployed: true, ...receipt };
  } finally {
    lock.release();
  }
}
export function deployStaged(args) {
  return deploy({ ...args, target: 'asset' });
}
export function deployApi(args) {
  return deploy({ ...args, target: 'api' });
}
export function deployRollback(args) {
  return deploy({ ...args, target: 'rollback' });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  // Flags win; env vars are the fallback (F1: workflows pass everything via
  // env, so no workflow input is ever interpolated into a run: step).
  const args = process.argv.slice(2);
  const get = (k, env) => {
    const i = args.indexOf(k);
    if (i >= 0 && args[i + 1] !== undefined) return args[i + 1];
    return process.env[env] || undefined;
  };
  const has = (k, env) => args.includes(k) || process.env[env] === 'true' || process.env[env] === '1';
  try {
    const target = get('--target', 'DEPLOY_TARGET') || 'asset';
    if (has('--record-api-service', 'DEPLOY_RECORD_API_SERVICE')) {
      console.log(JSON.stringify(recordApiServiceVerified({
        revision: get('--revision', 'DEPLOY_REVISION'),
        verifier: get('--authorizer', 'DEPLOY_AUTHORIZER'),
      })));
    } else if (has('--authorize', 'DEPLOY_AUTHORIZE')) {
      console.log(JSON.stringify(authorize({
        target,
        stage: get('--stage', 'DEPLOY_STAGE'),
        revision: get('--revision', 'DEPLOY_REVISION'),
        webCheck: get('--web-check', 'DEPLOY_WEB_CHECK'),
        apiCheck: get('--api-check', 'DEPLOY_API_CHECK'),
        authorizer: get('--authorizer', 'DEPLOY_AUTHORIZER'),
        rollbackReason: get('--rollback-reason', 'DEPLOY_ROLLBACK_REASON'),
      })));
    } else if (has('--supersede', 'DEPLOY_SUPERSEDE')) {
      console.log(JSON.stringify(supersede({ revision: get('--revision', 'DEPLOY_REVISION'), supersededBy: get('--superseded-by', 'DEPLOY_SUPERSEDED_BY') })));
    } else if (has('--deploy', 'DEPLOY_DEPLOY')) {
      console.log(JSON.stringify(deploy({
        target,
        stage: get('--stage', 'DEPLOY_STAGE'),
        revision: get('--revision', 'DEPLOY_REVISION'),
        execute: has('--execute', 'DEPLOY_EXECUTE'),
        webCheck: get('--web-check', 'DEPLOY_WEB_CHECK') || null,
        apiCheck: get('--api-check', 'DEPLOY_API_CHECK') || null,
      })));
    } else throw new Error('Usage: --authorize|--deploy|--supersede|--record-api-service [--target asset|api|rollback] --stage DIR --revision FULL_SHA [--execute] [--web-check id:success] [--api-check id:success] [--authorizer NAME] [--superseded-by REV] [--rollback-reason TEXT]');
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
