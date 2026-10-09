/**
 * The Python port's expected values must come from THIS TypeScript code.
 * Regenerate the fixtures in memory and require the committed files to match,
 * so a TS change without re-export (or a hand-edited fixture) fails CI.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { buildAll } from './parity/builder.js';

describe('parity fixtures are current TypeScript output', async () => {
  const all = await buildAll();
  for (const [name, cases] of Object.entries(all)) {
    it(`${name}.json matches a fresh TypeScript run (${cases.length} cases)`, () => {
      const committed = JSON.parse(
        readFileSync(new URL(`../../parity/fixtures/${name}.json`, import.meta.url), 'utf8'),
      ) as { cases: unknown };
      expect(committed.cases).toEqual(JSON.parse(JSON.stringify(cases)));
    });
  }
});
