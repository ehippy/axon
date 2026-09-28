//! Axon display daemon, native rewrite - owns the Whisplay color LCD + RGB
//! LED directly (no Python/PIL in the loop) and serves the same tiny local
//! HTTP API for button gestures and agent status pushes. See
//! docs/PROTOTYPE-PWNAGOTCHI.md for why this exists: the old pipeline's
//! RGB565 packing (pure-Python, no numpy) ate ~68% of every frame; this
//! rewrite removes that bottleneck entirely instead of just patching it.

mod animate;
mod canvas;
mod font;
mod http;
mod hud;
mod mqtt;
mod panel;
mod pisugar;
mod screens;
mod softpwm;
mod state;
mod voicebus;

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use canvas::Canvas;
use font::Fonts;
use panel::Panel;
use state::AppState;

pub struct Daemon {
    pub state: AppState,
    panel: Panel,
    render_count: u64,
}

impl Daemon {
    fn render(&mut self, fonts: &Fonts) {
        let t0 = std::time::Instant::now();
        self.state.refresh_sensors_if_stale();
        let t1 = std::time::Instant::now();
        let mut canvas = Canvas::new(canvas::W, canvas::H, hud::BG);
        screens::render(&self.state.screen, &mut canvas, fonts, &self.state);
        let t2 = std::time::Instant::now();
        let rotated = canvas.rotate_to_panel();
        let t3 = std::time::Instant::now();
        let buf = rotated.to_rgb565();
        let t4 = std::time::Instant::now();
        self.panel.draw_image(0, 0, panel::LCD_WIDTH, panel::LCD_HEIGHT, &buf);
        let t5 = std::time::Instant::now();
        if self.state.screen == "idle" || self.state.screen == "voice" {
            self.update_led();
        }
        let t6 = std::time::Instant::now();
        self.render_count += 1;
        if self.render_count % 100 == 0 {
            eprintln!(
                "render #{} sensors={:?} compose={:?} rotate={:?} pack={:?} spi={:?} led={:?} total={:?}",
                self.render_count,
                t1 - t0,
                t2 - t1,
                t3 - t2,
                t4 - t3,
                t5 - t4,
                t6 - t5,
                t6 - t0
            );
        }
    }

    fn update_led(&mut self) {
        // Attention flash (see animate.rs) takes over the LED entirely for
        // its short window - a fast blink, not a steady color, so it reads
        // as a notification rather than just a different status color.
        if let Some(until) = self.state.led_flash_until {
            let now = std::time::Instant::now();
            if now < until {
                let remaining = (until - now).as_secs_f32();
                let on = (remaining * 6.0) as u32 % 2 == 0; // ~3 blinks/sec
                let (r, g, b) = if on { self.state.led_flash_color } else { (0, 0, 0) };
                self.panel.set_rgb(r, g, b);
                return;
            }
            self.state.led_flash_until = None;
        }

        let bucket = hud::aggregate_status(&self.state.agents);
        let color = match bucket {
            None => hud::STATUS_EMPTY,
            Some(b) => hud::status_color(b),
        };
        // 0.35 ceiling keeps the LED from being glare-y at full saturation
        // (see hud.rs); dot_brightness rides on top of that same ceiling so
        // the physical LED breathes/pulses in lockstep with the on-screen dot.
        let (r, g, b) = hud::dim(color, 0.35 * self.state.dot_brightness as f32);
        self.panel.set_rgb(r, g, b);
    }

    fn handle_button(&mut self, mode: &str, fonts: &Fonts) {
        match mode {
            "single" => {
                self.state.screen = screens::HOME.to_string();
                self.render(fonts);
            }
            "double" => {
                // No page cycle (single screen) - just refresh, matching
                // daemon.py's CYCLE=[] no-op path.
                self.render(fonts);
            }
            "long" => {
                self.state.screen = "shutdown".to_string();
                self.panel.set_rgb(255, 80, 0);
                self.render(fonts);
                thread::sleep(Duration::from_secs(1));
                let _ = std::process::Command::new("/sbin/shutdown").arg("-h").arg("now").status();
            }
            _ => {}
        }
    }

    fn handle_status(&mut self, _agent_state: String, _agent_task: String, fonts: &Fonts) {
        // Kept for API parity with daemon.py's POST /status - screens.rs
        // doesn't read these fields (the multi-source `agents` map driven
        // by MQTT is what actually renders), so this is a no-visible-effect
        // refresh today, same as in the Python daemon.
        if self.state.screen == "idle" {
            self.render(fonts);
        }
    }
}

fn main() {
    let panel = Panel::new().expect("failed to init Whisplay panel");
    let mut panel = panel;
    panel.set_backlight(70);

    let fonts = Arc::new(Fonts::load());
    let state = AppState::new();
    let daemon = Arc::new(Mutex::new(Daemon { state, panel, render_count: 0 }));

    {
        let mut d = daemon.lock().unwrap();
        let mut canvas = Canvas::new(canvas::W, canvas::H, hud::BG);
        screens::render_boot(&mut canvas, &fonts, &d.state);
        let buf = canvas.rotate_to_panel().to_rgb565();
        d.panel.draw_image(0, 0, panel::LCD_WIDTH, panel::LCD_HEIGHT, &buf);
        println!("boot screen shown");
    }
    thread::sleep(Duration::from_millis(1200));
    {
        let mut d = daemon.lock().unwrap();
        d.render(&fonts);
    }

    {
        let shutdown_daemon = Arc::clone(&daemon);
        ctrlc::set_handler(move || {
            let mut d = shutdown_daemon.lock().unwrap();
            d.panel.set_rgb(0, 0, 0);
            std::process::exit(0);
        })
        .expect("failed to install signal handler");
    }

    {
        // Whisplay's own physical button, previously unused (PiSugar's
        // button handles screen refresh/shutdown) - now a walkie-talkie
        // push-to-talk trigger for gabbie, bridged over one persistent
        // MQTT connection (see PttPublisher's own doc comment for why it
        // has to be persistent, not one-shot per press). The green half of
        // the voice splash is also set directly here, locally, on press -
        // not round-tripped through gabbie's EventBus (see voicebus.rs's
        // module doc for why that matters).
        let ptt_publisher = mqtt::PttPublisher::connect();
        let button_daemon = Arc::clone(&daemon);
        let button_fonts = Arc::clone(&fonts);
        let mut d = daemon.lock().unwrap();
        if let Err(e) = d.panel.on_button(move |pressed| {
            ptt_publisher.publish(if pressed { "start" } else { "stop" });
            let mut bd = button_daemon.lock().unwrap();
            if pressed {
                bd.state.screen = "voice".to_string();
                bd.state.voice_state = Some("listening".to_string());
            }
            bd.state.voice_last_activity = Some(Instant::now());
            bd.render(&button_fonts);
        }) {
            eprintln!("failed to register button handler: {e}");
        }
    }

    mqtt::spawn(Arc::clone(&daemon), Arc::clone(&fonts));
    animate::spawn(Arc::clone(&daemon), Arc::clone(&fonts));
    voicebus::spawn(Arc::clone(&daemon), Arc::clone(&fonts));

    println!("listening on http://127.0.0.1:8500");
    http::serve(daemon, fonts);
}
