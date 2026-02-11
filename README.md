# Package Manager

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=kents00_Package-Manager&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=kents00_Package-Manager)
[![Semgrep](https://github.com/kents00/Package-Manager/actions/workflows/semgrep.yml/badge.svg)](https://github.com/kents00/Package-Manager/actions/workflows/semgrep.yml)
[![DeepSource](https://app.deepsource.com/gh/kents00/Package-Manager.svg/?label=active+issues&show_trend=true)](https://app.deepsource.com/gh/kents00/Package-Manager/)

The Package Manager is an add-on designed to streamline the management of Python packages directly within Blender. It provides an easy-to-use interface for searching, installing, and managing Python packages, along with bulk download capabilities for package requirements.

This add-on aims to simplify the workflow for Blender users who need to manage Python packages for scripting and plugin development. It eliminates the need to use external package managers by integrating package management into the Blender interface.

![Package Installer 1](https://github.com/user-attachments/assets/d0238775-e577-478e-a37f-ff95e1290e16)

## Features

- **Search PyPI** — Find Python packages with cached results, rate-limited requests, and **Consolidated Metadata** (Authors, Licenses, etc.).
- **Install & Upgrade** — Download, install, and upgrade packages without freezing Blender's UI.
- **Auto-Update System** — Background checker notifies you when new versions of your favorite packages are available on PyPI.
- **Documentation Support** — One-click access to official documentation and homepages directly from the UI.
- **Manage Installed Packages** — View, search, and uninstall packages with clear identification of **System-installed** packages.
- **Robust Error Handling** — Specific diagnostic messages for build failures (missing compilers, `pkg-config`), network issues, and permission errors.
- **Bulk Download** — Install all packages from a `requirements.txt` file in a single pip call.
- **Non-blocking UI** — Network and subprocess operations run in background threads.

## Usage

### 1. **Search Packages**

- **Search Packages:** Enter your search query in the "Search Packages" section to find packages on PyPI.
- **Documentation & Website:** Access the project's homepage or official documentation via dedicated buttons in the search results.
- **Install Packages:** Click the "Download" button to install a package.

### 2. **Manage Installed Packages**

- **Refresh & Search:** Keep your list up to date and filter it using the built-in search field.
- **Auto-Update Toggle:** Enable auto-update for a package to receive background notifications when a new version is released.
- **Update Package:** When an update is detected, a notification and an "Update" button will appear.
- **Uninstall:** Remove packages easily. Note that system-installed packages (marked with ⚠️) may require running Blender as Administrator to uninstall.

### 3. **Bulk Download Packages**

- **Requirements File:** Select a `requirements.txt` file and click "Download All" to install multiple dependencies at once.

## Installation

### As a Blender 4.2+ Extension

1. **Download** the extension as a `.zip` file from this repository.
2. Open Blender and go to **Edit > Preferences > Get Extensions**.
3. Click the dropdown arrow (⌄) next to "Repositories" and select **Install from Disk...**.
4. Select the `.zip` file.
5. Enable the extension.

### Accessing the Panel

- Switch to the **Text Editor** workspace.
- Open the sidebar (press `N` or drag from the right edge).
- You will find the **Package Manager** panel in the **Package Manager** tab.

## Technical Notes

- **Blender 4.2+**: Built for the latest Blender Python environment.
- **Verification**: Uses robust metadata checks to verify installations, supporting packages with complex casing or module-name mismatches.
- **Security & Safety**: Always uses the `--user` flag for installations to prevent corrupting system-level Python environments.
- **Caching**: Search results are cached for 5 minutes, and ETags are used to minimize bandwidth for repeated searches.
- **Update Checks**: Automatic version checks run in the background every 6 hours.

## Contributing

Feel free to open issues or submit pull requests if you find bugs or have suggestions for improvements. Contributions are welcome!
