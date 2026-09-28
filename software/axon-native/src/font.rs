//! Text rendering via ab_glyph (pure Rust, no fontconfig/freetype). Font is
//! face 0 of the bundled Font.ttc (WenQuanYi Micro Hei), extracted to a
//! standalone .ttf with fonttools since ab_glyph wants a single-face file -
//! see assets/font.ttf. Embedded at compile time so there's no on-disk path
//! dependency at runtime (unlike hud.py's FONT_PATH lookup).

use ab_glyph::{Font, FontRef, Glyph, PxScale, ScaleFont};

use crate::canvas::{Canvas, Color};

static FONT_BYTES: &[u8] = include_bytes!("../assets/font.ttf");

pub struct Fonts {
    font: FontRef<'static>,
}

// Point sizes, matching hud.py's font_* globals (font_wordmark dropped -
// only reachable via the unused vertical_text()).
pub const SIZE_TITLE: f32 = 20.0;
pub const SIZE_WATERMARK: f32 = 64.0;
pub const SIZE_TINY: f32 = 11.0;
pub const SIZE_BODY: f32 = 15.0;
pub const SIZE_SMALL: f32 = 12.0;
pub const SIZE_TASK: f32 = 17.0; // the "latest move" line - was SIZE_TINY (11), still too small at 14

impl Fonts {
    pub fn load() -> Self {
        let font = FontRef::try_from_slice(FONT_BYTES).expect("bundled font.ttf failed to parse");
        Fonts { font }
    }

    pub fn text_width(&self, size: f32, text: &str) -> f32 {
        let scaled = self.font.as_scaled(PxScale::from(size));
        text.chars().map(|c| scaled.h_advance(scaled.glyph_id(c))).sum()
    }

    /// Draws `text` with its top-left corner at (x, y), antialiased via
    /// glyph coverage blended onto whatever is already on the canvas
    /// (mirrors PIL's default antialiased text rendering closely enough).
    pub fn draw_text(&self, canvas: &mut Canvas, x: f32, y: f32, size: f32, text: &str, color: Color) {
        let scaled = self.font.as_scaled(PxScale::from(size));
        let ascent = scaled.ascent();
        let mut caret_x = x;
        for c in text.chars() {
            let glyph_id = scaled.glyph_id(c);
            let advance = scaled.h_advance(glyph_id);
            let glyph: Glyph = glyph_id.with_scale_and_position(PxScale::from(size), ab_glyph::point(caret_x, y + ascent));
            if let Some(outlined) = self.font.outline_glyph(glyph) {
                let bounds = outlined.px_bounds();
                outlined.draw(|gx, gy, coverage| {
                    let px = bounds.min.x as i32 + gx as i32;
                    let py = bounds.min.y as i32 + gy as i32;
                    canvas.blend_pixel(px, py, color, coverage);
                });
            }
            caret_x += advance;
        }
    }
}
