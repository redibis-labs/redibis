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

export const RANK = { direct: 3, contact: 2, identity: 1, other: 0 };

export function classOf(entityType) {
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
          RANK[classOf(a.entity_type)] >= RANK[classOf(b.entity_type)] ? a : b
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
  for (const seg of segments) {
    if (!seg.top) {
      container.appendChild(doc.createTextNode(seg.text));
      continue;
    }
    const mark = doc.createElement("mark");
    mark.className = `gw-hl gw-${classOf(seg.top.entity_type)}`;
    if (seg.allProposals) mark.classList.add("gw-proposal");
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
