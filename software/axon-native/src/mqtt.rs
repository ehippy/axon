//! MQTT subscriber - the display
//! subscribes to axon/agents/+/status and re-renders on updates. Port of
//! daemon.py's paho-mqtt wiring onto rumqttc's blocking client, which
//! reconnects with backoff internally the same way paho's loop_start() did.

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rumqttc::{Client, Event, MqttOptions, Packet, QoS};

use crate::font::Fonts;
use crate::hud::AgentInfo;
use crate::Daemon;

fn broker_host() -> String {
    std::env::var("AXON_BROKER").unwrap_or_else(|_| "localhost".to_string())
}
const PORT: u16 = 1883;
const TOPIC: &str = "axon/agents/+/status";
const PTT_TOPIC: &str = "axon/ptt";

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

pub fn spawn(shared: Arc<Mutex<Daemon>>, fonts: Arc<Fonts>) {
    thread::spawn(move || loop {
        let mut options = MqttOptions::new("axon-native", broker_host(), PORT);
        options.set_keep_alive(Duration::from_secs(60));
        let (client, mut connection) = Client::new(options, 10);
        if client.subscribe(TOPIC, QoS::AtMostOnce).is_err() || client.subscribe("axon/activity/view", QoS::AtLeastOnce).is_err() {
            thread::sleep(Duration::from_secs(3));
            continue;
        }
        for notification in connection.iter() {
            match notification {
                Ok(Event::Incoming(Packet::Publish(p))) => {
                    handle_publish(&shared, &fonts, &p.topic, &p.payload);
                }
                Ok(Event::Incoming(Packet::ConnAck(_))) => eprintln!("mqtt connected"),
                Ok(_) => {}
                Err(e) => {
                    eprintln!("mqtt error: {e}");
                    break; // drop out to the outer loop and reconnect
                }
            }
        }
        thread::sleep(Duration::from_secs(3));
    });
}

fn handle_publish(shared: &Arc<Mutex<Daemon>>, fonts: &Fonts, topic: &str, payload: &[u8]) {
    if topic == "axon/activity/view" {
        let Ok(json) = serde_json::from_slice::<serde_json::Value>(payload) else { return };
        let Some(sessions) = json["sessions"].as_array() else { return };
        let mut daemon = shared.lock().unwrap();
        daemon.state.agents.retain(|key, _| !key.starts_with('@'));
        daemon.state.activity_deadlines.clear();
        for session in sessions {
            if let Some(row) = axon_native::activity::activity_row(session) {
                let key = row.key;
                let source = row.source;
                let info = AgentInfo {state: row.state, task: row.task, ts: row.updated_at};
                daemon.state.agents.remove(&source);
                if matches!(session["state"].as_str(), Some("working" | "waiting" | "blocked")) {
                    let expires = session["updated_at"].as_f64().unwrap_or(0.0)
                        + session["lease_seconds"].as_f64().unwrap_or(900.0);
                    daemon.state.activity_deadlines.insert(key.clone(), expires);
                }
                daemon.state.agents.insert(key, info);
            }
        }
        if daemon.state.screen == "idle" { daemon.render(fonts); }
        return;
    }
    // topic shape: axon/agents/<source>/status
    let parts: Vec<&str> = topic.split('/').collect();
    if parts.len() != 4 {
        return;
    }
    let source = parts[2].to_string();
    let json: serde_json::Value = match serde_json::from_slice(payload) {
        Ok(v) => v,
        Err(_) => return,
    };
    let state = json.get("state").and_then(|v| v.as_str()).unwrap_or("idle").to_string();
    let task = json.get("task").and_then(|v| v.as_str()).unwrap_or("").to_string();
    // Publisher-side timestamp (epoch seconds; ms tolerated) so an agent's
    // age survives display restarts: retained messages otherwise look
    // brand-new on every boot and never age out. Arrival time is the
    // fallback for pre-contract publishers (gabbie) and LWT wills.
    let ts = json
        .get("ts")
        .and_then(|v| v.as_f64())
        .filter(|t| *t > 1e9)
        .map(|t| if t > 1e11 { t / 1000.0 } else { t })
        .unwrap_or_else(now);

    let mut daemon = shared.lock().unwrap();
    daemon.state.prune_agents(now());
    // Rich per-session rows replace the corresponding machine aggregate.
    if daemon.state.agents.keys().any(|key| key.starts_with(&format!("@{source}/"))) { return; }
    daemon.state.agents.insert(source, AgentInfo { state, task, ts });
    if daemon.state.screen == "idle" {
        daemon.render(fonts);
    }
}

/// Publishes the Whisplay button's press/release edge to gabbie, which
/// uses it as a walkie-talkie push-to-talk trigger. Payload is the literal
/// "start"/"stop" bytes, not JSON - a single edge-triggered signal with
/// nothing else to carry.
///
/// Uses ONE persistent connection for the process's lifetime rather than a
/// fresh connection per event - a fresh-connection-per-publish design
/// (the original version of this) let a quick tap's "start" and "stop"
/// race each other on independent TCP handshakes, with no guarantee which
/// one actually reached the broker first. When "stop" won that race,
/// gabbie's ptt.active got stuck permanently true: stuck listening, and
/// every subsequent reply immediately tripped the barge-in interrupt check
/// on its first token. A single connection makes publish order match call
/// order, which a fresh connection per call never guaranteed.
#[derive(Clone)]
pub struct PttPublisher {
    client: Arc<Mutex<Client>>,
}

impl PttPublisher {
    pub fn connect() -> Self {
        let (initial_client, initial_connection) = new_publisher_client();
        let client = Arc::new(Mutex::new(initial_client));
        let client_for_thread = Arc::clone(&client);
        thread::spawn(move || {
            let mut connection = initial_connection;
            loop {
                for notification in connection.iter() {
                    match notification {
                        Ok(Event::Incoming(Packet::ConnAck(_))) => eprintln!("ptt: mqtt connected"),
                        Err(e) => {
                            eprintln!("ptt: mqtt error: {e}");
                            break;
                        }
                        _ => {}
                    }
                }
                thread::sleep(Duration::from_secs(3));
                let (new_client, new_connection) = new_publisher_client();
                *client_for_thread.lock().unwrap() = new_client;
                connection = new_connection;
            }
        });
        PttPublisher { client }
    }

    pub fn publish(&self, event: &'static str) {
        eprintln!("ptt: {event}");
        if let Err(e) = self.client.lock().unwrap().publish(PTT_TOPIC, QoS::AtLeastOnce, false, event.as_bytes()) {
            eprintln!("ptt: publish failed: {e}");
        }
    }
}

fn new_publisher_client() -> (Client, rumqttc::Connection) {
    let mut options = MqttOptions::new("axon-native-ptt", broker_host(), PORT);
    options.set_keep_alive(Duration::from_secs(30));
    Client::new(options, 10)
}
