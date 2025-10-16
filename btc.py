# crypto_ticker.py
"""
实时自定义虚拟币价格监视器（Binance spot）
功能：
 - 显示自定义币对（例如 BTCUSDT、ETHUSDT）的现价（表格显示为基础币，例如 BTC）
 - 计算并显示 UTC+0 日内涨跌%、以及用户自定义的若干分钟周期的涨幅（以分钟为单位，例如 60,240,1440）
 - 刷新间隔以秒为单位设置（界面显示秒）
 - 支持自定义网络代理（socks5/http/https）
 - GUI（PyQt5），可置顶（Always on Top）
 - 可导出/导入自定义币对列表，保存配置到 config.json
 - 支持表格拖拽排序并自动保存
 - 立即刷新按钮
"""
import sys
import time
import json
import math
import threading
from datetime import datetime, timedelta, timezone

import requests
from PyQt5 import QtWidgets, QtCore, QtGui

CONFIG_FILE = "config.json"
BINANCE_BASE = "https://api.binance.com"

DEFAULT_CONFIG = {
    "symbols": ["BTCUSDT", "ETHUSDT"],
    "refresh_interval_sec": 5,
    "proxies": {},
    "window_always_on_top": True,
    "window_geometry": None,
    "period_minutes": [60, 240, 1440]  # default comparison periods in minutes
}

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            for k,v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            # normalize period_minutes to list of ints
            try:
                cfg["period_minutes"] = [int(x) for x in cfg.get("period_minutes", [])]
            except Exception:
                cfg["period_minutes"] = DEFAULT_CONFIG["period_minutes"][:]
            return cfg
    except Exception:
        return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存配置失败：", e)

def ms_floor_to_minute(ts_ms):
    return int(ts_ms // (60*1000) * (60*1000))

class BinanceFetcher:
    def __init__(self, proxies=None, session=None, timeout=8):
        self.proxies = proxies or {}
        self.timeout = timeout
        self.session = session or requests.Session()
        self.set_proxies(self.proxies)

    def set_proxies(self, proxies: dict):
        self.proxies = proxies or {}
        p = {}
        # socks5 key maps to both http/https if present
        if "socks5" in self.proxies and self.proxies["socks5"]:
            p["http"] = self.proxies["socks5"]
            p["https"] = self.proxies["socks5"]
        if "http" in self.proxies and self.proxies["http"]:
            p["http"] = self.proxies["http"]
        if "https" in self.proxies and self.proxies["https"]:
            p["https"] = self.proxies["https"]
        self.session.proxies = p

    def get_current_price(self, symbol):
        url = BINANCE_BASE + "/api/v3/ticker/price"
        r = self.session.get(url, params={"symbol": symbol}, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        return float(data["price"])

    def get_klines_at(self, symbol, target_dt: datetime):
        ts_ms = int(target_dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
        ts_floor = ms_floor_to_minute(ts_ms)
        url = BINANCE_BASE + "/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": ts_floor,
            "limit": 1
        }
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        arr = r.json()
        if not arr:
            return None
        k = arr[0]
        return {
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "close_time": k[6]
        }

    def get_close_price_at(self, symbol, dt: datetime):
        k = self.get_klines_at(symbol, dt)
        if not k:
            return None
        return k["close"]

class WorkerThread(QtCore.QObject):
    updated = QtCore.pyqtSignal(dict)  # symbol -> {price, changes: {period_min: pct, ...}, utc_day: pct}
    error = QtCore.pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        self.cfg = config
        self._running = False
        self.fetcher = BinanceFetcher(self.cfg.get("proxies", {}))
        self._lock = threading.Lock()

    def update_config(self, cfg):
        with self._lock:
            self.cfg = cfg
            self.fetcher.set_proxies(self.cfg.get("proxies", {}))

    @QtCore.pyqtSlot()
    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=self.loop, daemon=True).start()

    def stop(self):
        self._running = False

    def loop(self):
        while self._running:
            try:
                self.fetch_once()
            except Exception as e:
                self.error.emit(f"主循环错误：{e}")
            with self._lock:
                interval_sec = int(self.cfg.get("refresh_interval_sec", 5))
            interval_sec = max(1, interval_sec)
            for _ in range(interval_sec):
                if not self._running:
                    break
                time.sleep(1)

    @QtCore.pyqtSlot()
    def fetch_once(self):
        with self._lock:
            cfg_copy = dict(self.cfg)
        try:
            symbols = list(dict.fromkeys(cfg_copy.get("symbols", [])))
            period_minutes = list(cfg_copy.get("period_minutes", []))
            res = {}
            now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
            # utc day start
            day0 = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            # compute datetimes for periods
            period_dts = {pm: now_utc - timedelta(minutes=pm) for pm in period_minutes}
            for s in symbols:
                try:
                    price = self.fetcher.get_current_price(s)
                except Exception as e:
                    self.error.emit(f"获取 {s} 现价失败：{e}")
                    price = None
                changes = {}
                def pct_from_price(nowp, oldp):
                    try:
                        if nowp is None or oldp is None or oldp == 0:
                            return None
                        return (nowp - oldp) / oldp * 100.0
                    except Exception:
                        return None
                # periods
                for pm, dt in period_dts.items():
                    try:
                        oldp = self.fetcher.get_close_price_at(s, dt)
                    except Exception as e:
                        self.error.emit(f"获取 {s} {pm}m 历史价失败：{e}")
                        oldp = None
                    changes[str(pm)] = pct_from_price(price, oldp)
                # utc day
                try:
                    pday0 = self.fetcher.get_close_price_at(s, day0)
                except Exception as e:
                    self.error.emit(f"获取 {s} UTC日初价失败：{e}")
                    pday0 = None
                res[s] = {
                    "price": price,
                    "changes": changes,  # keyed by minute string, e.g. "60"
                    "utc_day": pct_from_price(price, pday0),
                    "timestamp": int(time.time())
                }
            self.updated.emit(res)
        except Exception as e:
            self.error.emit(f"fetch_once 错误：{e}")

class DraggableTable(QtWidgets.QTableWidget):
    rowsReordered = QtCore.pyqtSignal()
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.rowsReordered.emit()

def display_base_symbol(full_symbol: str) -> str:
    # remove common quote suffixes for display
    qs = ["USDT", "USDC", "BUSD", "TUSD", "EUR", "BTC", "ETH"]
    s = full_symbol.upper()
    for q in qs:
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)]
    # fallback: if contains '-', '/', show left part
    for sep in ['-', '/']:
        if sep in s:
            return s.split(sep)[0]
    return s

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.setWindowTitle("实时虚拟币价格（Binance）")
        self.resize(840, 480)

        w = QtWidgets.QWidget()
        self.setCentralWidget(w)
        layout = QtWidgets.QVBoxLayout(w)

        # top input
        top_h = QtWidgets.QHBoxLayout()
        self.symbol_edit = QtWidgets.QLineEdit()
        self.symbol_edit.setPlaceholderText("输入币对，英文逗号分隔，例如：BTCUSDT,ETHUSDT，然后按【添加】")
        top_h.addWidget(self.symbol_edit)
        add_btn = QtWidgets.QPushButton("添加")
        add_btn.clicked.connect(self.add_symbols)
        top_h.addWidget(add_btn)
        remove_btn = QtWidgets.QPushButton("移除选中")
        remove_btn.clicked.connect(self.remove_selected)
        top_h.addWidget(remove_btn)
        settings_btn = QtWidgets.QPushButton("设置")
        settings_btn.clicked.connect(self.open_settings)
        top_h.addWidget(settings_btn)
        refresh_now_btn = QtWidgets.QPushButton("立即刷新行情")
        refresh_now_btn.clicked.connect(self.immediate_refresh)
        top_h.addWidget(refresh_now_btn)
        layout.addLayout(top_h)

        # table
        self.table = DraggableTable(0, 2)  # we'll set columns properly later
        self.table.setSelectionBehavior(QtWidgets.QTableWidget.SelectRows)
        self.table.rowsReordered.connect(self.on_table_reordered)
        layout.addWidget(self.table)

        # bottom: controls (seconds-based refresh)
        bottom_h = QtWidgets.QHBoxLayout()
        self.top_checkbox = QtWidgets.QCheckBox("窗口置顶")
        self.top_checkbox.setChecked(self.cfg.get("window_always_on_top", True))
        self.top_checkbox.stateChanged.connect(self.toggle_always_on_top)
        bottom_h.addWidget(self.top_checkbox)

        refresh_lbl = QtWidgets.QLabel("刷新间隔(s):")
        bottom_h.addWidget(refresh_lbl)
        self.refresh_spin = QtWidgets.QSpinBox()
        self.refresh_spin.setRange(1, 3600)
        self.refresh_spin.setValue(self.cfg.get("refresh_interval_sec", 5))
        self.refresh_spin.valueChanged.connect(self.change_refresh_interval)
        bottom_h.addWidget(self.refresh_spin)
        plus1s = QtWidgets.QPushButton("+1s")
        plus1s.clicked.connect(lambda: self.adjust_seconds(1))
        bottom_h.addWidget(plus1s)
        plus5s = QtWidgets.QPushButton("+5s")
        plus5s.clicked.connect(lambda: self.adjust_seconds(5))
        bottom_h.addWidget(plus5s)
        minus1s = QtWidgets.QPushButton("-1s")
        minus1s.clicked.connect(lambda: self.adjust_seconds(-1))
        bottom_h.addWidget(minus1s)
        minus5s = QtWidgets.QPushButton("-5s")
        minus5s.clicked.connect(lambda: self.adjust_seconds(-5))
        bottom_h.addWidget(minus5s)

        export_btn = QtWidgets.QPushButton("导出币对(.csv)")
        export_btn.clicked.connect(self.export_symbols)
        bottom_h.addWidget(export_btn)
        import_btn = QtWidgets.QPushButton("导入币对(.csv)")
        import_btn.clicked.connect(self.import_symbols)
        bottom_h.addWidget(import_btn)

        layout.addLayout(bottom_h)

        # statusbar
        self.status = self.statusBar()

        # prepare table headers based on periods
        self.build_table_headers()

        # populate symbols from config
        for s in self.cfg.get("symbols", []):
            self.add_symbol_to_table(s)

        # worker thread
        self.worker = WorkerThread(self.cfg)
        self.thread = QtCore.QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.updated.connect(self.on_update)
        self.worker.error.connect(self.on_error)
        self.thread.start()

        # periodic push of config to worker (in case UI changed)
        self.cfg_timer = QtCore.QTimer()
        self.cfg_timer.setInterval(2000)
        self.cfg_timer.timeout.connect(self.push_config_to_worker)
        self.cfg_timer.start()

        self.toggle_always_on_top(self.top_checkbox.checkState())

    def build_table_headers(self):
        # create headers: Symbol | Price | UTC日初% | Δ{pm}m%... based on cfg period_minutes
        periods = list(self.cfg.get("period_minutes", []))
        # We'll show UTC day as a column after Price, then the periods in descending order for readability
        period_labels = [f"Δ{pm}m%" for pm in periods]
        headers = ["Symbol", "Price", "UTC日初%"] + period_labels
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setStretchLastSection(True)

    def closeEvent(self, event):
        geom = self.saveGeometry().toHex().data().decode()
        self.cfg["window_geometry"] = geom
        save_config(self.cfg)
        self.worker.stop()
        self.thread.quit()
        self.thread.wait(2000)
        event.accept()

    def push_config_to_worker(self):
        self.cfg["refresh_interval_sec"] = int(self.refresh_spin.value())
        self.cfg["symbols"] = self.get_table_symbols()  # full symbols
        # ensure period_minutes normalized
        try:
            self.cfg["period_minutes"] = [int(x) for x in self.cfg.get("period_minutes", [])]
        except Exception:
            self.cfg["period_minutes"] = DEFAULT_CONFIG["period_minutes"][:]
        self.worker.update_config(self.cfg)

    def add_symbols(self):
        text = self.symbol_edit.text().strip()
        if not text:
            return
        parts = [p.strip().upper() for p in text.replace("，", ",").split(",") if p.strip()]
        added = 0
        for s in parts:
            if self.add_symbol_to_table(s):
                added += 1
        self.symbol_edit.clear()
        if added:
            # save immediately
            self.cfg["symbols"] = self.get_table_symbols()
            save_config(self.cfg)
            self.status.showMessage(f"已添加 {added} 个币对并保存配置", 4000)

    def add_symbol_to_table(self, full_symbol: str) -> bool:
        # avoid duplicates by full symbol
        existing = self.get_table_symbols()
        full_symbol = full_symbol.upper()
        if full_symbol in existing:
            return False
        row = self.table.rowCount()
        self.table.insertRow(row)
        display = display_base_symbol(full_symbol)
        item_sym = QtWidgets.QTableWidgetItem(display)
        # store full symbol in user role
        item_sym.setData(QtCore.Qt.UserRole, full_symbol)
        self.table.setItem(row, 0, item_sym)
        self.table.setItem(row, 1, QtWidgets.QTableWidgetItem("-"))
        # other columns: first UTC day then periods
        col_count = self.table.columnCount()
        for c in range(2, col_count):
            it = QtWidgets.QTableWidgetItem("-")
            it.setTextAlignment(QtCore.Qt.AlignCenter)
            self.table.setItem(row, c, it)
        return True

    def remove_selected(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for r in rows:
            self.table.removeRow(r)
        self.cfg["symbols"] = self.get_table_symbols()
        save_config(self.cfg)
        self.status.showMessage(f"已移除 {len(rows)} 行并保存配置", 4000)

    def get_table_symbols(self):
        syms = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                full = item.data(QtCore.Qt.UserRole)
                if not full:
                    # fallback to cell text upper
                    full = item.text().strip().upper()
                syms.append(full)
        return syms

    def on_update(self, data: dict):
        # data keyed by full symbol strings
        period_keys = [str(pm) for pm in self.cfg.get("period_minutes", [])]
        for r in range(self.table.rowCount()):
            item_sym = self.table.item(r, 0)
            if not item_sym:
                continue
            full = item_sym.data(QtCore.Qt.UserRole) or item_sym.text().upper()
            info = data.get(full)
            if not info:
                continue
            price = info.get("price")
            if price is not None:
                self.table.item(r, 1).setText(f"{price:.8f}".rstrip("0").rstrip("."))
            # UTC day at column 2
            utc_val = info.get("utc_day")
            utc_cell = self.table.item(r, 2)
            if utc_val is None:
                utc_cell.setText("-")
                utc_cell.setForeground(QtGui.QBrush(QtGui.QColor("black")))
            else:
                txt = f"{utc_val:+.2f}%"
                utc_cell.setText(txt)
                if utc_val > 0:
                    utc_cell.setForeground(QtGui.QBrush(QtGui.QColor("#FF0000")))
                elif utc_val < 0:
                    utc_cell.setForeground(QtGui.QBrush(QtGui.QColor("#008000")))
                else:
                    utc_cell.setForeground(QtGui.QBrush(QtGui.QColor("black")))
            # period columns
            for i, pk in enumerate(period_keys):
                col = 3 + i - 1  # careful: headers were ["Symbol","Price","UTC日初%"] + periods -> periods start at col 3 (index 2 is UTC)
                # compute correct column: UTC is col 2, so first period col = 3
                col = 3 + i - 1 + 0  # adjust: simplify below
            # simpler: loop using header offset
            for idx, pk in enumerate(period_keys):
                col = 3 + idx - 1  # same fix: let's compute as 3+idx-1 => 2+idx? fix below
                # Actually correct mapping: Symbol(0),Price(1),UTC(2), period0(3), period1(4) ...
                col = 3 + idx
                cell = self.table.item(r, col)
                val = info.get("changes", {}).get(pk)
                if val is None:
                    cell.setText("-")
                    cell.setForeground(QtGui.QBrush(QtGui.QColor("black")))
                else:
                    text = f"{val:+.2f}%"
                    cell.setText(text)
                    if val > 0:
                        cell.setForeground(QtGui.QBrush(QtGui.QColor("#FF0000")))
                    elif val < 0:
                        cell.setForeground(QtGui.QBrush(QtGui.QColor("#008000")))
                    else:
                        cell.setForeground(QtGui.QBrush(QtGui.QColor("black")))
        self.status.showMessage(f"上次刷新：{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")

    def on_error(self, msg):
        self.status.showMessage(msg, 8000)

    def toggle_always_on_top(self, state):
        if state in (QtCore.Qt.Checked, True, 2):
            self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)
            self.cfg["window_always_on_top"] = True
        else:
            self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, False)
            self.cfg["window_always_on_top"] = False
        self.show()
        save_config(self.cfg)

    def change_refresh_interval(self, val):
        self.cfg["refresh_interval_sec"] = int(val)
        save_config(self.cfg)
        self.worker.update_config(self.cfg)
        self.status.showMessage(f"刷新间隔已设置为 {val} 秒", 3000)

    def adjust_seconds(self, delta):
        cur = int(self.refresh_spin.value())
        nxt = max(1, cur + delta)
        self.refresh_spin.setValue(nxt)

    def export_symbols(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存为 CSV", "symbols.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        syms = self.get_table_symbols()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(syms))
            QtWidgets.QMessageBox.information(self, "导出成功", f"已导出到 {path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "导出失败", str(e))

    def import_symbols(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "导入 CSV", "", "CSV 文件 (*.csv);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.strip().upper() for l in f if l.strip()]
            added = 0
            for s in lines:
                if self.add_symbol_to_table(s):
                    added += 1
            self.cfg["symbols"] = self.get_table_symbols()
            save_config(self.cfg)
            QtWidgets.QMessageBox.information(self, "导入成功", f"已导入 {len(lines)} 个币对，新增 {added} 个")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "导入失败", str(e))

    def open_settings(self):
        dlg = SettingsDialog(self.cfg, parent=self)
        if dlg.exec_():
            self.cfg = dlg.get_config()
            # rebuild headers and existing table columns to match new periods
            self.build_table_headers()
            # rebuild current rows: we must re-insert existing symbols into new column layout
            symbols = self.get_table_symbols()
            # clear and re-add
            self.table.setRowCount(0)
            for s in symbols:
                self.add_symbol_to_table(s)
            save_config(self.cfg)
            self.worker.update_config(self.cfg)
            self.status.showMessage("设置已保存", 3000)

    def on_table_reordered(self):
        self.cfg["symbols"] = self.get_table_symbols()
        save_config(self.cfg)
        self.status.showMessage("币对顺序已保存（拖拽完成）", 3000)

    def immediate_refresh(self):
        try:
            QtCore.QMetaObject.invokeMethod(self.worker, "fetch_once", QtCore.Qt.QueuedConnection)
            self.status.showMessage("已触发立即刷新（在后台执行）", 3000)
        except Exception as e:
            self.status.showMessage(f"触发立即刷新失败：{e}", 5000)

class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.cfg = dict(cfg)
        self.resize(560, 280)
        layout = QtWidgets.QFormLayout(self)

        self.http_edit = QtWidgets.QLineEdit(self.cfg.get("proxies", {}).get("http",""))
        self.https_edit = QtWidgets.QLineEdit(self.cfg.get("proxies", {}).get("https",""))
        self.socks5_edit = QtWidgets.QLineEdit(self.cfg.get("proxies", {}).get("socks5",""))

        layout.addRow("HTTP 代理（例如 http://127.0.0.1:8080）", self.http_edit)
        layout.addRow("HTTPS 代理（例如 http://127.0.0.1:8080）", self.https_edit)
        layout.addRow("SOCKS5 代理（例如 socks5h://127.0.0.1:1080）", self.socks5_edit)

        note = QtWidgets.QLabel("代理配置会应用到所有网络请求。如果同时填写 socks5 与 https，则以 socks5 优先覆盖 http/https。")
        note.setWordWrap(True)
        layout.addRow(note)

        # period minutes input
        periods_str = ",".join(str(int(x)) for x in self.cfg.get("period_minutes", []))
        self.periods_edit = QtWidgets.QLineEdit(periods_str)
        self.periods_edit.setPlaceholderText("以分钟为单位，逗号分隔，例如：60,240,1440（UTC日初列单独保留）")
        layout.addRow("涨幅周期（分钟, 逗号分隔）", self.periods_edit)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def accept(self):
        p = {}
        vhttp = self.http_edit.text().strip()
        vhttps = self.https_edit.text().strip()
        vsocks = self.socks5_edit.text().strip()
        if vhttp:
            p["http"] = vhttp
        if vhttps:
            p["https"] = vhttps
        if vsocks:
            p["socks5"] = vsocks
        self.cfg["proxies"] = p
        # parse periods
        s = self.periods_edit.text().strip()
        if s:
            try:
                parts = [int(x.strip()) for x in s.replace("，", ",").split(",") if x.strip()]
                # dedupe and sort ascending
                parts = sorted(list(dict.fromkeys(parts)))
                self.cfg["period_minutes"] = parts
            except Exception:
                QtWidgets.QMessageBox.warning(self, "输入错误", "请确认涨幅周期为逗号分隔的整数（分钟）。")
                return
        else:
            # empty -> clear
            self.cfg["period_minutes"] = []
        super().accept()

    def get_config(self):
        return self.cfg

def main():
    app = QtWidgets.QApplication(sys.argv)
    cfg = load_config()
    w = MainWindow()
    geom = cfg.get("window_geometry")
    if geom:
        try:
            w.restoreGeometry(QtCore.QByteArray.fromHex(bytes(geom, "utf-8")))
        except Exception:
            pass
    w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
