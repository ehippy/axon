//! MQTT subscriber - the display
//! subscribes to server views and integration status, reconnecting after outages.

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rumqttc::{Client, Event, MqttOptions, Packet, QoS};

use crate::font::Fonts;
use crate::Daemon;

use axon_native::config::Config;
const TOPIC: &str = "axon/integrations/+/status";

fn options(config: &Config, role: &str) -> MqttOptions {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let id = format!("{}-{role}-{}-{nonce}", config.device_id, std::process::id());
    MqttOptions::new(id, config.broker_host.clone(), config.broker_port)
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

pub fn spawn(shared: Arc<Mutex<Daemon>>, fonts: Arc<Fonts>, config: Config) {
    thread::spawn(move || loop {
        let mut options = options(&config, "display");
        options.set_keep_alive(Duration::from_secs(60));
        let (client, mut connection) = Client::new(options, 10);
        if client.subscribe(TOPIC, QoS::AtMostOnce).is_err()
            || client
                .subscribe("axon/activity/view", QoS::AtLeastOnce)
                .is_err()
        {
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
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(payload) else {
        return;
    };
    let mut daemon = shared.lock().unwrap();
    let applied = if topic == "axon/activity/view" {
        axon_native::model::apply_view(&mut daemon.state, &value)
    } else {
        let parts: Vec<_> = topic.split('/').collect();
        if parts.len() != 4
            || parts[0] != "axon"
            || parts[1] != "integrations"
            || parts[3] != "status"
        {
            return;
        }
        axon_native::model::apply_integration(&mut daemon.state, parts[2], &value)
    };
    if applied.is_ok() {
        daemon.state.prune_agents(now());
        if daemon.state.screen == "idle" {
            daemon.render(fonts);
        }
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
    topic: String,
}

impl PttPublisher {
    pub fn connect(config: Config) -> Self {
        let topic = config.ptt_topic();
        let (initial_client, initial_connection) = new_publisher_client(&config);
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
                let (new_client, new_connection) = new_publisher_client(&config);
                *client_for_thread.lock().unwrap() = new_client;
                connection = new_connection;
            }
        });
        PttPublisher { client, topic }
    }

    pub fn publish(&self, event: &'static str) {
        eprintln!("ptt: {event}");
        if let Err(e) = self.client.lock().unwrap().try_publish(
            self.topic.clone(),
            QoS::AtLeastOnce,
            false,
            event.as_bytes(),
        ) {
            eprintln!("ptt: publish failed: {e}");
        }
    }
}

fn new_publisher_client(config: &Config) -> (Client, rumqttc::Connection) {
    let mut options = options(config, "ptt");
    options.set_keep_alive(Duration::from_secs(30));
    Client::new(options, 10)
}
