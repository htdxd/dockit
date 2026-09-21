import { build } from 'esbuild';
import { copyFile, mkdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const root = new URL('../', import.meta.url);
const target = new URL('backend/skill_toolbox/web/static/', root);
await build({entryPoints:[fileURLToPath(new URL('src/web.ts', root))],outfile:fileURLToPath(new URL('app.js',target)),bundle:true,format:'esm',target:'es2022'});
await copyFile(new URL('src/style.css',root),new URL('desktop.css',target));
await mkdir(new URL('resume-templates/',target),{recursive:true});
const manifest = JSON.parse(await readFile(new URL('backend/skill_toolbox/skill_defs/resume_pro/manifest.json', root), 'utf8'));
for (const { value: name } of manifest.initial_form.template.options) {
  await copyFile(new URL(`public/resume-templates/${name}.jpg`,root),new URL(`resume-templates/${name}.jpg`,target));
}
console.log('Website built with shared desktop UI and CSS.');
