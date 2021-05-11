#!/usr/bin/env python3
import os
import threading
import time
import typing as t
import uuid

import gi
from pytradfri import Gateway
from pytradfri.api.libcoap_api import APIFactory
from pytradfri.device import Device
from pytradfri.device.light import Light
from pytradfri.error import PytradfriError
from pytradfri.group import Group
from pytradfri.mood import Mood
from pytradfri.util import load_json, save_json
from ratelimit import limits, sleep_and_retry
from zeroconf import ServiceBrowser, Zeroconf, ServiceInfo, ServiceListener

gi.require_version("AppIndicator3", "0.1")

from gi.repository import AppIndicator3, GLib, Gtk  # type: ignore

SUPERGROUP = 131073
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_FILE = os.path.join(GLib.get_user_config_dir(), "tradfri_standalone_psk.conf")
TIMEOUT = 5


class ZeroconfListener(ServiceListener):
    discovered_gateways: t.List[ServiceInfo]

    def __init__(self) -> None:
        super()
        self.discovered_gateways = []

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info and info.name == "TRADFRI gateway._hap._tcp.local.":
            self.discovered_gateways.append(info)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


class TradfriIndicator:
    indicator: AppIndicator3.Indicator
    """Indicator shown in the UI"""
    api_factory: APIFactory
    """API object to talk to the gateway via CoAP"""
    lights: t.Dict[int, Light]
    """Map from API ID to Devices, filtered to only contain lights"""
    moods: t.Dict[int, Mood]
    """Map from Mood ID to Moods"""
    groups: t.Dict[int, Group]
    """Map from Group ID to Groups"""

    _need_menu_update: threading.Condition
    """Ensure that menu updates are properly registered an not multiple threads """

    ignored_scenes: t.List[str]
    ignored_rooms: t.List[str]

    def __init__(self) -> None:
        self.ignored_scenes = []
        self.ignored_rooms = []
        self._need_menu_update = threading.Condition()
        # Block menu updating during initialization
        self._need_menu_update.acquire()
        self.indicator = AppIndicator3.Indicator.new(
            "tradfriindicator",
            os.path.join(SCRIPT_DIR, "lightbulb-fill.svg"),
            AppIndicator3.IndicatorCategory.HARDWARE,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self._build_menu())

        self._load_config()
        self._load_devices_and_rooms()

        threading.Thread(
            target=self._update_menu, name="Update Menu", daemon=True
        ).start()
        # Trigger a menu update
        self._need_menu_update.release()
        self._set_needs_menu_update()

    def _load_config(self) -> None:
        c = load_json(CONFIG_FILE)
        if isinstance(c, dict):
            conf: t.Dict[str, t.Any] = c
        else:
            conf = {}

        host = None
        host_name = None

        # Use zeroconf for finding the gateway
        zeroconf = Zeroconf()
        try:
            listener = ZeroconfListener()
            _browser = ServiceBrowser(zeroconf, "_hap._tcp.local.", listener)

            time.sleep(2)
            if listener.discovered_gateways:
                host = listener.discovered_gateways[0].parsed_addresses()[0]
                host_name = listener.discovered_gateways[0].server
                print(f"Connecting to {host_name} at {host}")
        finally:
            zeroconf.close()

        if host is None or host_name is None:
            raise PytradfriError(
                "Could not find Tradfri gateway and no IP address was provided"
            )

        try:
            identity = conf[host_name].get("identity")
            psk = conf[host_name].get("key")
            self.api_factory = APIFactory(
                host=host, psk_id=identity, psk=psk, timeout=TIMEOUT
            )
        except KeyError as kerr:
            print(
                "Please provide the 'Security Code' on the back of your "
                "Tradfri gateway:",
                end=" ",
            )
            key = input().strip()
            if len(key) != 16:
                raise PytradfriError("Invalid 'Security Code' provided.") from kerr

            identity = uuid.uuid4().hex
            self.api_factory = APIFactory(host=host, psk_id=identity, timeout=TIMEOUT)
            psk = self.api_factory.generate_psk(key)
            print("Generated PSK: ", psk)

            conf[host_name] = {"identity": identity, "key": psk}
            save_json(CONFIG_FILE, conf)

        self.ignored_scenes = conf[host_name].get("ignored_scenes", [])
        self.ignored_rooms = conf[host_name].get("ignored_rooms", [])

    def _load_devices_and_rooms(self) -> None:
        self.moods = {}
        self.groups = {}
        self.lights = {}

        gateway = Gateway()
        needed_lights = set()

        moods_command = gateway.get_moods(SUPERGROUP)
        mood_commands = self._execute_api(moods_command)
        moods = self._execute_api(mood_commands)
        for mood in moods:
            self.moods[mood.id] = mood

        groups_command = gateway.get_groups()
        group_commands = self._execute_api(groups_command)
        groups = self._execute_api(group_commands)
        for group in groups:
            self.groups[group.id] = group
            for device_id in group.member_ids:
                needed_lights.add(device_id)

        devices_command = gateway.get_devices()
        devices_commands = self._execute_api(devices_command)
        devices = self._execute_api(devices_commands)

        # Observe those lights which are part of the rooms we are interested in
        for dev in devices:
            if dev.has_light_control and dev.id in needed_lights:
                self.lights[dev.id] = dev.light_control.lights[0]
                self._observe(dev)

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        menu.append(Gtk.MenuItem.new_with_label("Loading..."))
        menu.show_all()
        return menu

    def _set_needs_menu_update(self) -> None:
        with self._need_menu_update:
            self._need_menu_update.notify_all()

    def _update_menu(self) -> None:
        self._need_menu_update.acquire()
        while True:
            self._need_menu_update.wait()

            menu = Gtk.Menu()

            menu_scenes = Gtk.MenuItem.new_with_label("Scenes")
            menu_scenes.set_sensitive(False)
            menu.append(menu_scenes)

            moods = list(self.moods.values())
            moods.sort(key=lambda m: m.name)
            for mood in moods:
                if mood.name in self.ignored_scenes:
                    continue

                m = Gtk.MenuItem.new_with_label(mood.name)
                m.connect("activate", self._activate_mood, mood)
                menu.append(m)

            menu.append(Gtk.SeparatorMenuItem())
            menu_rooms = Gtk.MenuItem.new_with_label("Rooms")
            menu_rooms.set_sensitive(False)
            menu.append(menu_rooms)

            groups = list(g for g in self.groups.values() if g.id != SUPERGROUP)
            groups.sort(key=lambda g: g.name)
            for group in groups:
                if group.name in self.ignored_rooms:
                    continue

                m = Gtk.CheckMenuItem.new_with_label(group.name)
                is_active, is_consistent = self._get_group_state(group)
                m.set_active(is_active)
                m.set_inconsistent(not is_consistent)
                m.connect("activate", self._activate_group, group)
                menu.append(m)

            menu.append(Gtk.SeparatorMenuItem())
            menu_quit = Gtk.MenuItem.new_with_label("Quit")
            menu_quit.connect("activate", self._quit)
            menu.append(menu_quit)

            menu.show_all()
            # Schedule the new menu to be set for the indicator
            GLib.idle_add(self.indicator.set_menu, menu)

    def _get_group_state(self, group: Group) -> t.Tuple[bool, bool]:
        """
        Get the state of the group. The first bool represents if any lamp is active. The second bool represents if all lights in the group have the same state.
        """
        light_states = [
            self.lights.get(device_id).state  # type:ignore
            for device_id in group.member_ids
            if self.lights.get(device_id) is not None
        ]
        if all(light_states):
            return (True, True)
        elif any(light_states):
            return (True, False)
        else:
            return (False, True)

    def _observe(self, device: Device) -> None:
        def callback(updated_device: Device) -> None:
            light = updated_device.light_control.lights[0]
            print("Got update for", updated_device.name)
            self.lights[updated_device.id] = light
            self._set_needs_menu_update()

        def err_callback(err: t.Any) -> None:
            if str(err) != "Observing stopped.":
                print(err)

        def worker() -> None:
            while True:
                self._execute_api(device.observe(callback, err_callback, duration=300))
                # Sleep a bit to avoid reconnect storms
                # Each reconnect will fetch the current state of the lamp
                # So even if we miss some events, after a couple of seconds everything should be in sync again
                time.sleep(5)

        threading.Thread(target=worker, daemon=True).start()

    @sleep_and_retry
    # Limit to x calls every y seconds
    @limits(calls=2, period=1)
    def _execute_api(self, command: t.Any) -> t.Any:
        return self.api_factory.request(command)

    def _activate_mood(self, _menu_item: Gtk.MenuItem, mood: Mood) -> None:
        print("Activate Mood", mood.name)
        gateway = Gateway()
        supergroup = self._execute_api(gateway.get_group(SUPERGROUP))
        self._execute_api(supergroup.activate_mood(mood.id))

    def _activate_group(self, menu_item: Gtk.MenuItem, group: Group) -> None:
        print("Activate Group", group.name)
        self._execute_api(group.set_state(menu_item.get_active()))

    def run(self) -> None:
        Gtk.main()

    def _quit(self, _menu_item: Gtk.MenuItem) -> None:
        Gtk.main_quit()


if __name__ == "__main__":
    tradfri_indicator = TradfriIndicator()
    tradfri_indicator.run()
