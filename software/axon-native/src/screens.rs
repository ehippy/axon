//! Port of software/display-service/screens.py. One real screen (idle) plus
//! two transient ones (boot, shutdown) - see docs/PROTOTYPE-PWNAGOTCHI.md
//! for why the UI collapsed down to this from an earlier multi-screen cycle.

use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::canvas::Canvas;
use crate::font::{Fonts, SIZE_BODY, SIZE_SMALL, SIZE_TASK, SIZE_TINY, SIZE_TITLE};
use crate::hud;
use crate::state::AppState;
use crate::canvas;

const MARGIN: i32 = 10;
const TOP_LEFT_INSET: i32 = 20;

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

fn relative_time(ts: f64) -> String {
    let delta = now() - ts;
    if delta < 60.0 {
        "now".to_string()
    } else if delta < 3600.0 {
        format!("{}m", (delta / 60.0) as i64)
    } else if delta < 86400.0 {
        format!("{}h", (delta / 3600.0) as i64)
    } else {
        format!("{}d", (delta / 86400.0) as i64)
    }
}

pub fn wifi_ssid() -> String {
    let out = Command::new("/usr/bin/nmcli")
        .args(["-t", "-f", "active,ssid", "dev", "wifi"])
        .output();
    match out {
        Ok(o) => {
            let text = String::from_utf8_lossy(&o.stdout);
            text.lines()
                .find_map(|l| l.strip_prefix("yes:"))
                .unwrap_or("?")
                .to_string()
        }
        Err(_) => "?".to_string(),
    }
}

pub fn render_boot(canvas: &mut Canvas, fonts: &Fonts, _state: &AppState) {
    hud::watermark(canvas, fonts, "AXON", 0.165, 20.0, 0.0);
    fonts.draw_text(canvas, MARGIN as f32, 40.0, SIZE_TITLE, "ONLINE", hud::FG);
    fonts.draw_text(canvas, MARGIN as f32, 75.0, SIZE_SMALL, "axon display", hud::FG);
    fonts.draw_text(canvas, MARGIN as f32, 92.0, SIZE_SMALL, "daemon started", hud::FG);
}

pub fn render_shutdown(canvas: &mut Canvas, fonts: &Fonts, _state: &AppState) {
    hud::watermark(canvas, fonts, "AXON", 0.165, 20.0, 0.0);
    fonts.draw_text(canvas, MARGIN as f32, 30.0, SIZE_TITLE, "POWERING", hud::FG);
    fonts.draw_text(canvas, MARGIN as f32, 55.0, SIZE_TITLE, "DOWN", hud::FG);
    fonts.draw_text(canvas, MARGIN as f32, 90.0, SIZE_SMALL, "long-press triggered", hud::FG);
}

pub fn render_idle(canvas: &mut Canvas, fonts: &Fonts, state: &AppState) {
    hud::watermark(canvas, fonts, "AXON", 0.165, 20.0, 0.0);
    render_idle_body(canvas, fonts, state);
}

fn render_idle_body(canvas: &mut Canvas, fonts: &Fonts, state: &AppState) {
    // The aggregate status dot that used to sit here was pure duplication
    // once agents got identity colors: it only ever showed the same
    // priority-collapsed bucket the RGB LED already shows physically, with
    // none of the per-agent detail the rows/orbits carry. Removed rather
    // than kept as decoration nobody could read anything from.
    let text_x = (MARGIN + TOP_LEFT_INSET) as f32;
    fonts.draw_text(canvas, text_x, 6.0, SIZE_TINY, &state.hostname, hud::FG);
    let hostname_w = fonts.text_width(SIZE_TINY, &state.hostname);
    fonts.draw_text(canvas, text_x + hostname_w + 8.0, 6.0, SIZE_TINY, &state.wifi_ssid, hud::FG);

    let icon_w = 18;
    hud::battery_icon(
        canvas,
        canvas::W as i32 - 6 - icon_w - icon_w - 3,
        5,
        state.battery_pct,
        state.battery_charging,
        icon_w,
        9,
    );

    hud::double_rule(canvas, 24, MARGIN, canvas::W as i32);

    let row_top = 34;
    if state.agents.is_empty() {
        fonts.draw_text(canvas, MARGIN as f32, (row_top + 40) as f32, SIZE_BODY, "NO AGENTS", hud::FG);
        fonts.draw_text(canvas, MARGIN as f32, (row_top + 60) as f32, SIZE_BODY, "REPORTING", hud::FG);
    } else {
        let mut ordered: Vec<(&String, &hud::AgentInfo)> = state.agents.iter().collect();
        ordered.sort_by(|a, b| b.1.ts.partial_cmp(&a.1.ts).unwrap());
        // 44 not 36 - gives the (now much bigger) task line room to breathe
        // without crowding the next row. Costs one row of max capacity
        // (floor(200/44)=4 vs floor(200/36)=5) - a fine trade, there are
        // rarely more than 2-3 agents reporting at once in practice.
        let row_h = if state.agents.keys().any(|s| s.starts_with('@')) { 60 } else { 44 };
        let max_rows = ((canvas::H as i32 - row_top - 6) / row_h).max(0) as usize;
        let mut ly = row_top;
        // Rotate pages so concurrent chats beyond the first screen remain visible.
        let pages = (ordered.len() + max_rows.max(1) - 1) / max_rows.max(1);
        let page = (std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default().as_secs() / 10) as usize % pages.max(1);
        for (source, info) in ordered.into_iter().skip(page * max_rows).take(max_rows) {
            let row_color = hud::agent_color(axon_native::activity::identity_source(source), &info.state);
            let title = if let Some(key) = source.strip_prefix('@') {
                let (agent, session) = key.split_once('/').unwrap_or((key, ""));
                format!("{} {}", agent, session.chars().take(4).collect::<String>())
            } else { source.clone() };
            let title: String = title.chars().take(21).collect();
            fonts.draw_text(canvas, MARGIN as f32, ly as f32, SIZE_BODY, &title.to_uppercase(), row_color);
            let age = relative_time(info.ts);
            let age_w = fonts.text_width(SIZE_TINY, &age);
            fonts.draw_text(canvas, canvas::W as f32 - 8.0 - age_w, (ly + 3) as f32, SIZE_TINY, &age, hud::FG);
            let task = if info.task.is_empty() { info.state.as_str() } else { info.task.as_str() };
            for (i, line) in task.lines().take(if row_h == 60 { 2 } else { 1 }).enumerate() {
                let line: String = line.chars().take(36).collect();
                fonts.draw_text(canvas, MARGIN as f32, (ly + 19 + i as i32 * 18) as f32, SIZE_TASK, &line, hud::FG);
            }
            ly += row_h;
        }
    }

    // One dot per active agent, each flying the wandering bezier loop at
    // its own phase, colored by that agent's own state - drawn last so
    // they pass over everything else.
    for ((fx, fy), color) in &state.fly_dots {
        canvas.fill_ellipse(fx.round() as i32, fy.round() as i32, 3, *color);
    }
}

/// Breathing radial gradient wash - green while gabbie is listening, blue
/// while she's thinking/speaking - drawn as a *background* behind the
/// normal dashboard (hostname/wifi/battery/agent rows), not a takeover of
/// it; the falloff darkens toward the corners, which conveniently matches
/// the panel's rounded-corner bezel with no extra inset math needed.
///
/// Draw order matters here: the watermark goes down first and the
/// gradient blends *over* it (not the other way around), so "AXON" sits
/// behind the glow - dimmed and tinted by it near the center, still
/// showing through plainly near the edges where the glow fades - rather
/// than sitting on top as a crisp label.
pub fn render_voice(canvas: &mut Canvas, fonts: &Fonts, state: &AppState) {
    hud::watermark(canvas, fonts, "AXON", 0.165, 20.0, 0.0);

    let color = match state.voice_state.as_deref() {
        Some("speaking") => (70, 150, 255), // blue - matches STATUS_COLORS["running"]
        _ => (70, 220, 130),                // green - listening, and the default
    };
    let (cx, cy) = (canvas::W as f32 / 2.0, canvas::H as f32 / 2.0);
    let max_r = (cx * cx + cy * cy).sqrt();
    let breath = state.voice_breath as f32;
    for y in 0..canvas.height {
        for x in 0..canvas.width {
            let (dx, dy) = (x as f32 - cx, y as f32 - cy);
            let t = ((dx * dx + dy * dy).sqrt() / max_r).min(1.0);
            let falloff = (1.0 - t) * (1.0 - t);
            canvas.blend_pixel(x as i32, y as i32, color, (falloff * breath).clamp(0.0, 1.0));
        }
    }
    render_idle_body(canvas, fonts, state);
}

pub fn render(screen: &str, canvas: &mut Canvas, fonts: &Fonts, state: &AppState) {
    match screen {
        "boot" => render_boot(canvas, fonts, state),
        "shutdown" => render_shutdown(canvas, fonts, state),
        "voice" => render_voice(canvas, fonts, state),
        _ => render_idle(canvas, fonts, state),
    }
}

pub const HOME: &str = "idle";
