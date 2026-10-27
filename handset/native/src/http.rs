//! Handset-local control API. The bind address defaults to loopback.
//!
//!   GET  /event/<single|double|long>  (PiSugar's button_shell hits this -
//!                                      GET+path because button_shell
//!                                      strips quote characters, so a JSON
//!                                      POST body doesn't survive it)
//!   POST /event  {"button": "single"|"double"|"long"}
//!   GET  /health

use std::sync::{Arc, Mutex};

use serde_json::json;
use tiny_http::{Method, Response, Server};

use crate::font::Fonts;
use crate::Daemon;

pub fn serve(shared: Arc<Mutex<Daemon>>, fonts: Arc<Fonts>, listen: std::net::SocketAddr) {
    let server = Server::http(listen).expect("failed to bind :8500");
    for mut request in server.incoming_requests() {
        let method = request.method().clone();
        let url = request.url().to_string();

        let (status, body) = match (&method, url.as_str()) {
            (Method::Get, "/health") => {
                let daemon = shared.lock().unwrap();
                (200, json!({"ok": true, "screen": daemon.state.screen}))
            }
            (Method::Get, path) if path.starts_with("/event/") => {
                let mode = &path["/event/".len()..];
                handle_event(&shared, &fonts, mode)
            }
            (Method::Post, "/event") => {
                let body = read_json_body(&mut request);
                let mode = body.get("button").and_then(|v| v.as_str()).unwrap_or("");
                handle_event(&shared, &fonts, mode)
            }
            _ => (404, json!({"error": "not found"})),
        };

        let response = Response::from_string(body.to_string())
            .with_status_code(status)
            .with_header(
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                    .unwrap(),
            );
        let _ = request.respond(response);
    }
}

fn handle_event(
    shared: &Arc<Mutex<Daemon>>,
    fonts: &Fonts,
    mode: &str,
) -> (u16, serde_json::Value) {
    if !matches!(mode, "single" | "double" | "long") {
        return (400, json!({"error": "bad button"}));
    }
    let mut daemon = shared.lock().unwrap();
    daemon.handle_button(mode, fonts);
    (200, json!({"ok": true, "screen": daemon.state.screen}))
}

fn read_json_body(request: &mut tiny_http::Request) -> serde_json::Value {
    let mut content = String::new();
    let _ = request.as_reader().read_to_string(&mut content);
    serde_json::from_str(&content).unwrap_or(json!({}))
}
