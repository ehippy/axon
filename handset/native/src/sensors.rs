//! Runtime sensor I/O; excluded from the portable display library.
use crate::pisugar;
use axon_native::state::AppState;
use std::process::Command;
use std::time::{Duration, Instant};

pub struct Sensors {
    updated: Option<Instant>,
}
impl Sensors {
    pub fn new() -> Self {
        Self { updated: None }
    }
    pub fn refresh_if_stale(&mut self, state: &mut AppState) {
        if self
            .updated
            .is_some_and(|at| at.elapsed() < Duration::from_secs(5))
        {
            return;
        }
        state.wifi_ssid = wifi_ssid();
        match pisugar::get_status() {
            Ok(b) => {
                state.battery_pct = b.percent;
                state.battery_charging = b.charging;
            }
            Err(_) => {
                state.battery_pct = 0;
                state.battery_charging = false;
            }
        }
        self.updated = Some(Instant::now());
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
