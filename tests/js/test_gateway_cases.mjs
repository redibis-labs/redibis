import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  caseFromScan,
  coerceImported,
  mergeCases,
  stampedDataset,
  DATASET_KIND,
} from "../../redibis/webapp/static/gateway_cases.mjs";

describe("gateway_cases", () => {
  it("stamps unicode-codepoint datasets with values and offsets", () => {
    const text = "Call 01522345678.";
    const c = caseFromScan({
      text,
      language: "en",
      id: "phone",
      spans: [{ start: 5, end: 16, entity_type: "PHONE_NUMBER", text: "01522345678" }],
    });
    assert.equal(c.expected_spans[0].value, "01522345678");
    assert.equal(Array.from(text).slice(5, 16).join(""), c.expected_spans[0].value);
    const ds = stampedDataset([c], { id: "pack" });
    assert.equal(ds.kind, DATASET_KIND);
    assert.equal(ds.offset_unit, "unicode_codepoint");
  });

  it("coerces scan JSON that still has text", () => {
    const parsed = coerceImported({
      run_uuid: "r1",
      text: "alice@x.com",
      analysers: { pii: { spans: [{ start: 0, end: 11, entity_type: "EMAIL_ADDRESS" }] } },
    });
    assert.equal(parsed.cases.length, 1);
    assert.equal(parsed.cases[0].expected_spans[0].entity_type, "EMAIL_ADDRESS");
  });

  it("refuses scan JSON without original text", () => {
    assert.throws(
      () => coerceImported({ analysers: { pii: { spans: [] } } }),
      /no original text/,
    );
  });

  it("merges cases with unique ids", () => {
    const a = [{ id: "case-1", text: "a", expected_spans: [] }];
    const b = [{ id: "case-1", text: "b", expected_spans: [] }];
    const merged = mergeCases(a, b);
    assert.equal(merged.length, 2);
    assert.equal(merged[1].id, "case-1-2");
  });
});
