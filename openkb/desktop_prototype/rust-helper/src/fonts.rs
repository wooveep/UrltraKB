//! Prototype adapter: layout and rasterization must use the same font files.
use merman_render::text::{DeterministicTextMeasurer, TextMeasurer, TextMetrics, TextStyle};
use rustybuzz::{Face, UnicodeBuffer};

pub struct BundledFonts {
    pub regular: Vec<u8>,
    pub bold: Vec<u8>,
}

impl BundledFonts {
    fn face(&self, style: &TextStyle) -> Face<'_> {
        let weight = style.font_weight.as_deref().unwrap_or("400");
        let bold = weight == "bold" || weight.parse::<u16>().unwrap_or(400) >= 600;
        Face::from_slice(if bold { &self.bold } else { &self.regular }, 0)
            .expect("font validated at startup")
    }
}

impl TextMeasurer for BundledFonts {
    fn measure(&self, text: &str, style: &TextStyle) -> TextMetrics {
        let face = self.face(style);
        let factor = style.font_size / f64::from(face.units_per_em());
        let lines = DeterministicTextMeasurer::normalized_text_lines(text);
        let mut width: f64 = 0.0;
        for line in &lines {
            let mut buffer = UnicodeBuffer::new();
            buffer.push_str(line);
            buffer.guess_segment_properties();
            let shaped = rustybuzz::shape(&face, &[], buffer);
            let advance: i32 = shaped.glyph_positions().iter().map(|p| p.x_advance).sum();
            width = width.max(f64::from(advance) * factor);
        }
        // Preserve explicit lines. Automatic wrapping is outside this minimal adapter's scope;
        // allowing a wider box is preferable to claiming unmeasured HTML wrapping compatibility.
        TextMetrics {
            width,
            height: lines.len() as f64 * style.font_size * 1.5,
            line_count: lines.len(),
        }
    }
}
