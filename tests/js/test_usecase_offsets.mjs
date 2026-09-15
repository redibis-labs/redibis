import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  offsetsFromPrefixAndSelected,
  sliceCodepoints,
  toChars,
} from "../../redibis/webapp/static/gateway_render.mjs";

function assertSlice(text, prefix, selected) {
  const off = offsetsFromPrefixAndSelected(prefix, selected);
  assert.ok(off, "expected a non-empty selection");
  assert.equal(sliceCodepoints(toChars(text), off.start, off.end), selected);
}

describe("use-case selection offsets", () => {
  it("selects inside a span that follows an Arabic run", () => {
    const text = "العنوان 15 شارع المعادي الجديد";
    const selected = "15 شارع المعادي الجديد";
    const prefix = text.slice(0, text.indexOf(selected));
    assertSlice(text, prefix, selected);
  });

  it("selects across a newline", () => {
    const text = "line one\n15 شارع المعادي";
    const selected = "one\n15";
    const prefix = text.slice(0, text.indexOf(selected));
    assertSlice(text, prefix, selected);
  });

  it("selects a mixed-script address (case-20 shape)", () => {
    const text = "Caller: العنوان 15 شارع المعادي الجديد، متفرع من شارع النصر، عمارة 4، شقة 12.\nAgent: تمام";
    const selected = "15 شارع المعادي الجديد، متفرع من شارع النصر، عمارة 4، شقة 12";
    const prefix = text.slice(0, text.indexOf(selected));
    assertSlice(text, prefix, selected);
    const off = offsetsFromPrefixAndSelected(prefix, selected);
    assert.equal(text.slice(off.start, off.end), selected);
  });
});
