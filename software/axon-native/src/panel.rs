//! Direct port of ~/Whisplay/runtime/whisplay.py's WhisplayBoard, trimmed to
//! the one platform axon actually runs on (Raspberry Pi, gpiochip0, SPI0/CS0
//! @ 100MHz). Pin numbers below are BCM (rppal's native numbering); the
//! comment gives the physical header pin they came from in the Python
//! driver's BOARD-mode map, so the two stay auditable against each other.

use std::thread;
use std::time::Duration;

use rppal::gpio::{Gpio, InputPin, OutputPin, Trigger};
use rppal::spi::{Bus, Mode, SlaveSelect, Spi};

use crate::softpwm::SoftPwm;

pub const LCD_WIDTH: u32 = 240;
pub const LCD_HEIGHT: u32 = 280;

// physical pin -> BCM, from whisplay.py's _RPI_BOARD_TO_BCM
const DC_BCM: u8 = 27; // board 13
const RST_BCM: u8 = 4; // board 7
const BACKLIGHT_BCM: u8 = 22; // board 15 ("LED_PIN" in the Python driver)
const RED_BCM: u8 = 25; // board 22
const GREEN_BCM: u8 = 24; // board 18
const BLUE_BCM: u8 = 23; // board 16
const BUTTON_BCM: u8 = 17; // board 11

const SPI_SPEED_HZ: u32 = 100_000_000;

pub struct Panel {
    spi: Spi,
    dc: OutputPin,
    rst: OutputPin,
    backlight: SoftPwm,
    red: SoftPwm,
    green: SoftPwm,
    blue: SoftPwm,
    // Kept alive so the interrupt registration on it isn't dropped;
    // button state itself is read through rppal's InputPin directly by
    // whoever registered the callback.
    _button: InputPin,
}

impl Panel {
    pub fn new() -> rppal::gpio::Result<Self> {
        let gpio = Gpio::new()?;

        let mut dc = gpio.get(DC_BCM)?.into_output();
        let mut rst = gpio.get(RST_BCM)?.into_output();
        let backlight_pin = gpio.get(BACKLIGHT_BCM)?.into_output();
        let red_pin = gpio.get(RED_BCM)?.into_output();
        let green_pin = gpio.get(GREEN_BCM)?.into_output();
        let blue_pin = gpio.get(BLUE_BCM)?.into_output();
        // External pull-down already on the board (per whisplay.py comment)
        // - no internal bias needed. HIGH = pressed.
        let button = gpio.get(BUTTON_BCM)?.into_input();

        dc.set_low();
        rst.set_high();

        let spi = Spi::new(Bus::Spi0, SlaveSelect::Ss0, SPI_SPEED_HZ, Mode::Mode0)
            .map_err(|e| rppal::gpio::Error::Io(std::io::Error::new(std::io::ErrorKind::Other, e)))?;

        // All four channels are active-low (see set_rgb/set_backlight) -
        // duty=1.0 (pin held HIGH the whole period) is "off", matching the
        // backlight/LEDs being dark before the boot screen renders.
        let backlight = SoftPwm::start(backlight_pin, 1000.0, 1.0);
        let red = SoftPwm::start(red_pin, 100.0, 1.0);
        let green = SoftPwm::start(green_pin, 100.0, 1.0);
        let blue = SoftPwm::start(blue_pin, 100.0, 1.0);

        let mut panel = Panel {
            spi,
            dc,
            rst,
            backlight,
            red,
            green,
            blue,
            _button: button,
        };

        panel.reset_lcd();
        panel.init_display();
        panel.fill_screen(0x0000);
        Ok(panel)
    }

    /// Register press/release callbacks, mirroring WhisplayBoard.on_button_press/release
    /// (edge-triggered instead of the Python driver's 5ms poll loop - same
    /// observable behavior, no busy-wait thread).
    ///
    /// Debounced at 150ms - measured real switch bounce on this button
    /// produces a dozen-plus rapid start/stop toggles for a single
    /// deliberate press (confirmed via MQTT logging: three presses showed
    /// up as 30+ edges within about a second). Without this, gabbie's
    /// ptt.active ends up in whatever state the last bounce happened to
    /// leave it in rather than reflecting the actual press.
    pub fn on_button<F>(&mut self, mut callback: F) -> rppal::gpio::Result<()>
    where
        F: FnMut(bool) + Send + 'static,
    {
        self._button
            .set_async_interrupt(Trigger::Both, Some(Duration::from_millis(150)), move |event| {
                callback(event.trigger == Trigger::RisingEdge);
            })
    }

    pub fn button_pressed(&self) -> bool {
        self._button.is_high()
    }

    fn reset_lcd(&mut self) {
        self.rst.set_high();
        thread::sleep(Duration::from_millis(100));
        self.rst.set_low();
        thread::sleep(Duration::from_millis(100));
        self.rst.set_high();
        thread::sleep(Duration::from_millis(120));
    }

    fn send_command(&mut self, cmd: u8, args: &[u8]) {
        self.dc.set_low();
        let _ = self.spi.write(&[cmd]);
        if !args.is_empty() {
            self.dc.set_high();
            let _ = self.spi.write(args);
        }
    }

    fn send_data(&mut self, data: &[u8]) {
        self.dc.set_high();
        // The kernel spidev interface caps a single transfer (default
        // bufsiz 4096 bytes) - unlike Python's spidev.writebytes2(), which
        // chunks internally in C, rppal's Spi::write() does not, and a
        // full 134400-byte frame in one call fails outright (EMSGSIZE).
        const MAX_CHUNK: usize = 4096;
        for chunk in data.chunks(MAX_CHUNK) {
            match self.spi.write(chunk) {
                Ok(n) if n != chunk.len() => {
                    eprintln!("spi short write: wrote {n} of {} bytes", chunk.len());
                }
                Err(e) => eprintln!("spi write error: {e}"),
                _ => {}
            }
        }
    }

    fn init_display(&mut self) {
        self.send_command(0x11, &[]); // sleep out
        thread::sleep(Duration::from_millis(120));
        self.send_command(0x36, &[0xC0]); // MADCTL - matches USE_HORIZONTAL=1 in whisplay.py
        self.send_command(0x3A, &[0x05]); // 16bpp
        self.send_command(0xB2, &[0x0C, 0x0C, 0x00, 0x33, 0x33]);
        self.send_command(0xB7, &[0x35]);
        self.send_command(0xBB, &[0x32]);
        self.send_command(0xC2, &[0x01]);
        self.send_command(0xC3, &[0x15]);
        self.send_command(0xC4, &[0x20]);
        self.send_command(0xC6, &[0x0F]);
        self.send_command(0xD0, &[0xA4, 0xA1]);
        self.send_command(
            0xE0,
            &[
                0xD0, 0x08, 0x0E, 0x09, 0x09, 0x05, 0x31, 0x33, 0x48, 0x17, 0x14, 0x15, 0x31, 0x34,
            ],
        );
        self.send_command(
            0xE1,
            &[
                0xD0, 0x08, 0x0E, 0x09, 0x09, 0x15, 0x31, 0x33, 0x48, 0x17, 0x14, 0x15, 0x31, 0x34,
            ],
        );
        self.send_command(0x21, &[]); // inversion on
        self.send_command(0x29, &[]); // display on
    }

    /// Always takes the driver's use_horizontal=0 branch - the only one
    /// draw_image() in the Python daemon ever exercised, and the +20 y
    /// offset is load-bearing (maps logical rows onto the controller's
    /// actual addressable window on this panel).
    fn set_window(&mut self, x0: u32, y0: u32, x1: u32, y1: u32) {
        self.send_command(
            0x2A,
            &[
                (x0 >> 8) as u8,
                (x0 & 0xFF) as u8,
                (x1 >> 8) as u8,
                (x1 & 0xFF) as u8,
            ],
        );
        let (ry0, ry1) = (y0 + 20, y1 + 20);
        self.send_command(
            0x2B,
            &[
                (ry0 >> 8) as u8,
                (ry0 & 0xFF) as u8,
                (ry1 >> 8) as u8,
                (ry1 & 0xFF) as u8,
            ],
        );
        self.send_command(0x2C, &[]);
    }

    fn fill_screen(&mut self, color: u16) {
        self.set_window(0, 0, LCD_WIDTH - 1, LCD_HEIGHT - 1);
        let hi = (color >> 8) as u8;
        let lo = (color & 0xFF) as u8;
        let mut buf = Vec::with_capacity((LCD_WIDTH * LCD_HEIGHT * 2) as usize);
        for _ in 0..(LCD_WIDTH * LCD_HEIGHT) {
            buf.push(hi);
            buf.push(lo);
        }
        self.send_data(&buf);
    }

    /// pixel_data must already be big-endian RGB565, width*height*2 bytes.
    pub fn draw_image(&mut self, x: u32, y: u32, width: u32, height: u32, pixel_data: &[u8]) {
        assert!(x + width <= LCD_WIDTH && y + height <= LCD_HEIGHT, "image exceeds panel bounds");
        self.set_window(x, y, x + width - 1, y + height - 1);
        self.send_data(pixel_data);
    }

    /// duty is 0-255 (matches the RGB tuples used throughout the daemon).
    /// The channel wiring is active-low, same inversion whisplay.py applies.
    pub fn set_rgb(&mut self, r: u8, g: u8, b: u8) {
        let duty = |v: u8| 1.0 - (v as f64 / 255.0);
        self.red.set_duty(duty(r));
        self.green.set_duty(duty(g));
        self.blue.set_duty(duty(b));
    }

    /// brightness 0-100. Backlight enable line is active-low, same as the
    /// Python driver's PWM mode ("duty_cycle = 100 - brightness").
    pub fn set_backlight(&mut self, brightness: u8) {
        let brightness = brightness.min(100) as f64;
        self.backlight.set_duty(1.0 - brightness / 100.0);
    }
}
