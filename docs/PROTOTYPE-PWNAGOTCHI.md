# Handset prototype

The current prototype uses a Raspberry Pi Zero 2 W, PiSugar3 battery controller,
and PiSugar Whisplay color LCD/button/audio HAT. The current implementation is
[handset/native](../handset/native/), with a portable rendering/state library and
a Linux hardware binary. See [installation](install-handset.md).

An earlier Waveshare 2.13-inch e-Paper experiment required its V3 driver and
periodic full refreshes. That implementation is no longer shipped. The current
LCD renders complete frames; one process owns the panel.

The server and broker can run separately or on the same device. Publishers and
handset communicate through documented [wire contracts](../protocol/README.md),
without assuming a particular machine or remote-access setup.

Hardware checks: verify panel revision, rotation, SPI/GPIO access, battery service,
RGB LED mapping, and physical button behavior against the Whisplay profile.
Gabbie is optional and owns voice processing; Axon owns button/display feedback.
