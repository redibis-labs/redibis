/** Portable Text Gateway test cases. Never scans. Offsets are Unicode code points. */

export const DATASET_KIND = "redibis.text_span_eval_dataset";
export const REPORT_KIND = "redibis.text_span_eval_report";
export const BATCH_REPORT_KIND = "redibis.text_span_eval_batch_report";
export const USECASE_KIND = "redibis.text_usecase";

function toChars(text) {
  return Array.from(text || "");
}

function sliceValue(text, start, end) {
  return toChars(text).slice(start, end).join("");
}

export function expectedFromSpan(span, text, index, prefix) {
  const start = Number(span.start) || 0;
  const end = Number(span.end) || 0;
  const value = String(span.value || span.text || sliceValue(text, start, end));
  const row = {
    id: String(span.id || (prefix || "s") + (index + 1)),
    start,
    end,
    entity_type: String(span.entity_type || ""),
    value,
  };
  if (span.reason) row.reason = String(span.reason);
  return row;
}

export function caseFromScan(opts) {
  const o = opts || {};
  const text = o.text || "";
  const spans = o.spans || [];
  const rejected = o.rejected || [];
  return {
    id: String(o.id || "case-1"),
    name: String(o.name || ""),
    text,
    language: String(o.language || "en"),
    tags: Array.isArray(o.tags) ? o.tags.slice() : [],
    expected_spans: spans.filter((s) => s && String(s.entity_type || "").trim())
      .map((s, i) => expectedFromSpan(s, text, i, "s")),
    forbidden_spans: rejected.filter((s) => s && String(s.entity_type || "").trim())
      .map((s, i) => expectedFromSpan(s, text, i, "f")),
  };
}

export function stampedDataset(cases, extra) {
  const x = extra || {};
  return {
    kind: DATASET_KIND,
    schema_version: x.schema_version || "1.2",
    offset_unit: "unicode_codepoint",
    redibis_version: x.redibis_version || "",
    id: x.id || "",
    cases: cases || [],
  };
}

function normalizeCase(raw, index) {
  const text = String((raw && raw.text) || "");
  const expected = Array.isArray(raw && raw.expected_spans)
    ? raw.expected_spans
    : Array.isArray(raw && raw.gold_spans) ? raw.gold_spans : [];
  const forbidden = Array.isArray(raw && raw.forbidden_spans) ? raw.forbidden_spans : [];
  return {
    id: String((raw && raw.id) || ("case-" + (index + 1))),
    name: String((raw && raw.name) || ""),
    text,
    language: String((raw && raw.language) || "en"),
    tags: Array.isArray(raw && raw.tags) ? raw.tags.slice() : [],
    expected_spans: expected.map((s, i) => expectedFromSpan(s, text, i, "s")),
    forbidden_spans: forbidden.map((s, i) => expectedFromSpan(s, text, i, "f")),
  };
}

export function uniqueCaseId(id, seen) {
  const base = String(id || "case").trim() || "case";
  let cand = base;
  let n = 1;
  while (seen[cand]) {
    n += 1;
    cand = base + "-" + n;
  }
  seen[cand] = true;
  return cand;
}

export function mergeCases(into, incoming) {
  const seen = {};
  (into || []).forEach((c) => { if (c && c.id) seen[c.id] = true; });
  const out = (into || []).slice();
  (incoming || []).forEach((raw, i) => {
    const c = normalizeCase(raw, out.length + i);
    c.id = uniqueCaseId(c.id, seen);
    out.push(c);
  });
  return out;
}

export function coerceImported(raw) {
  if (!raw || typeof raw !== "object") throw new Error("Not a JSON object.");
  if (raw.kind === DATASET_KIND) {
    const cases = Array.isArray(raw.cases) ? raw.cases : [];
    if (!cases.length) throw new Error("Dataset has no cases.");
    return { type: "dataset", id: String(raw.id || ""), cases: cases.map((c, i) => normalizeCase(c, i)) };
  }
  if (raw.kind === REPORT_KIND || raw.kind === BATCH_REPORT_KIND) {
    const cases = raw.kind === BATCH_REPORT_KIND
      ? (raw.files || []).flatMap((f) => (f && f.cases) || [])
      : (raw.cases || []);
    const usable = cases.filter((c) => c && (c.text || (c.expected_spans || []).length));
    if (!usable.length) throw new Error("Report has no cases with text.");
    return {
      type: "report",
      id: String(raw.dataset_id || ""),
      cases: usable.map((c, i) => normalizeCase(c, i)),
      report: raw,
    };
  }
  if (raw.kind === USECASE_KIND || (raw.text && (raw.expected_spans || raw.forbidden_spans) && !raw.analysers)) {
    return { type: "dataset", id: String(raw.id || raw.name || ""), cases: [normalizeCase(raw, 0)] };
  }
  if (raw.analysers && raw.analysers.pii) {
    const text = raw.text || raw.source_text || "";
    if (!text) throw new Error("Scan JSON has no original text. Download a test case from Gateway.");
    const pii = raw.analysers.pii || {};
    return {
      type: "dataset",
      id: String(raw.run_uuid || ""),
      cases: [caseFromScan({
        text,
        language: (raw.text_meta && raw.text_meta.language) || raw.language || "en",
        spans: pii.spans || [],
        rejected: pii.rejected_spans || [],
        id: raw.run_uuid || "scan",
        name: raw.run_uuid || "scan",
      })],
    };
  }
  throw new Error("Not a test-case, scan, or evaluation file.");
}
