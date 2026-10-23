const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {collectData} = require('../forge/collect-data.cjs');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'forge-data-test-'));
function write(file, text) {fs.mkdirSync(path.dirname(file), {recursive:true}); fs.writeFileSync(file, text);}
try {
  for (const layout of ['flat', 'csv', 'xml']) {
    const source = path.join(temporary, layout, 'source');
    const output = path.join(temporary, layout, 'output');
    const root = path.join(source, 'data');
    write(path.join(root, layout === 'flat' ? 'gi.yml' : 'defs/gi.yml'), 'ITEM: 1');
    if (layout === 'xml') {
      write(path.join(root, 'checks/oot/dungeon/chest.xml'), '<check game="oot"/>');
      write(path.join(root, 'checks/mm/dungeon/chest.xml'), '<check game="mm"/>');
      write(path.join(source, 'packages/generator/lib/names.ts'), 'export const names = {};');
    } else {
      write(path.join(root, layout === 'flat' ? 'pool_oot.csv' : 'pool/pool_oot.csv'), 'oot');
      write(path.join(root, layout === 'flat' ? 'pool_mm.csv' : 'pool/pool_mm.csv'), 'mm');
    }
    write(path.join(source, 'node_modules/decoy/data/gi.yml'), 'ignored');
    write(path.join(source, 'node_modules/decoy/data/pool_oot.csv'), 'ignored');
    const result = collectData(source, output);
    assert.equal(result.root, 'data');
    if (layout === 'xml') {
      assert.equal(fs.readFileSync(path.join(output, 'checks/oot/dungeon/chest.xml'), 'utf8'), '<check game="oot"/>');
      assert.equal(fs.readFileSync(path.join(output, 'checks/mm/dungeon/chest.xml'), 'utf8'), '<check game="mm"/>');
      assert.equal(fs.readFileSync(path.join(output, 'names.ts'), 'utf8'), 'export const names = {};');
    }
    assert.throws(()=>collectData(source, output), /not empty/);
    assert.throws(()=>collectData(source, path.join(source, 'nested')), /outside/);
  }
  const source = path.join(temporary, 'ambiguous');
  for (const pkg of ['a', 'b']) {
    write(path.join(source, pkg, 'data/defs/gi.yml'), 'ITEM: 1');
    write(path.join(source, pkg, 'data/pool/pool_oot.csv'), 'oot');
  }
  assert.throws(()=>collectData(source, path.join(temporary, 'bad-output')), /found 2/);
  assert.equal(fs.existsSync(path.join(temporary, 'bad-output')), false);
  const empty = path.join(temporary, 'empty-source');
  fs.mkdirSync(empty);
  assert.throws(()=>collectData(empty, path.join(temporary, 'empty-output')), /found 0/);
  assert.equal(fs.existsSync(path.join(temporary, 'empty-output')), false);
  const multiple = path.join(temporary, 'multiple-names');
  write(path.join(multiple, 'data/defs/gi.yml'), 'ITEM: 1');
  write(path.join(multiple, 'data/pool/pool_oot.csv'), 'oot');
  write(path.join(multiple, 'a/names.ts'), 'first');
  write(path.join(multiple, 'b/names.ts'), 'second');
  assert.throws(()=>collectData(multiple, path.join(temporary, 'names-output')), /Ambiguous names/);
  assert.equal(fs.existsSync(path.join(temporary, 'names-output')), false);
  console.log('PASS: flat/CSV/XML trees preserved; duplicate XML basenames separate; names.ts retained; ambiguous trees and merging refused');
} finally {
  assert.equal(path.dirname(path.resolve(temporary)), path.resolve(os.tmpdir()));
  fs.rmSync(temporary, {recursive:true, force:true});
}
