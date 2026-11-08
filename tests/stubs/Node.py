"""Test stub of RotorHazard's Node with the fields rh_oft reads and writes."""


class Node:
    def __init__(self):
        self.index = -1
        self.api_valid_flag = False
        self.frequency = 0
        self.current_rssi = 0
        self.node_peak_rssi = 0
        self.node_nadir_rssi = 0
        self.pass_peak_rssi = 0
        self.pass_nadir_rssi = 0
        self.max_rssi_value = 999
        self.node_lap_id = -1
        self.lap_timestamp = -1
        self.crossing_flag = False
        self.pass_crossing_flag = False
        self.enter_at_timestamp = 0
        self.exit_at_timestamp = 0
        self.debug_pass_count = 0
        self.enter_at_level = 0
        self.exit_at_level = 0
        self.history_values = []
        self.history_times = []
        self.firmware_version_str = None
