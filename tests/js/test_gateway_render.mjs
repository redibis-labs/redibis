import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  classOf,
  abbr,
  toChars,
  clampSpan,
  segmentsFor,
  concatenatedText,
  classifyBanners,
  renderHighlights,
  paintBanners,
} from "../../redibis/webapp/static/gateway_render.mjs";

function fakeDocument() {
  class TextNode {
    constructor(text) {
      this.nodeType = 3;
      this.textContent = String(text);
    }
  }
  class El {
    constructor(tag) {
      this.tagName = String(tag).toLowerCase();
      this.nodeType = 1;
      this.childNodes = [];
      this.className = "";
      this.classList = { add: (c) => { this.className += " " + c; } };
      this.dataset = {};
      this.tabIndex = 0;
      this.hidden = false;
    }
    get textContent() {
      if (this.childNodes.length) {
        return this.childNodes.map((n) => n.textContent).join("");
      }
      return this._text || "";
    }
    set textContent(v) {
      this._text = String(v);
      this.childNodes = [];
    }
    appendChild(n) {
      this.childNodes.push(n);
      return n;
    }
  }
  return {
    createTextNode: (t) => new TextNode(t),
    createElement: (t) => new El(t),
  };
}

describe("codepoints", () => {
  it("counts emoji as one codepoint", () => {
    const text = "👍Alice";
    assert.equal(toChars(text).length, 6);
    const { segments } = segmentsFor(text, [
      { start: 1, end: 6, entity_type: "PERSON" },
    ]);
    assert.equal(concatenatedText(segments), text);
    assert.equal(segments[0].text, "👍");
    assert.equal(segments[1].text, "Alice");
    assert.equal(segments[1].top.entity_type, "PERSON");
  });

  it("keeps Arabic offsets by codepoint", () => {
    const text = "اسم علي";
    assert.equal(toChars(text).length, 7);
    const { segments } = segmentsFor(text, [
      { start: 4, end: 7, entity_type: "PERSON" },
    ]);
    assert.equal(concatenatedText(segments), text);
    assert.equal(segments.at(-1).text, "علي");
  });
});

describe("clampSpan", () => {
  it("drops inverted and empty spans", () => {
    assert.equal(clampSpan({ start: 5, end: 2 }, 10), null);
    assert.equal(clampSpan({ start: 3, end: 3 }, 10), null);
  });
  it("clamps to [0, len]", () => {
    const s = clampSpan({ start: -2, end: 99, entity_type: "EMAIL_ADDRESS" }, 4);
    assert.equal(s.start, 0);
    assert.equal(s.end, 4);
  });
});

describe("overlap segmentation", () => {
  it("splits at overlap boundaries and prefers stronger class", () => {
    const text = "abcdefghij";
    const { segments } = segmentsFor(text, [
      { start: 0, end: 6, entity_type: "PERSON" },
      { start: 4, end: 10, entity_type: "EMAIL_ADDRESS" },
    ]);
    assert.equal(concatenatedText(segments), text);
    assert.ok(segments.length >= 3);
    const mid = segments.find((s) => s.start === 4 && s.end === 6);
    assert.ok(mid);
    assert.equal(classOf(mid.top.entity_type), "contact");
  });
});

describe("proposals and banners", () => {
  it("marks proposal-only spans", () => {
    const { segments } = segmentsFor("Alice", [
      { start: 0, end: 5, entity_type: "PERSON", is_proposal: true },
    ]);
    assert.equal(segments[0].allProposals, true);
    assert.equal(classOf("PERSON"), "identity");
    assert.equal(abbr("EMAIL_ADDRESS"), "EA");
  });

  it("emits truncated, missing NER, and empty banners", () => {
    const truncated = classifyBanners({
      truncated: true,
      maxChars: 20000,
      wantedEngines: "both",
      enginesRan: ["regex"],
      spanCount: 1,
    });
    assert.ok(truncated.some((b) => b.kind === "warn"));
    assert.ok(truncated.some((b) => /NER/i.test(b.text)));

    const empty = classifyBanners({
      truncated: false,
      maxChars: 20000,
      wantedEngines: "regex",
      enginesRan: ["regex"],
      spanCount: 0,
    });
    assert.equal(empty[0].kind, "ok");
  });
});

describe("safe DOM construction", () => {
  it("puts script-looking text in a text node", () => {
    const doc = fakeDocument();
    const host = doc.createElement("div");
    const raw = "<script>alert(1)</script>";
    renderHighlights(host, raw, [], doc);
    assert.equal(host.textContent, raw);
    assert.equal(host.childNodes.length, 1);
    assert.equal(host.childNodes[0].nodeType, 3);
  });

  it("paints banner text as text nodes", () => {
    const doc = fakeDocument();
    const host = doc.createElement("div");
    paintBanners(host, [{ kind: "warn", text: "<b>nohtml</b>" }], doc);
    assert.equal(host.hidden, false);
    assert.equal(host.childNodes[0].textContent, "<b>nohtml</b>");
    assert.equal(host.childNodes[0].childNodes[0].nodeType, 3);
  });
});
