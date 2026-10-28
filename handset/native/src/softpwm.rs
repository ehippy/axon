//! Plain sleep-based software PWM - deliberately not rppal's built-in
//! OutputPin::set_pwm_frequency, which busy-waits part of every cycle to
//! hit precise timing (see its own source comments: "Reserve some time for
//! busy waiting"). That costs real, sustained CPU per active channel -
//! measured at ~50% of one core just idling with the backlight + RGB LED
//! channels running, on a device meant to run off a battery. The original
//! Python driver's SoftPWM class never had this problem because it's just
//! sleep-based toggling with no precision busy-wait; this ports that same
//! approach instead of rppal's.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use rppal::gpio::OutputPin;

pub struct SoftPwm {
    duty_millis: Arc<AtomicU32>, // duty cycle * 1_000_000, 0..=1_000_000
    stop: Arc<AtomicBool>,
}

impl SoftPwm {
    /// `frequency_hz` fixed for the channel's lifetime; `duty` (0.0-1.0) is
    /// the fraction of each period the pin is held HIGH, updatable live.
    pub fn start(mut pin: OutputPin, frequency_hz: f64, duty: f64) -> Self {
        let duty_millis = Arc::new(AtomicU32::new((duty.clamp(0.0, 1.0) * 1_000_000.0) as u32));
        let stop = Arc::new(AtomicBool::new(false));
        let period = Duration::from_secs_f64(1.0 / frequency_hz);
        let duty_thread = Arc::clone(&duty_millis);
        let stop_thread = Arc::clone(&stop);

        thread::spawn(move || {
            while !stop_thread.load(Ordering::Relaxed) {
                let dc = duty_thread.load(Ordering::Relaxed) as f64 / 1_000_000.0;
                if dc <= 0.0 {
                    pin.set_low();
                    thread::sleep(period);
                } else if dc >= 1.0 {
                    pin.set_high();
                    thread::sleep(period);
                } else {
                    let on = period.mul_f64(dc);
                    pin.set_high();
                    thread::sleep(on);
                    pin.set_low();
                    thread::sleep(period - on);
                }
            }
            pin.set_low();
        });

        SoftPwm { duty_millis, stop }
    }

    pub fn set_duty(&self, duty: f64) {
        self.duty_millis.store(
            (duty.clamp(0.0, 1.0) * 1_000_000.0) as u32,
            Ordering::Relaxed,
        );
    }
}

impl Drop for SoftPwm {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}
