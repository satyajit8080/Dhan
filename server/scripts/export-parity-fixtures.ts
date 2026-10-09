/**
 * Writes parity/fixtures/<module>.json from the TypeScript implementation.
 *   cd server && npx tsx scripts/export-parity-fixtures.ts
 * test/parity-fixtures.test.ts fails if the committed files drift from this.
 */
import { writeFileSync, mkdirSync } from 'node:fs';
import { buildAll } from '../test/parity/builder.js';

const dir = new URL('../../parity/fixtures/', import.meta.url);
mkdirSync(dir, { recursive: true });
const all = await buildAll();
for (const [name, cases] of Object.entries(all)) {
  writeFileSync(new URL(`${name}.json`, dir), JSON.stringify({ module: name, generator: 'server/test/parity/builder.ts', cases }, null, 1) + '\n');
  console.log(`${name}: ${cases.length} cases`);
}
