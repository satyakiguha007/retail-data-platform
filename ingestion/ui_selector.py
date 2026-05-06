"""Shared Tkinter popup dialogs for data-source selection.

Two public functions used by both run_fx_rates.py and run_weather.py:

  ask_source(data_type, date_range) -> "realtime" | "synthetic"
      Shows a popup with two buttons: Fetch Real-Time vs Generate Synthetic.

  ask_retry_or_fallback(error_msg) -> "retry" | "fallback"
      Shown when the API fetch fails. User can try again or switch to synthetic.

  run_with_loading(task_fn, message) -> result
      Runs task_fn in a background thread while showing a "please wait" window.
      Re-raises any exception thrown by task_fn so the caller can handle it.
"""

from __future__ import annotations

import threading
import tkinter as tk


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _center(win: tk.Tk | tk.Toplevel, width: int, height: int) -> None:
    """Position window in the centre of the screen."""
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = (sw - width) // 2
    y = (sh - height) // 2
    win.geometry(f"{width}x{height}+{x}+{y}")


# ---------------------------------------------------------------------------
# Public dialogs
# ---------------------------------------------------------------------------

def ask_source(data_type: str, date_range: str) -> str:
    """Ask the user whether to fetch real data from an API or generate synthetic data.

    Parameters
    ----------
    data_type : str
        Short label shown in the popup title, e.g. "FX Rates" or "Weather".
    date_range : str
        Human-readable date range shown as a subtitle, e.g. "2023-01-01 to 2024-12-31".

    Returns
    -------
    "realtime"  – user wants live API data
    "synthetic" – user wants generated data (also returned if window is closed)
    """
    result = {"choice": "synthetic"}   # default: synthetic (safe fallback)

    root = tk.Tk()
    root.title("Select Data Source")
    root.resizable(False, False)
    _center(root, 460, 210)
    root.lift()
    root.attributes("-topmost", True)   # float above other windows
    root.configure(bg="#f3f3f3")

    # --- Title bar ---
    tk.Label(
        root,
        text=f"Data Source  —  {data_type}",
        font=("Segoe UI", 13, "bold"),
        bg="#f3f3f3",
        pady=12,
    ).pack()

    tk.Label(
        root,
        text=f"Date range: {date_range}",
        font=("Segoe UI", 9),
        fg="#666666",
        bg="#f3f3f3",
    ).pack()

    # --- Horizontal divider ---
    tk.Frame(root, height=1, bg="#cccccc").pack(fill="x", padx=24, pady=10)

    # --- Buttons ---
    btn_frame = tk.Frame(root, bg="#f3f3f3")
    btn_frame.pack(pady=4)

    def choose(val: str) -> None:
        result["choice"] = val
        root.destroy()           # exits mainloop()

    tk.Button(
        btn_frame,
        text="\U0001f310  Fetch Real-Time from API",  # 🌐
        width=25, height=2,
        bg="#0078d4", fg="white",
        activebackground="#005fa3", activeforeground="white",
        font=("Segoe UI", 10, "bold"),
        relief="flat", cursor="hand2",
        command=lambda: choose("realtime"),
    ).pack(side="left", padx=8)

    tk.Button(
        btn_frame,
        text="⚙️  Generate Synthetic Data",   # ⚙️
        width=25, height=2,
        bg="#e1e1e1", fg="#222222",
        activebackground="#c8c8c8",
        font=("Segoe UI", 10),
        relief="flat", cursor="hand2",
        command=lambda: choose("synthetic"),
    ).pack(side="left", padx=8)

    # Closing the window = treat as "synthetic"
    root.protocol("WM_DELETE_WINDOW", lambda: choose("synthetic"))

    root.mainloop()
    return result["choice"]


def ask_retry_or_fallback(error_msg: str) -> str:
    """Show an error dialog after a failed API fetch.

    Parameters
    ----------
    error_msg : str
        The exception message to display (truncated to 100 chars if longer).

    Returns
    -------
    "retry"    – user wants to try the API again
    "fallback" – user wants to switch to synthetic data (also returned on window close)
    """
    result = {"choice": "fallback"}

    # Keep the error display short enough to fit the window
    display_err = error_msg if len(error_msg) <= 100 else error_msg[:97] + "..."

    root = tk.Tk()
    root.title("API Fetch Failed")
    root.resizable(False, False)
    _center(root, 480, 210)
    root.lift()
    root.attributes("-topmost", True)
    root.configure(bg="#fff4f4")

    # --- Error heading ---
    tk.Label(
        root,
        text="⚠️  API Fetch Failed",   # ⚠️
        font=("Segoe UI", 13, "bold"),
        fg="#c42b1c",
        bg="#fff4f4",
        pady=12,
    ).pack()

    # --- Error detail ---
    tk.Label(
        root,
        text=display_err,
        font=("Segoe UI", 9),
        fg="#555555",
        bg="#fff4f4",
        wraplength=440,
    ).pack(padx=20)

    # --- Horizontal divider ---
    tk.Frame(root, height=1, bg="#f0b0b0").pack(fill="x", padx=24, pady=10)

    # --- Buttons ---
    btn_frame = tk.Frame(root, bg="#fff4f4")
    btn_frame.pack(pady=4)

    def choose(val: str) -> None:
        result["choice"] = val
        root.destroy()

    tk.Button(
        btn_frame,
        text="\U0001f504  Try Again",           # 🔄
        width=18, height=2,
        bg="#0078d4", fg="white",
        activebackground="#005fa3", activeforeground="white",
        font=("Segoe UI", 10, "bold"),
        relief="flat", cursor="hand2",
        command=lambda: choose("retry"),
    ).pack(side="left", padx=8)

    tk.Button(
        btn_frame,
        text="⚙️  Use Synthetic Data",  # ⚙️
        width=22, height=2,
        bg="#e1e1e1", fg="#222222",
        activebackground="#c8c8c8",
        font=("Segoe UI", 10),
        relief="flat", cursor="hand2",
        command=lambda: choose("fallback"),
    ).pack(side="left", padx=8)

    root.protocol("WM_DELETE_WINDOW", lambda: choose("fallback"))

    root.mainloop()
    return result["choice"]


def run_with_loading(task_fn: callable, message: str = "Fetching data...") -> object:
    """Run task_fn in a background thread while showing a 'please wait' window.

    Why a background thread?
    ------------------------
    Tkinter requires its main thread to keep running its event loop so the window
    stays responsive (painted, moveable). If we did the network call on the main
    thread, the window would freeze and appear broken.

    The trick:
      1. Show the loading window and call root.update() so it draws immediately.
      2. Start a daemon thread that runs the actual work (API call).
      3. When the thread finishes (success or error), it schedules root.destroy()
         on the main thread via root.after(0, ...) — the only thread-safe way
         to talk to tkinter.
      4. root.mainloop() was blocking; now that the window is destroyed it returns.
      5. We re-raise any exception so the caller can handle it normally.

    Parameters
    ----------
    task_fn  : callable taking no arguments; should return the result
    message  : string shown in the loading window

    Returns
    -------
    Whatever task_fn returns.

    Raises
    ------
    Whatever exception task_fn raises (propagated back to caller).
    """
    result: dict = {"value": None, "error": None}

    root = tk.Tk()
    root.title("Please Wait")
    root.resizable(False, False)
    _center(root, 340, 110)
    root.lift()
    root.attributes("-topmost", True)
    root.configure(bg="#f0f8ff")
    root.protocol("WM_DELETE_WINDOW", lambda: None)   # block manual close

    tk.Label(
        root,
        text=f"⏳  {message}",   # ⏳
        font=("Segoe UI", 11),
        bg="#f0f8ff",
        pady=35,
    ).pack()

    root.update()   # force the window to actually appear before we start the thread

    def worker() -> None:
        try:
            result["value"] = task_fn()
        except Exception as exc:
            result["error"] = exc
        finally:
            # Schedule destroy on the main (tkinter) thread — never call tkinter
            # directly from a non-main thread.
            root.after(0, root.destroy)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    root.mainloop()   # blocks here until root.destroy() fires
    thread.join()     # make sure worker is fully done before we continue

    if result["error"] is not None:
        raise result["error"]
    return result["value"]
