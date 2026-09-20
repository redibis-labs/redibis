import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  applyCuration,
  emptyCuration,
  isAccepted,
  isRejected,
  persistable,
  upsertEntry,
} from "../../redibis/webapp/static/gateway_curation.mjs";

const text = "Alice met https://instagram.com/a today";
const alice = { start: 0, end: 5, entity_type: "PERSON", text: "Alice", source: "engine", is_proposal: true };
const url = {
  start: 10, end: 32, entity_type: "URL", text: "https://instagram.com/a",
  source: "engine",
};

describe("applyCuration review decisions", () => {
  it("drops a rejected span so the word is ordinary text", () => {
    let cur = emptyCuration("r1");
    cur = upsertEntry(cur, { key: { start: 0, end: 5, entity_type: "PERSON", source: "engine" }, decision: "reject" });
    const out = applyCuration([alice, url], cur, text);
    assert.deepEqual(out.map((s) => s.entity_type), ["URL"]);
    assert.equal(isRejected(alice, cur), true);
  });

  it("keeps rejected spans only when keepRejected is set", () => {
    let cur = emptyCuration("r1");
    cur = upsertEntry(cur, { key: { start: 0, end: 5, entity_type: "PERSON", source: "engine" }, decision: "reject" });
    const listed = applyCuration([alice, url], cur, text, { keepRejected: true });
    assert.equal(listed[0].rejected, true);
    assert.equal(listed[0].text, "Alice");
  });

  it("accept clears is_proposal and marks accepted", () => {
    let cur = emptyCuration("r1");
    cur = upsertEntry(cur, { key: { start: 0, end: 5, entity_type: "PERSON", source: "engine" }, decision: "accept" });
    const out = applyCuration([alice], cur, text);
    assert.equal(out[0].is_proposal, false);
    assert.equal(out[0].accepted, true);
    assert.equal(isAccepted(alice, cur), true);
  });

  it("llm_verdict accept applies to the engine span at the same offsets", () => {
    let cur = emptyCuration("r1");
    cur = upsertEntry(cur, {
      key: { start: 0, end: 5, entity_type: "PERSON", source: "llm_verdict" },
      decision: "accept",
    });
    const out = applyCuration([alice], cur, text);
    assert.equal(out[0].accepted, true);
    assert.equal(out[0].is_proposal, false);
  });

  it("persistable curation never ships surface text", () => {
    let cur = emptyCuration("r1");
    cur = upsertEntry(cur, {
      key: { start: 0, end: 5, entity_type: "PERSON", source: "engine", text: "Alice" },
      decision: "reject",
      text: "Alice",
    });
    const raw = persistable(cur);
    assert.equal(JSON.stringify(raw).includes("Alice"), false);
  });
});
