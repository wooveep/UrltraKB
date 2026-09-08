//! Native rendering with explicit bundled fonts and no browser engine.
use merman::{
    MermaidConfig,
    render::{HeadlessRenderer, HostThemeProfile},
};
use resvg::{tiny_skia, usvg};
use serde_json::{Value, json};
use std::{
    fs,
    io::{self, Read},
    path::Path,
    sync::Arc,
};
mod fonts;
mod guard;

fn run(request: &Value) -> Result<Value, Box<dyn std::error::Error>> {
    let source = request["source"].as_str().ok_or("source missing")?;
    let dark = request["dark"].as_bool().unwrap_or(false);
    let id = request["id"].as_str().unwrap_or("prototype");
    let font_paths = request["fonts"]
        .as_array()
        .ok_or("explicit font files missing")?;
    if font_paths.len() != 2 {
        return Err("regular and bold fonts required".into());
    }
    let regular = fs::read(font_paths[0].as_str().ok_or("bad font path")?)?;
    let bold = fs::read(font_paths[1].as_str().ok_or("bad font path")?)?;
    for font in [&regular, &bold] {
        rustybuzz::Face::from_slice(font, 0).ok_or("invalid bundled font")?;
    }
    let svg = if request["kind"].as_str() == Some("svg") {
        source.to_string()
    } else {
        let mut theme = if dark {
            HostThemeProfile::editor_dark()
        } else {
            HostThemeProfile::editor_light()
        };
        theme.font_family = Some("Noto Sans CJK SC".to_string());
        let renderer = HeadlessRenderer::new()
            .with_strict_parsing()
            .with_text_measurer(Arc::new(fonts::BundledFonts { regular, bold }))
            .with_fixed_local_offset_minutes(Some(0))
            .with_site_config(MermaidConfig::from_value(json!({
                "theme": if dark { "dark" } else { "default" },
                "fontFamily": "Noto Sans CJK SC",
                "themeVariables": {"fontFamily": "Noto Sans CJK SC"},
                "securityLevel": "strict"
            })))
            .with_host_theme(&theme)
            .with_diagram_id(id);
        // The builder scopes its offset to parsing. Gantt layout also reads
        // local time; Windows ignores TZ, so keep the same offset through SVG.
        merman::time::with_fixed_local_offset_minutes(Some(0), || {
            renderer.render_svg_sync(source)
        })?
        .ok_or("no SVG produced")?
    };
    if request["svg_only"].as_bool().unwrap_or(false) {
        return Ok(json!({"ok":true,"svg":svg}));
    }
    let svg_path = request["svg_path"].as_str().ok_or("svg_path missing")?;
    let png_path = request["png_path"].as_str().ok_or("png_path missing")?;
    fs::write(svg_path, &svg)?;
    let mut options = usvg::Options::default();
    options.font_family = "Noto Sans CJK SC".to_string();
    let fonts = request["fonts"]
        .as_array()
        .ok_or("explicit font files missing")?;
    for font in fonts {
        options
            .fontdb_mut()
            .load_font_data(fs::read(Path::new(font.as_str().ok_or("bad font path")?))?);
    }
    options
        .fontdb_mut()
        .set_sans_serif_family("Noto Sans CJK SC");
    options.fontdb_mut().set_serif_family("Noto Sans CJK SC");
    options
        .fontdb_mut()
        .set_monospace_family("Noto Sans CJK SC");
    let tree = usvg::Tree::from_str(&svg, &options)?;
    let scale = request["scale"].as_f64().unwrap_or(1.0) as f32;
    let width = (tree.size().width() * scale).ceil() as u32;
    let height = (tree.size().height() * scale).ceil() as u32;
    if width == 0
        || height == 0
        || width > 8192
        || height > 8192
        || width as u64 * height as u64 > 33_554_432
    {
        return Err("raster dimensions exceed prototype limit".into());
    }
    let mut pixmap = tiny_skia::Pixmap::new(width, height).ok_or("raster allocation failed")?;
    resvg::render(
        &tree,
        tiny_skia::Transform::from_scale(scale, scale),
        &mut pixmap.as_mut(),
    );
    pixmap.save_png(png_path)?;
    Ok(
        json!({"ok":true,"svg_path":svg_path,"png_path":png_path,"width":width,"height":height,
        "font_faces": options.fontdb.faces().count(), "system_fonts":false, "scale":scale,
        "text_measurement":"bundled Noto + rustybuzz; explicit lines only"}),
    )
}

fn main() {
    guard::install();
    let mut input = String::new();
    let result = io::stdin()
        .read_to_string(&mut input)
        .map_err(|e| e.to_string())
        .and_then(|_| serde_json::from_str::<Value>(&input).map_err(|e| e.to_string()))
        .and_then(|request| run(&request).map_err(|e| e.to_string()));
    match result {
        Ok(value) => println!("{}", value),
        Err(error) => {
            println!("{}", json!({"ok":false,"error":error}));
            std::process::exit(1);
        }
    }
}
