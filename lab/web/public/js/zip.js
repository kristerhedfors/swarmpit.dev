// Run zips (PROTOCOL.md §5.1, docs/browser-swarm.md §3): a minimal stored (uncompressed) writer for the run
// download, and a bounded reader for trace uploads (stored and deflate entries, as written by this page, Python's
// zipfile, Finder/Explorer "compress" and `zip -r`). The reader never trusts the archive: entry names are checked
// against zip-slip and odd paths, every size is capped (declared and actual), CRCs are verified, and failures are
// ZipError with a fixed code only (no names or bytes from the archive in the message).
// Record signatures and fixed sizes (APPNOTE 4.3): local file header, central directory header, end of central dir.
const SIG_LOCAL = 0x04034b50, SIG_CENTRAL = 0x02014b50, SIG_END = 0x06054b50;
const LOCAL_LEN = 30, CENTRAL_LEN = 46, END_LEN = 22;
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

export function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// files: [{name, data: Uint8Array}] -> Blob
export function makeZip(files) {
  const enc = new TextEncoder();
  const parts = [], central = [];
  let offset = 0;
  for (const f of files) {
    const name = enc.encode(f.name), crc = crc32(f.data), size = f.data.length;
    const local = new DataView(new ArrayBuffer(LOCAL_LEN));
    local.setUint32(0, SIG_LOCAL, true); local.setUint16(4, 20, true); local.setUint16(8, 0, true);
    local.setUint32(14, crc, true); local.setUint32(18, size, true); local.setUint32(22, size, true);
    local.setUint16(26, name.length, true);
    parts.push(local, name, f.data);
    const cen = new DataView(new ArrayBuffer(CENTRAL_LEN));
    cen.setUint32(0, SIG_CENTRAL, true); cen.setUint16(4, 20, true); cen.setUint16(6, 20, true);
    cen.setUint32(16, crc, true); cen.setUint32(20, size, true); cen.setUint32(24, size, true);
    cen.setUint16(28, name.length, true); cen.setUint32(42, offset, true);
    central.push(cen, name);
    offset += LOCAL_LEN + name.length + size;
  }
  const cenSize = central.reduce((n, p) => n + p.byteLength, 0);
  const end = new DataView(new ArrayBuffer(END_LEN));
  end.setUint32(0, SIG_END, true); end.setUint16(8, files.length, true); end.setUint16(10, files.length, true);
  end.setUint32(12, cenSize, true); end.setUint32(16, offset, true);
  return new Blob([...parts, ...central, end], { type: "application/zip" });
}

// ---- reader

export class ZipError extends Error {
  constructor(code) { super(code); this.name = "ZipError"; this.code = code; }
}
// Upload limits: a full multi4 run zip is a few MiB to a few tens of MiB. These leave room but bound memory: an
// upload is inflated, decoded and converted next to the running VMs, so a zip bomb must not cost the tab.
export const ZIP_LIMITS = Object.freeze({ maxEntries: 4000, maxEntryBytes: 64 << 20, maxTotalBytes: 128 << 20, maxNameBytes: 400 });

const dec = new TextDecoder("utf-8", { fatal: false });
const toBytes = async (src) => (src instanceof Uint8Array ? src
  : src instanceof ArrayBuffer ? new Uint8Array(src)
  : typeof src?.arrayBuffer === "function" ? new Uint8Array(await src.arrayBuffer())
  : (() => { throw new ZipError("E_ZIP_INPUT"); })());

// A safe relative name ("a/b/c"), or null for a directory entry. Throws E_ZIP_NAME for absolute paths, drive
// letters, backslashes, "." / ".." / empty segments, control characters, or over-long names.
export function safeEntryName(raw, maxBytes = ZIP_LIMITS.maxNameBytes) {
  if (typeof raw !== "string" || !raw || new TextEncoder().encode(raw).length > maxBytes) throw new ZipError("E_ZIP_NAME");
  const dir = raw.endsWith("/");
  const name = dir ? raw.slice(0, -1) : raw;
  if (!name || name.startsWith("/") || /^[A-Za-z]:/.test(name) || name.includes("\\") || /[\x00-\x1f\x7f]/.test(name)) throw new ZipError("E_ZIP_NAME");
  for (const seg of name.split("/")) if (seg === "" || seg === "." || seg === "..") throw new ZipError("E_ZIP_NAME");
  return dir ? null : name;
}

async function inflateRaw(bytes, expect) {
  if (typeof DecompressionStream !== "function") throw new ZipError("E_ZIP_METHOD");
  const ds = new DecompressionStream("deflate-raw");
  const reader = new Blob([bytes]).stream().pipeThrough(ds).getReader();
  // chunks, not one buffer of the declared size: a tiny archive declaring huge entries allocates only what inflates
  const chunks = [];
  let got = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (got + value.length > expect) { reader.cancel().catch(() => {}); throw new ZipError("E_ZIP_SIZE"); }
      chunks.push(value);
      got += value.length;
    }
  } catch (e) {
    throw e instanceof ZipError ? e : new ZipError("E_ZIP_DEFLATE");
  }
  if (got !== expect) throw new ZipError("E_ZIP_SIZE");
  const out = new Uint8Array(expect);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}

// The end of central directory record's offset: the last signature within the final 64 KiB + 22 bytes (comment
// length max 65535) whose comment ends the archive. Throws E_ZIP_FORMAT if there is none.
function findEnd(dv) {
  const len = dv.byteLength;
  for (let i = len - END_LEN; i >= Math.max(0, len - END_LEN - 0xffff); i--) {
    if (dv.getUint32(i, true) === SIG_END && i + END_LEN + dv.getUint16(i + 20, true) === len) return i;
  }
  throw new ZipError("E_ZIP_FORMAT");
}

// readZip(Blob | ArrayBuffer | Uint8Array, limits?) -> [{name, data: Uint8Array}] in central-directory order.
// Directory entries are skipped; duplicate names, encryption, zip64, multi-disk archives and methods other than
// 0 (stored) and 8 (deflate) are rejected.
export async function readZip(src, limits = {}) {
  const L = { ...ZIP_LIMITS, ...limits };
  const b = await toBytes(src);
  const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
  const eocd = findEnd(dv);
  if (dv.getUint16(eocd + 4, true) !== 0 || dv.getUint16(eocd + 6, true) !== 0) throw new ZipError("E_ZIP_MULTIDISK");
  const count = dv.getUint16(eocd + 10, true), cdSize = dv.getUint32(eocd + 12, true), cdOff = dv.getUint32(eocd + 16, true);
  if (count === 0xffff || cdSize === 0xffffffff || cdOff === 0xffffffff) throw new ZipError("E_ZIP64");
  if (count > L.maxEntries) throw new ZipError("E_ZIP_LIMIT");
  if (cdOff + cdSize > eocd) throw new ZipError("E_ZIP_FORMAT");
  const out = [], seen = new Set();
  let p = cdOff, total = 0;
  for (let k = 0; k < count; k++) {
    if (p + CENTRAL_LEN > eocd || dv.getUint32(p, true) !== SIG_CENTRAL) throw new ZipError("E_ZIP_FORMAT");
    const flags = dv.getUint16(p + 8, true), method = dv.getUint16(p + 10, true), crc = dv.getUint32(p + 16, true);
    const csize = dv.getUint32(p + 20, true), usize = dv.getUint32(p + 24, true);
    const nlen = dv.getUint16(p + 28, true), xlen = dv.getUint16(p + 30, true), clen = dv.getUint16(p + 32, true);
    const loff = dv.getUint32(p + 42, true);
    if (p + CENTRAL_LEN + nlen > eocd) throw new ZipError("E_ZIP_FORMAT");
    const rawName = dec.decode(b.subarray(p + CENTRAL_LEN, p + CENTRAL_LEN + nlen));
    p += CENTRAL_LEN + nlen + xlen + clen;
    if (flags & 0x41) throw new ZipError("E_ZIP_ENCRYPTED");  // bit 0 encrypted, bit 6 strong encryption
    if (csize === 0xffffffff || usize === 0xffffffff || loff === 0xffffffff) throw new ZipError("E_ZIP64");
    const name = safeEntryName(rawName, L.maxNameBytes);
    if (name === null) continue;
    if (seen.has(name)) throw new ZipError("E_ZIP_DUPLICATE");
    seen.add(name);
    if (usize > L.maxEntryBytes || (total += usize) > L.maxTotalBytes) throw new ZipError("E_ZIP_LIMIT");
    if (method !== 0 && method !== 8) throw new ZipError("E_ZIP_METHOD");
    out.push({ name, data: await entryData(dv, { loff, cdOff, method, csize, usize, crc }) });
  }
  return out;
}

// One entry's bytes from its local header on: stored or inflated, size and CRC checked against the central
// directory. The local header's name/extra lengths can differ from the central copy; sizes come from the central one.
async function entryData(dv, { loff, cdOff, method, csize, usize, crc }) {
  if (loff + LOCAL_LEN > cdOff || dv.getUint32(loff, true) !== SIG_LOCAL) throw new ZipError("E_ZIP_FORMAT");
  const start = loff + LOCAL_LEN + dv.getUint16(loff + 26, true) + dv.getUint16(loff + 28, true);
  if (start + csize > cdOff) throw new ZipError("E_ZIP_FORMAT");
  const raw = new Uint8Array(dv.buffer, dv.byteOffset + start, csize);
  let data;
  if (method === 0) {
    if (csize !== usize) throw new ZipError("E_ZIP_SIZE");
    data = raw.slice();
  } else {
    data = await inflateRaw(raw, usize);
  }
  if (crc32(data) !== crc) throw new ZipError("E_ZIP_CRC");
  return data;
}

// Folder upload (<input webkitdirectory>): File objects -> the same [{name, data}] entries readZip returns, named by
// webkitRelativePath (the picked folder is the first segment), under the same name and size limits.
export async function entriesFromFiles(files, limits = {}) {
  const L = { ...ZIP_LIMITS, ...limits };
  const list = [...files];
  if (list.length > L.maxEntries) throw new ZipError("E_ZIP_LIMIT");
  let total = 0;
  const out = [], seen = new Set();
  for (const f of list) {
    const name = safeEntryName(f.webkitRelativePath || f.name, L.maxNameBytes);
    if (name === null || seen.has(name)) continue;
    seen.add(name);
    if (f.size > L.maxEntryBytes || (total += f.size) > L.maxTotalBytes) throw new ZipError("E_ZIP_LIMIT");
    out.push({ name, data: new Uint8Array(await f.arrayBuffer()) });
  }
  return out;
}
