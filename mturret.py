#!/usr/bin/python

# Copyright 2014 Josh Pieper, jjp@pobox.com.  All rights reserved.

import eventlet
import math
import numpy
import optparse
import os
import pygame
import sys
import time

import ConfigParser

sys.path.append('/home/josh/prog/mech')
import eventlet_serial
from quaternion import Euler
from quaternion import Quaternion
import servo_controller
import ukf_filter

def parsehex(hexdata):
    result = ''
    pos = 0
    length = len(hexdata)
    while pos < length:
        result += chr(int(hexdata[pos:pos+2], 16))
        pos += 2
    return result

class ServoConfig(object):
    def __init__(self, config):
        self.servo_type = 'herkulex'
        self.servo_pan_id = 2
        self.servo_tilt_id = 1
        self.servo_pan_sign = -1
        self.servo_tilt_sign = 1

        if not config.has_section('servo'):
            return

        self.servo_type = config.get('servo', 'type', self.servo_type)
        self.servo_pan_id = config.get('servo', 'pan_id', self.servo_pan_id)
        self.servo_tilt_id = config.get('servo', 'tilt_id',
                                        self.servo_tilt_id)
        self.servo_pan_sign = config.get('servo', 'pan_sign',
                                         self.servo_pan_sign)
        self.servo_tilt_sign = config.get('servo', 'tilt_sign',
                                          self.servo_tilt_sign)

class Pipe(object):
    def __init__(self, write_func):
        self.write_func = write_func
        self.queue = eventlet.queue.Queue(0)
        self.sem_write = eventlet.semaphore.Semaphore()
        self.sem_read = eventlet.semaphore.Semaphore()

    def write(self, data):
        assert self.sem_write.locked()
        self.write_func(data)

    def read(self, size):
        assert self.sem_read.locked()
        result = ''
        for x in range(size):
            data = self.queue.get(block=True)
            assert len(data) == 1
            result += data
        return result

    def readline(self):
        result = ''
        while True:
            data = self.read(1)
            result += data
            if data == '\r' or data == '\n':
                return result

    def fill_read(self, data):
        for x in data:
            self.queue.put(x, block=True)

IMU_UPDATE_PERIOD = 1.0 / 95

class AttitudeEstimator(object):
    # States are:
    # 0: attitude W
    # 1: attitude X
    # 2: attitude Y
    # 3: attitude Z
    # 4: gyro yaw bias
    # 5: gyro pitch bias
    # 6: gyro roll bias

    def state_function(self, state, dt):
        result = state

        this_attitude = Quaternion(
            result[0], result[1], result[2], result[3]).normalized()
        delta = Quaternion()
        for x in self.current_gyro:
            advance = Quaternion.integrate_rotation_rate(
                x.roll + result[4],
                x.pitch + result[5],
                x.yaw + result[6],
                dt)
            delta = delta * advance

        next_attitude = (this_attitude * delta).normalized()
        result[0] = next_attitude.w
        result[1] = next_attitude.x
        result[2] = next_attitude.y
        result[3] = next_attitude.z
        return result

    def attitude(self):
        return Quaternion(*self.ukf.state[0:4,0])

    def gyro_bias(self):
        return list(self.ukf.state[4:7,0])

    def gyro_bias_uncertainty(self):
        return [math.sqrt(x) for x in list(numpy.diag(self.ukf.covariance)[4:7])]

    def __init__(self):
        self.current_gyro = []
        self.init = False
        self.log = open('/tmp/attitude.csv', 'w')
        self.emit_header()
        self.ukf = None

    def emit_header(self):
        self.log.write(','.join(['state_%d' % x for x in range(7)] +
                                ['covar_%d_%d' % (x / 7, x % 7) for x in range(7 * 7)]) + '\n')

    def emit_log(self):
        self.log.write(','.join(['%g' % x for x in list(self.state.flatten())  + list(self.covariance.flatten())]) + '\n')

    def process_gyro(self, yaw_rps, pitch_rps, roll_rps):
        self.current_gyro += [
            Euler(yaw=yaw_rps, pitch=pitch_rps, roll=roll_rps)]

    @staticmethod
    def orientation_to_accel(quaternion):
        gravity = numpy.array([0, 0, 1.0])
        expected = quaternion.conjugated().rotate(gravity)
        return numpy.array([[expected[0]],
                            [expected[1]],
                            [expected[2]]])

    @staticmethod
    def accel_measurement(state):
        quaternion = Quaternion(
            state[0,0], state[1,0], state[2,0], state[3,0]).normalized()

        return AttitudeEstimator.orientation_to_accel(quaternion)

    @staticmethod
    def yaw_measurement(state):
        quaternion = Quaternion(
            state[0], state[1], state[2], state[3]).normalized()
        return numpy.array([quaternion.euler().yaw])

    @staticmethod
    def accel_to_orientation(x, y, z):
        roll = math.atan2(-x, z)
        pitch = math.atan2(y, math.sqrt(x**2 + z ** 2))

        quat = Quaternion.from_euler(roll, pitch, 0)
        return quat

    @staticmethod
    def covariance_limit(P):
        for x in range(7):
            if P[x, x] < 1e-9:
                P[x, x] = 1e-9
        for x in range(0, 4):
            if P[x, x] > .15:
                P[x, x] = .15
        for x in range(4, 7):
            if P[x, x] > math.radians(50):
                P[x, x] = math.radians(50)
        return P

    def process_yaw(self, mounting, yaw):
        if self.ukf is None:
            return

        def yaw_meas(state):
            this_attitude = Quaternion(
                state[0], state[1], state[2], state[3]).normalized()
            offset = this_attitude * mounting
            return numpy.array([[offset.euler().yaw]])

        self.ukf.update_measurement(
            numpy.array([[yaw]]),
            measurement_function=yaw_meas,
            measurement_noise=numpy.array([[math.radians(5)]]))

    def process_accel(self, x, y, z):
        # First, normalize.
        norm = math.sqrt(x * x + y * y + z * z)

        x /= norm
        y /= norm
        z /= norm

        # If this is our first update, then initialize the state with
        # the expected attitude based on the accelerometers.
        if not self.init:
            self.init = True

            quat = self.accel_to_orientation(x, y, z)
            state = numpy.array([[quat.w],
                                 [quat.x],
                                 [quat.y],
                                 [quat.z],
                                 [0.],
                                 [0.],
                                 [0.]])
            covariance = numpy.diag([1e-3, 1e-3, 1e-3, 1e-3,
                                     math.radians(1),
                                     math.radians(1),
                                     math.radians(1)])

            self.ukf = ukf_filter.UkfFilter(
                initial_state=state,
                initial_covariance=covariance,
                process_function=self.state_function,
                process_noise=numpy.diag([1e-8, 1e-8, 1e-8, 1e-8,
                                          math.radians(1e-6),
                                          math.radians(1e-6),
                                          math.radians(1e-6)]),
                measurement_function=self.accel_measurement,
                measurement_noise=numpy.diag([10.0, 10.0, 10.0]),
                covariance_limit=self.covariance_limit)

        self.ukf.update_state(IMU_UPDATE_PERIOD)
        self.ukf.update_measurement(numpy.array([[x],[y],[z]]))

        self.current_gyro = []
        #self.emit_log()


class Imu(object):
    ACCEL_FS_2G = 0
    ACCEL_FS_4G = 1
    ACCEL_FS_8G = 2
    ACCEL_FS_16G = 3

    ACCEL_SENS_2G = 0.001
    ACCEL_SENS_4G = 0.002
    ACCEL_SENS_8G = 0.004
    ACCEL_SENS_16G = 0.012

    def __init__(self, stream):
        self.stream = stream
        eventlet.spawn(self.run)

        self._start_time = None
        self._initial_bias_sum = (0, 0, 0)
        self._initial_bias_count = 0
        self._bias = None
        self._attitude = Quaternion()
        self._estimator = AttitudeEstimator()
        self._display_count = 0
        self._accel_count = 0
        self._mounting = (
            Quaternion.from_euler(math.radians(-90), 0, 0) *
            Quaternion.from_euler(0, math.radians(-60), 0))

    def run(self):
        with self.stream.sem_write:
            # Configure the L3GD20 gyroscope.
            #  20 CTRL_REG1 - 0b00111111 ODR=95Hz Filter=25Hz all enabled
            #  21 CTRL_REG2 - 0b00000000 no high pass filter
            #  22 CTRL_REG3 - 0b00000000 misc options off
            #  23 CTRL_REG4 - 0b10xx0000 BDU=1 FS=x
            self.stream.write('I2C s6bwa03f000080\n')

            # Then configure the LSM303DLHC accelerometer.
            #  20 CTRL_REG1_A 0b01010111 ODR=100Hz low_power=off all enabled
            #  21 CTRL_REG2_A 0b00000000
            #  22 CTRL_REG3_A 0b00000000
            #  23 CTRL_REG4_A 0b11xx1000 BDU=1 (block) BLE=1 (big) FS=x
            #                            HR=1 (high res)
            accel_fs = self.ACCEL_FS_4G
            self.stream.write('I2C s19wA0570000%02X\n' % (
                    0xc8 | (accel_fs << 4)))

            # Then start polling for each of them.
            self.stream.write('STB 1 0a I2C s6bwa7n01p0808r06\n')
            self.stream.write('STB 2 0a I2C s19wa7n01p0808r06\n')

        while True:
            with self.stream.sem_read:
                line = self.stream.readline().strip()
                fields = line.split(' ')
                assert fields[0] == '!STM'
                try:
                    if fields[1] == '01':
                        # gyro
                        self.handle_gyro(fields[4])
                    elif fields[1] == '02':
                        # accel
                        self.handle_accel(fields[4])
                    else:
                        raise RuntimeError('unknown stream!')
                except:
                    print >> sys.stderr, 'when handling:', line
                    raise

    def process_yaw(self, pan):
        self._estimator.process_yaw(self._mounting, pan)

    def handle_gyro(self, hexdata):
        data = parsehex(hexdata)
        pitch_rps = math.radians(-self.parse_rate(data[2], data[1]))
        roll_rps = math.radians(-self.parse_rate(data[4], data[3]))
        yaw_rps = math.radians(-self.parse_rate(data[6], data[5]))

        if self._start_time is None:
            self._start_time = time.time()

        elapsed = time.time() - self._start_time

        if elapsed < 1.0:
            # settling window
            pass
        elif elapsed < 2.0:
            # bias averaging window
            self._initial_bias_sum = (
                self._initial_bias_sum[0] + roll_rps,
                self._initial_bias_sum[1] + pitch_rps,
                self._initial_bias_sum[2] + yaw_rps)
            self._initial_bias_count += 1
        else:
            if self._bias is None:
                self._bias = (
                    self._initial_bias_sum[0] / self._initial_bias_count,
                    self._initial_bias_sum[1] / self._initial_bias_count,
                    self._initial_bias_sum[2] / self._initial_bias_count)

            aroll_rps = roll_rps - self._bias[0]
            apitch_rps = pitch_rps - self._bias[1]
            ayaw_rps = yaw_rps - self._bias[2]

            # this_delta = Quaternion.integrate_rotation_rate(
            #     aroll_rps,
            #     apitch_rps,
            #     ayaw_rps,
            #     IMU_UPDATE_PERIOD)
            # self._attitude = self._attitude * this_delta

            euler = self._attitude.euler()
            if 0:
                sys.stderr.write('%6.2f %6.2f %6.2f  %6.2f %6.2f %6.2f  \r' % (
                        apitch, aroll, ayaw,
                        math.degrees(euler.yaw),
                        math.degrees(euler.pitch),
                        math.degrees(euler.roll)))
                sys.stderr.flush()

        self._estimator.process_gyro(yaw_rps,
                                     pitch_rps,
                                     roll_rps)

    def parse_rate(self, high, low):
        value = (ord(high) << 8) | ord(low)
        if value > 32767:
            value -= 65536
        return value * 0.00875 # 250dps sensitivity

    def handle_accel(self, hexdata):
        # Python is too slow.  Decimate the accelerometer input to get
        # it to fit on one CPU.
        self._accel_count += 1
        if (self._accel_count % 2 != 0):
            return

        data = parsehex(hexdata)
        forward = -self.parse_accel(data[3], data[4])
        up = self.parse_accel(data[5], data[6])
        right = -self.parse_accel(data[1], data[2])

        self._estimator.process_accel(right, forward, up)

        euler = (self._estimator.attitude() * self._mounting).euler()
        bias_deg = [math.degrees(x) for x in self._estimator.gyro_bias()]
        bias_unc = [math.degrees(x) for x in self._estimator.gyro_bias_uncertainty()]
        self._display_count += 1
        if (self._display_count % 5) == 0:
            sys.stderr.write('y/p/r= %6.2f %6.2f %6.2f  bias=%5.3f %5.3f %5.3f  u=%6.2f %6.2f %6.2f  \r' % (
                    math.degrees(euler.yaw),
                    math.degrees(euler.pitch),
                    math.degrees(euler.roll),
                    bias_deg[0], bias_deg[1], bias_deg[2],
                    bias_unc[0], bias_unc[1], bias_unc[2]))

            sys.stderr.flush()

    def parse_accel(self, high, low):
        value = (ord(high) << 8) | ord(low)
        if value > 32767:
            value -= 65536
        return (value / 16) * self.ACCEL_SENS_4G

class MechTurret(object):
    CONFIG_FILE = os.path.expanduser('~/.config/mturret/mturret.ini')

    def __init__(self, serial_debug=None):
        config = ConfigParser.ConfigParser()
        config.read(self.CONFIG_FILE)
        self.servo_config = ServoConfig(config)

        self.fake_servo_port = Pipe(self._write_serial)
        self.fake_imu_port = Pipe(self._write_imu)

        self.controller = servo_controller.servo_controller(
            self.servo_config.servo_type,
            serial_port=self.fake_servo_port)

        self.avr = eventlet_serial.EventletSerial(
            port='/dev/serial/by-id/usb-JoshPieper_avr-aeg_1-if00',
            baudrate=115200,
            logfile=serial_debug)

        eventlet.spawn(self._read_avr)

        for servo in [self.servo_config.servo_pan_id,
                      self.servo_config.servo_tilt_id]:
            self.controller.port.set_position_kp(servo, 200)
            self.controller.port.set_position_kd(servo, 1000)
            self.controller.port.set_position_ki(servo, 400)

        self.controller.enable_power(servo_controller.POWER_ENABLE)

        self.imu = Imu(self.fake_imu_port)

        self._current_laser = 0
        self.toggle_laser()

    def _write_serial(self, data):
        with self.avr.sem_write:
            self.avr.write(
                'SRT %s\n' % ''.join(['%02X' % ord(x) for x in data]))

    def _write_imu(self, data):
        with self.avr.sem_write:
            self.avr.write(data)

    def _read_avr(self):
        while True:
            with self.avr.sem_read:
                line = self.avr.readline().strip()
            line = line.strip()
            if line.startswith('!SRD'):
                # The current servo commands used in mturret never
                # read from the servos, so if any data is emitted,
                # this just blocks forever.  Thus, just ignore all
                # data received from the servos here.
                if 0:
                    hexdata = line[5:]
                    data = parsehex(hexdata)
                    self.fake_servo_port.fill_read(data)
            elif line.startswith('!STM'):
                self.fake_imu_port.fill_read(line + '\n')
            elif line == '':
                pass
            elif not 'OK' in line:
                print >> sys.stderr, 'possible error: "%s"' % line

    def set_orientation(self, yaw_deg, pitch_deg):
        self.controller.set_pose(
            {
                self.servo_config.servo_pan_id :
                    yaw_deg * self.servo_config.servo_pan_sign,
                self.servo_config.servo_tilt_id :
                    pitch_deg * self.servo_config.servo_tilt_sign,
                },
            pose_time=0.3)

    def _toggle_laser(self):
        with self.avr.sem_write:
            self.avr.write('GPC D 4 %d\n' % self._current_laser)
            self._current_laser = self._current_laser ^ 1

    def toggle_laser(self):
        eventlet.spawn(self._toggle_laser)

    def _move_agitator(self):
        with self.avr.sem_write:
            self.avr.write('PWM 0 100 200\n')

    def move_agitator(self):
        eventlet.spawn(self._move_agitator)

    def _fire(self):
        with self.avr.sem_write:
            self.avr.write('PWM 1 200 100\n')

    def fire(self):
        eventlet.spawn(self._fire)

def main():
    parser = optparse.OptionParser()
    parser.add_option('--serial-debug', default=None,
                      help='write all serial to file')
    (options, args) = parser.parse_args()
    turret = MechTurret(serial_debug = options.serial_debug)

    pygame.init()
    pygame.joystick.init()

    clock = pygame.time.Clock()

    joystick = pygame.joystick.Joystick(0)
    joystick.init()

    print "name=", joystick.get_name()
    print "numaxes=", joystick.get_numaxes()
    print "numbuttons=", joystick.get_numbuttons()

    pan = 0.0
    tilt = 0.0
    current_time = time.time()
    ANGLE_STEP = 0.325

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return
            elif event.type == pygame.JOYBUTTONDOWN:
                if event.button == 0:
                    turret.toggle_laser()
                elif event.button == 1:
                    turret.move_agitator()
                elif event.button == 2:
                    turret.fire()
            elif event.type == pygame.JOYHATMOTION:
                if event.value[0] < 0:
                    pan -= ANGLE_STEP
                elif event.value[0] > 0:
                    pan += ANGLE_STEP
                elif event.value[1] < 0:
                    tilt += ANGLE_STEP
                elif event.value[1] > 0:
                    tilt -= ANGLE_STEP

        now = time.time()
        delta = now - current_time
        current_time = now

        x_move = joystick.get_axis(0)
        if abs(x_move) < 0.1:
            x_move = 0.0
        y_move = joystick.get_axis(1)
        if abs(y_move) < 0.1:
            y_move = 0.0

        pan += x_move * 100.0 * delta
        tilt += y_move * 100.0 * delta

        pan = max(-50.0, min(50.0, pan))
        tilt = max(-25.0, min(25.0, tilt))

        turret.set_orientation(pan, tilt)
        turret.imu.process_yaw(math.radians(pan))

        eventlet.sleep(0.05)
        clock.tick(50)


if __name__ == '__main__':
    main()