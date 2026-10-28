use axon_native::{
    config::Config,
    model,
    state::AppState,
    voice::{transition, VoiceTransition},
};
use serde_json::{json, Value};

#[test]
fn configuration_requires_explicit_voice_and_valid_addresses() {
    let config = Config::from_lookup(|_| None).unwrap();
    assert!(!config.voice_enabled);
    assert_eq!(config.http_listen.to_string(), "127.0.0.1:8500");
    assert_eq!(config.ptt_topic(), "axon/handsets/handset/ptt");
    assert!(Config::from_lookup(|k| (k == "AXON_PORT").then(|| "0".into())).is_err());
    assert!(Config::from_lookup(|k| (k == "AXON_DEVICE_ID").then(|| "bad/topic".into())).is_err());
    assert!(Config::from_lookup(|k| (k == "AXON_VOICE_ENABLED").then(|| "maybe".into())).is_err());
    let custom = Config::from_lookup(|k| match k {
        "AXON_VOICE_ENABLED" => Some("true".into()),
        "AXON_DEVICE_ID" => Some("device-a".into()),
        "AXON_PORT" => Some("2883".into()),
        _ => None,
    })
    .unwrap();
    assert!(custom.voice_enabled);
    assert_eq!(custom.broker_port, 2883);
    assert_eq!(custom.ptt_topic(), "axon/handsets/device-a/ptt");
}

#[test]
fn ambient_voice_events_do_not_take_over_display() {
    for event in ["listening", "transcribing", "heard", "awake", "unknown"] {
        assert_eq!(transition(event), None);
    }
    assert_eq!(transition("thinking"), Some(VoiceTransition::Speaking));
    assert_eq!(transition("done_speaking"), Some(VoiceTransition::Idle));
}

#[test]
fn server_and_integration_fixtures_have_separate_lifetimes() {
    let view: Value =
        serde_json::from_str(include_str!("../../../protocol/fixtures/view.json")).unwrap();
    let integration: Value =
        serde_json::from_str(include_str!("../../../protocol/fixtures/integration.json")).unwrap();
    let mut state = AppState::new("test-device".into());
    model::apply_integration(&mut state, "mirror", &integration).unwrap();
    model::apply_view(&mut state, &view).unwrap();
    assert_eq!(state.agents.len(), 2);
    let row = state.agents.get("@codex-example/example-session").unwrap();
    assert!(row.task.contains("Status verified"));
    assert_eq!(row.state, "idle");
    model::apply_view(&mut state, &json!({"version":1,"sessions":[]})).unwrap();
    assert_eq!(state.agents.len(), 1);
    assert!(state.agents.contains_key("integration:mirror"));
    assert!(model::apply_view(&mut state, &json!({"version":2,"sessions":[]})).is_err());
    assert!(model::apply_integration(&mut state, "mirror", &json!({"state":"running"})).is_err());
    state.prune_agents(1700000000.0 + 13.0 * 3600.0);
    assert!(state.agents.is_empty());
}

#[test]
fn rendering_and_rotation_do_not_need_hardware() {
    let mut canvas = axon_native::canvas::Canvas::new(280, 240, (0, 0, 0));
    let fonts = axon_native::font::Fonts::load();
    let state = AppState::new("test-device".into());
    axon_native::screens::render("idle", &mut canvas, &fonts, &state);
    let panel = canvas.rotate_to_panel();
    assert_eq!((panel.width, panel.height), (240, 280));
    assert_eq!(panel.to_rgb565().len(), 240 * 280 * 2);
}
