#!/usr/bin/env python3
import argparse
import os
import threading
import time
import typing as t
import uuid
import re

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

gi.require_version("AppIndicator3", "0.1")

from gi.repository import AppIndicator3, GLib, Gtk  # type: ignore

SUPERGROUP = 131073
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_FILE = os.path.join(GLib.get_user_config_dir(), "tradfri_standalone_psk.conf")
TIMEOUT = 5

IGNORE_SCENES = re.compile("EDIT TO IGNORE SOME SCENES")


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

    _menu_semaphore: threading.BoundedSemaphore
    """Ensure that the menu updating code only runs in one thread"""

    def __init__(self) -> None:
        self._menu_semaphore = threading.BoundedSemaphore(1)
        with self._menu_semaphore:
            self.indicator = AppIndicator3.Indicator.new(
                "tradfriindicator",
                os.path.join(SCRIPT_DIR, "lightbulb-fill.svg"),
                AppIndicator3.IndicatorCategory.HARDWARE,
            )
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.indicator.set_menu(self._build_menu())

            self._load_config()
            self._load_devices_and_rooms()
            self._update_menu()

    def _load_config(self) -> None:
        conf = load_json(CONFIG_FILE)

        parser = argparse.ArgumentParser()
        parser.add_argument(
            "host", metavar="IP", type=str, help="IP Address of your Tradfri gateway"
        )
        parser.add_argument(
            "-K",
            "--key",
            dest="key",
            required=False,
            help="Security code found on your Tradfri gateway",
        )
        args = parser.parse_args()

        if args.host not in conf and args.key is None:
            print(
                "Please provide the 'Security Code' on the back of your "
                "Tradfri gateway:",
                end=" ",
            )
            key = input().strip()
            if len(key) != 16:
                raise PytradfriError("Invalid 'Security Code' provided.")
            args.key = key

        try:
            identity = conf[args.host].get("identity")
            psk = conf[args.host].get("key")
            self.api_factory = APIFactory(
                host=args.host, psk_id=identity, psk=psk, timeout=TIMEOUT
            )
        except KeyError:
            identity = uuid.uuid4().hex
            self.api_factory = APIFactory(
                host=args.host, psk_id=identity, timeout=TIMEOUT
            )

            try:
                psk = self.api_factory.generate_psk(args.key)
                print("Generated PSK: ", psk)

                conf[args.host] = {"identity": identity, "key": psk}
                save_json(CONFIG_FILE, conf)
            except AttributeError as e:
                raise PytradfriError(
                    "Please provide the 'Security Code' on the "
                    "back of your Tradfri gateway using the "
                    "-K flag."
                ) from e

    def _load_devices_and_rooms(self) -> None:
        gateway = Gateway()

        devices_command = gateway.get_devices()
        devices_commands = self._execute_api(devices_command)
        devices = self._execute_api(devices_commands)

        self.lights = {}
        for dev in devices:
            if dev.has_light_control:
                self.lights[dev.id] = dev.light_control.lights[0]
                self._observe(dev)

        moods_command = gateway.get_moods(SUPERGROUP)
        mood_commands = self._execute_api(moods_command)
        moods = self._execute_api(mood_commands)
        self.moods = {}
        for mood in moods:
            self.moods[mood.id] = mood

        groups_command = gateway.get_groups()
        group_commands = self._execute_api(groups_command)
        groups = self._execute_api(group_commands)
        self.groups = {}
        for group in groups:
            self.groups[group.id] = group

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        menu.append(Gtk.MenuItem.new_with_label("Loading..."))
        menu.show_all()
        return menu

    def _update_menu(self) -> None:
        def update() -> None:
            with self._menu_semaphore:
                menu = Gtk.Menu()

                menu_scenes = Gtk.MenuItem.new_with_label("Scenes")
                menu_scenes.set_sensitive(False)
                menu.append(menu_scenes)

                moods = list(self.moods.values())
                moods.sort(key=lambda m: m.name)
                for mood in moods:
                    if IGNORE_SCENES.match(mood.name):
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

        threading.Thread(target=update, name="Update Menu", daemon=True).start()

    def _get_group_state(self, group: Group) -> t.Tuple[bool, bool]:
        """
        Get the state of the group. The first bool represents if any lamp is active. The second bool represents if all lights in the group have the same state.
        """
        light_states = [
            self.lights.get(device_id).state
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
            self._update_menu()

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
