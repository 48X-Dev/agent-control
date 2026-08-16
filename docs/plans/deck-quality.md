# Deck quality: why the background is missing, and what a template has to carry

Status: design. Nothing built. Extends `agent-file-outputs.md` section 4.6, which shipped the builders.

Scope: decks an agent writes look like the deployment's own, not like a library default. Documents and spreadsheets are out of scope: neither has the same problem, because neither inherits a visual identity.

**Author's note on verification.** Every claim about the two decks was measured from their OOXML while writing, not inferred. The template's layout background, the original deck's per-slide backgrounds, the slide dimensions, the media counts and the placeholder sets are all read from the files. Claims about what PowerPoint renders from a given inheritance chain follow the OOXML spec and are **unverified against the application itself**; Phase 0 opens the output in PowerPoint and Keynote, because a deck that validates and looks wrong is the failure this document exists to prevent.

---

## 0. What ships, in one paragraph

The template preparation step lifts the brand's per-slide identity onto the layout, so every generated slide inherits it instead of rendering white. The builder gains the ability to target a layout by name, so a template author can offer `Title`, `Section` and `Bullets` and have the agent's content land in the right one. Neither is a change to what the model produces; both are changes to what its output is poured into.

---

## 1. The correction: nothing is being lost in transit

The obvious reading is that the builder drops the background. It does not. Measured:

| | value |
|---|---|
| template layout background | `<a:solidFill><a:schemeClr val="lt1"/></a:solidFill>` — plain white |
| template master background | none |
| every original slide's background | `<a:solidFill><a:srgbClr val="F8FAFC"/></a:solidFill>` |

**The brand background is on the slides, not on the master or the layout.** A generated slide inherits from the layout, the layout is white, so the slide is white. The inheritance chain works correctly and there is nothing in it to inherit.

That is a property of how the deck was authored — almost certainly exported from a design tool, where every slide is self-contained and the master is vestigial. It is extremely common and it is why "use the real deck as a template" produces a blank-looking result.

The background itself is trivially recoverable: **one solid fill, `#F8FAFC`, identical on all 29 slides.** This is not a hard problem once located.

---

## 2. The decisions

### 2.1 The template is prepared, not just stripped (design question 1)

Today's preparation deletes every slide and keeps masters, layouts, theme and size. That preserves everything the deck declared and, as section 1 shows, the deck declared almost nothing.

**Decision: preparation also lifts per-slide identity onto the layout when it is uniform across the deck.** Concretely: if every slide carries the same `<p:bg>`, write it to the layout and drop it from nothing (the slides are going anyway). Uniformity is the condition — a deck with three different backgrounds has design intent that a single layout cannot express, and preparation says so rather than picking one.

This runs where the stripping runs, as a script an operator invokes on a real deck. It is not part of the executor and never runs at agent time.

### 2.2 Layouts are targeted by name (design question 2)

The template has one layout called `DEFAULT` with **no placeholders**, so `_pptx_slide` falls back to adding text boxes at geometry it chooses. That is why the output is on-brand-coloured but not on-brand-composed.

**Decision: the builder takes a layout name per slide kind and uses it when the template offers it.** `Title`, `Section`, `Bullets` as the default vocabulary; a template that offers none of them falls back to today's behaviour unchanged.

This moves the interesting work to the template author, which is the right place for it. A designer can compose a bullets layout once, with the brand's furniture and placeholder positions, and every generated deck inherits composition rather than just colour.

### 2.3 What this deliberately does not attempt (design question 3)

**Not cloning reference slides.** Copying a slide's shape tree and substituting text is the obvious way to get an exact match, and it is a trap: the shapes carry hand-tuned positions for the text that was there, so different-length content overflows or collides, and the failure is invisible until a human opens it. A placeholder-based layout is the format's own answer to this and it reflows.

**Not generating images.** A deck whose identity is 19 hand-placed images cannot be reproduced by a bullets builder, and pretending otherwise sets the expectation this document should be lowering.

---

## 3. Edge cases, each with its decided behaviour

| Case | Behaviour |
|---|---|
| Slides carry different backgrounds | Preparation refuses to lift and says which slides differ. Picking the most common one is a design decision the tool should not make. |
| Background is an image, not a fill | Lifted the same way, with the media part carried into the template. Image backgrounds make the template larger; the size is reported. |
| Template already has a layout background | Left alone. An author who set one meant it. |
| Named layout missing from the template | Falls back to the current behaviour and logs which name was missing, once. A deck that silently ignored the template would be the current bug wearing a config option. |
| Named layout exists but has no placeholders | Text boxes, as today. The name bought layout-specific furniture even if it offers no placeholders. |
| Placeholder too small for the content | The format reflows or overflows depending on autofit, which is the template author's setting. Not detectable from the builder, and stated here rather than discovered. |
| Template has content slides left in | Already a trap: they prepend to every deck. Preparation refuses a template with slides and names the count. |
| Deck is 4:3 and the template is 16:9 | Template wins. Slide size is a presentation-level property and there is only one. |
| Fonts not installed on the reader's machine | Falls back to whatever the reader has, as any deck would. Embedding fonts is a licensing question, not a technical one, and is out of scope. |
| Two agents write decks concurrently | No shared state; the template is read-only and opened per call. |

---

## 4. Testing

- a prepared template carries the background the source deck's slides carried;
- preparation refuses a deck whose slides disagree, naming the ones that differ;
- preparation refuses a template that still has slides;
- a generated slide with no explicit background resolves to the layout's, asserted on the XML rather than by eye;
- a named layout is used when present and the fallback is used when absent, both asserted;
- the no-template path is byte-comparable to today's output, so this cannot regress a deployment that never configures one.

---

## 5. Phases and effort

**Phase 0, does it render, 2 days.** Prepare the template, generate a deck, open it in **PowerPoint and Keynote**. The whole plan rests on inheritance behaving as the spec says, and nothing here has verified that against an application. Cheap, and it can invalidate 2.1.

**Phase 1, preparation lifts the background, 3 days.** The script, the uniformity check, the refusals, the tests. This alone fixes the reported problem.

**Phase 2, named layouts, 1 week.** Builder support, the fallback, the log line, tests.

**Phase 3, a designed template, not engineering.** Somebody authors `Title`, `Section` and `Bullets` in PowerPoint with the brand's furniture. This is where the remaining quality lives and no amount of code substitutes for it.

Roughly two weeks of engineering, and the largest single improvement is Phase 3, which is a design task.

---

## 6. The riskiest assumption

**That a placeholder-based template is achievable for this brand.** The existing deck has no placeholders anywhere, which suggests the design was never expressed in PowerPoint's own vocabulary. If the brand cannot be expressed as layouts — because it depends on per-slide bespoke composition — then Phase 2 buys little and the honest ceiling is "right colours, right size, right fonts, generic composition". Phase 3 is where that gets discovered, and it should be attempted before Phase 2 is built.

---

## 7. Open questions

1. Should the agent choose the layout, or should the tool infer it from content shape (one bullet list → `Bullets`, a heading alone → `Section`)? Inference is less surface for a model to get wrong.
2. Should a deck open with a title slide at all? Today's builder always adds one.
3. Is `#F8FAFC` the brand background, or an artefact of the export? Worth asking whoever owns the deck before baking it into a template.
