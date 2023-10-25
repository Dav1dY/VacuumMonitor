import paho.mqtt.client as mqtt
import json
import socket
import time
import logging
from logging.handlers import TimedRotatingFileHandler
import re
import threading
import select
import os


class Vacuum:
    def __init__(self):
        self.init_success = False

        self.logger = logging.getLogger('VacuumLogger')
        self.logger.setLevel(logging.INFO)

        dir_name = '/vault/VacuumMonitor/log'
        if not os.path.exists(dir_name):
            # noinspection PyBroadException
            try:
                os.makedirs(dir_name)
            except Exception:
                return

        # noinspection PyBroadException
        try:
            handler = TimedRotatingFileHandler('/vault/VacuumMonitor/log/VacuumMonitor.log', when='midnight', backupCount=30)
        except Exception:
            return
        handler.suffix = "%Y-%m-%d"
        formatter = logging.Formatter('%(asctime)s -  %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.info("Initializing.")
        self.logger.info("*******************************")

        # load config file
        self.loaded_data = None
        # noinspection PyBroadException
        try:
            with open("/vault/VacuumMonitor/config/vacuum_config.json", 'r') as f:
                self.loaded_data = json.load(f)
            self.logger.info("vacuum_config load success.")
        except FileNotFoundError:
            self.logger.error("vacuum_config was not found.")
            return
        except json.JSONDecodeError:
            self.logger.error("An error occurred while decoding the JSON.")
            return
        except Exception:
            self.logger.error("An unexpected error occurred: ", exc_info=True)
            return

        # init parameters
        # noinspection PyBroadException
        try:
            self.query_config_topic = "/Devices/adc_agent/QueryConfig"
            self.last_time = int(time.time())
            self.current_time = 0
            self.port_in_use = 0
            self.connection_state = False
            self.update_state = False
            self.sock = None
            self.client = None
            self.scheduled_report_ready = False
            self.scheduled_report_thread = None

            try:
                with open('/vault/data_collection/test_station_config/gh_station_info.json', 'r') as f:
                    data = json.load(f)
                    if 'ghinfo' in data and 'STATION_NUMBER' in data['ghinfo']:
                        self.station_number = data['ghinfo']['STATION_NUMBER']
                    else:
                        self.logger.error('Cannot find STATION_NUMBER.')
                        return
                    if 'ghinfo' in data and 'STATION_TYPE' in data['ghinfo']:
                        self.station_type = data['ghinfo']['STATION_TYPE']
                    else:
                        self.logger.error('Cannot find STATION_TYPE.')
                        return
            except FileNotFoundError:
                self.logger.error('File gh_station_info.json not found.')
                return
            except json.JSONDecodeError:
                self.logger.error('An error occurred while decoding the JSON file.')
                return
            except Exception as e:
                self.logger.error(f"An error occurred : {e}.")
                return
            self.logger.info("Load station info success.")
            if self.station_type == "QT-BCM2" or self.station_type == "BOOT-ARGS":
                maincomponent_id = "work_station_" + self.station_type
            else:
                maincomponent_id = "work_station_" + self.station_type + "_" + self.station_number
            subcomponent = "VacuumMonitor"
            self.config_topic = "/Devices/" + maincomponent_id + "/" + subcomponent + "/" + "Config"
            self.analog_topic = "/Devices/" + maincomponent_id + "/" + subcomponent + "/" + "Analog"

            if self.loaded_data is not None:
                self.broker = self.loaded_data.get('broker', "10.0.1.200")
                self.port = int(self.loaded_data.get('broker_port', "1883"))
                self.target_address = self.loaded_data.get('target_address', "10.0.1.202")
                self.start_port = int(self.loaded_data.get('start_port', "4096"))
                self.end_port = int(self.loaded_data.get('end_port', "4101"))
                self.config_path = self.loaded_data.get('config_path', "Config2Send_Vacuum.json")
                self.report_interval = int(self.loaded_data.get('report_interval', "5"))
                self.connect_retry_times = int(self.loaded_data.get('connect_retry_times', "3"))
                self.socket_timeout = int(self.loaded_data.get('socket_timeout', "3"))
            else:
                self.broker = "10.0.1.200"
                self.port = 1883
                self.target_address = "10.0.1.202"
                self.start_port = 4096
                self.end_port = 4101
                self.config_path = "Config2Send_Vacuum.json"
                self.report_interval = 5
                self.connect_retry_times = 3
                self.socket_timeout = 3
        except Exception:
            self.logger.error("Initialize parameters fail.")
            return
        self.logger.info("All parameters loaded success.")

        # load config_data
        # noinspection PyBroadException
        try:
            with open(self.config_path, 'r') as f:
                self.config_data = json.load(f)
            self.logger.info("Config2Send_Vacuum.json load success.")
        except FileNotFoundError:
            self.logger.error("Config2Send_Vacuum.json was not found.")
            return
        except json.JSONDecodeError:
            self.logger.error("An error occurred while decoding the JSON.")
            return
        except Exception:
            self.logger.error("An unexpected error occurred: ", exc_info=True)
            return
        self.config_data = json.dumps(self.config_data)

        # init socket client
        if not self.socket_init():
            return
        if not self.socket_connect_with_retry():
            return

        # init mqtt
        if not self.mqtt_client_init():
            return
        if not self.mqtt_connect():
            return

        self.start_scheduled_init()
        self.scheduled_report_ready = True
        self.init_success = True

    def socket_init(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            return True
        except socket.error as e:
            self.logger.error(f"Failed to create a socket. Error: {e}")
            return False

    def socket_connect_with_retry(self) -> bool:
        retry_times = 0
        while retry_times < self.connect_retry_times:
            if not self.sock:
                if not self.socket_init():
                    return False
            retry_times += 1
            self.logger.info(f"Retry time = {retry_times}.")
            if self.connect_to_target():
                if self.is_socket_connected():
                    return True
            # time.sleep(1)
            self.sock = None
        self.logger.error("Socket connect fail.")
        return False

    def mqtt_client_init(self) -> bool:
        try:
            self.client = mqtt.Client(self.broker, self.port)
            self.logger.info("mqtt client established.")
        except Exception as e:
            self.logger.error(f"Failed to establish mqtt client: {e}")
            return False
        try:
            self.client.on_message = self.on_message
            self.logger.info("Message callback registered.")
        except Exception as e:
            self.logger.error(f"Failed to register message callback: {e}")
            return False
        try:
            self.client.on_connect = self.on_connect
            self.logger.info("Connect callback registered.")
        except Exception as e:
            self.logger.error(f"Failed to register connect callback: {e}")
            return False
        return True

    def mqtt_connect(self) -> bool:
        retry_times = 0
        while retry_times < self.connect_retry_times:
            retry_times += 1
            try:
                self.client.connect("localhost", 1883, 60)
                self.logger.info("Connect to broker success.")
                break
            except Exception as e:
                self.logger.error(f"Failed to connect to the broker: {e}.")
                if retry_times == self.connect_retry_times:
                    return False

        (subscribe_result, mid) = self.client.subscribe("/Devices/adc_agent/QueryConfig")
        # self.client.subscribe("")   add more topics
        if subscribe_result == 0:
            self.logger.info("subscribe success.")
        else:
            self.logger.error(f"Failed to subscribe. Result code: {subscribe_result}")
            return False
        return True

    def start_scheduled_init(self):
        if self.scheduled_report_thread is not None:
            self.logger.error("Scheduled report already started.")
            return
        self.scheduled_report_thread = threading.Thread(target=self.scheduled_report)
        self.scheduled_report_thread.setDaemon(True)

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.logger.info("Connected successfully.")
        else:
            self.logger.error(f"Connection failed with error code {rc}.")

    def on_message(self, client, userdata, message):
        if message.topic == '/Devices/adc_agent/QueryConfig':  # query config
            if self.client:
                try:
                    client.publish(self.config_topic, self.config_data)
                    self.logger.info("Config message published.")
                except Exception as e:
                    self.logger.error(f"Failed to publish message: {e}.")
            else:
                self.logger.error("Mqtt client not exist.")
        elif message.topic == '/Test' or message.topic == '/Try':  # suck or release
            message = "00000,CHECK_ANALOG#"
            message = message.encode()
            if self.sock:
                # send cmd
                _, ready_to_write, _ = select.select([], [self.sock], [], self.socket_timeout)
                if ready_to_write[0]:
                    try:
                        if self.socket_send(message):
                            self.logger.info("Command sent.")
                        else:
                            self.logger.error("Failed to send command.")
                    except Exception as e:
                        self.logger.error(f"Failed to send command: {e}")
                        return
                else:
                    self.logger.error("Socket unable to write, timeout.")
                    return

                # recv reply
                ready_to_read, _, _ = select.select([self.sock], [], [], self.socket_timeout)
                if ready_to_read[0]:
                    try:
                        data = self.sock.recv(1024)
                        self.logger.info("Reply received.")
                    except Exception as e:
                        self.logger.error(f"Failed to receive analog: {e}")
                        return
                else:
                    self.logger.error("Socket unable to read, timeout.")
                    return

                # handle data
                self.update_json(data)
                if self.update_state:
                    try:
                        with open('/vault/VacuumMonitor/Analog.json', 'r') as f:
                            json_data = json.dumps(json.load(f))
                            if self.client:
                                try:
                                    client.publish(self.analog_topic, json_data)
                                    self.logger.info("Analog message published.")
                                except Exception as e:
                                    self.logger.error(f"Failed to publish message: {e}")
                            else:
                                self.logger.error("Mqtt client not exist.")
                    except FileNotFoundError:
                        self.logger.error("The file 'Analog.json' was not found.")
                    except json.JSONDecodeError:
                        self.logger.error("An error occurred while decoding the JSON.")
                    except Exception as e:
                        self.logger.error(f"An unexpected error occurred: {e}")

    def connect_to_target(self) -> bool:
        self.port_in_use = 0
        self.logger.info("Starting to connecting to plc.")

        for p in range(self.start_port, self.end_port + 1):
            try:
                self.logger.info(f"Starting to connecting to {self.target_address}:{p}.")
                self.sock.connect((self.target_address, p))
                # todo: takes 20s when fail, can modify timeout
                self.port_in_use = p
                break
            except socket.error:
                self.logger.error(f"Port {p} fail.")
        if self.port_in_use == 0:
            self.logger.error("All port failed.")
            return False
        self.logger.info(f"Connect to {self.target_address}:{self.port_in_use}.")
        return True

    def is_socket_connected(self) -> bool:
        # check self.sock before call this
        try:
            self.sock.sendall(b'')
            return True
        except socket.error:
            return False

    def update_json(self, data):
        self.update_state = False

        if data:
            try:
                data = data.decode('utf-8')
            except UnicodeDecodeError as e:
                self.logger.error(f"Failed to decode message: {e}.")
                return
            try:
                match = re.search(',REPORT_ANALOG,(\\s*)(\\d+)', data)
                if match:
                    data = int(match.group(2))
                else:
                    self.logger.error("Receive bad message1.")
                    return
            except ValueError:
                self.logger.error("Receive bad message2.")
                return
        else:
            # case that socket receives timeout and have no data send back
            data = 0

        self.current_time = int(time.time())

        try:
            with open('/vault/VacuumMonitor/Analog.json', 'r+') as f:
                json_data = json.load(f)
                json_data['value'] = float(data/4000)
                json_data['interval'] = self.current_time - self.last_time
                json_data['timestamp'] = self.current_time
                f.seek(0)
                json.dump(json_data, f)
                f.truncate()
        except FileNotFoundError:
            self.logger.error("The file 'Analog.json' was not found.")
            return
        except json.JSONDecodeError:
            self.logger.error("An error occurred while decoding the JSON.")
            return
        except Exception as e:
            self.logger.error(f"An unexpected error occurred: {e}")
            return

        self.last_time = self.current_time
        self.update_state = True

    def scheduled_report(self):
        self.logger.info("Thread start.")
        message = "00000,QUERY_ANALOG#"
        message = message.encode()
        last_time = time.time()

        while self.scheduled_report_ready and self.sock and self.client:
            while time.time()-last_time <= self.report_interval:
                time.sleep(1)
            last_time = time.time()

            # send cmd
            _, ready_to_write, _ = select.select([], [self.sock], [], self.socket_timeout)
            if ready_to_write:
                try:
                    if self.socket_send(message):
                        self.logger.info("Command sent.")
                    else:
                        self.logger.error("Failed to send command.")
                except Exception as e:
                    self.logger.error(f"Failed to send command: {e}")
                    continue
            else:
                self.logger.error("Socket unable to write, timeout.")
                continue
            time.sleep(0.1)

            # recv reply
            ready_to_read, _, _ = select.select([self.sock], [], [], self.socket_timeout)
            if ready_to_read:
                try:
                    data = self.sock.recv(1024)
                    self.logger.info("Reply received.")
                except Exception as e:
                    self.logger.error(f"Failed to receive analog: {e}")
                    continue
                    # break
            else:
                self.logger.error("Socket unable to read, timeout.")
                continue
                # break

            self.logger.info(f"DATA = {data}.")
            self.update_json(data)  # update json

            if self.update_state:
                try:
                    with open('/vault/VacuumMonitor/Analog.json', 'r') as f:
                        json_data = json.dumps(json.load(f))
                        if self.client:
                            try:
                                self.client.publish(self.analog_topic, json_data)
                                self.logger.info("Analog message published.")
                            except Exception as e:
                                self.logger.error(f"Failed to publish message: {e}")
                        else:
                            self.logger.error("Mqtt client not exist.")
                except FileNotFoundError:
                    self.logger.error("The file 'Analog.json' was not found.")
                except json.JSONDecodeError:
                    self.logger.error("An error occurred while decoding the JSON.")
                except Exception as e:
                    self.logger.error(f"An unexpected error occurred: {e}")

        self.logger.info("Thread end.")
        self.scheduled_report_ready = False

    def socket_send(self, message) -> bool:
        if not self.sock:
            self.logger.error("Socket client not exist.")
            return False

        for reconnect_retry_times in range(self.connect_retry_times):
            for send_retry_times in range(self.connect_retry_times):
                try:
                    self.sock.sendall(message)
                    return True
                except socket.error as e:
                    self.logger.error(f"Socket error: {e}, try {send_retry_times} times.")
                    time.sleep(1)
                except Exception as e:
                    self.logger.error(f"Failed to send command: {e}, try {send_retry_times} times.")
                    time.sleep(1)
            self.logger.error(f"Retry {self.connect_retry_times} times.")
            self.sock.close()
            self.connect_to_target()
        self.logger.error(f"Reconnect failed {self.connect_retry_times} times, send fail")
        return False

    def start_scheduled_report(self):
        if self.scheduled_report_thread.is_alive():
            self.logger.error("Report thread is already on.")
            return
        self.logger.info("Starting scheduled report.")
        self.scheduled_report_ready = True
        self.scheduled_report_thread.start()
        self.logger.info("Scheduled report start.")

    def start(self):
        if self.client:
            self.client.loop_start()
            self.logger.info("Mqtt loop started.")
            self.start_scheduled_report()
        else:
            self.logger.error("Mqtt client not exist.")
        while self.scheduled_report_ready:
            pass


if __name__ == '__main__':
    new = Vacuum()
    if new.init_success:
        new.start()