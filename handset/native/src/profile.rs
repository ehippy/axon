//! PiSugar Whisplay device profile. BCM pin numbering.
pub const LCD_WIDTH: u32 = 240;
pub const LCD_HEIGHT: u32 = 280;

// physical pin -> BCM, from whisplay.py's _RPI_BOARD_TO_BCM
pub const DC_BCM: u8 = 27; // board 13
pub const RST_BCM: u8 = 4; // board 7
pub const BACKLIGHT_BCM: u8 = 22; // board 15 ("LED_PIN" in the Python driver)
pub const RED_BCM: u8 = 25; // board 22
pub const GREEN_BCM: u8 = 24; // board 18
pub const BLUE_BCM: u8 = 23; // board 16
pub const BUTTON_BCM: u8 = 17; // board 11

pub const SPI_SPEED_HZ: u32 = 100_000_000;
