use std::collections::HashMap;
use std::time::Instant;

use crate::hud::AgentInfo;

// Agents age out after this much silence. A dead publisher can never
// retract its retained status message, so consumers must expire entries —
// otherwise a departed "logging off" ghost sits on the AGENTS screen
// forever. Payload `ts` (see mqtt.rs) keeps the age honest across restarts.
pub const AGENT_FORGET: f64 = 12.0 * 3600.0;

pub struct AppState {
    pub screen: String,
    pub hostname: String,
    pub agents: HashMap<String, AgentInfo>,
    pub activity_deadlines: HashMap<String, f64>,
    /// 0.0-1.0 multiplier applied to the status dot's color (and, scaled
    /// down, the RGB LED) - the animation ticker drives this to breathe on
    /// "running" and pulse sharper on "error"; stays 1.0 (no-op) otherwise.
    pub dot_brightness: f64,
    pub wifi_ssid: String,
    pub battery_pct: i32,
    pub battery_charging: bool,
    /// One flying dot per active agent, each riding the same wandering
    /// bezier loop at its own phase offset and colored by that agent's own
    /// state - position + color, updated every animation tick.
    pub fly_dots: Vec<((f64, f64), (u8, u8, u8))>,
    /// Some("listening") (button held, set directly by the button
    /// callback - local and instant, never false-triggers on ambient
    /// room noise the way gabbie's own VAD-driven "listening" event
    /// would) or Some("speaking") (from gabbie's EventBus - see
    /// voicebus.rs - "thinking"/"speaking"/"confirming", which only ever
    /// fire after a genuinely accepted utterance) shows the breathing
    /// splash; None means no voice activity.
    pub voice_state: Option<String>,
    /// 0.0-1.0 brightness the voice splash's radial gradient breathes at.
    pub voice_breath: f64,
    /// Last time voice_state changed for a real reason (button edge, or a
    /// qualifying gabbie event) - the animate ticker force-reverts to idle
    /// if this goes stale, so a missed/skipped terminal event (gabbie
    /// crashing mid-reply, a turn that never actually engages) can never
    /// leave the splash stuck up forever.
    pub voice_last_activity: Option<Instant>,
    /// Set by the animation ticker when an agent transitions into "idle"
    /// (just finished, waiting on you) or "error" - a brief attention
    /// flash on the RGB LED, distinct from the steady state-driven color,
    /// so a status change too minor for gabbie to interrupt you about
    /// still gets *something* passive and immediate. None once expired.
    pub led_flash_until: Option<Instant>,
    pub led_flash_color: (u8, u8, u8),
}

impl AppState {
    pub fn new(hostname: String) -> Self {
        AppState {
            screen: "idle".to_string(),
            hostname,
            agents: HashMap::new(),
            activity_deadlines: HashMap::new(),
            dot_brightness: 1.0,
            wifi_ssid: "?".to_string(),
            battery_pct: 0,
            battery_charging: false,
            fly_dots: Vec::new(),
            voice_state: None,
            voice_breath: 1.0,
            voice_last_activity: None,
            led_flash_until: None,
            led_flash_color: (255, 255, 255),
        }
    }

    /// Drop agents silent past AGENT_FORGET. The map holds a handful of
    /// fleet members, so this is free; call from the animation tick and
    /// on every inbound status.
    pub fn prune_agents(&mut self, now: f64) {
        self.agents.retain(|_, a| now - a.ts < AGENT_FORGET);
    }
}
