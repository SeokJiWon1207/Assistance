import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { build } from 'esbuild';
import postcss from 'postcss';
import tailwind from '@tailwindcss/postcss';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const out=path.join(root,'../backend/static/vendor/bklit');
const check=process.argv.includes('--check');
const result=await build({absWorkingDir:root,entryPoints:['src/charts.tsx'],bundle:true,format:'esm',target:['es2022'],minify:true,
  keepNames:true,jsx:'automatic',write:false,legalComments:'eof',define:{'process.env.NODE_ENV':'"production"'},
  alias:{'@':path.join(root,'vendor/bklit')},outfile:'charts.js'});
// No Tailwind preflight. Scope all utility selectors to the chart island.
const cssInput=path.join(root,'src/utilities.css');
const css=await postcss([tailwind({base:root,optimize:true}),{postcssPlugin:'scope-bklit',OnceExit(cssRoot){
  cssRoot.walkRules(rule=>{
    if(rule.parent.type==='rule' || (rule.parent.type==='atrule' && /keyframes/.test(rule.parent.name)))return;
    rule.selectors=rule.selectors.map(selector=>selector.includes(':root')||selector===':host'?'.bklit-root':`.bklit-root ${selector}`);
  });
}}]).process(await fs.readFile(cssInput,'utf8'),{from:cssInput,map:false});
const outputs={'charts.js':result.outputFiles[0].contents,'charts.css':Buffer.from(css.css),'LICENSE':await fs.readFile(path.join(root,'vendor/bklit/LICENSE'))};
for(const [name,content] of Object.entries(outputs)){
  const file=path.join(out,name);
  if(check){if(!Buffer.from(content).equals(await fs.readFile(file)))throw Error(`Stale ${name}: run npm run build`);}
  else {await fs.mkdir(out,{recursive:true});await fs.writeFile(file,content);}
}
const hash=createHash('sha256');
async function hashTree(dir){for(const name of (await fs.readdir(dir)).sort()){const p=path.join(dir,name);if((await fs.stat(p)).isDirectory())await hashTree(p);else {hash.update(path.relative(root,p));hash.update(await fs.readFile(p));}}}
await hashTree(path.join(root,'src'));await hashTree(path.join(root,'vendor'));await hashTree(path.join(root,'scripts'));
for(const file of ['package.json','package-lock.json','tsconfig.json','../backend/static/modules/chart_models.js'])hash.update(await fs.readFile(path.join(root,file)));
const manifest=JSON.stringify({sourceHash:hash.digest('hex'),outputs:Object.fromEntries(Object.entries(outputs).map(([name,bytes])=>[name,createHash('sha256').update(bytes).digest('hex')]))},null,2)+'\n';
if(check){if(manifest!==await fs.readFile(path.join(out,'build.json'),'utf8'))throw Error('Stale build manifest');}
else await fs.writeFile(path.join(out,'build.json'),manifest);
console.log(`Bklit ${check?'verified':'built'}: ${Math.round(outputs['charts.js'].length/1024)} KiB JS, ${Math.round(outputs['charts.css'].length/1024)} KiB CSS`);
