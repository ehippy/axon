//! Pure Gabbie adapter: ambient events must not take over the display.
#[derive(Debug, PartialEq)]
pub enum VoiceTransition {
    Speaking,
    Idle,
}
pub fn transition(event: &str) -> Option<VoiceTransition> {
    match event {
        "thinking" | "speaking" | "confirming" => Some(VoiceTransition::Speaking),
        "done_speaking" => Some(VoiceTransition::Idle),
        _ => None,
    }
}
