# Merman 0.7.0 / resvg 0.46.0 prototype API evidence

Checked 2026-09-07 against tagged upstream source. This is source inspection, not a compilation or a rendering result. It supplies the Rust helper for [原型：公式与九类 Mermaid 能否在最终 Qt 控件中正确显示？](https://github.com/wooveep/UrltraKB/issues/6).

## Minimal feature boundary

```toml
[dependencies]
merman = { version = "=0.7.0", default-features = false, features = ["render"] }
resvg = { version = "=0.46.0", default-features = false, features = ["text"] }
# serde_json is needed by json! below; chrono is needed only for fixed_today.
# Pin the chosen versions exactly and retain Cargo.lock.
```

Merman's `render` feature enables its renderer facade; it has no per-diagram feature switches. Do not enable `raster` for this helper: that additionally brings image/resvg/usvg/tiny-skia/svg2pdf and uses Merman's system-font raster implementation. No `ratex-math` is needed for the accepted ordinary Mermaid corpus. Merman 0.7.0 requires Rust 1.95 / edition 2024. [Merman manifest](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/Cargo.toml), [workspace](https://github.com/Latias94/merman/blob/v0.7.0/Cargo.toml)

resvg re-exports both `usvg` and `tiny_skia`. Its `text` feature enables SVG text support, while `system-fonts`, `memmap-fonts`, and raster-image codecs are separate features. Thus explicit file bytes can be loaded without enabling system font discovery; `std::fs::read` plus `load_font_data` avoids fontdb's feature-gated `load_font_file`. [resvg manifest](https://github.com/linebender/resvg/blob/v0.46.0/crates/resvg/Cargo.toml), [resvg exports](https://github.com/linebender/resvg/blob/v0.46.0/crates/resvg/src/lib.rs), [usvg manifest](https://github.com/linebender/resvg/blob/v0.46.0/crates/usvg/Cargo.toml), [fontdb API](https://github.com/RazrFalcon/fontdb/blob/v0.23.0/src/lib.rs)

## Mermaid source to safe SVG

```rust
use merman::{MermaidConfig, render::HeadlessRenderer};

let config = MermaidConfig::from_value(serde_json::json!({
    "theme": if dark { "dark" } else { "default" },
    "fontFamily": "Noto Sans CJK SC",
    "themeVariables": { "fontFamily": "Noto Sans CJK SC" }
}));
let renderer = HeadlessRenderer::new()
    .with_strict_parsing()
    .with_site_config(config)
    .with_vendored_text_measurer()
    .with_fixed_local_offset_minutes(Some(0))
    .with_diagram_id(diagram_id);
// For date-dependent Gantt snapshots, additionally:
// .with_fixed_today(Some(chrono::NaiveDate::from_ymd_opt(2026, 9, 7).unwrap()))
let safe_svg = renderer.render_svg_resvg_safe_sync(source)?
    .ok_or("no Mermaid diagram detected")?;
```

These methods are public on `HeadlessRenderer`. All families use the same automatic detection/parse/layout/render entry point. `with_diagram_id` sanitizes the ID, avoiding cross-diagram internal marker collisions. `render_svg_resvg_safe_sync` explicitly uses `SvgPipeline::resvg_safe()`; unlike `render_svg_sync`, it does not apply an arbitrary stored renderer pipeline. If adding a root background/custom postprocessor, construct and pass that complete pipeline with `render_svg_with_pipeline_sync(source, &pipeline)`. [Facade implementation](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/src/render/mod.rs), [pipeline example](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/examples/example_06_svg_pipeline.rs)

Host defaults belong in `with_site_config`, not a string prepended to the document. Source frontmatter and `%%{init}%%` may override site config. The official custom output example sets both `fontFamily` and `themeVariables.fontFamily`. It also demonstrates `RootBackgroundPostprocessor::new(color)` and scoped CSS. The prototype's normal theme samples should have no source theme overrides so the comparison is meaningful. [Custom output example](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/examples/example_11_custom_output_environment.rs), [request-level precedence](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/src/render/mod.rs)

For reproducible Gantt, set both fixed date and local UTC offset, or use explicit dates and `todayMarker off`. The public fixed-date setters are available directly on `HeadlessRenderer`, not just its `Engine`. [Gantt example](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/examples/example_08_deterministic_gantt.rs), [facade](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/src/render/mod.rs)

## Safe SVG to PNG with only supplied fonts

```rust
use resvg::{tiny_skia, usvg};

let mut opt = usvg::Options::default();
opt.font_family = "Noto Sans CJK SC".to_string();
opt.languages = vec!["zh-CN".to_string(), "en".to_string()];
// Load both regular and bold OTFs if used by the corpus.
for font_path in font_paths {
    opt.fontdb_mut().load_font_data(std::fs::read(font_path)?);
}
opt.fontdb_mut().set_sans_serif_family("Noto Sans CJK SC");
if opt.fontdb.faces().count() == 0 {
    return Err("no usable bundled font faces".into());
}
let tree = usvg::Tree::from_str(&safe_svg, &opt)?;
let logical = tree.size();
// Check finite scale, dimensions, max edge and pixel budget before allocating.
let width = (logical.width() * scale).ceil() as u32;
let height = (logical.height() * scale).ceil() as u32;
let mut pixmap = tiny_skia::Pixmap::new(width, height)
    .ok_or("invalid or excessive output dimensions")?;
// Optional: pixmap.fill(tiny_skia::Color::from_rgba8(r, g, b, 255));
resvg::render(&tree, tiny_skia::Transform::from_scale(scale, scale),
              &mut pixmap.as_mut());
pixmap.save_png(output_path)?;
```

`Options::default()` creates an empty database; `fontdb_mut()` gets mutable access through `Arc::make_mut`. `load_font_data(Vec<u8>)` returns `()`, so successful file I/O alone is not evidence of a usable font; record the resulting family names/face count and verify they are the intended family. This code deliberately never calls `load_system_fonts`. `resvg::render` returns `()`, whereas SVG parsing and PNG saving return errors. [Options](https://github.com/linebender/resvg/blob/v0.46.0/crates/usvg/src/parser/options.rs), [font loading](https://github.com/RazrFalcon/fontdb/blob/v0.23.0/src/lib.rs), [render API](https://github.com/linebender/resvg/blob/v0.46.0/crates/resvg/src/lib.rs), [upstream minimal raster example](https://github.com/linebender/resvg/blob/v0.46.0/crates/resvg/examples/minimal.rs)

The lack of system font scanning does not make usvg's image resolver a filesystem/network security boundary; this helper's accepted input should be Merman-generated SVG. Missing external image/icon resources belong in explicit failures or boundary results, rather than silently broadening the ordinary corpus. Keep the safe SVG and PNG separately for the Qt SVG/PNG comparison.

## Errors and the remaining font-layout gap

`render_svg_resvg_safe_sync` returns `Result<Option<String>, HeadlessError>`. `None` is “no diagram detected”, not a successful blank diagram. `HeadlessError` is either `Parse(merman_core::Error)` or `Render(merman_render::Error)`, both transparent `Display` wrappers. Parse variants include `DetectType`, `UnsupportedDiagram { diagram_type }`, `DiagramParse { diagram_type, message }`, malformed frontmatter, invalid directive JSON and invalid frontmatter YAML. The generic parse error has no structured line/column field; preserve its full message and source. [Headless errors](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/src/render/mod.rs), [parser errors](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman-core/src/error.rs)

**Bundled glyphs do not guarantee correct label measurement.** `VendoredFontMetricsTextMeasurer` uses built-in browser-derived tables; it does not read the supplied OTF. Unknown family names without a generic fallback use `DeterministicTextMeasurer`; adding `sans-serif` switches to the built-in generic table, not the real Noto font. This makes Chinese long labels, wrapping and collisions a mandatory visual check. [Metric lookup](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman-render/src/text/font_metrics.rs)

If the ordinary samples reveal a measurement failure, a public extension exists: `with_text_measurer(Arc<dyn TextMeasurer + Send + Sync>)`. Its required method is `measure(&self, text: &str, style: &TextStyle) -> TextMetrics`; additional wrapping/advance/bbox methods have defaults. `TextStyle` carries `font_family`, `font_size`, `font_weight`; `TextMetrics` carries `width`, `height`, `line_count`. Those style/metric types are public in `merman_render::text` (add an exact `merman-render` dependency if implementing this). Implementing only `measure` does not establish all wrapping semantics; this is a repair seam if the prototype proves it necessary, not a reason to add a custom layout system before testing. [Trait](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman-render/src/text/measure.rs), [types](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman-render/src/text/types.rs), [facade setter](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/src/render/mod.rs)

## Versioned fixture anchors

Use original synthetic samples for this project; these small upstream fixtures establish basic accepted spelling:

| Family | Minimal syntax anchor | Upstream fixture |
| --- | --- | --- |
| C4 | `C4Context`, `Person(customerA, "Customer", "A customer")`, `System(sys, "Banking System", "Does banking")`, `Rel(customerA, sys, "Uses")` | [basic.mmd](https://github.com/Latias94/merman/blob/v0.7.0/fixtures/c4/basic.mmd) |
| Gantt | `gantt`, `dateFormat YYYY-MM-DD`, `section A`, `Task1 :a1, 2024-01-01, 1d` | [basic.mmd](https://github.com/Latias94/merman/blob/v0.7.0/fixtures/gantt/basic.mmd) |
| Mindmap | `mindmap` followed by indented `root`, then further-indented `a` and `b` | [basic.mmd](https://github.com/Latias94/merman/blob/v0.7.0/fixtures/mindmap/basic.mmd) |
| State | `stateDiagram-v2`, `[*] --> Idle`, nested `state Idle { ... }`, `Idle --> Processing : input received` | [state corpus](https://github.com/Latias94/merman/blob/v0.7.0/fixtures/state/zed_pr_57644_state.mmd) |

The tagged project has no extra feature needed for flowchart, sequence, class, state, ER, mindmap, pie, Gantt, or C4. Its own compatibility matrix is coverage evidence; this prototype must still establish final Qt output for the project's exact source/font/theme/scale corpus. [Tagged alignment matrix](https://github.com/Latias94/merman/blob/v0.7.0/docs/alignment/STATUS.md)
