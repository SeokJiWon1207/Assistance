// Maintainer tool: copy an audited checkout, never execute upstream scripts.
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
const checkout = process.argv[2];
if (!checkout) throw Error('Usage: node scripts/vendor-bklit.mjs /path/to/bklit-ui');
const upstream = path.join(checkout, 'packages/ui/src');
const destination = new URL('../vendor/bklit/', import.meta.url).pathname;
const entries = ['charts/bar-chart.tsx','charts/bar.tsx','charts/bar-x-axis.tsx','charts/bar-y-axis.tsx','charts/chart-legend-hover.tsx',
  'charts/legend/index.ts','charts/tooltip/chart-tooltip.tsx','charts/grid.tsx','charts/sparkline.tsx',
  'charts/line-chart.tsx','charts/line.tsx','charts/x-axis.tsx','charts/y-axis.tsx','charts/chart-brush.tsx',
  'charts/pie-chart.tsx','charts/pie-slice.tsx','charts/pie-center.tsx','charts/heatmap/index.ts'];
const manifest = { repository: 'https://github.com/bklit/bklit-ui', commit: execFileSync('git',['-C',checkout,'rev-parse','HEAD'],{encoding:'utf8'}).trim(), license:'MIT', files:{} };
const queue=[...entries];const seen=new Set();
while(queue.length){
  let relative=queue.shift();
  relative=['','.ts','.tsx','/index.ts','/index.tsx'].map(ext=>relative+ext).find(p=>fs.existsSync(path.join(upstream,p)) && fs.statSync(path.join(upstream,p)).isFile());
  if(!relative) throw Error('Missing upstream import');
  if(seen.has(relative))continue; seen.add(relative);
  const content=fs.readFileSync(path.join(upstream,relative));
  const target=path.join(destination,relative);fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,content);
  manifest.files[relative]=createHash('sha256').update(content).digest('hex');
  for(const match of content.toString().matchAll(/(?:from\s*|import\s*)["']([^"']+)["']/g)){
    const spec=match[1];
    if(spec.startsWith('.'))queue.push(path.normalize(path.join(path.dirname(relative),spec)));
    else if(spec.startsWith('@/'))queue.push(spec.slice(2));
  }
}
fs.copyFileSync(path.join(checkout,'LICENSE'),path.join(destination,'LICENSE'));
fs.writeFileSync(path.join(destination,'manifest.json'),JSON.stringify(manifest,null,2)+'\n');
console.log('Vendored original Bklit files:',seen.size,manifest.commit);
