"""Small Tkinter panel for changing selected stock LTP values."""
from __future__ import annotations

import os
import math
import tkinter as tk
from tkinter import messagebox, ttk

import httpx


DEFAULT_ROOT = "http://127.0.0.1:8000"
SELECTED_STOCKS = (
    ("SBIN-EQ", "NSE", "3045"),
    ("RELIANCE-EQ", "NSE", "2885"),
    ("NIFTY", "NSE", "99926000"),
)


def set_mode(base_url: str, exchange: str, token: str, mode: str, ltp: float | None = None) -> None:
    data = {"exchange": exchange, "symboltoken": token, "mode": mode}
    if ltp is not None:
        data["ltp"] = ltp
    response = httpx.post(
        base_url.rstrip("/") + "/admin/market",
        data=data,
        timeout=5,
        follow_redirects=False,
    )
    if response.status_code != 303:
        raise RuntimeError(f"Server returned HTTP {response.status_code}")


def update_ltp(base_url: str, exchange: str, token: str, ltp: str) -> None:
    price = float(ltp)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("LTP must be greater than zero")
    set_mode(base_url, exchange, token, "HIJACK", price)


def adjust_price(current: float, amount: float, mode: str, increase: bool) -> float:
    if not math.isfinite(current) or current <= 0 or not math.isfinite(amount) or amount <= 0:
        raise ValueError("LTP and adjustment must be greater than zero")
    if mode == "Percentage":
        if not increase and amount >= 100:
            raise ValueError("Percentage decrease must be below 100")
        price = current * (1 + amount / 100 if increase else 1 - amount / 100)
    elif mode == "Value":
        price = current + amount if increase else current - amount
    else:
        raise ValueError("Adjustment type must be Value or Percentage")
    if price <= 0:
        raise ValueError("Adjusted LTP must be greater than zero")
    return round(price, 2)


class LTPControlPanel:
    def __init__(self, window: tk.Tk, base_url: str) -> None:
        self.window = window
        self.base_url = base_url
        self.stocks = {
            f"{exchange} / {symbol} ({token})": (symbol, exchange, token)
            for symbol, exchange, token in SELECTED_STOCKS
        }
        self.stock = tk.StringVar(value=next(iter(self.stocks)))
        self.ltp = tk.StringVar(value="100.00")
        self.adjustment = tk.StringVar(value="1")
        self.adjustment_mode = tk.StringVar(value="Value")
        self.status = tk.StringVar(value=f"Server: {base_url}")
        self.hijack_enabled = False
        self.active_hijack = None

        window.title("SmartAPI LTP Control")
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=12)
        frame.grid()
        ttk.Label(frame, text="Selected stock").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        selector = ttk.Combobox(
            frame, textvariable=self.stock, values=list(self.stocks), state="readonly", width=30
        )
        selector.grid(row=0, column=1, padx=4, pady=4)
        selector.bind("<<ComboboxSelected>>", self.select_stock)
        ttk.Label(frame, text="LTP").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        entry = ttk.Entry(frame, textvariable=self.ltp, width=18)
        entry.grid(row=1, column=1, sticky="w", padx=4, pady=4)
        entry.bind("<Return>", lambda _: self.apply())
        self.hijack_button = ttk.Button(frame, text="Enable HIJACK", command=self.toggle_hijack)
        self.hijack_button.grid(row=2, column=0, columnspan=2, pady=8)
        ttk.Label(frame, text="Adjustment").grid(row=3, column=0, sticky="w", padx=4, pady=4)
        adjustment_entry = ttk.Entry(frame, textvariable=self.adjustment, width=10)
        adjustment_entry.grid(row=3, column=1, sticky="w", padx=4, pady=4)
        mode_box = ttk.Combobox(
            frame, textvariable=self.adjustment_mode, values=("Value", "Percentage"),
            state="readonly", width=12,
        )
        mode_box.grid(row=4, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(frame, text="Adjustment type").grid(row=4, column=0, sticky="w", padx=4, pady=4)
        update_button = ttk.Button(frame, text="Update LTP", command=self.apply)
        update_button.grid(row=5, column=0, columnspan=2, pady=8)
        increase_button = ttk.Button(frame, text="Increase", command=lambda: self.adjust(True))
        increase_button.grid(row=6, column=0, padx=4, pady=4, sticky="ew")
        decrease_button = ttk.Button(frame, text="Decrease", command=lambda: self.adjust(False))
        decrease_button.grid(row=6, column=1, padx=4, pady=4, sticky="ew")
        self.edit_widgets = (
            entry, adjustment_entry, mode_box, update_button, increase_button, decrease_button,
        )
        self.set_controls()
        ttk.Label(frame, textvariable=self.status).grid(
            row=7, column=0, columnspan=2, sticky="w", padx=4, pady=4
        )

    def set_controls(self) -> None:
        state = "normal" if self.hijack_enabled else "disabled"
        for widget in self.edit_widgets:
            widget.configure(state=state)
        self.hijack_button.configure(
            text="Disable HIJACK" if self.hijack_enabled else "Enable HIJACK"
        )

    def toggle_hijack(self) -> None:
        symbol, exchange, token = self.stocks[self.stock.get()]
        if self.hijack_enabled:
            target = self.active_hijack or (exchange, token)
            try:
                set_mode(self.base_url, target[0], target[1], "YAHOO")
            except (httpx.HTTPError, RuntimeError) as exc:
                self.status.set(f"Error: {exc}")
                messagebox.showerror("HIJACK update failed", str(exc))
                return
            self.hijack_enabled = False
            self.active_hijack = None
            self.status.set(f"{symbol} returned to YAHOO mode")
        else:
            self.hijack_enabled = True
            self.status.set(f"HIJACK editing enabled for {symbol}")
        self.set_controls()

    def select_stock(self, _=None) -> None:
        if self.active_hijack:
            try:
                set_mode(self.base_url, self.active_hijack[0], self.active_hijack[1], "YAHOO")
            except (httpx.HTTPError, RuntimeError) as exc:
                self.show_error(exc)
                return
        self.active_hijack = None
        self.hijack_enabled = False
        self.ltp.set("100.00")
        self.set_controls()
        self.status.set(f"Select Enable HIJACK to edit {self.stocks[self.stock.get()][0]}")

    def apply(self) -> None:
        try:
            _, exchange, token = self.stocks[self.stock.get()]
            self.save_ltp(exchange, token, float(self.ltp.get()))
        except (KeyError, TypeError, ValueError, httpx.HTTPError, RuntimeError) as exc:
            self.show_error(exc)

    def adjust(self, increase: bool) -> None:
        try:
            price = adjust_price(
                float(self.ltp.get()), float(self.adjustment.get()),
                self.adjustment_mode.get(), increase,
            )
            _, exchange, token = self.stocks[self.stock.get()]
            self.save_ltp(exchange, token, price)
        except (KeyError, TypeError, ValueError, httpx.HTTPError, RuntimeError) as exc:
            self.show_error(exc)

    def save_ltp(self, exchange: str, token: str, price: float) -> None:
        symbol = self.stocks[self.stock.get()][0]
        update_ltp(self.base_url, exchange, token, f"{price:.8f}")
        self.active_hijack = (exchange, token)
        self.ltp.set(f"{price:.2f}")
        self.status.set(f"Updated {symbol} LTP to {price:.2f}")

    def show_error(self, error: Exception) -> None:
        self.status.set(f"Error: {error}")
        messagebox.showerror("LTP update failed", str(error))


def main() -> None:
    window = tk.Tk()
    LTPControlPanel(window, os.getenv("SMARTAPI_ROOT", DEFAULT_ROOT))
    window.mainloop()


if __name__ == "__main__":
    main()
