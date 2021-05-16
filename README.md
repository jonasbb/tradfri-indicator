# Tradfri Indicator

You can install and run the program as shown.
During the first connection the script will ask for the “Security Code”, which is printed on the backside of the gateway.
Further, connects will not require that code again.
The IP address of the gateway will be detected automatically.

```bash
# Install
pip3 install --user git+https://github.com/jonasbb/tradfri-indicator

# Run
python3 -m tradfri_indicator
```

## Autostart

To enable autostart for the indicator create a desktop file `~/.config/autostart/tradfri-indicator.desktop` with the following content.

```ini
[Desktop Entry]
Name=Tradfri Indicator
GenericName=Tradfri Indicator
Comment=AppIndicator which allows controling a Tradfri Gateway
Exec=python3 -m tradfri_indicator
Terminal=false
Type=Application
X-GNOME-Autostart-enabled=true
```
