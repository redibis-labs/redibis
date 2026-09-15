export const DIRECT = new Set([
  "EG_NATIONAL_ID", "NATIONAL_ID", "PASSPORT", "CREDIT_CARD",
  "IBAN_CODE", "IBAN", "SSN", "US_SSN", "TAX_ID", "IMEI", "IMSI",
]);
export const CONTACT = new Set([
  "EMAIL_ADDRESS", "PHONE_NUMBER", "ADDRESS", "LOCATION",
  "URL", "IP_ADDRESS", "MSISDN",
]);
export const IDENT = new Set([
  "PERSON", "DATE_OF_BIRTH", "DATE_TIME", "NRP", "AGE",
]);

export const RANK = {
  extra: 6, fp: 5, bad: 5, fn: 4, miss: 4, near: 3, direct: 3, tp: 3, exact: 3,
  contact: 2, gold: 2, identity: 1, proposal: 0, other: 0,
};

const CLASS_TINT = {
  exact: "exact",
  equivalent: "exact",
  superset: "near",
  subset: "near",
  overlap_partial: "near",
  split: "near",
  merged: "near",
  type_mismatch: "bad",
  spurious: "bad",
  guard_violation: "bad",
  missed: "miss",
};

export function classOf(entityType, evalKind) {
  if (evalKind) return evalKind;
  const t = String(entityType || "");
  if (DIRECT.has(t)) return "direct";
  if (CONTACT.has(t)) return "contact";
  if (IDENT.has(t)) return "identity";
  return "other";
}

export function abbr(entityType) {
  return String(entityType || "")
    .split("_")
    .map((w) => w[0] || "")
    .join("")
    .slice(0, 3)
    .toUpperCase();
}

export function toChars(text) {
  return Array.from(text || "");
}

export function sliceCodepoints(chars, start, end) {
  return chars.slice(start, end).join("");
}

export function clampSpan(span, len) {
  const s = Math.max(0, Math.min(Number(span.start ?? 0), len));
  const e = Math.max(0, Math.min(Number(span.end ?? 0), len));
  return e > s ? { ...span, start: s, end: e } : null;
}

export function segmentsFor(text, spans) {
  const chars = toChars(text);
  const n = chars.length;
  const valid = (spans || []).map((s) => clampSpan(s, n)).filter(Boolean);
  const active = Array.from({ length: n }, () => []);
  valid.forEach((s, i) => {
    for (let p = s.start; p < s.end; p++) active[p].push(i);
  });
  const out = [];
  let i = 0;
  while (i < n) {
    const key = active[i].join(",");
    let j = i + 1;
    while (j < n && active[j].join(",") === key) j++;
    const ids = key ? active[i].slice() : [];
    const covering = ids.map((id) => valid[id]);
    const top = covering.length
      ? covering.reduce((a, b) =>
          RANK[classOf(a.entity_type, a.eval_kind)] >= RANK[classOf(b.entity_type, b.eval_kind)] ? a : b
        )
      : null;
    out.push({
      start: i,
      end: j,
      text: sliceCodepoints(chars, i, j),
      spanIds: ids,
      spans: covering,
      top,
      allProposals: covering.length > 0 && covering.every((s) => s.is_proposal),
    });
    i = j;
  }
  return { chars, valid, segments: out };
}

export function concatenatedText(segments) {
  return segments.map((s) => s.text).join("");
}

export function offsetsFromPrefixAndSelected(prefixText, selectedText) {
  const start = toChars(prefixText || "").length;
  const end = start + toChars(selectedText || "").length;
  return end > start ? { start, end } : null;
}

export function selectionOffsets(container, selection) {
  const sel = selection || (typeof window !== "undefined" ? window.getSelection() : null);
  if (!container || !sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  if (!container.contains(range.commonAncestorContainer)) return null;
  const pre = range.cloneRange();
  pre.selectNodeContents(container);
  pre.setEnd(range.startContainer, range.startOffset);
  return offsetsFromPrefixAndSelected(pre.toString(), range.toString());
}

export function overlayEvalSpans(expected, predicted, metric, proposals) {
  const matchedExpected = new Set(
    (metric && metric.matches || []).map((m) => m.expected_index)
  );
  const matchedPredicted = new Set(
    (metric && metric.matches || []).map((m) => m.predicted_index)
  );
  const out = [];
  (expected || []).forEach((span, i) => {
    out.push({
      ...span,
      eval_kind: matchedExpected.has(i) ? "tp" : "fn",
    });
  });
  (predicted || []).forEach((span, i) => {
    if (matchedPredicted.has(i)) return;
    out.push({ ...span, eval_kind: "fp" });
  });
  (proposals || []).forEach((span) => {
    out.push({ ...span, is_proposal: true, eval_kind: "proposal" });
  });
  return out;
}

export function overlayFromMatchClasses(expected, predicted, rows, proposals) {
  const out = [];
  (rows || []).forEach((row) => {
    const tint = CLASS_TINT[row.class] || "near";
    const ei = row.expected_index;
    const pi = row.predicted_index;
    const gold = ei == null ? null : (expected || [])[ei];
    const pred = pi == null ? null : (predicted || [])[pi];
    if (gold) {
      out.push({
        ...gold,
        eval_kind: row.class === "missed" ? "miss" : tint,
        match_class: row.class,
        extra_left: row.extra_left,
        extra_right: row.extra_right,
      });
    }
    if (pred && row.class !== "missed") {
      out.push({
        ...pred,
        eval_kind: tint,
        match_class: row.class,
      });
      if (gold && (row.extra_left || row.extra_right)) {
        if (pred.start < gold.start) {
          out.push({
            start: pred.start,
            end: gold.start,
            entity_type: pred.entity_type,
            eval_kind: "extra",
            match_class: row.class,
          });
        }
        if (pred.end > gold.end) {
          out.push({
            start: gold.end,
            end: pred.end,
            entity_type: pred.entity_type,
            eval_kind: "extra",
            match_class: row.class,
          });
        }
      }
    }
  });
  (proposals || []).forEach((span) => {
    out.push({ ...span, is_proposal: true, eval_kind: "proposal" });
  });
  return out;
}

export function classifyBanners({ truncated, maxChars, wantedEngines, enginesRan, spanCount }) {
  const out = [];
  const ran = enginesRan || [];
  if (truncated) {
    out.push({
      kind: "warn",
      text: `Only the first ${Number(maxChars).toLocaleString()} characters were scanned. Findings cover that portion only.`,
    });
  }
  if ((wantedEngines === "both" || wantedEngines === "ner") && !ran.includes("ner")) {
    out.push({
      kind: "warn",
      text: "NER model not loaded. Names and organisations were not detected.",
    });
  }
  if (!spanCount && !truncated) {
    out.push({ kind: "ok", text: "No personal data found in this text." });
  }
  return out;
}

export function renderHighlights(container, text, spans, documentRef) {
  const doc = documentRef || (typeof document !== "undefined" ? document : null);
  if (!doc || !container) return concatenatedText(segmentsFor(text, spans).segments);
  const { segments } = segmentsFor(text, spans);
  container.textContent = "";
  container.dir = "auto";
  container.style.unicodeBidi = "isolate";
  for (const seg of segments) {
    if (!seg.top) {
      container.appendChild(doc.createTextNode(seg.text));
      continue;
    }
    const mark = doc.createElement("mark");
    mark.className = `gw-hl gw-${classOf(seg.top.entity_type, seg.top.eval_kind)}`;
    if (seg.allProposals) mark.classList.add("gw-proposal");
    mark.dir = "auto";
    mark.style.unicodeBidi = "isolate";
    mark.dataset.abbr = abbr(seg.top.entity_type);
    mark.dataset.spans = JSON.stringify(
      seg.spans.map((s) => ({
        entity_type: s.entity_type,
        score: s.score,
        engine: s.engine,
        recognizer: s.recognizer,
        validator: s.validator,
        context_boost: s.context_boost,
        is_proposal: s.is_proposal,
        eval_kind: s.eval_kind,
        start: s.start,
        end: s.end,
      }))
    );
    mark.tabIndex = 0;
    mark.appendChild(doc.createTextNode(seg.text));
    container.appendChild(mark);
  }
  return concatenatedText(segments);
}

export function paintBanners(host, items, documentRef) {
  const doc = documentRef || (typeof document !== "undefined" ? document : null);
  if (!doc || !host) return;
  host.textContent = "";
  if (!items.length) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  for (const item of items) {
    const el = doc.createElement("div");
    el.className = `gw-banner ${item.kind}`;
    el.appendChild(doc.createTextNode(item.text));
    host.appendChild(el);
  }
}

/**
 * Remap original-span metadata onto de-identified text using ``applied``
 * actions (original start/end + before_len/after_len).
 */
export function remapSpansAfterDeid(applied, originalSpans) {
  const byKey = new Map();
  for (const s of originalSpans || []) {
    if (s == null || s.start == null || s.end == null) continue;
    byKey.set(`${s.start}:${s.end}`, s);
  }
  const sorted = [...(applied || [])].sort(
    (a, b) => Number(a.start || 0) - Number(b.start || 0)
  );
  let delta = 0;
  const out = [];
  for (const a of sorted) {
    const origStart = Number(a.start || 0);
    const origEnd = Number(a.end || 0);
    const afterLen = Number(a.after_len);
    const beforeLen = Number(a.before_len);
    if (!(afterLen >= 0) || !(beforeLen >= 0)) continue;
    const orig = byKey.get(`${origStart}:${origEnd}`) || {};
    const start = origStart + delta;
    const end = start + afterLen;
    if (end <= start) {
      delta += afterLen - beforeLen;
      continue;
    }
    out.push({
      entity_type: a.entity_type || orig.entity_type || "PII",
      score: a.score != null ? a.score : orig.score,
      engine: orig.engine || "deid",
      recognizer: orig.recognizer || a.strategy || "",
      validator: orig.validator || "",
      context_boost: !!orig.context_boost,
      is_proposal: !!orig.is_proposal,
      start,
      end,
    });
    delta += afterLen - beforeLen;
  }
  return out;
}
