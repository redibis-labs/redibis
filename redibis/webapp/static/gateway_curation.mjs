/** Client-side span curation. Mirrors redibis.pii.curation.apply_curation. Never scans. */

export const DECISIONS = ["accept", "reject", "modify"];
export const SOURCES = ["engine", "llm_verdict", "manual"];

export function emptyCuration(runUuid) {
  return { kind: "redibis.span_curation", schema_version: "1.0", run_uuid: runUuid || "", text_digest: "", entries: [], auto_trim: false };
}

export function spanKey(span, source) {
  return {
    start: Number(span.start) || 0,
    end: Number(span.end) || 0,
    entity_type: String(span.entity_type || ""),
    source: String(span.source || source || "engine"),
  };
}

function keyId(key) {
  return [key.start, key.end, key.entity_type, key.source || "engine"].join("\t");
}

export function upsertEntry(curation, entry) {
  const next = emptyCuration(curation && curation.run_uuid);
  next.text_digest = (curation && curation.text_digest) || "";
  next.auto_trim = !!(curation && curation.auto_trim);
  const id = keyId(entry.key);
  next.entries = ((curation && curation.entries) || []).filter((e) => keyId(e.key) !== id);
  next.entries.push(entry);
  return next;
}

export function applyCuration(spans, curation, text, opts) {
  const src = Array.isArray(spans) ? spans : [];
  const entries = (curation && curation.entries) || [];
  const keepRejected = !!(opts && opts.keepRejected);
  const byId = {};
  entries.forEach((e) => { byId[keyId(e.key)] = e; });
  const out = [];
  const seen = {};
  src.forEach((span) => {
    const key = spanKey(span, span.source || "engine");
    let entry = byId[keyId(key)];
    if (!entry) entry = byId[keyId({ ...key, source: "engine" })];
    if (entry && entry.decision === "reject") {
      if (keepRejected) {
        out.push(Object.assign({}, span, {
          text: (text || "").slice(key.start, key.end),
          source: key.source,
          rejected: true,
        }));
      }
      return;
    }
    if (entry && entry.decision === "modify") {
      const start = entry.new_start != null ? entry.new_start : key.start;
      const end = entry.new_end != null ? entry.new_end : key.end;
      if (start < 0 || end > (text || "").length || end <= start) return;
      const slice = (text || "").slice(start, end);
      if (!slice) return;
      const copy = Object.assign({}, span, {
        start, end, text: slice,
        entity_type: entry.new_entity_type || span.entity_type,
        source: key.source,
      });
      out.push(copy);
      seen[[start, end, copy.entity_type].join("\t")] = true;
      return;
    }
    const copy = Object.assign({}, span, { text: (text || "").slice(key.start, key.end), source: key.source });
    out.push(copy);
    seen[[copy.start, copy.end, copy.entity_type].join("\t")] = true;
  });
  entries.forEach((entry) => {
    if (entry.decision !== "accept") return;
    if (entry.key.source !== "manual" && entry.key.source !== "llm_verdict") return;
    const start = entry.new_start != null ? entry.new_start : entry.key.start;
    const end = entry.new_end != null ? entry.new_end : entry.key.end;
    const et = entry.new_entity_type || entry.key.entity_type;
    const ident = [start, end, et].join("\t");
    if (seen[ident]) return;
    if (start < 0 || end > (text || "").length || end <= start) return;
    const slice = (text || "").slice(start, end);
    if (!slice) return;
    out.push({
      start, end, entity_type: et, text: slice, score: 1,
      engine: entry.key.source === "manual" ? "manual" : "llm",
      source: entry.key.source,
    });
    seen[ident] = true;
  });
  return out;
}

export function persistable(curation) {
  const raw = JSON.parse(JSON.stringify(curation || emptyCuration()));
  (raw.entries || []).forEach((e) => {
    if (e.key) {
      delete e.key.text;
      delete e.key.surface;
      delete e.key.value;
    }
    delete e.text;
    delete e.surface;
    delete e.value;
  });
  return raw;
}

export function entityCounts(spans) {
  const counts = {};
  (spans || []).forEach((s) => {
    const et = s.entity_type || "";
    counts[et] = (counts[et] || 0) + 1;
  });
  return counts;
}

export function isRejected(span, curation) {
  const key = spanKey(span, span.source || "engine");
  const hit = ((curation && curation.entries) || []).find((e) => keyId(e.key) === keyId(key) || keyId(e.key) === keyId({ ...key, source: "engine" }));
  return !!(hit && hit.decision === "reject");
}
