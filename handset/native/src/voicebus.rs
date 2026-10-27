//! Optional Gabbie JSON-lines adapter. Voice state mapping lives in the portable
//! library; deployment selects the endpoint. Button feedback is local, ambient
//! voice events are ignored, and the animation timeout handles lost completion.

use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::font::Fonts;
use crate::Daemon;

use axon_native::voice::{transition, VoiceTransition};

pub fn spawn(shared: Arc<Mutex<Daemon>>, fonts: Arc<Fonts>, endpoint: std::net::SocketAddr) {
    thread::spawn(move || loop {
        if let Ok(stream) =
            TcpStream::connect_timeout(&endpoint, std::time::Duration::from_millis(500))
        {
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

    let (screen, voice_state): (&str, Option<&str>) = match transition(event) {
        Some(VoiceTransition::Speaking) => ("voice", Some("speaking")),
        Some(VoiceTransition::Idle) => ("idle", None),
        None => return,
    };

    let mut daemon = shared.lock().unwrap();
    daemon.state.screen = screen.to_string();
    daemon.state.voice_state = voice_state.map(String::from);
    daemon.state.voice_last_activity = Some(Instant::now());
    daemon.render(fonts);
}
