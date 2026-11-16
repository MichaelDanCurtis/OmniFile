// Generates, from the authoritative contract service/openapi/portal-api.yaml
// (decision 32: the SPA is a client of it):
//   src/api/schema.gen.ts     the typed client's paths and schemas
//   src/mocks/examples.gen.ts the YAML's response `examples`, typed, for the mock
//
//   node scripts/gen-client.mjs          write both files
//   node scripts/gen-client.mjs --check  fail if either committed file is stale (CI)
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import openapiTS, { NULL, STRING, astToString, tsUnion } from "openapi-typescript";

const spec = new URL("../../service/openapi/portal-api.yaml", import.meta.url);
const schemaOut = new URL("../src/api/schema.gen.ts", import.meta.url);
const examplesOut = new URL("../src/mocks/examples.gen.ts", import.meta.url);

const banner = (what) => `/**
 * GENERATED from service/openapi/portal-api.yaml by scripts/gen-client.mjs (${what}).
 * Do not edit. Run \`npm run gen:client\` after the contract changes;
 * CI runs \`npm run check:client\` and fails when this file is stale.
 */

`;

// Response fields marked x-extensible-enum are open (portal-api.md 1, decision 35):
// they may gain values (new backend kinds, tiers, migration states), so they are
// typed as plain strings, with the known values in a doc comment. Code renders
// them through lookups that fall back to the raw value (src/lib/format.ts).
// (`"a" | (string & {})` would keep editor hints, but openapi-fetch's response
// types turn the intersection into an object.)
function openEnum(schema) {
  const known = schema["x-extensible-enum"];
  if (!Array.isArray(known) || schema.enum) return undefined;
  const nullable = Array.isArray(schema.type) && schema.type.includes("null");
  return nullable ? tsUnion([STRING, NULL]) : STRING;
}

// defaultNonNullable off: a field with a default is optional in a request body
// (override_archive, window_hours, notify.requester ...), as the contract means.
const ast = await openapiTS(spec, {
  alphabetize: false,
  immutable: false,
  defaultNonNullable: false,
  transform: openEnum,
});
const schemaTs = banner("types") + astToString(ast);

// The examples are single-line JSON flow items under a component schema:
//     Share:
//       examples:
//         - {"share_id": "4", ...}
async function examplesTs() {
  const yaml = await readFile(spec, "utf8");
  const lines = yaml.split("\n");
  const found = [];
  let schema = null;
  for (let i = 0; i < lines.length; i++) {
    const name = /^ {4}([A-Za-z][A-Za-z0-9]*):\s*$/.exec(lines[i]);
    if (name) schema = name[1];
    if (/^ {6}examples:\s*$/.test(lines[i]) && schema) {
      const items = [];
      for (let j = i + 1; j < lines.length && /^ {8}- /.test(lines[j]); j++) {
        const text = lines[j].replace(/^ {8}- /, "");
        try {
          items.push(JSON.parse(text));
        } catch (error) {
          throw new Error(
            `${schema}: example on line ${j + 1} is not one-line JSON (${error.message})`,
          );
        }
      }
      if (items.length === 0) throw new Error(`${schema}: examples on line ${i + 1} has no items`);
      found.push([schema, items]);
    }
  }
  if (found.length === 0) throw new Error("No examples found in portal-api.yaml");
  const first = found
    .map(
      ([name, items]) =>
        `  ${name}: ${JSON.stringify(items[0])} satisfies S[${JSON.stringify(name)}],`,
    )
    .join("\n");
  const all = found
    .map(
      ([name, items]) =>
        `  ${name}: [\n${items.map((item) => `    ${JSON.stringify(item)},`).join("\n")}\n  ] satisfies S[${JSON.stringify(name)}][],`,
    )
    .join("\n");
  return (
    banner("examples") +
    `import type { components } from "../api/schema.gen";\n\ntype S = components["schemas"];\n\n` +
    `/** Each schema's first example from the YAML, checked against its generated type. */\nexport const example = {\n${first}\n};\n\n` +
    `/** Every example, per schema. */\nexport const examples = {\n${all}\n};\n`
  );
}

const outputs = [
  [schemaOut, schemaTs],
  [examplesOut, await examplesTs()],
];

if (process.argv.includes("--check")) {
  let stale = false;
  for (const [file, text] of outputs) {
    let current = "";
    try {
      current = await readFile(file, "utf8");
    } catch {
      // missing counts as stale
    }
    if (current !== text) {
      stale = true;
      console.error(`${fileURLToPath(file)} is stale against service/openapi/portal-api.yaml.`);
    }
  }
  if (stale) {
    console.error("Run `npm run gen:client` in portal/ and commit the result.");
    process.exit(1);
  }
  console.log("Generated client and examples are up to date with portal-api.yaml.");
} else {
  for (const [file, text] of outputs) {
    await writeFile(file, text);
    console.log(`Wrote ${fileURLToPath(file)}`);
  }
}
