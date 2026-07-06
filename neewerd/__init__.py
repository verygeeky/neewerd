"""neewerd — a small daemon that owns the Bluetooth link to Neewer TL90C tubes
and exposes them through pluggable I/O modules (MQTT, OSC, HTTP, local socket).

Core idea: connect once, hold the links, and be a dumb pipe to the tubes.
"""
__version__ = "0.1.0"
