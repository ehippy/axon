//! Portable drawing helpers and the shared status palette.

use std::collections::HashMap;

use crate::canvas::{Canvas, Color};
use crate::font::{Fonts, SIZE_WATERMARK};

pub const FG: Color = crate::canvas::WHITE;
pub const BG: Color = crate::canvas::BLACK;

pub const STATUS_EMPTY: Color = (90, 90, 90);

pub fn status_color(state: &str) -> Color {
    match state {
        "error" => (255, 60, 60),
        "running" => (70, 150, 255),
        _ => (90, 200, 120), // "idle" and unknown fall back to idle's color
    }
}

/// Per-agent identity color, so agents don't collapse into the same blue
/// just because they're both "running" - which was the common case, since
/// that's where an agent spends most of its time. Error still overrides to
/// red regardless of identity - that urgency needs to read instantly, not
/// get lost in a curated palette. Everything else keeps its own hue.
pub fn agent_color(source: &str, state: &str) -> Color {
    if state == "error" {
        return status_color("error");
    }
    let source = source.to_lowercase();
    // Sources are install-scoped ("hermes-69f471e05c0f4aa2"); the agent name
    // is the prefix before the install hash.
    let agent = source.split_once('-').map_or(source.as_str(), |(head, _)| head);
    match agent {
        // Claude Code's terracotta identity color.
        "claude" => (218, 119, 86),
        // Hermes integration color.
        "hermes" => (240, 190, 40),
        // The smart mirror (Glass) - white, its particle swarm's own color.
        "mirror" => (255, 255, 255),
        // Pi coding agent - its branding blue, rgb(106, 159, 204).
        "pi" => (106, 159, 204),
        // Unknown/future agents: a neutral hue distinct from the curated
        // colors above and from the state-based palette used elsewhere
        // (status_color's green/blue/red), so it doesn't get misread as
        // meaning something it doesn't.
        _ => (170, 170, 190),
    }
}

#[derive(Clone)]
pub struct AgentInfo {
    pub state: String,
    pub task: String,
    pub ts: f64, // unix seconds
}

/// Same priority order the LED uses: error beats running beats idle. None
/// if agents is empty.
pub fn aggregate_status(agents: &HashMap<String, AgentInfo>) -> Option<&'static str> {
    if agents.is_empty() {
        return None;
    }
    let states: Vec<&str> = agents.values().map(|a| a.state.as_str()).collect();
    if states.contains(&"error") {
        return Some("error");
    }
    if states.contains(&"running") {
        return Some("running");
    }
    Some("idle")
}

pub fn dim(color: Color, factor: f32) -> Color {
    (
        (color.0 as f32 * factor) as u8,
        (color.1 as f32 * factor) as u8,
        (color.2 as f32 * factor) as u8,
    )
}

/// Big, dim logo mark snapped to the bottom-right corner. margin_x clears
/// the panel's rounded-corner radius on the right; margin_y is 0 by design
/// (flush against the bottom edge, outside the curved-corner region).
pub fn watermark(
    canvas: &mut Canvas,
    fonts: &Fonts,
    text: &str,
    opacity: f32,
    margin_x: f32,
    margin_y: f32,
) {
    let w = fonts.text_width(SIZE_WATERMARK, text);
    let x = crate::canvas::W as f32 - margin_x - w;
    let y = crate::canvas::H as f32 - margin_y - SIZE_WATERMARK;
    fonts.draw_text(canvas, x, y, SIZE_WATERMARK, text, dim(FG, opacity));
}

/// Tight little battery glyph: outline + nub + proportional fill, plus a
/// small cutout "+" mark over the fill when charging.
pub fn battery_icon(canvas: &mut Canvas, x: i32, y: i32, pct: i32, charging: bool, w: i32, h: i32) {
    canvas.outline_rect(x, y, x + w, y + h, FG);
    let nub_h = h / 2;
    canvas.fill_rect(
        x + w + 1,
        y + (h - nub_h) / 2,
        x + w + 2,
        y + (h - nub_h) / 2 + nub_h,
        FG,
    );
    let fill_w = (((w - 2) as f32) * (pct.clamp(0, 100) as f32) / 100.0) as i32;
    if fill_w > 0 {
        canvas.fill_rect(x + 1, y + 1, x + 1 + fill_w, y + h - 1, FG);
    }
    if charging {
        let (cx, cy) = (x + w / 2, y + h / 2);
        canvas.draw_line(cx - 2, cy, cx + 2, cy, BG);
        canvas.draw_line(cx, cy - 2, cx, cy + 2, BG);
    }
}

pub fn double_rule(canvas: &mut Canvas, y: i32, x0: i32, x1: i32) {
    canvas.draw_line(x0, y, x1, y, FG);
    canvas.draw_line(x0, y + 3, x1, y + 3, FG);
}

#[cfg(test)]
mod hud_tests {
    use super::agent_color;

    #[test]
    fn install_scoped_sources_keep_their_agent_color() {
        // Sources are install-scoped; the name before the dash is the agent.
        assert_eq!(agent_color("pi-69f471e05c0f4aa2", "running"), (106, 159, 204));
        assert_eq!(agent_color("pi-69f471e05c0f4aa2", "idle"), (106, 159, 204));
        assert_eq!(agent_color("hermes-69f471e05c0f4aa2", "running"), (240, 190, 40));
        assert_eq!(agent_color("mirror", "idle"), (255, 255, 255));
        // Error always overrides identity; unknown agents stay neutral.
        assert_eq!(agent_color("pi-69f471e05c0f4aa2", "error"), (255, 60, 60));
        assert_eq!(agent_color("mystery-69f471e05c0f4aa2", "running"), (170, 170, 190));
    }
}
