//! Client for gabbie's EventBus (Projects/gabbie/eventbus.py) - a local
//! TCP JSON-lines broadcaster on 127.0.0.1:8765. Gabbie now runs directly
//! on axon (see docs/PROTOTYPE-PWNAGOTCHI.md), so this is a same-machine
//! connection, not a network integration - no MQTT needed for this
//! direction, unlike the button->gabbie path (see mqtt.rs's PttPublisher).
//!
//! Only drives the *blue* ("speaking") half of the voice splash - see
//! main.rs's button callback for the green ("listening") half, which is
//! set directly and locally on button press rather than round-tripping
//! through here. Deliberately does NOT react to gabbie's "listening" or
//! "transcribing" events: gabbie publishes those continuously for her own
//! ambient VAD monitoring too, not just real engagement (she's always
//! listening for the wake word), so mapping them to the splash meant it
//! was permanently up any time there was room noise. "thinking",
//! "speaking", and "confirming" only ever fire after a genuinely accepted
//! utterance (button hold or real wake word), so those are the reliable
//! signal. "done_speaking" reverts to idle; animate.rs's timeout is the
//! backstop for any turn that doesn't cleanly reach that event.

use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::font::Fonts;
use crate::Daemon;

const ADDR: &str = "127.0.0.1:8765";

pub fn spawn(shared: Arc<Mutex<Daemon>>, fonts: Arc<Fonts>) {
    thread::spawn(move || loop {
        if let Ok(stream) = TcpStream::connect(ADDR) {
            let reader = BufReader::new(stream);
            for line in reader.lines() {
                match line {
                    Ok(l) => handle_event(&shared, &fonts, &l),
                    Err(_) => break,
                }
            }
        }
        thread::sleep(Duration::from_secs(2));
    });
}

fn handle_event(shared: &Arc<Mutex<Daemon>>, fonts: &Fonts, line: &str) {
    let json: serde_json::Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(_) => return,
    };
    let event = match json.get("event").and_then(|v| v.as_str()) {
        Some(e) => e,
        None => return,
    };

    let (screen, voice_state): (&str, Option<&str>) = match event {
        "thinking" | "speaking" | "confirming" => ("voice", Some("speaking")),
        "done_speaking" => ("idle", None),
        _ => return, // listening/transcribing/heard/awake/etc. - see module doc
    };

    let mut daemon = shared.lock().unwrap();
    daemon.state.screen = screen.to_string();
    daemon.state.voice_state = voice_state.map(String::from);
    daemon.state.voice_last_activity = Some(Instant::now());
    daemon.render(fonts);
}
