//! Animation ticker: drives everything that changes on its own, without a
//! button press or an MQTT message triggering it.
//!
//!  - one small dot per active agent, each on its own tilted elliptical
//!    orbit around a shared center - distinct radius and tilt per orbit
//!    (inner shells smaller, like real orbital mechanics), colored by that
//!    agent's own state. An atom, not a shared racetrack. Orbital speed
//!    tracks that agent's own recency: a fresh report kicks it into a fast
//!    spin that exponentially decays back to the shell's slow baseline as
//!    the report ages - motion literally shows "how recently did this
//!    agent do something," not just a fixed decorative speed per shell.
//!  - the status dot/RGB LED breathing on "running", pulsing sharp on
//!    "error", perfectly still on "idle" - motion as signal, not just color
//!  - a full-screen breathing radial gradient while screen=="voice"
//!    (driven by voicebus.rs from gabbie's own state) - green while she's
//!    listening, blue while she's thinking/speaking
//!  - a brief RGB LED attention flash when an agent transitions into
//!    "idle" (just finished, waiting on input) or "error" - lighter-weight
//!    than gabbie interrupting you to say something, and independent of
//!    her (this reads the same MQTT agent stream axon-native already has,
//!    no round trip through gabbie needed)
//!
//! Runs continuously at TICK while screen=="idle" (not gated off during
//! idle/no-agents the way the dot/LED animation alone was - the orbiting
//! dots are an always-on feature, so this trades the "zero CPU when
//! nothing's happening" property for "the screen is visibly always alive").

use std::collections::HashMap;
use std::f64::consts::PI;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::font::Fonts;
use crate::hud;
use crate::Daemon;

fn now_unix() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

// Real measured render cost is ~18ms/frame (12ms of that is the SPI bus
// transfer itself, near its 100MHz theoretical floor) - 30ms leaves a
// comfortable margin above 30fps rather than cutting it exactly at 33.3ms.
const TICK: Duration = Duration::from_millis(30); // ~33Hz
const BREATHE_PERIOD_SECS: f64 = 2.4;
const PULSE_PERIOD_SECS: f64 = 0.5;
const VOICE_BREATHE_PERIOD_SECS: f64 = 3.0; // slower - a full-screen glow reads calmer slow
// Safety net, not a UX timer - see the "voice" branch below. Was 12s, but a
// real trace showed inference-host taking 26.3s to first token on one turn,
// so 12s was reverting the splash to idle mid-"thinking", before "speaking"
// ever had a chance to fire - the button->blue handoff looked broken when
// it was actually just racing real inference latency. 60s comfortably
// covers slow turns while still catching a genuinely stuck/crashed one.
const VOICE_TIMEOUT: Duration = Duration::from_secs(60);
const LED_FLASH_DURATION: Duration = Duration::from_millis(1500);
const LED_FLASH_IDLE_COLOR: (u8, u8, u8) = (255, 255, 255); // distinct from the steady idle green
const LED_FLASH_ERROR_COLOR: (u8, u8, u8) = (255, 60, 60); // matches STATUS_COLORS["error"]

// Shared "nucleus" all orbits are centered on, in logical (280x240) canvas
// coordinates.
const ORBIT_CENTER: (f64, f64) = (140.0, 118.0);
const ORBIT_BASE_RX: f64 = 26.0; // innermost shell's semi-major axis
const ORBIT_RX_STEP: f64 = 20.0; // each shell out adds this much radius
const ORBIT_ASPECT: f64 = 0.6; // semi-minor / semi-major, flattens the ellipse
const ORBIT_TILT_STEP: f64 = PI / 5.0; // each shell tilted 36deg further
const ORBIT_BASE_PERIOD_SECS: f64 = 14.0; // innermost shell's lap time at baseline (fully decayed) speed
const ORBIT_PERIOD_STEP_SECS: f64 = 6.0; // each shell out orbits slower at baseline

// Recency -> speed: right after a report, angular velocity is boosted by
// up to (1 + BOOST_MULTIPLIER)x the shell's baseline; that boost decays
// exponentially with this time constant as the report ages.
const DECAY_TAU_SECS: f64 = 9.0;
const BOOST_MULTIPLIER: f64 = 7.0;

/// Angular velocity (rad/s) for shell `i` given how long ago (seconds)
/// that agent last reported in.
fn angular_velocity(i: usize, age_secs: f64) -> f64 {
    let period = ORBIT_BASE_PERIOD_SECS + i as f64 * ORBIT_PERIOD_STEP_SECS;
    let omega_base = 2.0 * PI / period;
    let boost = (-age_secs.max(0.0) / DECAY_TAU_SECS).exp();
    omega_base * (1.0 + BOOST_MULTIPLIER * boost)
}

/// Position on agent index `i`'s own tilted elliptical orbit at the given
/// accumulated phase angle (radians).
fn orbit_position(i: usize, phase: f64) -> (f64, f64) {
    let i = i as f64;
    let rx = ORBIT_BASE_RX + i * ORBIT_RX_STEP;
    let ry = rx * ORBIT_ASPECT;
    let tilt = i * ORBIT_TILT_STEP;

    let (ex, ey) = (rx * phase.cos(), ry * phase.sin());
    let (cos_t, sin_t) = (tilt.cos(), tilt.sin());
    (
        ORBIT_CENTER.0 + ex * cos_t - ey * sin_t,
        ORBIT_CENTER.1 + ex * sin_t + ey * cos_t,
    )
}

pub fn spawn(shared: Arc<Mutex<Daemon>>, fonts: Arc<Fonts>) {
    thread::spawn(move || {
        let start = Instant::now();
        // Each agent's accumulated orbital phase, integrated tick by tick
        // from its own (recency-dependent) angular velocity - owned by
        // this thread, not shared state, since it's purely an animation
        // concern. Keyed by name so an agent keeps its own accumulated
        // phase continuous even if other agents come and go around it.
        let mut phases: HashMap<String, f64> = HashMap::new();
        // Previous state per agent, purely to detect transitions (see the
        // flash-detection block below) - owned by this thread like
        // `phases`, not shared state.
        let mut prev_states: HashMap<String, String> = HashMap::new();
        let mut last_tick = Instant::now();
        loop {
            // Target a fixed PERIOD (TICK), not a fixed sleep on top of
            // whatever render() costs - sleeping the full TICK every time
            // regardless of render duration was silently halving the real
            // frame rate (30ms sleep + ~20ms render ~= 60ms/frame, not 30).
            let cycle_start = Instant::now();
            let dt = (cycle_start - last_tick).as_secs_f64().min(0.5);
            last_tick = cycle_start;
            {
                let mut daemon = shared.lock().unwrap();
                daemon.state.prune_agents(now_unix());
                let mut screen = daemon.state.screen.clone();

                // LED attention flash: detect "just went idle" (finished a
                // run, waiting on input) or "just errored" transitions.
                // Runs regardless of which screen is up - the LED is
                // independent of what's on the panel.
                let mut flash: Option<(u8, u8, u8)> = None;
                for (name, info) in daemon.state.agents.iter() {
                    let prev = prev_states.get(name).map(String::as_str);
                    let became_idle = info.state == "idle" && prev == Some("running");
                    let became_error = info.state == "error" && prev != Some("error");
                    if became_idle || became_error {
                        flash = Some(if became_error { LED_FLASH_ERROR_COLOR } else { LED_FLASH_IDLE_COLOR });
                    }
                }
                if let Some(color) = flash {
                    daemon.state.led_flash_until = Some(cycle_start + LED_FLASH_DURATION);
                    daemon.state.led_flash_color = color;
                }
                prev_states = daemon.state.agents.iter().map(|(k, v)| (k.clone(), v.state.clone())).collect();

                // Safety net: if the splash has been up with no qualifying
                // activity (button edge, or a real "thinking"/"speaking"
                // from gabbie - see voicebus.rs) for this long, force it
                // back to idle. Covers any turn that doesn't cleanly reach
                // "done_speaking" - a skipped utterance, a crash mid-reply -
                // so a missed event can never leave it stuck up forever
                // the way the old event mapping did.
                if screen == "voice" {
                    let stale = daemon
                        .state
                        .voice_last_activity
                        .map_or(true, |t| t.elapsed() > VOICE_TIMEOUT);
                    if stale {
                        daemon.state.screen = "idle".to_string();
                        daemon.state.voice_state = None;
                        screen = "idle".to_string();
                    }
                }

                // "voice" draws the full idle dashboard too (see
                // screens::render_voice) - the orbiting agent dots and
                // status-dot breathing stay live underneath the splash,
                // not frozen at whatever they were when it opened.
                if screen == "idle" || screen == "voice" {
                    let t = start.elapsed().as_secs_f64();
                    let now = now_unix();

                    // Stable order (by name) so a given agent keeps the
                    // same shell index (radius/tilt) as long as it keeps
                    // reporting, instead of jumping shells whenever the
                    // agent set changes.
                    let mut names: Vec<String> = daemon.state.agents.keys().cloned().collect();
                    names.sort();
                    daemon.state.fly_dots = names
                        .iter()
                        .enumerate()
                        .map(|(i, name)| {
                            let info = &daemon.state.agents[name];
                            let age = now - info.ts;
                            let omega = angular_velocity(i, age);
                            let phase = phases.entry(name.clone()).or_insert(i as f64 * 0.9);
                            *phase = (*phase + omega * dt) % (2.0 * PI);
                            (orbit_position(i, *phase), hud::agent_color(name, &info.state))
                        })
                        .collect();

                    let bucket = hud::aggregate_status(&daemon.state.agents);
                    daemon.state.dot_brightness = match bucket {
                        Some("running") => {
                            0.4 + 0.6 * (0.5 + 0.5 * (2.0 * PI * t / BREATHE_PERIOD_SECS).sin())
                        }
                        Some("error") => {
                            if (t % PULSE_PERIOD_SECS) < PULSE_PERIOD_SECS / 2.0 { 1.0 } else { 0.15 }
                        }
                        _ => 1.0,
                    };

                    if screen == "voice" {
                        daemon.state.voice_breath =
                            0.35 + 0.65 * (0.5 + 0.5 * (2.0 * PI * t / VOICE_BREATHE_PERIOD_SECS).sin());
                    }

                    daemon.render(&fonts);
                }
            }
            let elapsed = cycle_start.elapsed();
            if elapsed < TICK {
                thread::sleep(TICK - elapsed);
            }
        }
    });
}
