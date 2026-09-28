//! Minimal software canvas: RGB u8 pixel buffer + the handful of drawing
//! primitives screens.rs actually needs, plus RGB565 packing and the 90deg
//! rotation onto the panel's physical orientation. Port of the drawing half
//! of software/display-service/hud.py, PIL replaced by hand-rolled fills
//! (rectangles/lines/ellipses are all trivial - no need for a dependency).

use crate::panel;

pub type Color = (u8, u8, u8);

pub const BLACK: Color = (0, 0, 0);
pub const WHITE: Color = (255, 255, 255);

// Logical composition canvas - landscape; rotated 90deg clockwise onto the
// panel's physical portrait orientation right before it's pushed to
// hardware. See rotate_to_panel() below.
pub const W: u32 = 280;
pub const H: u32 = 240;

pub struct Canvas {
    pub width: u32,
    pub height: u32,
    pixels: Vec<u8>, // RGB, row-major, 3 bytes/pixel
}

impl Canvas {
    pub fn new(width: u32, height: u32, bg: Color) -> Self {
        let mut pixels = Vec::with_capacity((width * height * 3) as usize);
        for _ in 0..(width * height) {
            pixels.push(bg.0);
            pixels.push(bg.1);
            pixels.push(bg.2);
        }
        Canvas { width, height, pixels }
    }

    #[inline]
    fn idx(&self, x: i32, y: i32) -> Option<usize> {
        if x < 0 || y < 0 || x as u32 >= self.width || y as u32 >= self.height {
            return None;
        }
        Some((y as u32 * self.width + x as u32) as usize * 3)
    }

    pub fn set_pixel(&mut self, x: i32, y: i32, color: Color) {
        if let Some(i) = self.idx(x, y) {
            self.pixels[i] = color.0;
            self.pixels[i + 1] = color.1;
            self.pixels[i + 2] = color.2;
        }
    }

    pub fn get_pixel(&self, x: i32, y: i32) -> Color {
        match self.idx(x, y) {
            Some(i) => (self.pixels[i], self.pixels[i + 1], self.pixels[i + 2]),
            None => BLACK,
        }
    }

    /// Blend `color` onto the existing pixel by `coverage` (0.0-1.0) - used
    /// for antialiased glyph edges. BG is always pure black in practice, so
    /// this doubles as the "opacity" math hud.watermark used to do by hand.
    pub fn blend_pixel(&mut self, x: i32, y: i32, color: Color, coverage: f32) {
        if coverage <= 0.0 {
            return;
        }
        if coverage >= 1.0 {
            self.set_pixel(x, y, color);
            return;
        }
        let under = self.get_pixel(x, y);
        let mix = |a: u8, b: u8| (a as f32 * (1.0 - coverage) + b as f32 * coverage).round() as u8;
        self.set_pixel(x, y, (mix(under.0, color.0), mix(under.1, color.1), mix(under.2, color.2)));
    }

    /// Inclusive of both corners, matching PIL's ImageDraw.rectangle.
    pub fn fill_rect(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, color: Color) {
        for y in y0..=y1 {
            for x in x0..=x1 {
                self.set_pixel(x, y, color);
            }
        }
    }

    pub fn outline_rect(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, color: Color) {
        for x in x0..=x1 {
            self.set_pixel(x, y0, color);
            self.set_pixel(x, y1, color);
        }
        for y in y0..=y1 {
            self.set_pixel(x0, y, color);
            self.set_pixel(x1, y, color);
        }
    }

    pub fn draw_line(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, color: Color) {
        let (mut x0, mut y0) = (x0, y0);
        let dx = (x1 - x0).abs();
        let dy = (y1 - y0).abs();
        let sx = if x0 < x1 { 1 } else { -1 };
        let sy = if y0 < y1 { 1 } else { -1 };
        let mut err = dx - dy;
        loop {
            self.set_pixel(x0, y0, color);
            if x0 == x1 && y0 == y1 {
                break;
            }
            let e2 = 2 * err;
            if e2 > -dy {
                err -= dy;
                x0 += sx;
            }
            if e2 < dx {
                err += dx;
                y0 += sy;
            }
        }
    }

    pub fn fill_ellipse(&mut self, cx: i32, cy: i32, r: i32, color: Color) {
        for y in -r..=r {
            for x in -r..=r {
                if x * x + y * y <= r * r {
                    self.set_pixel(cx + x, cy + y, color);
                }
            }
        }
    }

    /// Pack into big-endian RGB565, matching WhisplayBoard.draw_image()'s
    /// expected wire format exactly (same bit packing hud.to_rgb565 used).
    pub fn to_rgb565(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.pixels.len() / 3 * 2);
        for chunk in self.pixels.chunks_exact(3) {
            let (r, g, b) = (chunk[0], chunk[1], chunk[2]);
            let val: u16 = ((r as u16 & 0xF8) << 8) | ((g as u16 & 0xFC) << 3) | (b as u16 >> 3);
            out.push((val >> 8) as u8);
            out.push((val & 0xFF) as u8);
        }
        out
    }

    /// Rotate this (W x H) logical frame 90deg clockwise into the panel's
    /// fixed physical (PANEL_W x PANEL_H) orientation.
    pub fn rotate_to_panel(&self) -> Canvas {
        assert_eq!((self.width, self.height), (W, H), "rotate_to_panel expects the logical WxH canvas");
        let mut out = Canvas::new(panel::LCD_WIDTH, panel::LCD_HEIGHT, BLACK);
        // Clockwise rotation: dst(x, y) = src(y, height-1-x)  where dst is
        // (height x width) i.e. (280's height becomes width... concretely:
        // new_x = H-1-old_y, new_y = old_x for a -90deg (clockwise) turn.
        for y in 0..self.height {
            for x in 0..self.width {
                let color = self.get_pixel(x as i32, y as i32);
                let nx = (self.height - 1 - y) as i32;
                let ny = x as i32;
                out.set_pixel(nx, ny, color);
            }
        }
        out
    }
}
