// Unit tests for public/js/zip.js: Python's zipfile must read the writer's archive with matching names and CRCs, and
// readZip must read stored and deflate archives (ours and Python's) while refusing unsafe names, oversize entries and
// corrupt data with fixed codes.
// Contents are generated placeholders.
//   cd lab/web && node --test test/zip.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ZipError, entriesFromFiles, makeZip, readZip, safeEntryName } from "../public/js/zip.js";

const enc = new TextEncoder();
const code = (c) => (e) => e instanceof ZipError && e.code === c;
const pattern = (n, k, sh = 9) => new Uint8Array(n).map((_, i) => (i * k + (i >> sh)) & 0xff);

// fn(path of "t.zip" in a fresh temp dir), the dir removed afterwards
async function withTmpZip(fn) {
  const dir = mkdtempSync(join(tmpdir(), "labzip-"));
  try { return await fn(join(dir, "t.zip")); } finally { rmSync(dir, { recursive: true, force: true }); }
}
// the bytes of a zip written by a python3 zipfile script (argv[1] is the output path)
const pyZip = (script) => withTmpZip((path) => {
  execFileSync("python3", ["-c", script, path]);
  return new Uint8Array(readFileSync(path));
});

test("python3 zipfile reads the archive: names in order, testzip passes", async () => {
  const big = new Uint8Array(300 * 1024).map((_, i) => (i * 7) & 0xff);
  const files = [
    { name: "run-x/run.json", data: enc.encode('{"placeholder": true}') },
    { name: "run-x/a0/steps.jsonl", data: big },
    { name: "run-x/a0/FATAL", data: new Uint8Array(0) },
  ];
  const r = await withTmpZip(async (path) => {
    writeFileSync(path, new Uint8Array(await makeZip(files).arrayBuffer()));
    return JSON.parse(execFileSync("python3", ["-c", [
      "import sys, json, zipfile",
      "z = zipfile.ZipFile(sys.argv[1])",
      "print(json.dumps({'names': z.namelist(), 'bad': z.testzip(), 'sizes': [i.file_size for i in z.infolist()]}))",
    ].join("\n"), path], { encoding: "utf8" }));
  });
  assert.deepEqual(r.names, files.map((f) => f.name));
  assert.equal(r.bad, null);
  assert.deepEqual(r.sizes, files.map((f) => f.data.length));
});

// ---- readZip (uploads): stored + deflate entries, zip-slip names, limits, CRC. Placeholder contents only.

test("readZip: makeZip round trip (stored), order and bytes kept", async () => {
  const files = [
    { name: "run-x/run.json", data: enc.encode('{"placeholder": true}') },
    { name: "run-x/a0/steps.jsonl", data: pattern(70000, 7) },
    { name: "run-x/a0/FATAL", data: new Uint8Array(0) },
  ];
  const got = await readZip(makeZip(files));
  assert.deepEqual(got.map((e) => e.name), files.map((f) => f.name));
  for (let i = 0; i < files.length; i++) assert.deepEqual(got[i].data, files[i].data);
  // Uint8Array and ArrayBuffer inputs too
  const bytes = new Uint8Array(await makeZip(files).arrayBuffer());
  assert.equal((await readZip(bytes)).length, 3);
  assert.equal((await readZip(bytes.buffer)).length, 3);
});

test("readZip: python zipfile deflate + stored entries, directory entries skipped", async () => {
  const b = await pyZip([
    "import sys, zipfile",
    "z = zipfile.ZipFile(sys.argv[1], 'w')",
    "z.writestr(zipfile.ZipInfo('runs/'), b'')",
    "z.writestr('runs/r1/run.json', b'{\"placeholder\": 1}' * 300, compress_type=zipfile.ZIP_DEFLATED)",
    "z.writestr('runs/r1/a0/steps.jsonl', bytes(((i * 13 + (i >> 9)) & 0xff) for i in range(200000)), compress_type=zipfile.ZIP_DEFLATED)",
    "z.writestr('runs/r1/empty', b'', compress_type=zipfile.ZIP_DEFLATED)",
    "z.writestr('runs/r1/stored.txt', b'placeholder', compress_type=zipfile.ZIP_STORED)",
    "z.close()",
  ].join("\n"));
  const got = await readZip(b);
  assert.deepEqual(got.map((e) => e.name), ["runs/r1/run.json", "runs/r1/a0/steps.jsonl", "runs/r1/empty", "runs/r1/stored.txt"]);
  assert.equal(new TextDecoder().decode(got[0].data), '{"placeholder": 1}'.repeat(300));
  assert.deepEqual(got[1].data, pattern(200000, 13));
  assert.equal(got[2].data.length, 0);
  assert.equal(new TextDecoder().decode(got[3].data), "placeholder");
});

test("readZip: zip-slip and odd names are rejected", async () => {
  for (const name of ["../x", "a/../../x", "/abs", "a\\b", "C:/x", "a//b", "./a", "a/\u0001b"]) {
    await assert.rejects(readZip(makeZip([{ name, data: enc.encode("p") }])), code("E_ZIP_NAME"), name);
  }
  assert.equal(safeEntryName("a/b/"), null);
  assert.equal(safeEntryName("a/b.json"), "a/b.json");
});

test("readZip: limits, duplicates, CRC, encryption, garbage", async () => {
  const one = makeZip([{ name: "a", data: pattern(5000, 3) }]);
  await assert.rejects(readZip(one, { maxEntryBytes: 1000 }), code("E_ZIP_LIMIT"));
  await assert.rejects(readZip(makeZip([{ name: "a", data: pattern(800, 3) }, { name: "b", data: pattern(800, 5) }]), { maxTotalBytes: 1000 }), code("E_ZIP_LIMIT"));
  await assert.rejects(readZip(makeZip([{ name: "a", data: enc.encode("p") }, { name: "b", data: enc.encode("q") }]), { maxEntries: 1 }), code("E_ZIP_LIMIT"));
  await assert.rejects(readZip(makeZip([{ name: "a", data: enc.encode("p") }, { name: "a", data: enc.encode("q") }])), code("E_ZIP_DUPLICATE"));
  await assert.rejects(readZip(enc.encode("not a zip at all, placeholder bytes")), code("E_ZIP_FORMAT"));
  await assert.rejects(readZip(new Uint8Array(0)), code("E_ZIP_FORMAT"));
  // flip one data byte: CRC mismatch
  const b = new Uint8Array(await one.arrayBuffer());
  const bad = b.slice(); bad[30 + 1 + 10] ^= 0xff;
  await assert.rejects(readZip(bad), code("E_ZIP_CRC"));
  // set the encryption flag in the central directory
  const enc1 = b.slice();
  const cd = new DataView(enc1.buffer).getUint32(enc1.length - 22 + 16, true);
  enc1[cd + 8] |= 1;
  await assert.rejects(readZip(enc1), code("E_ZIP_ENCRYPTED"));
});

test("readZip: a deflate bomb is refused by its declared size, a lying size by the actual output", async () => {
  const b = await pyZip([
    "import sys, zipfile",
    "z = zipfile.ZipFile(sys.argv[1], 'w', compression=zipfile.ZIP_DEFLATED)",
    "z.writestr('big', b'\\0' * (8 << 20))",
    "z.close()",
  ].join("\n"));
  assert.ok(b.length < 100000);
  await assert.rejects(readZip(b, { maxEntryBytes: 1 << 20 }), code("E_ZIP_LIMIT"));
  // shrink the declared uncompressed size (central directory) below the real one
  const lie = b.slice();
  const dv = new DataView(lie.buffer);
  const cd = dv.getUint32(lie.length - 22 + 16, true);
  dv.setUint32(cd + 24, 1000, true);
  await assert.rejects(readZip(lie), code("E_ZIP_SIZE"));
});

test("entriesFromFiles: folder upload names and limits", async () => {
  const f = (path, s) => ({ name: path.split("/").pop(), webkitRelativePath: path, size: s.length, arrayBuffer: async () => enc.encode(s).buffer });
  const got = await entriesFromFiles([f("r1/run.json", "{}"), f("r1/a0/steps.jsonl", "placeholder")]);
  assert.deepEqual(got.map((e) => e.name), ["r1/run.json", "r1/a0/steps.jsonl"]);
  await assert.rejects(entriesFromFiles([f("r1/x", "12345")], { maxEntryBytes: 2 }), code("E_ZIP_LIMIT"));
  await assert.rejects(entriesFromFiles([f("../x", "1")]), code("E_ZIP_NAME"));
});

test("readZip: deflate output spanning many stream chunks is reassembled exactly", async () => {
  const n = 3 << 20;  // several DecompressionStream chunks
  const b = await pyZip([
    "import sys, zipfile",
    "z = zipfile.ZipFile(sys.argv[1], 'w')",
    `z.writestr('r/a0/steps.jsonl', bytes(((i * 7 + (i >> 11)) & 0xff) for i in range(${n})), compress_type=zipfile.ZIP_DEFLATED)`,
    "z.close()",
  ].join("\n"));
  const [e] = await readZip(b);
  assert.equal(e.data.length, n);
  assert.deepEqual(e.data, pattern(n, 7, 11));
});
