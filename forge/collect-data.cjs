// Preserve a single upstream data tree without flattening XML directories.
// Node is already required by every toolchain supported by forge.
const fs = require('node:fs');
const path = require('node:path');

function collectData(source, destination) {
  source = path.resolve(source);
  destination = path.resolve(destination);
  if (destination === source || destination.startsWith(source + path.sep)) {
    throw new Error('The output must be outside the source checkout');
  }
  const files = [];
  const ignored = new Set(['node_modules', '.git', 'build', 'out', 'dist']);
  function walk(dir) {
    for (const entry of fs.readdirSync(dir, {withFileTypes:true}).sort((a,b)=>a.name.localeCompare(b.name))) {
      if (entry.isSymbolicLink()) continue;
      const file = path.join(dir, entry.name);
      if (entry.isDirectory() && !ignored.has(entry.name)) walk(file);
      else if (entry.isFile()) files.push(file);
    }
  }
  walk(source);
  const roots = files.filter(f => path.basename(f) === 'gi.yml').map(f =>
    path.basename(path.dirname(f)) === 'defs' ? path.dirname(path.dirname(f)) : path.dirname(f));
  const candidates = [...new Set(roots)].filter(root =>
    files.some(f => f === path.join(root, 'pool_oot.csv') ||
                    f === path.join(root, 'pool', 'pool_oot.csv') ||
                    f.startsWith(path.join(root, 'checks') + path.sep) && f.endsWith('.xml')));
  if (candidates.length !== 1) throw new Error(`Expected one complete data tree; found ${candidates.length}`);
  const root = candidates[0];
  const selected = files.filter(f => f.startsWith(root + path.sep) && /\.(?:csv|ya?ml|xml|ts)$/.test(f));
  const names = files.filter(f => path.basename(f) === 'names.ts' && !f.startsWith(root + path.sep));
  if (names.length > 1 || names.length && selected.some(f => path.relative(root, f) === 'names.ts')) {
    throw new Error('Ambiguous names.ts files in source checkout');
  }
  // Refuse to merge generations in an old output directory.
  if (fs.existsSync(destination) && fs.readdirSync(destination).length) throw new Error('Data destination is not empty');
  fs.mkdirSync(destination, {recursive:true});
  for (const file of selected) {
    const output = path.join(destination, path.relative(root, file));
    fs.mkdirSync(path.dirname(output), {recursive:true});
    fs.copyFileSync(file, output);
  }
  if (names.length) fs.copyFileSync(names[0], path.join(destination, 'names.ts'));
  return {files:selected.length + names.length, root:path.relative(source, root)};
}

module.exports = {collectData};
if (require.main === module) {
  try {
    if (process.argv.length !== 4) throw new Error('Usage: node collect-data.cjs SOURCE OUTPUT');
    console.log(JSON.stringify(collectData(process.argv[2], process.argv[3])));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
