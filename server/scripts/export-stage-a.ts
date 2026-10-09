/** Writes parity/stage_a/<scenario>/{bodies,reference}.json from the TS plugin client.
 *    cd server && npx tsx scripts/export-stage-a.ts            (~12 s: real rate limits) */
import { mkdirSync, writeFileSync } from 'node:fs';
import { scenarios, runScenario } from '../test/parity/stageA.js';

for (const [name, s] of Object.entries(scenarios())) {
  const dir = new URL(`../../parity/stage_a/${name}/`, import.meta.url);
  mkdirSync(dir, { recursive: true });
  const r = await runScenario(s);
  writeFileSync(new URL('bodies.json', dir), JSON.stringify({ note: 'MOCK Dhan-shaped HTTP bodies (see server/test/parity/stageA.ts)', bodies: s.bodies }, null, 1) + '\n');
  // Receipt times depend on wall-clock elapsed during the TS run; they are
  // recorded so the Python replay uses the identical timestamps.
  writeFileSync(new URL('reference.json', dir), JSON.stringify({ strikes: s.strikes, asOfMs: s.asOfMs, ...r }, null, 1) + '\n');
  console.log(name, (r.expected as { status: string }).status, r.calls.join(' -> '));
}
