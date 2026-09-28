//! Port of software/display-service/pisugar_client.py - pisugar-server's
//! line-based plain-text TCP protocol on 127.0.0.1:8423.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

pub struct BatteryStatus {
    pub percent: i32,
    pub charging: bool,
}

pub fn get_status() -> std::io::Result<BatteryStatus> {
    let mut stream = TcpStream::connect(("127.0.0.1", 8423))?;
    // pisugar-server answers both commands in a single write and never
    // closes the connection on its own, so looping reads until EOF/timeout
    // (as the old Python client also did) always pays the full timeout
    // waiting for a close that never comes. One read is enough - the whole
    // reply arrives in a single loopback packet.
    stream.set_read_timeout(Some(Duration::from_millis(250)))?;
    stream.set_write_timeout(Some(Duration::from_millis(250)))?;
    stream.write_all(b"get battery\nget battery_charging\n")?;

    let mut buf = [0u8; 4096];
    let n = stream.read(&mut buf)?;
    let raw = String::from_utf8_lossy(&buf[..n]).into_owned();

    let mut percent = 0i32;
    let mut charging = false;
    for line in raw.lines() {
        if let Some((key, val)) = line.split_once(':') {
            let (key, val) = (key.trim(), val.trim());
            match key {
                "battery" => percent = val.parse::<f64>().unwrap_or(0.0) as i32,
                "battery_charging" => charging = val == "true",
                _ => {}
            }
        }
    }
    Ok(BatteryStatus { percent, charging })
}
