//! Validated deployment configuration, independent of hardware.
use std::net::SocketAddr;

#[derive(Clone, Debug)]
pub struct Config {
    pub broker_host: String,
    pub broker_port: u16,
    pub device_id: String,
    pub http_listen: SocketAddr,
    pub voice_enabled: bool,
    pub voice_endpoint: SocketAddr,
}
impl Config {
    pub fn from_env() -> Result<Self, String> {
        Self::from_lookup(|key| std::env::var(key).ok())
    }
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Self, String> {
        let value = |key, default: &str| get(key).unwrap_or_else(|| default.to_string());
        let broker_host = value("AXON_BROKER", "localhost");
        if broker_host.trim().is_empty() {
            return Err("AXON_BROKER must not be empty".into());
        }
        let broker_port = value("AXON_PORT", "1883")
            .parse::<u16>()
            .map_err(|_| "Invalid AXON_PORT")?;
        if broker_port == 0 {
            return Err("AXON_PORT must be nonzero".into());
        }
        let device_id = value("AXON_DEVICE_ID", "handset");
        if device_id.is_empty()
            || device_id.len() > 80
            || !device_id
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
        {
            return Err(
                "AXON_DEVICE_ID must contain 1..80 letters, digits, dots, underscores or hyphens"
                    .into(),
            );
        }
        let http_listen = value("AXON_HTTP_LISTEN", "127.0.0.1:8500")
            .parse()
            .map_err(|_| "Invalid AXON_HTTP_LISTEN")?;
        let voice_enabled = match value("AXON_VOICE_ENABLED", "false").as_str() {
            "true" => true,
            "false" => false,
            _ => return Err("AXON_VOICE_ENABLED must be true or false".into()),
        };
        let voice_endpoint = value("AXON_VOICE_ENDPOINT", "127.0.0.1:8765")
            .parse()
            .map_err(|_| "Invalid AXON_VOICE_ENDPOINT")?;
        Ok(Self {
            broker_host,
            broker_port,
            device_id,
            http_listen,
            voice_enabled,
            voice_endpoint,
        })
    }
    pub fn ptt_topic(&self) -> String {
        format!("axon/handsets/{}/ptt", self.device_id)
    }
}
