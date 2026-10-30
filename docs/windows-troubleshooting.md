# Windows: troubleshooting launch errors

## Error: 'Add' is not recognized / parts of commands are printed as errors

The original Windows batch files used an encoding and line ending combination that some versions of `cmd.exe` do not parse correctly.

**Fixed:** `setup-windows.bat` and `start-windows.bat` now contain ASCII-only instructions and use CRLF line endings. Download the current repository archive again. Do **not** mix batch files from previous ZIP downloads.

1. Download **Code → Download ZIP** from the GitHub repository again.
2. Extract the entire archive to a regular local folder, such as `C:\PharmDesk`.
3. Install Python 3.11 or newer (with the Python Launcher) if needed.
4. Run `setup-windows.bat`, wait for the message `Setup complete!`.
5. Run `start-windows.bat`.
6. Open `http://127.0.0.1:8000`.

If a batch script still fails, open **PowerShell** in the extracted project folder and run:

```powershell
py --version
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.cli seed-demo
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The project is intended for local use only. Do not share private pharmacy datasets, prescriptions, or patient information in the public repository.
