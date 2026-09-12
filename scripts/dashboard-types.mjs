import { execFileSync } from 'node:child_process';
import { writeFile } from 'node:fs/promises';
import { compile } from 'json-schema-to-typescript';

const schema = JSON.parse(
  execFileSync(
    'uv',
    [
      'run',
      'python',
      '-c',
      'import json; from nyanpasu.transcript.models import TranscriptContract; print(json.dumps(TranscriptContract.model_json_schema(mode="serialization")))',
    ],
    { encoding: 'utf8' },
  ),
);
function removePropertyTitles(value) {
  if (!value || typeof value !== 'object') return;
  for (const property of Object.values(value.properties ?? {})) delete property.title;
  for (const child of Object.values(value)) removePropertyTitles(child);
}
removePropertyTitles(schema);
await writeFile(
  'frontend/dashboard/src/api-types.ts',
  await compile(schema, 'TranscriptContract', { $refOptions: { resolve: { external: false } } }),
);
execFileSync('pnpm', ['exec', 'vp', 'fmt', 'frontend/dashboard/src/api-types.ts'], {
  stdio: 'inherit',
});
