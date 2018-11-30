#!/usr/bin/python3 -B

# Copyright 2015-2018 Josh Pieper, jjp@pobox.com.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

'''%prog [options]

Interactively display and update values from an embedded device.
'''

import binascii
import optparse
import os
import re
import serial
import struct
import sys
import time

import matplotlib

matplotlib.use('Qt4Agg')
matplotlib.rcParams['backend.qt4'] = 'PySide'

from matplotlib.backends import backend_qt4agg
from matplotlib.backends.backend_qt4agg import FigureCanvasQTAgg as FigureCanvas

from qtconsole.qt import QtCore, QtGui
from qtconsole.history_console_widget import HistoryConsoleWidget

SCRIPT_PATH=os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(SCRIPT_PATH, '../python'))
sys.path.append(os.path.join(SCRIPT_PATH, '../bazel-bin/utils'))

import telemetry_archive
import ui_tview_main_window


LEFT_LEGEND_LOC = 3
RIGHT_LEGEND_LOC = 2

DEFAULT_RATE = 100


def read_varuint(data, offset):
    '''Return (varuint, next_offset)'''

    result = 0
    shift = 0
    for i in range(5):
        if offset >= len(data):
            return None, offset
        this_byte, = struct.unpack('<B', data[offset:offset+1])
        result |= (this_byte & 0x7f) << shift
        shift += 7
        offset += 1

        if (this_byte & 0x80) == 0:
            return result, offset

    assert False


class MultiplexStream:
    def __init__(self, stream, destination_id):
        self._stream = stream
        self._destination_id = destination_id
        self._read_buffer = b''
        self._write_buffer = b''

    def read(self, max_bytes):
        to_return, self._read_buffer = self._read_buffer[0:max_bytes], self._read_buffer[max_bytes:]
        return to_return

    def write(self, data):
        self._write_buffer += data

    def flush(self):
        if len(self._write_buffer):
            self.poll()

    def poll(self):
        header = struct.pack('<HBB', 0xab54, 0x80, self._destination_id)

        to_write = min(len(self._write_buffer), 100)
        payload = struct.pack('<BBB', 0x40, 1, to_write)

        this_write, self._write_buffer = self._write_buffer[0:to_write], self._write_buffer[to_write:]
        payload += this_write

        # So we don't need varuint
        assert len(payload) < 127

        frame = header + struct.pack('<B', len(payload)) + payload

        crc = binascii.crc_hqx(frame, 0xffff)
        frame += struct.pack('<H', crc)

        self._stream.write(frame)

        try:
            self._stream.timeout = 0.02

            result_frame_start = self._stream.read(7)
            if len(result_frame_start) < 7:
                return

            header, source, dest = struct.unpack(
                '<HBB', result_frame_start[:4])
            if header != 0xab54:
                # TODO: resynchronize
                self._stream.read(8192)
                return

            payload_len, payload_offset = read_varuint(result_frame_start, 4)
            if payload_len is None:
                # We don't yet have enough
                return

            result_frame_remainder = self._stream.read(
                6 + (payload_offset - 4) + payload_len - len(result_frame_start))
        finally:
            self._stream.timeout = 0.0

        result_frame = result_frame_start + result_frame_remainder

        if dest != 0x00:
            return

        if source != self._destination_id:
            return

        payload = result_frame[payload_offset:-2]
        if len(payload) < 3:
            return

        sbo = 0
        subframe_id, sbo = read_varuint(payload, sbo)
        channel, sbo = read_varuint(payload, sbo)
        server_len, sbo = read_varuint(payload, sbo)

        if subframe_id is None or channel is None or server_len is None:
            return

        if subframe_id != 0x41:
            return

        if channel != 1:
            return

        payload_rest = payload[sbo:]
        if server_len != len(payload_rest):
            return

        self._read_buffer += payload_rest


# TODO jpieper: Factor these out of tplot.py
def _get_data(value, name):
    fields = name.split('.')
    for field in fields:
        if isinstance(value, list):
            value = value[int(field)]
        else:
            value = getattr(value, field)
    return value


def _set_tree_widget_data(item, struct,
                          getter=lambda x, y: getattr(x, y),
                          required_size=0):
    if item.childCount() < required_size:
        for i in range(item.childCount(), required_size):
            subitem = QtGui.QTreeWidgetItem(item)
            subitem.setText(0, str(i))
    for i in range(item.childCount()):
        child = item.child(i)
        name = child.text(0)

        field = getter(struct, name)
        if isinstance(field, tuple) and child.childCount() > 0:
            _set_tree_widget_data(child, field)
        elif isinstance(field, list):
            _set_tree_widget_data(child, field,
                                  getter=lambda x, y: x[int(y)],
                                  required_size=len(field))
        else:
            child.setText(1, repr(field))


class RecordSignal(object):
    def __init__(self):
        self._index = 0
        self._callbacks = {}

    def connect(self, handler):
        result = self._index
        self._index += 1
        self._callbacks[result] = handler

        class Connection(object):
            def __init__(self, parent, index):
                self.parent = parent
                self.index = index

            def remove(self):
                del self.parent._callbacks[self.index]

        return Connection(self, result)

    def update(self, value):
        for handler in self._callbacks.values():
            handler(value)
        return len(self._callbacks) != 0


class PlotItem(object):
    def __init__(self, axis, plot_widget, name, signal):
        self.axis = axis
        self.plot_widget = plot_widget
        self.name = name
        self.line = None
        self.xdata = []
        self.ydata = []
        self.connection = signal.connect(self._handle_update)

    def _make_line(self):
        line = matplotlib.lines.Line2D([], [])
        line.set_label(self.name)
        line.set_color(self.plot_widget.COLORS[self.plot_widget.next_color])
        self.plot_widget.next_color = (
            self.plot_widget.next_color + 1) % len(self.plot_widget.COLORS)

        self.axis.add_line(line)
        self.axis.legend(loc=self.axis.legend_loc)

        self.line = line

    def remove(self):
        self.line.remove()
        self.connection.remove()
        # NOTE jpieper: matplotlib gives us no better way to remove a
        # legend.
        if len(self.axis.lines) == 0:
            self.axis.legend_ = None
        else:
            self.axis.legend(loc=self.axis.legend_loc)
        self.plot_widget.canvas.draw()

    def _handle_update(self, value):
        if self.plot_widget.paused:
            return

        if self.line is None:
            self._make_line()

        now = time.time()
        self.xdata.append(now)
        self.ydata.append(value)

        # Remove elements from the beginning until there is at most
        # one before the window.
        oldest_time = now - self.plot_widget.history_s
        oldest_index = None
        for i in range(len(self.xdata)):
            if self.xdata[i] >= oldest_time:
                oldest_index = i - 1
                break

        if oldest_index and oldest_index > 1:
            self.xdata = self.xdata[oldest_index:]
            self.ydata = self.ydata[oldest_index:]

        self.line.set_data(self.xdata, self.ydata)

        self.axis.relim()
        self.axis.autoscale()

        self.plot_widget.data_update()


class PlotWidget(QtGui.QWidget):
    COLORS = 'rbgcmyk'

    def __init__(self, parent=None):
        QtGui.QWidget.__init__(self, parent)

        self.history_s = 20.0
        self.next_color = 0
        self.paused = False

        self.last_draw_time = 0.0

        self.figure = matplotlib.figure.Figure()
        self.canvas = FigureCanvas(self.figure)

        self.canvas.mpl_connect('key_press_event', self.handle_key_press)
        self.canvas.mpl_connect('key_release_event', self.handle_key_release)

        self.left_axis = self.figure.add_subplot(111)
        self.left_axis.grid()
        self.left_axis.fmt_xdata = lambda x: '%.3f' % x

        self.left_axis.legend_loc = LEFT_LEGEND_LOC

        self.right_axis = None

        def draw():
            # NOTE jpieper: For some reason, on the first repaint
            # event, the height is negative, which throws spurious
            # errors.  Paper over that here.
            l, b, w, h = self.figure.bbox.bounds
            if h < 0:
                return
            FigureCanvas.draw(self.canvas)
            self.canvas.repaint()

        self.canvas.draw = draw

        self.toolbar = backend_qt4agg.NavigationToolbar2QT(self.canvas, self)
        self.pause_action = QtGui.QAction(u'Pause', self)
        self.pause_action.setCheckable(True)
        self.pause_action.toggled.connect(self._handle_pause)
        self.toolbar.addAction(self.pause_action)

        layout = QtGui.QVBoxLayout(self)
        layout.addWidget(self.toolbar, 0)
        layout.addWidget(self.canvas, 1)

        self.canvas.setFocusPolicy(QtCore.Qt.ClickFocus)

    def _handle_pause(self, value):
        self.paused = value

    def add_plot(self, name, signal, axis_number):
        axis = self.left_axis
        if axis_number == 1:
            if self.right_axis is None:
                self.right_axis = self.left_axis.twinx()
                self.right_axis.legend_loc = RIGHT_LEGEND_LOC
            axis = self.right_axis
        item = PlotItem(axis, self, name, signal)
        return item

    def remove_plot(self, item):
        item.remove()

    def data_update(self):
        now = time.time()
        elapsed = now - self.last_draw_time
        if elapsed > 0.1:
            self.last_draw_time = now
            self.canvas.draw()

    def _get_axes_keys(self):
        result = []
        result.append(('1', self.left_axis))
        if self.right_axis:
            result.append(('2', self.right_axis))
        return result

    def handle_key_press(self, event):
        if event.key not in ['1', '2']:
            return
        for key, axis in self._get_axes_keys():
            if key == event.key:
                axis.set_navigate(True)
            else:
                axis.set_navigate(False)

    def handle_key_release(self, event):
        if event.key not in ['1', '2']:
            return
        for key, axis in self._get_axes_keys():
            axis.set_navigate(True)


class SizedTreeWidget(QtGui.QTreeWidget):
    def __init__(self, parent=None):
        QtGui.QTreeWidget.__init__(self, parent)
        self.setColumnCount(2)
        self.headerItem().setText(0, 'Name')
        self.headerItem().setText(1, 'Value')

    def sizeHint(self):
        return QtCore.QSize(350, 500)


class TviewConsoleWidget(HistoryConsoleWidget):
    line_input = QtCore.Signal(str)

    def __init__(self, *args, **kw):
        super(TviewConsoleWidget, self).__init__(*args, **kw)

        self._prompt = '>>> '
        self.clear()

        # The bionic version of ConsoleWidget seems to get the cursor
        # position screwed up after a clear.  Let's just fix it up
        # here.
        self._append_before_prompt_cursor.setPosition(0)

    def sizeHint(self):
        return QtCore.QSize(600, 200)

    def add_text(self, data):
        assert data.endswith('\n') or data.endswith('\r')
        self._append_plain_text(data, before_prompt=True)
        self._control.moveCursor(QtGui.QTextCursor.End)

    def _handle_timeout(self):
        self._append_plain_text('%s\r\n' % time.time(),
                                before_prompt=True)
        self._control.moveCursor(QtGui.QTextCursor.End)

    def _is_complete(self, source, interactive):
        return True, False

    def _execute(self, source, hidden):
        self.line_input.emit(source)
        self._show_prompt(self._prompt)
        return True


class Record:
    def __init__(self, archive):
        self.archive = archive
        self.tree_item = None
        self.signals = {}

    def get_signal(self, name):
        if name not in self.signals:
            self.signals[name] = RecordSignal()

        return self.signals[name]

    def update(self, struct):
        count = 0
        for key, signal in self.signals.items():
            value = _get_data(struct, key)
            if signal.update(value):
                count += 1
        return count != 0


class NoEditDelegate(QtGui.QStyledItemDelegate):
    def __init__(self, parent=None):
        QtGui.QStyledItemDelegate.__init__(self, parent=parent)

    def createEditor(self, parent, option, index):
        return None


def _get_item_name(item):
    name = item.text(0)
    while item.parent() and item.parent().parent():
        name = item.parent().text(0) + '.' + name
        item = item.parent()

    return name


def _get_item_root(item):
    while item.parent().parent():
        item = item.parent()
    return item.text(0)


class Device:
    STATE_LINE = 0
    STATE_CONFIG = 1
    STATE_TELEMETRY = 2
    STATE_SCHEMA = 3
    STATE_DATA = 4

    def __init__(self, stream, console, prefix,
                 config_tree_item, data_tree_item):
        self._stream = stream
        self._console = console
        self._prefix = prefix
        self._config_tree_item = config_tree_item
        self._data_tree_item = data_tree_item

        self._buffer = b''
        self._serial_state = self.STATE_LINE
        self._telemetry_records = {}
        self._schema_name = None
        self._config_tree_items = {}
        self._config_callback = None

        self._start_time = None

    def start(self):
        # Stop the spew.
        self._stream.write('\r\n'.encode('latin1'))
        self._stream.write('tel stop\r\n'.encode('latin1'))

        # We want to wait a little bit, discard everything we have
        # received, and then initialize the device.
        self._start_time = time.time()

    def _setup_device(self, callback):
        # When we start, get a listing of all configuration options
        # and all available telemetry channels.
        def after_config():
            self.update_telemetry(callback)
        self.update_config(after_config)

    def poll(self):
        self._stream.poll()

        if self._start_time is not None:
            now = time.time()
            if now - self._start_time < 0.2:
                return
            # Discard any junk that may be there.
            self._stream.read(8192)
            self._start_time = None

            self._setup_device(None)


        data = self._stream.read(8192)

        self._buffer += data

        while True:
            old_len = len(self._buffer)
            self._handle_serial_data()
            if len(self._buffer) == old_len:
                break

        self._stream.flush()

    def write(self, data):
        self._stream.write(data)

    def config_item_changed(self, name, value):
        if self._serial_state == self.STATE_CONFIG:
            return

        self.write_line('conf set %s %s\r\n' % (name, value))

    def _handle_serial_data(self):
        if self._serial_state == self.STATE_LINE:
            self._handle_serial_line()
        elif self._serial_state == self.STATE_CONFIG:
            self._handle_config()
        elif self._serial_state == self.STATE_TELEMETRY:
            self._handle_telemetry()
        elif self._serial_state == self.STATE_SCHEMA:
            self._handle_schema()
        elif self._serial_state == self.STATE_DATA:
            self._handle_data()
        else:
            assert False

    def _handle_serial_line(self):
        line = self._get_serial_line()
        if line is None:
            return

        line = line.decode('latin1')

        display = True
        if line == '':
            display = False

        if line.startswith('schema '):
            self._serial_state = self.STATE_SCHEMA
            self._schema_name = line.split(' ', 1)[1].strip()
        elif line.startswith('emit '):
            self._serial_state = self.STATE_DATA
            self._schema_name = line.split(' ', 1)[1].strip()
            display = False

        if display:
            self._console.add_text(self._prefix + line + '\n')

    def _get_serial_line(self):
        # Consume any newlines at the start of our buffer.
        pos = 0
        while pos < len(self._buffer) and self._buffer[pos] in b'\r\n':
            pos += 1
        self._buffer = self._buffer[pos:]

        # Look for a trailing newline
        end = 0
        while end < len(self._buffer) and self._buffer[end] not in b'\r\n':
            end += 1

        if end >= len(self._buffer):
            return

        line, self._buffer = self._buffer[:end], self._buffer[end+1:]

        return line

    def update_config(self, callback):
        # Clear out our config tree.
        self._config_tree_item.takeChildren()
        self._config_tree_items = {}

        self._config_callback = callback
        self.write_line('conf enumerate\r\n')

        # TODO jpieper: In the current protocol this is racy, as there
        # is no header on the config enumeration.  I should probably
        # add one.
        self._serial_state = self.STATE_CONFIG

    def _handle_config(self):
        line = self._get_serial_line()
        if not line:
            return

        line = line.decode('latin1')
        self._console.add_text(self._prefix + line + '\n')

        if line.startswith('OK'):
            # We're done with config now.
            self._serial_state = self.STATE_LINE
            cbk, self._config_callback = self._config_callback, None
            if cbk:
                cbk()
        else:
            # Add it into our tree view.
            key, value = line.split(' ', 1)
            name, rest = key.split('.', 1)
            if name not in self._config_tree_items:
                item = QtGui.QTreeWidgetItem(self._config_tree_item)
                item.setText(0, name)
                self._config_tree_items[name] = item

            def add_config(item, key, value):
                if key == '':
                    item.setText(1, value)
                    item.setFlags(QtCore.Qt.ItemIsEditable |
                                  QtCore.Qt.ItemIsSelectable |
                                  QtCore.Qt.ItemIsEnabled)
                    return

                fields = key.split('.', 1)
                this_field = fields[0]
                next_key = ''
                if len(fields) > 1:
                    next_key = fields[1]

                child = None
                # See if we already have an appropriate child.
                for i in range(item.childCount()):
                    if item.child(i).text(0) == this_field:
                        child = item.child(i)
                        break
                if child is None:
                    child = QtGui.QTreeWidgetItem(item)
                    child.setText(0, this_field)
                add_config(child, next_key, value)

            add_config(self._config_tree_items[name], rest, value)

            # TODO(jpieper)
            # self.ui.configTreeWidget.resizeColumnToContents(0)

    def update_telemetry(self, callback):
        self._data_tree_item.takeChildren()
        self._telemetry_records = {}

        self._telemetry_callback = callback
        self.write_line('tel list\r\n')

        self._serial_state = self.STATE_TELEMETRY

    def write_line(self, line):
        self._console.add_text(self._prefix + line)
        self._stream.write(line.encode('latin1'))

    def _handle_telemetry(self):
        line = self._get_serial_line()
        if not line:
            return

        line = line.decode('latin1')
        self._console.add_text(self._prefix + line + '\n')

        if line.startswith('OK'):
            # Now we need to start getting schemas.
            self._serial_state = self.STATE_LINE
            self._update_schema()
        else:
            name = line.strip()
            self._telemetry_records[name] = None

    def _update_schema(self):
        # Find a channel we don't have a schema for and request it.
        for name in self._telemetry_records.keys():
            if self._telemetry_records[name] is None:
                self.write_line('tel schema %s\r\n' % name)
                self._serial_state = self.STATE_LINE
                return

        self._serial_state = self.STATE_LINE
        # Guess we are done.  Update our tree view.

        # TODO(jpieper)
        # self.ui.telemetryTreeWidget.resizeColumnToContents(0)

        cbk, self._telemetry_callback = self._telemetry_callback, None
        if cbk:
            cbk()

    def _handle_schema(self):
        schema = self._handle_sized_block()
        if not schema:
            return

        name, self._schema_name = self._schema_name, None

        if name in self._telemetry_records:
            if self._telemetry_records[name]:
                return

        archive = telemetry_archive.ReadArchive(schema, name)

        record = Record(archive)
        self._telemetry_records[name] = record
        record.tree_item = self._add_schema_to_tree(name, archive, record)

        self._console.add_text(self._prefix + '<schema name=%s>\n' % name)

        # Now look to see if there are any more we should request.
        self._update_schema()

    def _handle_data(self):
        data = self._handle_sized_block()
        if not data:
            return

        name, self._schema_name = self._schema_name, None

        if name not in self._telemetry_records:
            return

        record = self._telemetry_records[name]
        if record:
            struct = record.archive.deserialize(data)
            record.update(struct)
            _set_tree_widget_data(record.tree_item, struct)

        self._serial_state = self.STATE_LINE

    def _handle_sized_block(self):
        # Wait until we have the complete schema in the buffer.  It
        # will start with the final newline from the first line.
        if len(self._buffer) < 5:
            return

        size = struct.unpack('<I', self._buffer[1:5])[0]
        if size > 2 ** 24:
            # Whoops, probably bogus.
            print('Invalid schema size, skipping whatever we were doing.')
            self._serial_state = self.STATE_LINE
            return

        if len(self._buffer) < 5 + size:
            return

        block = self._buffer[5:5+size]
        self._buffer = self._buffer[5+size:]
        return block

    class Schema:
        def __init__(self, name, parent, record):
            self._name = name
            self._parent = parent
            self.record = record

        def expand(self):
            self._parent.write_line('tel fmt %s 0\r\n' % self._name)
            self._parent.write_line('tel rate %s %d\r\n' %
                                    (self._name, DEFAULT_RATE))

        def collapse(self):
            self._parent.write_line('tel rate %s 0\r\n' % self._name)


    def _add_schema_to_tree(self, name, archive, record):
        item = QtGui.QTreeWidgetItem(self._data_tree_item)
        item.setText(0, name)

        schema = Device.Schema(name, self, record)
        item.setData(0, QtCore.Qt.UserRole, schema)

        # TODO jpieper: Factor this out of tplot.py.
        def add_item(parent, element):
            if 'fields' not in element:
                return
            for field in element['fields']:
                name = field['name']

                item = QtGui.QTreeWidgetItem(parent)
                item.setText(0, name)

                if 'children' in field:
                    for child in field['children']:
                        add_item(item, child)

        add_item(item, archive.root)
        return item



class TviewMainWindow(QtGui.QMainWindow):

    def __init__(self, options, parent=None):
        QtGui.QMainWindow.__init__(self, parent)

        self.options = options
        self.port = None
        self.devices = []
        self.default_rate = 100

        self._serial_timer = QtCore.QTimer()
        self._serial_timer.timeout.connect(self._poll_serial)
        self._serial_timer.start(10)

        self.ui = ui_tview_main_window.Ui_TviewMainWindow()
        self.ui.setupUi(self)

        self.ui.configTreeWidget = SizedTreeWidget()
        self.ui.configDock.setWidget(self.ui.configTreeWidget)

        self.ui.telemetryTreeWidget = SizedTreeWidget()
        self.ui.telemetryDock.setWidget(self.ui.telemetryTreeWidget)

        self.ui.telemetryTreeWidget.itemExpanded.connect(
            self._handle_tree_expanded)
        self.ui.telemetryTreeWidget.itemCollapsed.connect(
            self._handle_tree_collapsed)
        self.ui.telemetryTreeWidget.setContextMenuPolicy(
            QtCore.Qt.CustomContextMenu)
        self.ui.telemetryTreeWidget.customContextMenuRequested.connect(
            self._handle_telemetry_context_menu)

        self.ui.configTreeWidget.setItemDelegateForColumn(
            0, NoEditDelegate(self))
        self.ui.configTreeWidget.itemExpanded.connect(
            self._handle_config_expanded)
        self.ui.configTreeWidget.itemChanged.connect(
            self._handle_config_item_changed)

        self.ui.plotItemRemoveButton.clicked.connect(
            self._handle_plot_item_remove)

        self.console = TviewConsoleWidget()
        self.console.ansi_codes = False
        self.console.line_input.connect(self._handle_user_input)
        self.ui.consoleDock.setWidget(self.console)

        self.tabifyDockWidget(self.ui.configDock, self.ui.telemetryDock)

        layout = QtGui.QVBoxLayout(self.ui.plotHolderWidget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.ui.plotHolderWidget.setLayout(layout)
        self.ui.plotWidget = PlotWidget(self.ui.plotHolderWidget)
        layout.addWidget(self.ui.plotWidget)

        def update_plotwidget(value):
            self.ui.plotWidget.history_s = value
        self.ui.historySpin.valueChanged.connect(update_plotwidget)

        QtCore.QTimer.singleShot(0, self._handle_startup)

    def _open(self):
        self.port = serial.Serial(
            port=self.options.serial,
            baudrate=self.options.baudrate,
            timeout=0.0)

        self.devices = []
        self.ui.configTreeWidget.clear()
        self.ui.telemetryTreeWidget.clear()

        for device_id in [int(x) for x in self.options.devices.split(',')]:
            stream = MultiplexStream(self.port, device_id)

            config_item = QtGui.QTreeWidgetItem()
            config_item.setText(0, str(device_id))
            self.ui.configTreeWidget.addTopLevelItem(config_item)

            data_item = QtGui.QTreeWidgetItem()
            data_item.setText(0, str(device_id))
            self.ui.telemetryTreeWidget.addTopLevelItem(data_item)

            device = Device(stream, self.console, '{}>'.format(device_id),
                            config_item,
                            data_item)

            config_item.setData(0, QtCore.Qt.UserRole, device)
            device.start()

            self.devices.append(device)

    def _handle_startup(self):
        self.console._control.setFocus()

    def _poll_serial(self):
        if self.port is None:
            if os.path.exists(self.options.serial):
                self._open()
            else:
                return
        else:
            [x.poll() for x in self.devices]

    def _handle_user_input(self, line):
        if self.devices:
            self.devices[0].write((line + '\n').encode('latin1'))

    def _handle_tree_expanded(self, item):
        self.ui.telemetryTreeWidget.resizeColumnToContents(0)
        user_data = item.data(0, QtCore.Qt.UserRole)
        if user_data:
            user_data.expand()

    def _handle_tree_collapsed(self, item):
        user_data = item.data(0, QtCore.Qt.UserRole)
        if user_data:
            user_data.collapse()

    def _handle_telemetry_context_menu(self, pos):
        item = self.ui.telemetryTreeWidget.itemAt(pos)
        if item.childCount() > 0:
            return

        menu = QtGui.QMenu(self)
        left_action = menu.addAction('Plot Left')
        right_action = menu.addAction('Plot Right')
        menu.addSeparator()
        copy_name = menu.addAction('Copy Name')
        copy_value = menu.addAction('Copy Value')

        requested = menu.exec_(self.ui.telemetryTreeWidget.mapToGlobal(pos))

        if requested == left_action or requested == right_action:
            top = item
            while top.parent().parent():
                top = top.parent()

            schema = top.data(0, QtCore.Qt.UserRole)
            record = schema.record

            name = _get_item_name(item)
            root = _get_item_root(item)

            leaf = name.split('.', 1)[1]
            axis = 0
            if requested == right_action:
                axis = 1
            plot_item = self.ui.plotWidget.add_plot(
                name, record.get_signal(leaf), axis)
            self.ui.plotItemCombo.addItem(name, plot_item)
        elif requested == copy_name:
            QtGui.QApplication.clipboard().setText(item.text(0))
        elif requested == copy_value:
            QtGui.QApplication.clipboard().setText(item.text(1))
        else:
            # The user cancelled.
            pass

    def _handle_config_expanded(self, item):
        self.ui.configTreeWidget.resizeColumnToContents(0)

    def _handle_config_item_changed(self, item, column):
        if not item.parent():
            return

        top = item
        while top.parent():
            top = top.parent()

        device = top.data(0, QtCore.Qt.UserRole)
        device.config_item_changed(_get_item_name(item), item.text(1))

    def _handle_plot_item_remove(self):
        index = self.ui.plotItemCombo.currentIndex()

        if index < 0:
            return

        item = self.ui.plotItemCombo.itemData(index)
        self.ui.plotWidget.remove_plot(item)
        self.ui.plotItemCombo.removeItem(index)


def main():
    usage, description = __doc__.split('\n\n', 1)
    parser = optparse.OptionParser(usage=usage, description=description)

    parser.add_option('--serial', '-s', default='/dev/ttyACM0')
    parser.add_option('--baudrate', '-b', type='int', default=115200)
    parser.add_option('--devices', '-d', type='str', default='1')

    options, args = parser.parse_args()
    assert len(args) == 0

    app = QtGui.QApplication(sys.argv)

    tv = TviewMainWindow(options)
    tv.show()

    app.exec_()


if __name__ == '__main__':
    main()
